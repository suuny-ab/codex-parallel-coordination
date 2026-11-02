"""Finite resource close/reclamation over CORE and the registered ARCHIVE result.

Host callbacks are code-bound. No JSON grants roles, no resource scans or daemon.
"""
from copy import deepcopy
from pathlib import Path

from coordination import writer_lock
from evidence import artifact, callback_result, fingerprint, io_path, load_bound, ref, require, write_atomic
from resource_lifecycle import agent_eligibility, ended


def task_binding(task):
    return {k:deepcopy(task.get(k)) for k in ('owner_agent_id','worktree','branch','phase','delivery_revision',
        'source_version','merge','cancellation','cancellation_request','authorization','requirements')}


def save(runtime, state, task_id, operation_id, result):
    runtime._save(state, dict(op='resource-flow', task_id=task_id, operation_id=operation_id))


def confirmed_close(runtime, state, record, captured, result):
    executor=state['runtime']['executors'][record['executor_id']]
    record.update(result,actual_result=captured)
    executor.update(lease='closed',execution='unknown')
    value=dict(schema='resource-close-confirmation/1',project_id=runtime.project_id,task_id=record['task_id'],
        operation_id=record['operation_id'],executor_id=executor['id'],generation=executor['generation'],actual_result=captured,
        outcome='confirmed_closed',scope=record['proof_scope'],platform_capacity_count_known=False)
    path=io_path(runtime.directory/'receipts'/(fingerprint(value)+'.json'))
    path.parent.mkdir(parents=True,exist_ok=True);write_atomic(path,value);record['close_confirmation']=ref(path)


def close(runtime, task_id, operation_id, generation, adapter, *, authorized=False):
    require(authorized is True and operation_id, 'current exact close authority required')
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._resource_actor(state, adapter.actor_id, task_id); runtime._scope(state, task_id)
        task = state['tasks'][task_id]; identity = task['owner_agent_id']
        old = state['runtime']['operations'].get(operation_id)
        if old:
            require(old.get('operation')=='close' and old['task_id']==task_id and old['executor_id']==identity and
                    old['generation']==generation and old.get('preservation') is None, 'close operation identity differs')
            if old['status'] in ('intent','unknown') and old.get('callback_binding'):
                captured=callback_result(runtime.directory,old['callback_binding'])
                if captured and load_bound(captured)['result'].get('status')=='closed':
                    executor=agent_eligibility(runtime,state,task_id,adapter.scope,closing_operation=operation_id)
                    require(executor['routes']==old['executor_routes'] and executor['authority']==old['executor_authority'],
                            'retained close identity changed')
                    confirmed_close(runtime,state,old,captured,load_bound(captured)['result'])
                    save(runtime,state,task_id,operation_id,old)
            return deepcopy(old)
        executor = agent_eligibility(runtime, state, task_id, adapter.scope)
        require(executor['lease']=='open' and executor['generation']==generation, 'original open generation required')
        require(adapter.channel in executor['routes'], 'actual host channel differs')
        record = dict(operation='close', operation_id=operation_id, task_id=task_id, executor_id=identity,
            generation=generation, status='intent', project_id=runtime.project_id, preservation=None,
            proof_scope=adapter.scope, cleanup_authorized=True, actor_id=adapter.actor_id,
            executor_routes=deepcopy(executor['routes']), executor_authority=executor['authority'])
        launch = state['runtime']['operations'].get(task.get('worker_run',{}).get('attempt_id'),{})
        record.update({k:launch[k] for k in ('attempt_id','request_id','permit_id','launch_identity') if k in launch})
        record.setdefault('attempt_id', operation_id)
        binding = dict(project_id=runtime.project_id, task_id=task_id, operation_id=operation_id,
                       actor_id=adapter.actor_id, executor=deepcopy(executor), task=task_binding(task))
        record['callback_binding'] = binding
        state['runtime']['operations'][operation_id] = record; executor['lease']='closing'
        save(runtime, state, task_id, operation_id, record)
    try:
        result = adapter.call('close', target=executor['routes'][adapter.channel]['target_id'], authorized=True) if \
            adapter.capabilities()['close'] else dict(status='unsupported')
    except Exception as error: result = dict(status='unknown', reason=str(error))
    captured = callback_result(runtime.directory, binding, result=result, capture=True)
    try:
        with writer_lock(runtime.path):
            state = runtime._load(); record = state['runtime']['operations'][operation_id]
            executor = state['runtime']['executors'][identity]
            record['actual_result'] = captured
            current = dict(executor, lease=binding['executor']['lease'])
            if current != binding['executor'] or task_binding(state['tasks'][task_id]) != binding['task']:
                record.update(status='unknown', reason='identity/task changed; retained actual close result requires reconciliation')
            else:
                record.update(result)
                executor['lease']='closed' if result['status']=='closed' else ('open' if result['status']=='unsupported' else 'unknown')
                if result['status']=='closed':confirmed_close(runtime,state,record,captured,result)
            save(runtime, state, task_id, operation_id, record)
            return deepcopy(record)
    except (ValueError,OSError) as error:
        return dict(status='registration_pending',operation_id=operation_id,actual_result=captured,
                    reason=str(error),next_action='reconcile_original_retained_result_without_repeating')


