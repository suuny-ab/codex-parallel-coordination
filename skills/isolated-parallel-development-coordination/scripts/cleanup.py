"""Task cleanup state in the canonical ledger; finite scoped side-effect callbacks."""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path

from coordination import writer_lock
from evidence import artifact, callback_result, load_bound, require, write_atomic, fingerprint, ref, io_path
from task_completion import definition, verified, preservation_completion, completion_flags

KINDS={'agent','process','worktree','database','environment','artifact','local_branch','remote_branch'}


def canonical_path(path):
    value=str(Path(path).resolve())
    if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
    elif value.startswith('\\\\?\\'):value=value[4:]
    return Path(value)


def register(runtime, task_id, resource, authority_ref, actor_id):
    artifact(authority_ref)
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        require(resource.get('id') and resource.get('kind') in KINDS,'explicit resource identity/kind required')
        resources=state['runtime'].setdefault('resources',{})
        require(resource['id'] not in resources,'resource already registered')
        root=canonical_path(state['runtime']['cleanup_root'])
        path=canonical_path(resource['path']) if resource.get('path') else None
        if resource['kind'] in ('worktree','database','environment','artifact'):
            require(path and path!=root and path.is_relative_to(root),'resource path outside managed cleanup root')
        elif resource['kind'] in ('local_branch','remote_branch'):
            task=state['tasks'][task_id]
            require(resource.get('branch')==task['branch'] and resource['branch'].startswith('codex/') and
                    resource['branch']!=state['project']['main_branch'] and resource.get('expected_head'),
                    'exact dedicated task branch required')
        else:
            require(resource.get('executor_id') if resource['kind']=='agent' else resource.get('process_identity'),
                    'exact registered executor/process identity required')
            if resource['kind']=='agent':
                executor=state['runtime']['executors'][resource['executor_id']]
                require(resource['executor_id']==state['tasks'][task_id]['owner_agent_id'] and
                        resource.get('generation')==executor['generation'],'agent resource identity/generation mismatch')
        value=dict(deepcopy(resource),task_id=task_id,project_id=runtime.project_id,authority=authority_ref,
            references=list(resource.get('references',[task_id])),cleanup_status='retained')
        if path is not None:value['path']=str(path)
        if path is not None and resource['kind'] in ('worktree','database','environment','artifact') and io_path(path).exists():
            info=io_path(path).stat()
            value['path_identity']=[info.st_dev,info.st_ino]
        value.setdefault('attempt_id',state['tasks'][task_id].get('worker_run',{}).get('attempt_id','resource-'+resource['id']))
        require(task_id in value['references'] and all(k in state['tasks'] for k in value['references']), 'resource references must be registered tasks')
        resources[resource['id']]=value
        runtime._save(state,dict(op='register-resource',task_id=task_id,resource_id=resource['id']))
        return deepcopy(value)


