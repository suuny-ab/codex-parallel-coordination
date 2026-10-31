"""Task cleanup state in the canonical ledger; finite scoped side-effect callbacks."""
from copy import deepcopy
from pathlib import Path

from coordination import writer_lock
from evidence import artifact, load_bound, require, write_atomic, fingerprint, ref, io_path
from task_completion import definition, verified, preservation_completion, completion_flags

KINDS={'agent','process','worktree','database','environment','artifact'}


def register(runtime, task_id, resource, authority_ref, actor_id):
    artifact(authority_ref)
    with writer_lock(runtime.path):
        state=runtime._load();runtime._actor(state,actor_id);runtime._scope(state,task_id)
        require(resource.get('id') and resource.get('kind') in KINDS,'explicit resource identity/kind required')
        resources=state['runtime'].setdefault('resources',{})
        require(resource['id'] not in resources,'resource already registered')
        root=Path(state['runtime']['cleanup_root']).resolve()
        path=Path(resource['path']).resolve() if resource.get('path') else None
        if resource['kind'] in ('worktree','database','environment','artifact'):
            require(path and path!=root and path.is_relative_to(root),'resource path outside managed cleanup root')
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
        value.setdefault('attempt_id',state['tasks'][task_id].get('worker_run',{}).get('attempt_id','resource-'+resource['id']))
        require(task_id in value['references'] and all(k in state['tasks'] for k in value['references']), 'resource references must be registered tasks')
        resources[resource['id']]=value
        runtime._save(state,dict(op='register-resource',task_id=task_id,resource_id=resource['id']))
        return deepcopy(value)


def eligibility(runtime,state,task_id,preservation_ref,scope):
    task=state['tasks'][task_id]
    if definition(task)[0]=='read_only':verified(state,task_id)
    else:require(task['phase']=='completed' and task.get('merge',{}).get('verified'),'task not completed on target main')
    require(not task.get('publication_dependencies'),'release dependency still uses development resources')
    inventory=load_bound(preservation_ref)
    if definition(task)[0]=='read_only':preservation_completion(state,task_id,inventory,scope)
    else:
        proof=load_bound(inventory['merge_verification'])
        require(proof.get('project_id')==runtime.project_id and proof.get('task_id')==task_id and
            proof.get('source_head')==task['merge'].get('head_sha') and proof.get('main_head') and
            proof.get('main_ref')==state['project']['main_branch'] and proof.get('contains_source') is True and
            proof.get('scope')==scope and proof.get('evidence'),'target main proof mismatch')
        for item in proof['evidence']:artifact(item)
    require(inventory.get('files') and inventory.get('unregistered_evidence_preserved') is True,'external evidence preservation required')
    for item in inventory['files']:
        require(not artifact(item).resolve().is_relative_to(runtime.project_root),'preservation must outlive project resources')
        if item.get('original'):
            original=Path(item['original']['path'])
            if original.exists():artifact(item['original'])
            else:
                cleaned=[r for r in state['runtime'].get('resources',{}).values() if r.get('path') and
                    original.resolve().is_relative_to(Path(r['path']).resolve()) and r['cleanup_status']=='cleaned']
                require(cleaned,'original evidence disappeared outside confirmed cleanup')
                for resource in cleaned:artifact(resource['cleanup_result']['evidence'])
            require(item['sha256']==item['original']['sha256'],'external copy bytes differ')
    artifact(inventory['handoff'])
    current=task.get('delivery')
    require(current and any(row.get('original')==current and row['sha256']==current['sha256'] for row in inventory['files']),
            'current task delivery not externally preserved')
    for t in state['tasks'].values():
        if t['owner_agent_id']==task['owner_agent_id']:
            require(t['phase']=='completed' and t.get('worker_run',{}).get('status')!='running','executor still serves active work')
    executor=state['runtime']['executors'].get(task['owner_agent_id'])
    require(executor and (executor['execution'] in ('idle','completed') or executor['lease']=='closed') and executor.get('active_children')==[] and
            executor.get('pending_work') is False,'active or unverified executor/descendants')
    require(not executor.get('shared') and not executor.get('protected'),'shared/protected executor remains in use')
    require(executor['lease'] in ('open','closed'),'uncertain close/resume must be reconciled before new cleanup')
    require(state['items'].get(state['active_human_item'],{}).get('task_id')!=task_id,'active user item protects task')