def archive_protection(runtime, state, task_id):
    """Consume only ARCHIVE's registered minimal fact; never rerun its mover."""
    from task_state import archive_binding, archive_manifest
    task=state['tasks'][task_id];manifest=archive_manifest(state,task_id)
    archive=task.get('archive');policy=state['project'].get('archive_policy')
    protected=[policy['archive_root']] if policy else []
    if not archive:
        items=[manifest['spec'],*(manifest['deliverables'] if task['phase']=='completed' else [])]
        protected.extend(str(Path(item['path']).parent) for item in items)
        return dict(verified=True,protected_paths=protected,status='not_registered')
    operation=state['runtime']['operations'].get(archive['operation_id'],{})
    producer=task.get('producer_results',{}).get('archive:'+archive['operation_id'],{})
    plan=operation.get('plan',{})
    require(operation.get('operation')=='archive' and operation.get('task_id')==task_id and
        archive.get('schema')=='task-archive/1' and archive.get('project_id')==runtime.project_id and
        archive.get('task_id')==task_id and archive.get('plan')==plan and
        producer.get('result')==archive and plan.get('binding')==archive_binding(task) and
        plan.get('policy')==policy, 'registered ARCHIVE task/source/result binding differs')
    protected.append(plan['policy']['archive_root'])
    protected.extend(archive['protected_directories'])
    if operation['status']!='intent':
        require(operation.get('result')=={k:v for k,v in archive.items() if k!='plan'},
                'registered ARCHIVE observation differs')
    return dict(verified=True,protected_paths=protected,status=archive['status'])


def file_eligibility(runtime, state, task_id, resource, scope):
    from cleanup import canonical_path
    task = ended(state, task_id, scope)
    require(not resource.get('protected') and not resource.get('shared') and resource.get('references') == [task_id],
            'shared/protected or foreign resource remains in use')
    if task['owner_agent_id']:
        executor = agent_eligibility(runtime,state,task_id,scope)
        require(executor['lease']=='closed' and any(o.get('operation')=='close' and o.get('status')=='closed' and
            o.get('task_id')==task_id and o.get('executor_id')==executor['id'] and
            o.get('generation')==executor['generation'] and o.get('executor_routes')==executor['routes'] and
            o.get('executor_authority')==executor['authority'] for o in state['runtime']['operations'].values()),
            'actual author close unconfirmed')
    else:
        require(not any(o.get('operation') in ('spawn','send') and o.get('task_id')==task_id and
            (o.get('executor_id') or o.get('status') not in ('capacity','not_started','unsupported','queued'))
            for o in state['runtime']['operations'].values()), 'unknown original creation protects resources')
    if task['phase']=='cancelled':
        require(task['cancellation']['pr_status'] in ('closed','not_required'), 'original PR close remains pending')
    if resource['kind'] in ('local_branch','remote_branch'):
        require(resource.get('branch')==task['branch'] and task['branch']!=state['project']['main_branch'] and
            task['branch'].startswith('codex/'), 'main/foreign branch protected')
        tree=state['runtime'].get('resources',{}).get(task_id+':worktree',{})
        require(tree.get('cleanup_status')=='cleaned', 'remove dedicated worktree before task branch')
    if resource.get('path'):
        path = canonical_path(resource['path']); root = canonical_path(state['runtime']['cleanup_root'])
        require(path!=root and path.is_relative_to(root) and path!=canonical_path(runtime.project_root), 'protected cleanup root')
        protection = archive_protection(runtime,state,task_id)
        require(protection.get('verified') is True, 'actual archive result unverified')
        for protected in protection['protected_paths']:
            protected = canonical_path(protected)
            require(not path.is_relative_to(protected) and not protected.is_relative_to(path), 'pending/archive location protects resource')
        for key,other in state['tasks'].items():
            if key!=task_id and other.get('worktree'):
                other_path=canonical_path(other['worktree'])
                require(not path.is_relative_to(other_path) and not other_path.is_relative_to(path), 'another task worktree protected')
        for other in state['runtime'].get('resources',{}).values():
            if other['id']!=resource['id'] and other.get('path') and other['cleanup_status']!='cleaned':
                other_path=canonical_path(other['path'])
                require(not path.is_relative_to(other_path) and not other_path.is_relative_to(path), 'overlapping resource protected')
        if resource.get('path_identity') and io_path(path).exists():
            info=io_path(path).stat()
            require([info.st_dev,info.st_ino]==resource['path_identity'], 'resource identity changed')
        require(not any(r['kind']=='process' and r['task_id']==task_id and r['cleanup_status']!='cleaned'
            for r in state['runtime'].get('resources',{}).values()), 'stop dedicated process tree before file cleanup')
    return task