def eligibility(runtime,state,task_id,preservation_ref,scope,*,require_pr_closed=True,closing_operation=None,resource_paths=True):
    task=state['tasks'][task_id]
    cancelled=task['phase']=='cancelled'
    from resource_lifecycle import inventory
    if not cancelled or task.get('resource_contract')=='new-task/1':
        inventory(state,task_id,require_paths=resource_paths)
    if cancelled:
        from development_lifecycle import cancelled as verify_cancelled
        record=verify_cancelled(state,task_id,scope)
        if require_pr_closed:
            require(record['pr_status'] in ('closed','not_required'),'cancelled PR must be confirmed closed before resource cleanup')
            if record['pr_status']=='closed':artifact(record['pr_result']['evidence'])
    elif definition(task)[0]=='read_only':verified(state,task_id)
    else:require(task['phase']=='completed' and task.get('merge',{}).get('verified'),'task must be merged into target main and verified before reclamation')
    require(not task.get('publication_dependencies'),'release dependency still uses development resources')
    inventory=load_bound(preservation_ref)
    from preservation_contract import verify_inventory
    verify_inventory(state, task_id, inventory)
    if inventory.get('remote_verification'):
        from resource_lifecycle import remote_binding, preserved_binding
        require(task.get('preservation')==preservation_ref and inventory.get('binding')==preserved_binding(task),
                'canonical cloud-bound preservation required')
        remote_binding(state,task_id,inventory['source'],inventory['remote_verification'],scope)
    executor=state['runtime']['executors'].get(task['owner_agent_id'])
    require(executor and inventory.get('executor_id')==executor['id'] and inventory.get('generation')==executor['generation'],
            'preservation executor/generation changed')
    artifact(executor['authority'])
    if cancelled:
        require(inventory.get('cancellation_verification')==task['cancellation']['proof'],'preservation not bound to cancellation')
    elif definition(task)[0]=='read_only':preservation_completion(state,task_id,inventory,scope)
    else:
        proof=load_bound(inventory['merge_verification'])
        require(proof.get('project_id')==runtime.project_id and proof.get('task_id')==task_id and
            proof.get('source_head')==task['merge'].get('head_sha') and proof.get('main_head') and
            proof.get('main_ref')==state['project']['main_branch'] and proof.get('contains_source') is True and
            proof.get('scope')==scope and task['merge'].get('scope',scope)==scope and proof.get('evidence'),'target main proof mismatch')
        for item in proof['evidence']:artifact(item)
    require(inventory.get('files') and inventory.get('unregistered_evidence_preserved') is True,'external evidence preservation required')
    def external(item):
        path=canonical_path(artifact(item))
        require(not path.is_relative_to(canonical_path(runtime.project_root)) and not path.is_relative_to(canonical_path(task['worktree'])) and
                not path.is_relative_to(canonical_path(state['runtime']['cleanup_root'])),'preservation must outlive all managed resources')
        for resource in state['runtime'].get('resources',{}).values():
            require(not resource.get('path') or not path.is_relative_to(canonical_path(resource['path'])),'preservation inside cleanup resource')
    external(preservation_ref)
    external(executor['authority'])
    external(executor['observation'])
    if cancelled:
        external(task['cancellation']['proof'])
        for item in load_bound(task['cancellation']['proof'])['evidence']:external(item)
    elif definition(task)[0]=='code':
        external(inventory['merge_verification'])
        for item in proof['evidence']:external(item)
    for item in inventory['files']:
        external(item)
        if item.get('original'):
            original=Path(item['original']['path'])
            if item.get('git_source'):
                from evidence import verify_historical_copy
                verify_historical_copy(state,task_id,item)
                continue
            durable=(inventory.get('remote_verification') and task.get('preservation')==preservation_ref and
                any(r.get('task_id')==task_id and r.get('path') and
                    canonical_path(original).is_relative_to(canonical_path(r['path']))
                    for r in state['runtime'].get('resources',{}).values()))
            if io_path(original).exists() and not durable:artifact(item['original'])
            elif durable:pass
            else:
                cleaned=[r for r in state['runtime'].get('resources',{}).values() if r.get('path') and
                    canonical_path(original).is_relative_to(canonical_path(r['path'])) and r['cleanup_status']=='cleaned']
                require(cleaned or durable,'original evidence disappeared outside confirmed cleanup/preserved cloud completion')
                for resource in cleaned:artifact(resource['cleanup_result']['evidence'])
            require(item['sha256']==item['original']['sha256'],'external copy bytes differ')
    for item in inventory.get('git_sources', []):
        from evidence import historical_git_artifact
        _, provenance = historical_git_artifact(state,task_id,item['git_source']['report'],item['original'])
        require(provenance==item['git_source'],'preserved Git source identity changed')
    external(inventory['handoff'])
    sources=[task.get('delivery') or task.get('pause_checkpoint')]
    if task.get('development_binding'):sources.append(task['development_binding']['spec'])
    if cancelled:sources+=load_bound(task['cancellation']['proof'])['artifacts']
    else:require(sources[0],'current task delivery not externally preserved')
    for source in filter(None,sources):
        require(any(row.get('original')==source and row['sha256']==source['sha256'] for row in inventory['files']),
                'current task delivery/work not externally preserved')
    for t in state['tasks'].values():
        if t['owner_agent_id']==task['owner_agent_id']:
            require(t['phase'] in ('completed','cancelled') and (t['phase']=='cancelled' or t.get('worker_run',{}).get('status')!='running'),
                    'executor still serves active work')
    executor=state['runtime']['executors'].get(task['owner_agent_id'])
    require(executor and (executor['execution'] in ('idle','completed') or executor['lease']=='closed') and executor.get('active_children')==[] and
            executor.get('pending_work') is False,'active or unverified executor/descendants')
    require(not executor.get('shared') and not executor.get('protected'),'shared/protected executor remains in use')
    pending=state['runtime']['operations'].get(closing_operation,{}) if closing_operation else {}
    native_closing=(executor['lease']=='closing' and pending.get('operation')=='close' and
        pending.get('native_request') and pending.get('task_id')==task_id and pending.get('executor_id')==executor['id'] and
        pending.get('generation')==executor['generation'] and pending.get('status') in ('intent','unknown'))
    require(executor['lease'] in ('open','closed') or native_closing,'uncertain close/resume must be reconciled before new cleanup')
    if executor['lease']=='closed':
        require(any(operation.get('operation')=='close' and operation.get('status')=='closed' and
            operation.get('task_id')==task_id and operation.get('executor_id')==executor['id'] and
            operation.get('generation')==executor['generation'] and operation.get('executor_routes')==executor['routes'] and
            operation.get('executor_authority')==executor['authority'] for operation in state['runtime']['operations'].values()),
            'closed executor identity has no current canonical confirmation')
    observation=load_bound(executor['observation'])
    require(observation.get('executor_id')==executor['id'] and observation.get('generation')==executor['generation'] and
            observation.get('execution') in ('idle','completed') and observation.get('active_children')==[] and
            observation.get('pending_work') is False,'stale or active executor observation')
    require(task['owner_agent_id'] not in (state['coordinator_id'],state.get('delivery',{}).get('agent_id')),
            'main/delivery executor is protected')
    require(not task.get('acceptance_environment_requires_agent'),'acceptance environment requires live executor')
    require(state['items'].get(state['active_human_item'],{}).get('task_id')!=task_id,'active user item protects task')


