"""Connect canonical CORE decisions to RESOURCE receipts without a second ledger."""
from pathlib import Path
import uuid

from .resources.support import fingerprint, load, load_bound, ref, write_atomic


class ResourceBridge:
    def __init__(self, runtime, controller, artifact_cleanup, evidence_root):
        self.runtime = runtime
        self.controller = controller
        self.artifacts = artifact_cleanup
        self.evidence = Path(evidence_root)
        if not self.evidence.is_absolute():
            raise ValueError("explicit absolute external evidence root required")
        if self.evidence.resolve().is_relative_to(runtime.project_root.resolve()):
            raise ValueError("evidence must outlive project checkout cleanup")
        _, policy = controller.read()
        for roots in policy.get("cleanup_roots", {}).values():
            if any(self.evidence.resolve().is_relative_to(Path(root).resolve()) for root in roots):
                raise ValueError("evidence cannot reside in a managed cleanup subtree")

    def _external(self, resource):
        if resource.get("path") and self.evidence.resolve().is_relative_to(Path(resource["path"]).resolve()):
            raise ValueError("cleanup evidence must outlive the resource")

    def _record(self, result):
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / ("cleanup-" + uuid.uuid4().hex + ".json")
        write_atomic(path, result)
        return ref(path)

    def _task_permits(self, task_id):
        state,_=self.controller.read()
        return {key:dict(request_id=key,permit_id=row.get('permit_id'),status=row['status'],attempt_id=row['spec']['attempt_id'],
                         launch_identity=row['spec']['launch_identity']) for key,row in state['capacity_requests'].items()
                if row['spec']['project_id']==self.runtime.project_id and row['spec']['task_id']==task_id}

    def _record_permits(self, task_id, operation_id, *, result=None):
        from coordination import writer_lock
        permits=self._task_permits(task_id)
        with writer_lock(self.runtime.path):
            state=self.runtime._load()
            if task_id not in state['tasks']:return
            task=state['tasks'][task_id];task['resource_permits']=permits
            if result and result.get('status')=='pending_cleanup':
                task.setdefault('cleanup',{}).update(status='pending_cleanup',plugin_permits=permits)
                if result.get('reason'):task['cleanup'].update(reason=result['reason'],resource_ids=result.get('resource_ids',[]))
                if operation_id in state['runtime']['operations']:state['runtime']['operations'][operation_id]['status']='pending_cleanup'
            self.runtime._save(state,dict(op='agent-permit-result',task_id=task_id,operation_id=operation_id))

    def _unbound_permits(self, task_id):
        state=self.runtime._load();owner=state['tasks'][task_id]['owner_agent_id']
        pending=[]
        for key,permit in self._task_permits(task_id).items():
            if permit['status']!='granted':continue
            known=any(o.get('task_id')==task_id and o.get('request_id')==key and o.get('permit_id')==permit['permit_id'] and
                o.get('operation') in ('spawn','send','codex_exec') and (owner and o.get('executor_id')==owner or
                    o.get('operation')=='spawn' and o.get('status') in ('capacity','unsupported','not_started','queued') and
                    not o.get('executor_id') and not o.get('target_id')) for o in state['runtime']['operations'].values())
            if not known:pending.append(key)
        return pending

    def admit(self, spec_ref):
        """Reserve actual RESOURCE quota and bind a CORE admission artifact."""
        spec = load_bound(spec_ref)
        record = self.controller.capacity_acquire(spec_ref["path"])
        if record["spec"] != spec:
            raise ValueError("capacity source changed during admission; reconcile reserved quota")
        if record["status"] != "granted":
            self._record_permits(spec['task_id'],spec['request_id'])
            return {"status": record["status"], "request_id": spec["request_id"], "admission": None}
        value = dict(spec, status="granted", permit_id=record["permit_id"], resource_source=spec_ref)
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / ("admission-" + fingerprint(value) + ".json")
        if path.exists():
            if load(path) != value:
                raise ValueError("immutable admission evidence changed")
        else:
            write_atomic(path, value)
        self._record_permits(spec['task_id'],spec['request_id'])
        return {"status": "granted", "request_id": spec["request_id"], "admission": ref(path)}

    def prepare_development(self, spec, actor_id, *, authority_ref=None):
        """Normal new-task entry: plan a removable location before any host action."""
        from resource_lifecycle import managed_path
        from codex_parallel_coordination.worktrees import GitWorktrees
        state = self.runtime._load()
        target = managed_path(state, spec["worktree"])
        self.artifacts.adapters["git_worktree"] = GitWorktrees(self.runtime.project_root, state["runtime"]["cleanup_root"])
        # Check both components' exact policy before creating anything.
        self.artifacts._scope(dict(project_id=self.runtime.project_id, path=str(target),
                                   kind="git_worktree"), self.controller.read()[1])
        prepared = dict(spec, resource_contract="new-task/1")
        if authority_ref is not None:
            prepared["resource_authority"] = authority_ref
        return self.runtime.prepare(prepared, actor_id)

    def plan_development(self, task_id, worktree, branch, authority, actor_id):
        """Attach resources to an already approved STATE task; never create a second task."""
        from coordination import writer_lock
        from evidence import require
        from resource_lifecycle import managed_path
        from evidence import artifact
        artifact(authority)
        with writer_lock(self.runtime.path):
            state=self.runtime._load();self.runtime._actor(state,actor_id,control=True);self.runtime._scope(state,task_id)
            task=state['tasks'][task_id]
            require(task.get('state_contract') and task['owner_agent_id'] is None and task['phase']=='backlog' and
                not task.get('cancellation_request'), 'approved unowned STATE task required')
            require(task['requirements']['user_reference'] in (authority,authority['path']), 'original STATE approval differs')
            require(task['worktree'] in (None,str(worktree)) and task['branch'] in (None,branch), 'resource plan immutable')
            require(branch.startswith('codex/') and branch!=state['project']['main_branch'], 'dedicated task branch required')
            target=managed_path(state,worktree)
            self.artifacts._scope(dict(project_id=self.runtime.project_id,path=str(target),kind='git_worktree'),self.controller.read()[1])
            task.update(worktree=str(target),branch=branch,resource_contract='new-task/1',resource_authority=authority)
            self.runtime._save(state,dict(op='plan-development-resources',task_id=task_id))
            return dict(worktree=str(target),branch=branch)

    def create_worktree(self, task_id, operation_id, authority, actor_id, *, authorized=False, base=None, adapter=None):
        from codex_parallel_coordination.worktrees import GitWorktrees
        from resource_lifecycle import create
        state = self.runtime._load(); task = state["tasks"][task_id]
        adapter = adapter or GitWorktrees(self.runtime.project_root, state["runtime"]["cleanup_root"])
        self.artifacts.adapters["git_worktree"] = adapter
        resource = dict(id=task_id+":worktree", kind="worktree", path=task["worktree"])
        # This is a code-bound capability; JSON cannot choose an import or shell command.
        def invoke(value):
            record = dict(resource_id=value["id"], path=value["path"],
                          metadata=dict(repository_root=str(adapter.repository)))
            metadata = adapter.create(record, task["branch"], base or state["project"]["main_branch"])
            self.artifacts.register(dict(record, project_id=self.runtime.project_id, task_id=task_id,
                attempt_id=operation_id, kind="git_worktree", references=[], protected=False, metadata=metadata))
            return dict(metadata=metadata, attempt_id=operation_id)
        # RESOURCE scope is checked before Git, including missing capabilities/foreign roots.
        self.artifacts._scope(dict(project_id=self.runtime.project_id, path=task["worktree"],
                                   kind="git_worktree"), self.controller.read()[1])
        return create(self.runtime, task_id, operation_id, resource, authority, actor_id, invoke, authorized=authorized)

    def observe_worktree_creation(self, task_id, operation_id, authority, actor_id, *, base, adapter=None):
        """Observe/register the original Git allocation; never add a second tree."""
        from coordination import writer_lock
        from evidence import require
        from .worktrees import GitWorktrees
        state=self.runtime._load();self.runtime._resource_actor(state,actor_id,task_id);self.runtime._scope(state,task_id)
        task=state['tasks'][task_id];operation=state['runtime']['operations'].get(operation_id)
        resource=state['runtime'].get('resources',{}).get(task_id+':worktree')
        require(resource or operation and operation.get('operation')=='create-resource' and operation['task_id']==task_id and
            operation.get('authority')==authority and operation.get('status') in ('intent','unknown'), 'original creation/reservation required')
        if resource:
            require(resource['task_id']==task_id and resource['authority']==authority and resource['path']==task['worktree'],
                    'original resource reservation differs')
        adapter=adapter or GitWorktrees(self.runtime.project_root,state['runtime']['cleanup_root'])
        spec=dict(resource_id=task_id+':worktree',path=task['worktree'],project_id=self.runtime.project_id,task_id=task_id,
            attempt_id=resource['attempt_id'] if resource else operation_id,kind='git_worktree',references=[],protected=False,
            metadata=dict(repository_root=str(adapter.repository),branch='refs/heads/'+task['branch']))
        observed=adapter.observe(spec)
        require(observed.get('exists') and observed.get('safe') and observed.get('head')==base, 'original Git tree not verified at admitted baseline')
        self.artifacts.adapters['git_worktree']=adapter
        if spec['resource_id'] not in self.controller.read()[0]['cleanup_resources']:self.artifacts.register(spec)
        if resource:return resource
        value=dict(id=task_id+':worktree',kind='worktree',path=task['worktree'],metadata=spec['metadata'],
            task_id=task_id,project_id=self.runtime.project_id,attempt_id=operation_id,authority=authority,
            references=[task_id],cleanup_status='retained',path_identity=[observed['identity']['device'],observed['identity']['inode']])
        with writer_lock(self.runtime.path):
            current=self.runtime._load();self.runtime._resource_actor(current,actor_id,task_id)
            require(current['tasks'][task_id]==task and current['runtime']['operations'][operation_id]==operation,
                    'creation changed during actual Git observation')
            current['runtime'].setdefault('resources',{})[value['id']]=value
            current['runtime']['operations'][operation_id].update(status='registered',actual=value)
            self.runtime._save(current,dict(op='create-resource-outcome',task_id=task_id,operation_id=operation_id))
        return value

    def create_temporary(self, task_id, operation_id, resource_id, kind, path, authority, actor_id, *,
                         authorized=False):
        """Create only empty dedicated directories or SQLite files, then register both receipts."""
        from resource_lifecycle import create
        kinds = {"environment": "acceptance_environment", "database": "temporary_database",
                 "artifact": "temporary_file"}
        if kind not in kinds:
            raise ValueError("supported dedicated temporary kind required")
        resource = dict(id=resource_id, kind=kind, path=str(path))
        spec = dict(resource_id=resource_id, project_id=self.runtime.project_id, task_id=task_id,
            attempt_id=operation_id, kind=kinds[kind], path=str(path), references=[], protected=False)
        self.artifacts._scope(spec, self.controller.read()[1])
        def invoke(value):
            target = Path(value["path"])
            if kind == "environment":
                target.mkdir(parents=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb"): pass
            self.artifacts.register(spec)
            return dict(attempt_id=operation_id)
        return create(self.runtime, task_id, operation_id, resource, authority, actor_id, invoke, authorized=authorized)

    def observe_remote_completion(self, task_id, actor_id, *, scope="host_verified"):
        """Finite actual remote read at the normal pre-reclamation boundary."""
        from .worktrees import GitWorktrees
        from evidence import report_for, require
        state=self.runtime._load();self.runtime._resource_actor(state,actor_id,task_id);self.runtime._scope(state,task_id)
        task=state["tasks"][task_id]
        require(task["phase"]=="completed" and task.get("merge",{}).get("verified"),"verified task completion required")
        report=report_for(state,task_id)
        adapter=GitWorktrees(self.runtime.project_root,state["runtime"]["cleanup_root"])
        record=dict(resource_id=task_id+":worktree",path=task["worktree"],
            metadata=dict(repository_root=str(self.runtime.project_root),expected_head=task["merge"]["head_sha"]))
        value=adapter.remote_completion(record,state["project"]["main_branch"],task["mr"]["url"],scope=scope)
        value.update(project_id=self.runtime.project_id,task_id=task_id,delivery_revision=task["delivery_revision"],
                     report=task["delivery"],source_digest=report["source"]["digest"])
        return self._record(value)

    def cleanup(self, resource):
        """Pass as Runtime.cleanup_task's trusted callback, never as JSON authority."""
        self._external(resource)
        from evidence import require
        proof=load_bound(resource['cleanup_proof'])
        flow=proof.get('flow_contract')=='resource-flow/1'
        if flow and resource['kind'] in ('local_branch','remote_branch','worktree'):
            context=dict(resource_id=resource['id'],project_id=resource['project_id'],task_id=resource['task_id'],
                         attempt_id=resource['attempt_id'],path=resource.get('path'))
            require(self.runtime.verify_cleanup_receipt(context,proof)['verified'], 'current canonical resource guard required')
            from .worktrees import GitWorktrees
            state=self.runtime._load();task=state['tasks'][resource['task_id']]
            adapter=GitWorktrees(self.runtime.project_root,state['runtime']['cleanup_root'])
            if resource['kind'] in ('local_branch','remote_branch'):
                result=adapter.remove_branch(resource['branch'],resource['expected_head'],
                    remote=resource['kind']=='remote_branch',cancelled=task['phase']=='cancelled',main_branch=state['project']['main_branch'])
                return dict(status='cleaned' if result['status']=='removed' else
                    ('failed' if result['status']=='failed_no_change' else 'unknown'),evidence=self._record(result))
            if task['phase']=='cancelled':
                stored=self.controller.read()[0]['cleanup_resources'][resource['id']]
                spec=dict(stored['spec'],references=[])
                result=adapter.remove_cancelled(spec,stored['identity'])
                absent=adapter.observe(spec)
                return dict(status='cleaned' if result['status']=='removed' and not absent['exists'] else
                    ('failed' if result['status']=='failed_no_change' else 'unknown'),evidence=self._record(result))
        if resource["kind"] == "process":
            return self._process_result(resource, terminate=True)
        if resource["kind"] == "worktree" and resource.get("metadata", {}).get("branch"):
            from .worktrees import GitWorktrees
            state = self.runtime._load()
            adapter = GitWorktrees(self.runtime.project_root, state["runtime"]["cleanup_root"])
            self.artifacts.adapters["git_worktree"] = adapter
            stored = self.controller.read()[0]["cleanup_resources"][resource["id"]]["spec"]
            head = adapter.observe(stored).get("head")
            if head != state["tasks"][resource["task_id"]]["merge"]["head_sha"]:
                raise ValueError("worktree HEAD differs from the verified tested main inclusion")
        # A fresh authorized CORE round is the explicit retry request. RESOURCE
        # retries only failed_no_change; unknown outcomes remain observe-only.
        result = self.artifacts.cleanup(resource["id"], resource["cleanup_proof"],
                                        self.runtime.verify_cleanup_receipt, retry=True)
        evidence = self._record(result)
        if result["status"] == "reclaimed":
            status = "cleaned"
        elif result.get("result", {}).get("status") == "failed_no_change":
            status = "failed"
        else:
            status = "unknown"
        return {"status": status, "evidence": evidence}

    def run_process(self, task_id, spec_ref, authority, actor_id, *, authorized=False):
        """RESOURCE registers the actual Job/PID before resume; CORE binds its receipt."""
        from contextlib import ExitStack
        from coordination import writer_lock
        from resource_lifecycle import temporary_actor
        from evidence import artifact, require, callback_result
        spec = load_bound(spec_ref)
        require(authorized is True and spec["project_id"] == self.runtime.project_id and spec["task_id"] == task_id,
                "current task-scoped process creation authority required")
        artifact(authority)
        require(spec.get('resource_kind')!='shared_tool', 'dedicated task process required; shared tool protected')
        with ExitStack() as locks:
            locks.enter_context(writer_lock(self.runtime.path))
            state = self.runtime._load(); temporary_actor(state, task_id, actor_id, "process"); self.runtime._scope(state, task_id)
            require(state["tasks"][task_id].get("resource_contract") == "new-task/1" and
                    state["tasks"][task_id]["phase"] not in ("completed","cancelled"), "unfinished managed task required")
            if actor_id==state['tasks'][task_id]['owner_agent_id']:
                cwd=Path(spec['cwd']).resolve()
                roots=[Path(state['tasks'][task_id]['worktree']).resolve()]+[Path(x['path']).resolve() for x in
                    state['runtime'].get('resources',{}).values() if x['task_id']==task_id and x.get('path') and x['kind']=='environment']
                require(any(cwd==root or cwd.is_relative_to(root) for root in roots), 'developer process cwd outside own task resources')
            operation_id = "process:"+spec["request_id"]
            identity = dict(task_id=task_id, source=spec_ref, authority=authority)
            prior = state["runtime"]["operations"].get(operation_id)
            if prior:
                require(all(prior.get(k) == v for k,v in identity.items()), "process operation differs")
                return prior
            require(spec["request_id"] not in state["runtime"].get("resources",{}), "resource identity already registered")
            operation = dict(identity, operation="create-resource", operation_id=operation_id,
                             status="intent", owner=actor_id)
            state["runtime"]["operations"][operation_id] = operation
            binding = dict(project_id=self.runtime.project_id, task_id=task_id, operation_id=operation_id,
                           actor_id=actor_id, source=spec_ref, authority=authority)
            operation["callback_binding"]=binding
            self.runtime._save(state, dict(op="process-creation-intent", task_id=task_id))
            locks.close()
            try:
                actual = self.controller.submit(spec_ref["path"])
                require(actual["spec"] == spec, "process spec changed; inspect original Job")
                result = dict(status="registered" if actual.get("process") else "queued", request=actual)
            except Exception as error:
                result = dict(status="unknown", reason=type(error).__name__,
                              next_action="inspect_registered_process_without_relaunch")
            captured = callback_result(self.runtime.directory, binding, result=result, capture=True)
            try: locks.enter_context(writer_lock(self.runtime.path))
            except ValueError as error:
                if not str(error).startswith('writer lock exists;'):raise
                return dict(status='registration_pending',actual_result=captured,
                            next_action='observe_original_process_without_relaunch',reason=str(error))
            state = self.runtime._load(); operation = state["runtime"]["operations"][operation_id]
            operation.update(status=result["status"], actual_result=captured)
            actual = result.get("request", {})
            existing=state["runtime"].get("resources",{}).get(spec["request_id"])
            if actual.get("process") and existing:
                require(existing['process_identity']['nonce']==actual['nonce'] and
                    existing['process_identity']['process']==actual['process'], 'observed process identity changed')
            if actual.get("process") and not existing:
                state["runtime"].setdefault("resources",{})[spec["request_id"]] = dict(id=spec["request_id"],
                    kind="process", task_id=task_id, project_id=self.runtime.project_id, attempt_id=spec["attempt_id"],
                    process_identity=dict(request_id=spec["request_id"], nonce=actual["nonce"], process=actual["process"],
                                          launch_identity=spec["launch_identity"], source=spec_ref),
                    references=[task_id], authority=authority, cleanup_status="retained")
            self.runtime._save(state, dict(op="process-creation-outcome", task_id=task_id))
            return operation

    def _process_result(self, resource, *, terminate=False):
        identity = resource["process_identity"]
        request = self.controller.read()[0]["requests"][identity["request_id"]]
        from evidence import load_bound as core_bound, preserved_read
        flow=resource.get('cleanup_proof') and load_bound(resource['cleanup_proof']).get('flow_contract')=='resource-flow/1'
        if flow: source=core_bound(identity['source'])
        else:
            with preserved_read(self.runtime._load(), resource["task_id"]):
                source = core_bound(identity["source"])
        if (request.get("nonce") != identity["nonce"] or request.get("process") != identity["process"]
                or request["spec"] != source):
            raise ValueError("managed process identity changed")
        evidence = request.get("exit_evidence")
        result = load_bound(evidence) if evidence else {}
        if (request["status"] in ("completed","failed","reclaimed","recovered_exit") and result.get("tree_empty") is True
                and result.get("nonce") == identity["nonce"] and result.get("process") == identity["process"]):
            return dict(status="cleaned", evidence=self._record(dict(request=request, whole_tree_exit_confirmed=True)))
        if terminate:
            self.controller.task_cleanup(identity["request_id"],resource["project_id"],resource["task_id"],identity["nonce"],
                resource["cleanup_proof"],self.runtime.verify_cleanup_receipt)
        return dict(status="unknown", reason="Actual whole-tree exit unconfirmed; inspect RESOURCE, do not relaunch")

    def observe_process(self, task_id, request_id, actor_id):
        """Bind a prior normal creation intent after explicit RESOURCE admission/observation."""
        from coordination import writer_lock
        from evidence import require
        with writer_lock(self.runtime.path):
            state = self.runtime._load()
            from resource_lifecycle import temporary_actor
            temporary_actor(state, task_id, actor_id, "process"); self.runtime._scope(state, task_id)
            operation = state["runtime"]["operations"]["process:"+request_id]
            require(operation.get("operation") == "create-resource" and operation["task_id"] == task_id,
                    "this normal task creation intent required")
            spec = load_bound(operation["source"])
            request = self.controller.read()[0]["requests"][request_id]
            require(request["spec"] == spec and spec["project_id"] == self.runtime.project_id and spec["task_id"] == task_id,
                    "exact original process request required")
            if operation["status"] == "registered" or not request.get("process"):
                return operation
            state["runtime"].setdefault("resources",{})[request_id] = dict(id=request_id, kind="process",
                task_id=task_id, project_id=self.runtime.project_id, attempt_id=spec["attempt_id"],
                process_identity=dict(request_id=request_id,nonce=request["nonce"],process=request["process"],
                    launch_identity=spec["launch_identity"],source=operation["source"]),
                references=[task_id],authority=operation["authority"],cleanup_status="retained")
            operation.update(status="registered")
            self.runtime._save(state,dict(op="observe-created-process",task_id=task_id,request_id=request_id))
            return operation

    def reconcile_cleanup(self, task_id, resource_id, actor_id):
        """Confirm observed absence after an unknown response; never re-remove."""
        canonical = self.runtime._load()["runtime"].get("resources",{}).get(resource_id,{})
        if canonical.get("kind") == "process":
            if canonical.get("task_id") != task_id:
                raise ValueError("process belongs to another task")
            result = self._process_result(canonical)
            if result["status"] != "cleaned":
                return dict(status="pending_cleanup",owner=actor_id,outcome=result)
            observation = self._record(dict(resource_id=resource_id,status="cleaned",evidence=[result["evidence"]]))
            return self.runtime.reconcile_cleanup(task_id,resource_id,observation,actor_id)
        state, _ = self.controller.read()
        self._external(state["cleanup_resources"][resource_id]["spec"])
        result = self.artifacts.observe(resource_id)
        evidence = self._record(result)
        if result["status"] != "reclaimed":
            return result
        observation = self._record({"resource_id": resource_id, "status": "cleaned", "evidence": [evidence]})
        return self.runtime.reconcile_cleanup(task_id, resource_id, observation, actor_id)

    def release_agent_quota(self, operation_id, actor_id):
        """Release a quota only after CORE has persisted a confirmed host outcome."""
        receipt = self.runtime.release_receipt(operation_id, actor_id)
        context = load_bound(receipt)
        result=self.controller.capacity_release(context["request_id"], context["permit_id"],receipt,self.runtime.verify_release_receipt)
        try:self._record_permits(context['task_id'],operation_id)
        except (ValueError,OSError) as error:
            return dict(result,registration='pending',reason=str(error))
        return result

    def close_developer(self, task_id, operation_id, adapter, *, authorized=False):
        """Independent author close and local quota release; file archive is separate."""
        from evidence import require
        from resource_lifecycle import ended
        state=self.runtime._load();self.runtime._resource_actor(state,adapter.actor_id,task_id);self.runtime._scope(state,task_id)
        require(authorized is True, "current exact developer close authority required")
        task=ended(state,task_id,adapter.scope)
        unknown=self._unbound_permits(task_id)
        if unknown:return dict(status='pending_close',reason='observe_unbound_granted_admission',request_ids=unknown,native_agent_closed=False)
        if not task['owner_agent_id']:
            uncertain=[o for o in state['runtime']['operations'].values() if o.get('task_id')==task_id and
                o.get('operation') in ('spawn','send') and (o.get('executor_id') or
                    o.get('status') not in ('capacity','unsupported','not_started','queued'))]
            require(not uncertain, 'unknown original author; observe without redispatch')
            releases=[]
            for identity,operation in state['runtime']['operations'].items():
                if operation.get('task_id')==task_id and operation.get('operation') in ('spawn','send') and operation.get('permit_id'):
                    releases.append(self.release_agent_quota(identity,adapter.actor_id))
            return dict(status='not_acquired',plugin_releases=releases,native_agent_closed=False)
        executor=state['runtime']['executors'][task['owner_agent_id']]
        if executor['lease']=='closed':
            closed=next(o for o in state['runtime']['operations'].values() if o.get('operation')=='close' and
                o.get('task_id')==task_id and o.get('executor_id')==executor['id'] and
                o.get('generation')==executor['generation'] and o.get('status')=='closed')
        else:
            closed=self.runtime.lifecycle('close',executor['id'],task_id,operation_id,executor['generation'],adapter,
                                          preservation_ref=None,authorized=True)
        if closed['status']!='closed':return dict(status='pending_close',outcome=closed,plugin_released=False)
        release=self.release_agent_quota(closed['operation_id'],adapter.actor_id) if closed.get('request_id') and closed.get('permit_id') else None
        return dict(status='closed',close_operation=closed['operation_id'],plugin_release=release,
                    platform_capacity_count_known=False)

    def reclaim_resources(self, task_id, operation_id, actor_id, *, cleanup_remote_branch=False, **options):
        from evidence import require
        from .worktrees import GitWorktrees
        from resource_lifecycle import ended
        state=self.runtime._load();self.runtime._resource_actor(state,actor_id,task_id);self.runtime._scope(state,task_id)
        require(options.get('authorized') is True, 'current exact resource cleanup authority required')
        task=ended(state,task_id,options.get('scope','host_verified'))
        unknown=self._unbound_permits(task_id)
        if unknown:
            result=dict(status='pending_cleanup',reason='observe_unbound_granted_admission',request_ids=unknown)
            self._record_permits(task_id,operation_id,result=result)
            return result
        if task['phase']=='cancelled':
            retained=self.cancel_queued_processes(task_id,actor_id)
            if retained:
                result=dict(status='pending_cleanup',reason='shared_queued_process_protected',resource_ids=retained)
                self._record_permits(task_id,operation_id,result=result)
                return result
        tree=state['runtime'].get('resources',{}).get(task_id+':worktree')
        if tree and tree.get('metadata',{}).get('branch'):
            adapter=GitWorktrees(self.runtime.project_root,state['runtime']['cleanup_root'])
            for remote in ([False,True] if cleanup_remote_branch else [False]):
                kind='remote_branch' if remote else 'local_branch';rid=task_id+':'+kind
                if rid not in state['runtime']['resources']:
                    head=adapter.branch_head(task['branch'],remote=remote)
                    if head:
                        self.runtime.register_resource(task_id,dict(id=rid,kind=kind,branch=task['branch'],expected_head=head),
                            tree['authority'],actor_id)
        result=self.runtime.reclaim_resources(task_id,operation_id,self.cleanup,actor_id,**options)
        if any(row['status']=='granted' for row in self._task_permits(task_id).values()):
            result.update(status='pending_cleanup',plugin_permits=self._task_permits(task_id),next_owner=actor_id)
        self._record_permits(task_id,operation_id,result=result)
        return result

    def cancel_queued_processes(self, task_id, actor_id):
        from coordination import writer_lock
        from development_lifecycle import cancelled
        from evidence import require,io_path
        state=self.runtime._load();self.runtime._resource_actor(state,actor_id,task_id);self.runtime._scope(state,task_id)
        cancelled(state,task_id)
        retained=[]
        operations=[(key,value) for key,value in state['runtime']['operations'].items() if
            value.get('task_id')==task_id and value.get('operation')=='create-resource' and value.get('status')=='queued' and value.get('source')]
        for operation_id,operation in operations:
            source=load_bound(operation['source']);request=self.controller.read()[0]['requests'][source['request_id']]
            if source.get('resource_kind')=='shared_tool':
                retained.append(source['request_id']);continue
            if request.get('process'):continue  # Actual Job is observed/reclaimed through its registered identity.
            value=dict(project_id=self.runtime.project_id,task_id=task_id,operation_id=operation_id,source=operation['source'])
            path=io_path(self.runtime.directory/'resource-proofs'/(fingerprint(value)+'.json'))
            path.parent.mkdir(parents=True,exist_ok=True);write_atomic(path,value);proof=ref(path)
            def verify_queued(context,reference):
                current=self.runtime._load();self.runtime._resource_actor(current,actor_id,task_id)
                cancelled(current,task_id)
                stored=current['runtime']['operations'][operation_id]
                require(stored.get('status') in ('queued','cancelled_not_started') and stored.get('source')==operation['source'] and
                        load_bound(reference)==value and load_bound(stored['source'])==context, 'original queued intent differs')
                return dict(verified=True,task_cancelled_verified=True,never_started=True)
            result=self.controller.cancel_queued(source['request_id'],self.runtime.project_id,task_id,proof,verify_queued)
            with writer_lock(self.runtime.path):
                current=self.runtime._load();self.runtime._resource_actor(current,actor_id,task_id)
                require(current['runtime']['operations'][operation_id]['source']==operation['source'], 'original queued source changed')
                current['runtime']['operations'][operation_id].update(status='cancelled_not_started',cancellation_result=result)
                self.runtime._save(current,dict(op='create-resource-outcome',task_id=task_id,operation_id=operation_id))

        # Agent-slot requests are the same Controller queue, and have no author yet.
        for key,request in self.controller.read()[0]['capacity_requests'].items():
            spec=request['spec']
            if spec['project_id']!=self.runtime.project_id or spec['task_id']!=task_id or request['status']!='queued':continue
            proof_value=dict(project_id=self.runtime.project_id,task_id=task_id,request_id=key,original_spec=spec)
            path=io_path(self.runtime.directory/'resource-proofs'/(fingerprint(proof_value)+'.json'))
            path.parent.mkdir(parents=True,exist_ok=True);write_atomic(path,proof_value);proof=ref(path)
            def verify_admission(context,reference):
                current=self.runtime._load();self.runtime._resource_actor(current,actor_id,task_id);cancelled(current,task_id)
                require(context==spec and load_bound(reference)==proof_value, 'original queued admission differs')
                return dict(verified=True,task_cancelled_verified=True,never_started=True)
            self.controller.cancel_queued(key,self.runtime.project_id,task_id,proof,verify_admission,resource_class='agent_execution_slot')

        return retained

    def bind_cli(self, command, actor_id, *, user_origin, observe_cli):
        """The trusted host observes the supervised process; RESOURCE verifies its real permit."""
        from evidence import require
        task=self.runtime._load()['tasks'][command['task_id']]
        if task.get('cli_dispatch'):return self.runtime.real_use(command,actor_id)
        require(callable(observe_cli), 'actual managed CLI observer required')
        def observe(value):
            launch=load_bound(value['launch']);permit=load_bound(launch['admission'])
            request=self.controller.read()[0]['capacity_requests'][permit['request_id']]
            require(request['status']=='granted' and request['permit_id']==permit['permit_id'] and
                request['spec']=={k:permit[k] for k in request['spec']}, 'current RESOURCE permit differs')
            actual=observe_cli(value)
            return dict(actual,admission=launch['admission'],admission_context={k:permit[k] for k in
                ('project_id','task_id','request_id','permit_id','attempt_id','launch_identity')})
        return self.runtime.real_use(command,actor_id,user_origin=user_origin,observe_cli=observe)