def verify(runtime, context, proof):
    """Canonical RESOURCE callback; called under RESOURCE lock, never takes CORE lock."""
    try:
        value=load_bound(proof) if 'path' in proof and 'sha256' in proof else proof
        state=runtime._load(); resource=state['runtime']['resources'][value['resource_id']]
        operation=state['runtime']['operations'][value['cleanup_operation_id']]
        require(operation.get('operation')=='resource-reclaim' and operation.get('authorized') is True and
            resource['cleanup_status']=='intent' and load_bound(resource['cleanup_proof'])==value,
            'current resource-flow intent required')
        runtime._resource_actor(state,operation['actor_id'],value['task_id']);runtime._scope(state,value['task_id'])
        require(all(context.get(k)==value.get(k) for k in ('project_id','task_id','attempt_id')) and
            context.get('resource_id',context.get('request_id'))==resource['id'], 'resource context differs')
        if resource.get('path'):
            from cleanup import canonical_path
            require(canonical_path(context['path'])==canonical_path(resource['path']), 'resource path differs')
        file_eligibility(runtime,state,value['task_id'],resource,operation['scope'])
        from task_completion import completion_flags
        return dict(completion_flags(state,value['task_id']),verified=True,preservation_valid=True,
            no_active_work=True,no_active_descendants=True,no_release_dependencies=True,
            exclusive_task_resource=True,cleanup_authorized=True,protected=False)
    except (ValueError,KeyError,TypeError,OSError): return dict(verified=False)