def recovered_results(runtime,state,task_id,operation_id,preservation_ref,actor_id,scope):
    """Validate a retained return in a candidate before accepting its deleted source."""
    record=state['runtime']['operations'].get(operation_id,{})
    require(not record or record.get('operation')=='cleanup' and record.get('task_id')==task_id and
            record.get('preservation')==preservation_ref and record.get('scope')==scope,'cleanup ID belongs elsewhere')
    recovered=[]
    for rid,binding in record.get('callback_captures',{}).items():
        resource=state['runtime']['resources'][rid]
        if resource['cleanup_status']!='intent':continue
        captured=callback_result(runtime.directory,binding)
        if not captured:continue
        value=load_bound(captured);result=value['result']
        require(binding.get('operation_id')==operation_id and binding.get('task_id')==task_id and
                binding.get('project_id')==runtime.project_id and binding.get('actor_id')==actor_id and
                binding.get('preservation')==preservation_ref and binding.get('scope')==scope and
                binding.get('resource')==resource,'retained cleanup return identity changed')
        task=deepcopy(state['tasks'][task_id]);before=deepcopy(binding['task'])
        task.pop('cleanup',None);before.pop('cleanup',None)
        require(task==before,'task changed before cleanup return reconciliation')
        record.setdefault('external_results',{})[rid]=deepcopy(result)
        record.setdefault('callback_results',{})[rid]=captured
        if not isinstance(result,dict) or result.get('status')!='cleaned':continue
        require(not resource.get('protected') and all(state['tasks'][key]['phase'] in ('completed','cancelled')
                for key in resource['references']),'retained cleanup resource protected or referenced')
        if resource.get('shared'):
            unused=load_bound(resource['unused_proof'])
            require(unused.get('resource_id')==rid and unused.get('active_references')==[],'shared references remain')
        artifact(result['evidence'])
        require(resource['kind'] in ('worktree','database','environment','artifact','process'), 'not a file/process cleanup return')
        if resource.get('path'):
            path=canonical_path(resource['path']);root=canonical_path(state['runtime']['cleanup_root'])
            require(path!=root and path.is_relative_to(root) and not io_path(path).exists(),'original cleanup target remains or escaped')
            for other in state['runtime']['resources'].values():
                if other['id']==rid or not other.get('path') or other['cleanup_status']=='cleaned':continue
                target=canonical_path(other['path'])
                require(not path.is_relative_to(target) and not target.is_relative_to(path),'overlapping cleanup resource')
            for key,other in state['tasks'].items():
                if key==task_id:continue
                target=canonical_path(other['worktree'])
                require(not path.is_relative_to(target) and not target.is_relative_to(path),'another task worktree is protected')
        resource.update(cleanup_status='cleaned',cleanup_result=deepcopy(result))
        recovered.append(rid)
    return recovered