def run(runtime,task_id,operation_id,preservation_ref,invoke,actor_id,*,authorized=False,scope='fixture_only'):
    require(authorized is True and operation_id,'separate exact cleanup authorization required')
    with writer_lock(runtime.path):
        state=runtime._load();runtime._actor(state,actor_id);runtime._scope(state,task_id)
        eligibility(runtime,state,task_id,preservation_ref,scope)
        operations=state['runtime']['operations']
        if operation_id in operations:
            old=operations[operation_id]
            require(old['operation']=='cleanup' and old['task_id']==task_id,'cleanup ID belongs elsewhere')
            return deepcopy(old)
        task=state['tasks'][task_id]
        resources=[r for r in state['runtime'].get('resources',{}).values() if r['task_id']==task_id]
        record=dict(operation='cleanup',task_id=task_id,operation_id=operation_id,status='intent',results={},preservation=preservation_ref,
                    cleanup_authorized=authorized,scope=scope)
        operations[operation_id]=record
        task['cleanup']=dict(status='pending_cleanup',operation_id=operation_id)
        runtime._save(state,dict(op='cleanup-intent',task_id=task_id))
        for resource in resources:
            rid=resource['id']
            if resource['cleanup_status']=='cleaned':
                record['results'][rid]=dict(status='cleaned',already_confirmed=True);continue
            if resource['cleanup_status'] in ('intent','unknown'):
                record['results'][rid]=dict(status='unknown',reason='inspect previous uncertain effect; do not retry');continue
            try:
                require(not resource.get('protected'),'protected resource')
                require(all(state['tasks'][k]['phase']=='completed' for k in resource['references']),'other task still references resource')
                if resource.get('shared'):
                    unused=load_bound(resource['unused_proof'])
                    require(unused.get('resource_id')==rid and unused.get('active_references')==[],'shared references not verified unused')
                if resource.get('path'):
                    root=Path(state['runtime']['cleanup_root']).resolve();path=Path(resource['path']).resolve()
                    require(path!=root and path.is_relative_to(root),'resolved cleanup path escaped managed root')
                if resource['kind']=='agent':
                    executor=state['runtime']['executors'][resource['executor_id']]
                    confirmed=[o for o in operations.values() if o.get('operation')=='close' and
                        o.get('executor_id')==resource['executor_id'] and o.get('generation')==resource['generation'] and
                        o.get('status')=='closed' and o.get('release_receipt')]
                    require(executor['lease']=='closed' and confirmed,'close via canonical lifecycle entry before resource aggregation')
                    result=dict(status='cleaned',already_confirmed=True,evidence=confirmed[-1]['release_receipt'])
                    artifact(result['evidence'])
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
                runtime._save(state,dict(op='resource-cleanup-intent',resource_id=rid))
                # No shell or arbitrary command from JSON; invoke is host-owned.
                result=invoke(deepcopy(resource))
                require(isinstance(result,dict) and result.get('status') in ('cleaned','failed','unknown'),'explicit cleanup result required')
                if result['status']=='cleaned':
                    artifact(result['evidence'])
                    if resource['kind']=='agent':
                        require(result.get('closed') is True,'agent report completion is not confirmed close')
                        executor=state['runtime']['executors'][resource['executor_id']]
                        require(executor['generation']==resource['generation'],'agent generation changed')
                        executor.update(lease='closed',execution='unknown')
            except Exception as error:
                result=dict(status='failed' if resource['cleanup_status']!='intent' else 'unknown',reason=type(error).__name__)
            resource['cleanup_status']=result['status'];resource['cleanup_result']=deepcopy(result);record['results'][rid]=deepcopy(result)
            runtime._save(state,dict(op='resource-cleanup-outcome',resource_id=rid))
        executor=state['runtime']['executors'][task['owner_agent_id']]
        complete=all(r['cleanup_status']=='cleaned' for r in resources) and executor['lease']=='closed'
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
        require(resource['cleanup_status']=='intent' and operation['status']=='intent' and
            operation['cleanup_authorized'] is True and load_bound(resource['cleanup_proof'])==value,'not a current canonical cleanup intent')
        require(context.get('resource_id',context.get('request_id'))==resource['id'] and
            all(context.get(k)==value.get(k) for k in ('project_id','task_id','attempt_id')),'resource context differs')
        if resource.get('path'):require(Path(context['path']).resolve()==Path(resource['path']).resolve(),'resource path differs')
        eligibility(runtime,state,value['task_id'],operation['preservation'],operation['scope'])
        require(not resource.get('protected') and all(state['tasks'][k]['phase']=='completed' for k in resource['references']),
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
        state=runtime._load();runtime._actor(state,actor_id);runtime._scope(state,task_id)
        resource=state['runtime']['resources'][resource_id]
        require(resource['task_id']==task_id and resource['cleanup_status'] in ('unknown','intent'),'not an uncertain owned resource')
        require(facts.get('resource_id')==resource_id and facts.get('status') in ('cleaned','retained') and
                facts.get('evidence'),'actual effect observation required')
        for item in facts['evidence']:artifact(item)
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