def reclaim(runtime, task_id, operation_id, invoke, actor_id, *, authorized=False, scope='host_verified'):
    require(authorized is True and operation_id, 'current exact resource reclaim authority required')
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        ended(state,task_id,scope)
        old=state['runtime']['operations'].get(operation_id)
        require(not old or old.get('operation')=='resource-reclaim' and old['task_id']==task_id and
                old['actor_id']==actor_id and old['scope']==scope, 'original reclaim identity differs')
        if old and old['status']=='cleanup_completed': return deepcopy(old)
        record=old or dict(operation='resource-reclaim', operation_id=operation_id, task_id=task_id,
            actor_id=actor_id, scope=scope, authorized=True, status='intent', results={})
        state['runtime']['operations'][operation_id]=record
        resources=[r for r in state['runtime'].get('resources',{}).values() if r['task_id']==task_id]
        keys=[r['id'] for r in sorted(resources,key=lambda r: (r['kind']!='process',r['kind'] in ('local_branch','remote_branch')))]
        save(runtime,state,task_id,operation_id,record)
    for rid in keys:
        with writer_lock(runtime.path):
            state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
            record=state['runtime']['operations'][operation_id]; resource=state['runtime']['resources'][rid]
            if resource['cleanup_status']=='cleaned':
                record['results'][rid]=dict(status='cleaned',already_confirmed=True);continue
            if resource['kind']=='agent':
                executor=agent_eligibility(runtime,state,task_id,scope)
                result=dict(status='retained',reason='author close remains pending')
                if executor['lease']=='closed':
                    confirmed=next(o for o in state['runtime']['operations'].values() if o.get('operation')=='close' and
                        o.get('task_id')==task_id and o.get('executor_id')==executor['id'] and
                        o.get('generation')==executor['generation'] and o.get('status')=='closed')
                    evidence=confirmed.get('release_receipt') or confirmed.get('close_confirmation') or confirmed['actual_result']
                    artifact(evidence)
                    result=dict(status='cleaned',already_confirmed=True,evidence=evidence)
                    resource.update(cleanup_status='cleaned',cleanup_result=result)
                record['results'][rid]=result;save(runtime,state,task_id,operation_id,record);continue
            pending=resource['cleanup_status'] in ('intent','unknown')
            captured=callback_result(runtime.directory,resource['flow_binding']) if pending and resource.get('flow_binding') else None
            if pending and not captured:
                record['results'][rid]=dict(status='unknown',reason='observe original uncertain cleanup; do not repeat')
                save(runtime,state,task_id,operation_id,record);continue
            try:
                file_eligibility(runtime,state,task_id,resource,scope)
            except (ValueError,KeyError,OSError) as error:
                record['results'][rid]=dict(status='retained',reason=str(error),next_owner=actor_id)
                save(runtime,state,task_id,operation_id,record);continue
            if captured:
                result=load_bound(captured)['result']; binding=resource['flow_binding']
            else:
                resource['cleanup_status']='intent'
                proof=dict(flow_contract='resource-flow/1', project_id=runtime.project_id,task_id=task_id,resource_id=rid,
                    attempt_id=resource['attempt_id'],cleanup_operation_id=operation_id,scope=scope,path=resource.get('path'))
                proof.update({k:v for k,v in resource.get('process_identity',{}).items() if k in ('request_id','nonce')})
                path=io_path(runtime.directory/'resource-proofs'/(fingerprint(proof)+'.json'))
                path.parent.mkdir(parents=True,exist_ok=True);write_atomic(path,proof);resource['cleanup_proof']=ref(path)
                binding=dict(project_id=runtime.project_id,task_id=task_id,operation_id=operation_id,actor_id=actor_id,
                             resource={k:deepcopy(v) for k,v in resource.items() if k!='flow_binding'})
                resource['flow_binding']=binding
                save(runtime,state,task_id,operation_id,record)
        if not captured:
            try: result=invoke(deepcopy(binding['resource']))
            except Exception as error: result=dict(status='unknown',reason=str(error))
            captured=callback_result(runtime.directory,binding,result=result,capture=True)
        try:
            with writer_lock(runtime.path):
                state=runtime._load();record=state['runtime']['operations'][operation_id];resource=state['runtime']['resources'][rid]
                record.setdefault('actual_results',{})[rid]=captured
                if {k:v for k,v in resource.items() if k!='flow_binding'} != binding['resource']:
                    record['results'][rid]=dict(status='unknown',reason='resource changed; retain actual result for reconciliation')
                else:
                    require(result.get('status') in ('cleaned','failed','unknown'), 'explicit cleanup result required')
                    if result['status']=='cleaned':
                        artifact(result['evidence'])
                        require(not resource.get('path') or not io_path(resource['path']).exists(), 'cleanup object still exists')
                    resource.update(cleanup_status=result['status'],cleanup_result=deepcopy(result))
                    record['results'][rid]=deepcopy(result)
                save(runtime,state,task_id,operation_id,record)
        except (ValueError,OSError) as error:
            return dict(status='registration_pending',operation_id=operation_id,resource_id=rid,actual_result=captured,
                        reason=str(error),next_action='reconcile_original_retained_result_without_repeating')
    with writer_lock(runtime.path):
        state=runtime._load();record=state['runtime']['operations'][operation_id]
        resources=[r for r in state['runtime'].get('resources',{}).values() if r['task_id']==task_id]
        unresolved=[o for o in state['runtime']['operations'].values() if o.get('task_id')==task_id and
            o.get('operation')=='create-resource' and o.get('status') not in ('registered','cancelled_not_started')]
        owner=state['tasks'][task_id]['owner_agent_id']
        quiet=(state['runtime']['executors'][owner]['lease']=='closed' if owner else not any(
            o.get('task_id')==task_id and o.get('operation') in ('spawn','send') and
            (o.get('executor_id') or o.get('status') not in ('capacity','not_started','unsupported','queued'))
            for o in state['runtime']['operations'].values()))
        record['status']='cleanup_completed' if all(r['cleanup_status']=='cleaned' for r in resources) and not unresolved and quiet else 'pending_cleanup'
        state['tasks'][task_id]['cleanup']=dict(status=record['status'],results=deepcopy(record['results']),owner=actor_id)
        save(runtime,state,task_id,operation_id,record)
        return deepcopy(record)