def run(runtime,task_id,operation_id,preservation_ref,invoke,actor_id,*,authorized=False,scope='fixture_only'):
    require(authorized is True and operation_id,'separate exact cleanup authorization required')
    with ExitStack() as locks:
        locks.enter_context(writer_lock(runtime.path))
        state=runtime._load();authority=runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        recovered=recovered_results(runtime,state,task_id,operation_id,preservation_ref,actor_id,scope)
        eligibility(runtime,state,task_id,preservation_ref,scope)
        if recovered:runtime._save(state,dict(op='reconcile-cleanup-callback-results',task_id=task_id,resources=recovered))
        safe_to_save=True
        operations=state['runtime']['operations']
        continuing=operation_id in operations
        if continuing:
            old=operations[operation_id]
            require(old['operation']=='cleanup' and old['task_id']==task_id and old['preservation']==preservation_ref and
                    old['scope']==scope,'cleanup ID belongs elsewhere')
            if old['status']=='cleanup_completed':return deepcopy(old)
        task=state['tasks'][task_id]
        resources=[r for r in state['runtime'].get('resources',{}).values() if r['task_id']==task_id]
        record=operations[operation_id] if continuing else dict(operation='cleanup',task_id=task_id,operation_id=operation_id,status='intent',results={},preservation=preservation_ref,
                    cleanup_authorized=authorized,scope=scope)
        if authority:record.update(resource_authority=authority,actor_id=actor_id)
        operations[operation_id]=record
        task['cleanup']=dict(status='pending_cleanup',operation_id=operation_id,owner=actor_id)
        if not continuing:runtime._save(state,dict(op='cleanup-intent',task_id=task_id))
        for rid in [r['id'] for r in resources]:
            resource=state['runtime']['resources'][rid]
            if resource['cleanup_status']=='cleaned':
                record['results'][rid]=dict(status='cleaned',already_confirmed=True);continue
            if resource['cleanup_status'] in ('intent','unknown'):
                record['results'][rid]=dict(status='unknown',reason='inspect previous uncertain effect; do not retry');continue
            if continuing and resource['cleanup_status']=='failed':continue
            try:
                require(not resource.get('protected'),'protected resource')
                require(all(state['tasks'][k]['phase'] in ('completed','cancelled') for k in resource['references']),'other task still references resource')
                if resource.get('shared'):
                    unused=load_bound(resource['unused_proof'])
                    require(unused.get('resource_id')==rid and unused.get('active_references')==[],'shared references not verified unused')
                if resource.get('path'):
                    root=canonical_path(state['runtime']['cleanup_root']);path=canonical_path(resource['path'])
                    require(path!=root and path.is_relative_to(root),'resolved cleanup path escaped managed root')
                    if resource.get('path_identity'):
                        info=io_path(path).stat()
                        require([info.st_dev,info.st_ino]==resource['path_identity'],'resource path now identifies another object')
                    for other in state['runtime'].get('resources',{}).values():
                        if other['id']==rid or not other.get('path') or other['cleanup_status']=='cleaned':continue
                        other_path=canonical_path(other['path'])
                        require(not path.is_relative_to(other_path) and not other_path.is_relative_to(path),
                                'overlapping registered resource must be resolved before cleanup')
                    for key,other in state['tasks'].items():
                        if key==task_id:continue
                        other_path=canonical_path(other['worktree'])
                        require(not path.is_relative_to(other_path) and not other_path.is_relative_to(path),'another task worktree is protected')
                if resource['kind']=='agent':
                    executor=state['runtime']['executors'][resource['executor_id']]
                    confirmed=[o for o in operations.values() if o.get('operation')=='close' and
                        o.get('executor_id')==resource['executor_id'] and o.get('generation')==resource['generation'] and
                        o.get('task_id')==task_id and o.get('status')=='closed' and
                        (o.get('release_receipt') or o.get('close_confirmation'))]
                    require(executor['generation']==resource['generation'] and executor['lease']=='closed' and confirmed,
                            'close via canonical current lifecycle entry before resource aggregation')
                    close=confirmed[-1]
                    result=dict(status='cleaned',already_confirmed=True,evidence=close.get('release_receipt') or close['close_confirmation'])
                    artifact(result['evidence'])
                    valid=(runtime.verify_release_receipt(load_bound(result['evidence']),result['evidence']) if close.get('release_receipt')
                           else runtime.verify_close_confirmation(result['evidence']))
                    require(valid['verified'],
                            'canonical release is stale')
                    resource.update(cleanup_status='cleaned',cleanup_result=deepcopy(result))
                    record['results'][rid]=result
                    runtime._save(state,dict(op='aggregate-confirmed-agent-close',resource_id=rid))
                    continue
                resource['cleanup_status']='intent'
                guard=dict(project_id=runtime.project_id,task_id=task_id,resource_id=rid,attempt_id=resource['attempt_id'],
                    cleanup_operation_id=operation_id,preservation=preservation_ref,scope=scope,path=resource.get('path'))
                for key in ('request_id','nonce'):
                    if key in resource.get('process_identity',{}):guard[key]=resource['process_identity'][key]
                proof_path=io_path(runtime.directory/'cleanup-proofs'/(fingerprint(guard)+'.json'))
                proof_path.parent.mkdir(exist_ok=True)
                if proof_path.exists():require(load_bound(ref(proof_path))==guard,'cleanup proof changed')
                else:write_atomic(proof_path,guard)
                resource['cleanup_proof']=ref(proof_path)
                bound=deepcopy(resource);task_before=deepcopy(task)
                binding=dict(project_id=runtime.project_id,task_id=task_id,operation_id=operation_id,
                    actor_id=actor_id,preservation=preservation_ref,scope=scope,resource=bound,task=task_before)
                record.setdefault('callback_captures',{})[rid]=binding
                runtime._save(state,dict(op='resource-cleanup-intent',resource_id=rid))
                # No shell or arbitrary command from JSON; invoke is host-owned.
                safe_to_save=False
                locks.close()
                try:
                    result=invoke(bound)
                except Exception as error:
                    result=dict(status='unknown',reason=str(error),error_type=type(error).__name__)
                captured=callback_result(runtime.directory,binding,result=result,capture=True)
                locks.enter_context(writer_lock(runtime.path))
                state=runtime._load();task=state['tasks'][task_id]
                record=state['runtime']['operations'][operation_id];operations=state['runtime']['operations']
                resource=state['runtime']['resources'][rid]
                resources=[r for r in state['runtime']['resources'].values() if r['task_id']==task_id]
                safe_to_save=True
                record.setdefault('callback_results',{})[rid]=captured
                record.setdefault('external_results',{})[rid]=deepcopy(result)
                if resource!=bound:
                    identity={k:v for k,v in resource.items() if k not in ('cleanup_status','cleanup_result','cleanup_observations')}
                    original={k:v for k,v in bound.items() if k not in ('cleanup_status','cleanup_result','cleanup_observations')}
                    if resource['cleanup_status']=='cleaned' and resource.get('cleanup_result')==result and identity==original:
                        # Another caller already adopted this exact captured return under the same intent.
                        if record['status']=='cleanup_completed':return deepcopy(record)
                        continue
                    record.setdefault('conflicts',{})[rid]='resource changed during external call; current canonical facts retained'
                    runtime._save(state,dict(op='resource-cleanup-conflict',resource_id=rid))
                    continue
                require(task==task_before,'task changed during cleanup; actual result retained')
                runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
                require(isinstance(result,dict) and result.get('status') in ('cleaned','failed','unknown'),'explicit cleanup result required')
                if result['status']=='cleaned':
                    artifact(result['evidence'])
                    if resource.get('path') and resource['kind'] in ('worktree','database','environment','artifact'):
                        require(not io_path(resource['path']).exists(),'cleanup reported success but resource still exists')
                    if resource['kind']=='agent':
                        require(result.get('closed') is True,'agent report completion is not confirmed close')
                        executor=state['runtime']['executors'][resource['executor_id']]
                        require(executor['generation']==resource['generation'],'agent generation changed')
                        executor.update(lease='closed',execution='unknown')
                    candidate=deepcopy(state)
                    candidate['runtime']['resources'][rid].update(cleanup_status='cleaned',cleanup_result=deepcopy(result))
                    eligibility(runtime,candidate,task_id,preservation_ref,scope,resource_paths=False)
            except Exception as error:
                if not safe_to_save:raise  # Never classify a failed re-lock/load by saving the old snapshot.
                result=dict(status='failed' if resource['cleanup_status']!='intent' else 'unknown',reason=str(error),error_type=type(error).__name__)
            resource['cleanup_status']=result['status'];resource['cleanup_result']=deepcopy(result);record['results'][rid]=deepcopy(result)
            runtime._save(state,dict(op='resource-cleanup-outcome',resource_id=rid))
        executor=state['runtime']['executors'][task['owner_agent_id']]
        complete=all(r['cleanup_status']=='cleaned' for r in resources) and executor['lease']=='closed' and not record.get('conflicts')
        if executor['lease']!='closed':record['agent_context']='not_confirmed_closed'
        record['status']='cleanup_completed' if complete else 'pending_cleanup'
        task['cleanup'].update(status=record['status'],results=deepcopy(record['results']))
        runtime._save(state,dict(op='cleanup-round',task_id=task_id))
        return deepcopy(record)


