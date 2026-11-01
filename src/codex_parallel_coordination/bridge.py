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

    def admit(self, spec_ref):
        """Reserve actual RESOURCE quota and bind a CORE admission artifact."""
        spec = load_bound(spec_ref)
        record = self.controller.capacity_acquire(spec_ref["path"])
        if record["spec"] != spec:
            raise ValueError("capacity source changed during admission; reconcile reserved quota")
        if record["status"] != "granted":
            return {"status": record["status"], "request_id": spec["request_id"], "admission": None}
        value = dict(spec, status="granted", permit_id=record["permit_id"], resource_source=spec_ref)
        self.evidence.mkdir(parents=True, exist_ok=True)
        path = self.evidence / ("admission-" + fingerprint(value) + ".json")
        if path.exists():
            if load(path) != value:
                raise ValueError("immutable admission evidence changed")
        else:
            write_atomic(path, value)
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

    def create_worktree(self, task_id, operation_id, authority, actor_id, *, authorized=False):
        from codex_parallel_coordination.worktrees import GitWorktrees
        from resource_lifecycle import create
        state = self.runtime._load(); task = state["tasks"][task_id]
        adapter = GitWorktrees(self.runtime.project_root, state["runtime"]["cleanup_root"])
        self.artifacts.adapters["git_worktree"] = adapter
        resource = dict(id=task_id+":worktree", kind="worktree", path=task["worktree"])
        # This is a code-bound capability; JSON cannot choose an import or shell command.
        def invoke(value):
            record = dict(resource_id=value["id"], path=value["path"],
                          metadata=dict(repository_root=str(adapter.repository)))
            metadata = adapter.create(record, task["branch"], state["project"]["main_branch"])
            self.artifacts.register(dict(record, project_id=self.runtime.project_id, task_id=task_id,
                attempt_id=operation_id, kind="git_worktree", references=[], protected=False, metadata=metadata))
            return dict(metadata=metadata, attempt_id=operation_id)
        # RESOURCE scope is checked before Git, including missing capabilities/foreign roots.
        self.artifacts._scope(dict(project_id=self.runtime.project_id, path=task["worktree"],
                                   kind="git_worktree"), self.controller.read()[1])
        return create(self.runtime, task_id, operation_id, resource, authority, actor_id, invoke, authorized=authorized)

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
        from coordination import writer_lock
        from evidence import artifact, require
        spec = load_bound(spec_ref)
        require(authorized is True and spec["project_id"] == self.runtime.project_id and spec["task_id"] == task_id,
                "current task-scoped process creation authority required")
        artifact(authority)
        with writer_lock(self.runtime.path):
            state = self.runtime._load(); self.runtime._resource_actor(state, actor_id, task_id); self.runtime._scope(state, task_id)
            require(state["tasks"][task_id].get("resource_contract") == "new-task/1" and
                    state["tasks"][task_id]["phase"] not in ("completed","cancelled"), "unfinished managed task required")
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
            self.runtime._save(state, dict(op="process-creation-intent", task_id=task_id))
            try:
                actual = self.controller.submit(spec_ref["path"])
                require(actual["spec"] == spec, "process spec changed; inspect original Job")
                if actual.get("process"):
                    state["runtime"].setdefault("resources",{})[spec["request_id"]] = dict(id=spec["request_id"],
                        kind="process", task_id=task_id, project_id=self.runtime.project_id, attempt_id=spec["attempt_id"],
                        process_identity=dict(request_id=spec["request_id"], nonce=actual["nonce"], process=actual["process"],
                                              launch_identity=spec["launch_identity"], source=spec_ref),
                        references=[task_id], authority=authority, cleanup_status="retained")
                    operation.update(status="registered")
                else:
                    operation.update(status="queued", reason=actual.get("reason"), next_action="obtain_resource_admission")
            except Exception as error:
                operation.update(status="unknown", reason=type(error).__name__, next_action="inspect_registered_process_without_relaunch")
            self.runtime._save(state, dict(op="process-creation-outcome", task_id=task_id))
            return operation

    def _process_result(self, resource, *, terminate=False):
        identity = resource["process_identity"]
        request = self.controller.read()[0]["requests"][identity["request_id"]]
        from evidence import load_bound as core_bound, preserved_read
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
            state = self.runtime._load(); self.runtime._resource_actor(state, actor_id, task_id); self.runtime._scope(state, task_id)
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
        return self.controller.capacity_release(context["request_id"], context["permit_id"],
                                                receipt, self.runtime.verify_release_receipt)