def cancel_stopped(runtime, task_id, operation_id, actor_id, *, observe_stop=None, observe_main=None, authorized=False, scope='host_verified'):
    """Finite observations continue the same cancel request; they never send stop or merge."""
    from development_lifecycle import cancellation_fields
    require(authorized is True and operation_id and scope in ('fixture_only','host_verified'), 'current cancellation authority required')
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        task=state['tasks'][task_id]
        require(task.get('cancellation_request'), 'actual main cancellation decision required')
        if task['phase']=='completed':return dict(status='already_merged',next_action='normal_closeout_without_main_revert')
        old=state['runtime']['operations'].get(operation_id)
        if old:
            require(old.get('operation') in ('cancel-stop','cancel-stop-verified') and old['task_id']==task_id and
                    old['actor_id']==actor_id and old['scope']==scope, 'original cancellation operation differs')
            if old['status']=='cancelled':return deepcopy(old)
        reservation=state.get('merge_reservation')
        if reservation and reservation.get('task_id')==task_id:
            return dict(status='pending_merge_reconciliation',next_owner=actor_id,operation=deepcopy(reservation))
        owner=task['owner_agent_id']
        record=old or dict(operation='cancel-stop',operation_id=operation_id,task_id=task_id,actor_id=actor_id,
                           scope=scope,status='observing',observations=[])
        record['status']='observing';sequence=len(record['observations'])+1
        binding=dict(project_id=runtime.project_id,task_id=task_id,operation_id=operation_id,actor_id=actor_id,
                     task=task_binding(task),scope=scope,sequence=sequence)
        state['runtime']['operations'][operation_id]=record
        save(runtime,state,task_id,operation_id,record)
    facts={}
    if owner:
        require(callable(observe_stop), 'actual author stop observer required')
        facts['stop']=observe_stop(deepcopy(task))
    if task.get('mr') or task.get('source_version'):
        require(callable(observe_main), 'actual original PR/main observer required')
        facts['main']=observe_main(deepcopy(task))
    captured=callback_result(runtime.directory,binding,result=facts,capture=True)
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        require(task_binding(state['tasks'][task_id])==binding['task'] and
                (not state.get('merge_reservation') or state['merge_reservation'].get('task_id')!=task_id),
                'task or serial merge changed; actual stop/main observation retained')
        task=state['tasks'][task_id];record=state['runtime']['operations'][operation_id]
        record['observations'].append(captured)
        reason=None
        if 'main' in facts:
            main=facts['main']
            require(main.get('task_id')==task_id and main.get('mr')==task.get('mr') and main.get('scope')==scope,
                    'original PR/main observation differs')
            if main.get('merged') is not False:
                reason='actual_merge_observed' if main.get('merged') is True else 'merge_unknown'
        if owner:
            executor=state['runtime']['executors'][owner];stop=facts['stop']
            require(stop.get('executor_id')==owner and stop.get('generation')==executor['generation'] and stop.get('scope')==scope,
                    'actual author observation identity differs')
            if stop.get('execution') not in ('idle','completed') or stop.get('pending_work') is not False or stop.get('active_children')!=[]:
                reason=reason or 'author_stop_unknown'
        elif any(o.get('task_id')==task_id and o.get('operation') in ('spawn','send') and
            (o.get('executor_id') or o.get('status') not in ('capacity','not_started','unsupported','queued'))
            for o in state['runtime']['operations'].values()):reason=reason or 'original_creation_unknown'
        if reason:
            record.update(status='unknown',reason=reason,next_owner=actor_id,actual_result=captured)
            save(runtime,state,task_id,operation_id,record)
            return dict(record,next_action='observe_original_creation_stop_and_main_without_redispatch')
        if owner:
            path=io_path(runtime.directory/'stop-observations'/(fingerprint(facts['stop'])+'.json'))
            path.parent.mkdir(parents=True,exist_ok=True);write_atomic(path,facts['stop'])
            executor.update(execution=facts['stop']['execution'],pending_work=False,active_children=[],observation=ref(path))
        record.update(operation='cancel-stop-verified',status='cancelled',actual_result=captured)
        record.pop('reason',None);record.pop('next_owner',None)
        task.update(phase='cancelled',cancellation=dict(mode='resource-flow/1',proof=captured,
            binding=cancellation_fields(task),flow_operation=operation_id,scope=scope,
            pr_status='pending' if task.get('mr') else 'not_required'),cleanup=dict(status='pending_cleanup',owner=actor_id))
        if task.get('worker_run'):task['worker_run']['status']='cancelled'
        for item in state['items'].values():
            if item.get('task_id')==task_id and item['status'] in ('queued','active','waiting_user'):
                item['status']='superseded'
                if state['active_human_item']==item['id']:state['active_human_item']=None
                if state.get('priority_item')==item['id']:state['priority_item']=None
        save(runtime,state,task_id,operation_id,record)
        return deepcopy(record)