def verify(runtime,context,proof):
    """RESOURCE may call while holding its lock; this never acquires CORE lock."""
    try:
        value=load_bound(proof) if 'path' in proof and 'sha256' in proof else proof
        state=runtime._load();resource=state['runtime']['resources'][value['resource_id']]
        operation=state['runtime']['operations'][value['cleanup_operation_id']]
        if operation.get('resource_authority'):
            require(runtime._resource_actor(state,operation['actor_id'],value['task_id'])==operation['resource_authority'],
                    'task resource authority changed')
        require(resource['cleanup_status']=='intent' and operation['status']=='intent' and
            operation['cleanup_authorized'] is True and load_bound(resource['cleanup_proof'])==value,'not a current canonical cleanup intent')
        require(context.get('resource_id',context.get('request_id'))==resource['id'] and
            all(context.get(k)==value.get(k) for k in ('project_id','task_id','attempt_id')),'resource context differs')
        if resource.get('path'):require(canonical_path(context['path'])==canonical_path(resource['path']),'resource path differs')
        eligibility(runtime,state,value['task_id'],operation['preservation'],operation['scope'])
        require(not resource.get('protected') and all(state['tasks'][k]['phase'] in ('completed','cancelled') for k in resource['references']),
                'resource protected or referenced')
        if resource.get('shared'):
            unused=load_bound(resource['unused_proof'])
            require(unused.get('resource_id')==resource['id'] and unused.get('active_references')==[],'shared resource still in use')
        return dict(completion_flags(state,value['task_id']),verified=True,preservation_valid=True,no_active_work=True,
            no_active_descendants=True,no_release_dependencies=True,exclusive_task_resource=True,cleanup_authorized=True,protected=False)
    except (ValueError,KeyError,TypeError,OSError):return dict(verified=False)