def close_cancelled_pr(runtime, task_id, operation_id, invoke, actor_id, *, authorized=False, scope='host_verified'):
    from development_lifecycle import cancelled
    require(authorized is True and operation_id, 'current exact original PR close authority required')
    with writer_lock(runtime.path):
        state=runtime._load();runtime._resource_actor(state,actor_id,task_id);runtime._scope(state,task_id)
        cancelled(state,task_id,scope);task=state['tasks'][task_id];cancel=task['cancellation']
        if cancel['pr_status'] in ('closed','not_required'):return deepcopy(cancel)
        old=state['runtime']['operations'].get(operation_id)
        if old:
            require(old.get('operation')=='cancel-pr' and old['task_id']==task_id, 'original PR close identity differs')
            return deepcopy(old)
        require(cancel['pr_status'] not in ('intent','unknown'), 'observe original uncertain PR close; do not reissue')
        record=dict(operation='cancel-pr',operation_id=operation_id,task_id=task_id,mr=deepcopy(task['mr']),
                    actor_id=actor_id,status='intent',scope=scope)
        binding=dict(project_id=runtime.project_id,task_id=task_id,operation_id=operation_id,mr=record['mr'],actor_id=actor_id)
        record['callback_binding']=binding
        state['runtime']['operations'][operation_id]=record
        cancel.update(pr_status='intent',pr_operation_id=operation_id)
        save(runtime,state,task_id,operation_id,record)
    try:result=invoke(deepcopy(record))
    except Exception as error:result=dict(status='unknown',reason=str(error))
    captured=callback_result(runtime.directory,binding,result=result,capture=True)
    try:
        with writer_lock(runtime.path):
            state=runtime._load();cancelled(state,task_id,scope);task=state['tasks'][task_id]
            record=state['runtime']['operations'][operation_id];record['actual_result']=captured
            require(task['mr']==binding['mr'], 'PR identity changed; actual result retained')
            require(result.get('status') in ('closed','failed','unknown'), 'actual PR close result required')
            if result['status']=='closed':artifact(result['evidence'])
            record.update(result);task['cancellation'].update(pr_status=result['status'],pr_result=deepcopy(result))
            save(runtime,state,task_id,operation_id,record)
            return deepcopy(record)
    except (ValueError,OSError) as error:
        return dict(status='registration_pending',operation_id=operation_id,actual_result=captured,
                    reason=str(error),next_action='reconcile_original_retained_result_without_repeating')


def verify_close(runtime, reference):
    try:
        state=runtime._load();value=load_bound(reference);record=state['runtime']['operations'][value['operation_id']]
        require(record.get('close_confirmation')==reference and record.get('status')=='closed' and
            record.get('actual_result')==value['actual_result'] and value.get('outcome')=='confirmed_closed',
            'canonical actual close confirmation required')
        executor=agent_eligibility(runtime,state,record['task_id'],record['proof_scope'])
        require(executor['lease']=='closed' and executor['id']==value['executor_id'] and
            executor['generation']==value['generation'] and executor['routes']==record['executor_routes'] and
            executor['authority']==record['executor_authority'], 'close identity changed')
        require(load_bound(value['actual_result'])['result'].get('status')=='closed', 'actual close unconfirmed')
        return dict(verified=True,scope=record['proof_scope'],platform_capacity_count_known=False)
    except (ValueError,KeyError,TypeError,OSError):return dict(verified=False)