def reconcile(runtime,task_id,resource_id,observation_ref,actor_id):
    """Read actual effect after an unknown response, never resend the delete/close."""
    facts=load_bound(observation_ref)
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        resource=state['runtime']['resources'][resource_id]
        require(resource['task_id']==task_id and resource['cleanup_status'] in ('unknown','intent'),'not an uncertain owned resource')
        require(facts.get('resource_id')==resource_id and facts.get('status') in ('cleaned','retained') and
                facts.get('evidence'),'actual effect observation required')
        for item in facts['evidence']:artifact(item)
        if facts['status']=='cleaned' and resource.get('path') and resource['kind'] in ('worktree','database','environment','artifact'):
            require(not io_path(resource['path']).exists(),'resource path remains or was replaced; reconcile current identity')
        if resource['kind']=='agent' and facts['status']=='cleaned':require(facts.get('closed') is True,'idle is not closed')
        if resource['kind']=='agent' and facts['status']=='cleaned':
            executor=state['runtime']['executors'][resource['executor_id']]
            require(executor['generation']==resource['generation'] and executor['lease']=='closed',
                    'canonical lifecycle must already confirm the current agent close')
        resource['cleanup_status']='cleaned' if facts['status']=='cleaned' else 'retained'
        if facts['status']=='cleaned':resource['cleanup_result']=dict(status='cleaned',evidence=observation_ref)
        resource.setdefault('cleanup_observations',[]).append(observation_ref)
        runtime._save(state,dict(op='reconcile-resource-cleanup',resource_id=resource_id))
        return deepcopy(resource)
