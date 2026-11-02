"""Finite Ram intent / ROOT tool claim / Ram reconciliation; no host calls or daemon."""
from copy import deepcopy

from coordination import writer_lock
from evidence import artifact, fingerprint, io_path, load_bound, ref, require, write_atomic
from resource_lifecycle import preserved_binding

CLOSE = 'multi_agent_v1__close_agent'
WAIT = 'multi_agent_v1__wait_agent'


def registered(state, task_id, *, require_tree=True):
    task = state['tasks'][task_id]; executor = state['runtime']['executors'][task['owner_agent_id']]
    resources = state['runtime'].get('resources', {})
    agent = resources.get(task_id+':agent', {})
    tree = resources.get(task_id+':worktree', {})
    require(agent.get('kind') == 'agent' and agent.get('task_id') == task_id and
        agent.get('executor_id') == executor['id'] and agent.get('generation') == executor['generation'] and
        agent.get('references') == [task_id] and not agent.get('shared') and not agent.get('protected'),
        'current exclusive registered native author required; no historical adoption')
    require(not require_tree or (tree.get('kind') == 'worktree' and tree.get('task_id') == task_id and
        tree.get('path') == task['worktree'] and tree.get('path_identity')), 'current registered task worktree required')
    artifact(agent['authority'])
    require(task['phase'] in ('completed','cancelled') and executor['routes'].get('native_agent'), 'ended native task required')
    return executor


def prepare(runtime, task_id, operation_id, preservation, actor_id, *, authorized=False, scope='host_verified'):
    require(authorized is True and operation_id and scope in ('host_verified','fixture_only'),
            'current bounded native close authorization required')
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._resource_actor(state,actor_id,task_id); runtime._scope(state,task_id)
        old = state['runtime']['operations'].get(operation_id)
        if old:
            require(old.get('native_request') and old.get('task_id') == task_id and old.get('preservation') == preservation and
                old.get('proof_scope') == scope, 'native operation belongs to another intent')
            return deepcopy(old)
        executor = registered(state,task_id,require_tree=preservation is not None)
        from cleanup import eligibility
        if preservation is None:
            from resource_lifecycle import agent_eligibility
            agent_eligibility(runtime,state,task_id,scope)
        else: eligibility(runtime,state,task_id,preservation,scope,resource_paths=False)
        if scope=='host_verified' and preservation is not None:
            saved=load_bound(preservation)
            require(saved.get('remote_verification') and state['tasks'][task_id].get('preservation')==preservation,
                    'canonical cloud-bound handoff must precede native close')
        require(executor['lease'] == 'open', 'prior native close must be reconciled, never reissued')
        route = executor['routes']['native_agent']
        request = dict(schema='native-close-request/1',project_id=runtime.project_id,task_id=task_id,
            operation_id=operation_id,executor_id=executor['id'],generation=executor['generation'],
            controller_id=state['coordinator_id'],scope=scope,preservation=preservation,
            executor_routes=deepcopy(executor['routes']),executor_authority=executor['authority'],
            task_binding=preserved_binding(state['tasks'][task_id]),
            close=dict(tool=CLOSE,arguments=dict(target=route['target_id'])),
            wait=dict(tool=WAIT,arguments=dict(targets=[route['target_id']],timeout_ms=10000)))
        path = io_path(runtime.directory/'native-close-requests'/(fingerprint(request)+'.json'))
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():require(load_bound(ref(path))==request,'immutable native request changed')
        else:write_atomic(path,request)
        launch = state['runtime']['operations'].get(state['tasks'][task_id].get('worker_run',{}).get('attempt_id'),{})
        record = dict(operation='close',operation_id=operation_id,task_id=task_id,executor_id=executor['id'],
            generation=executor['generation'],status='intent',project_id=runtime.project_id,preservation=preservation,
            proof_scope=scope,cleanup_authorized=True,executor_routes=deepcopy(executor['routes']),
            executor_authority=executor['authority'],actor_id=actor_id,native_request=ref(path),
            attempt_id=launch.get('attempt_id',operation_id))
        record.update({key:launch[key] for key in ('request_id','permit_id','launch_identity') if key in launch})
        authority = runtime._resource_actor(state,actor_id,task_id)
        if authority:record['resource_authority']=authority
        state['runtime']['operations'][operation_id]=record
        executor['lease']='closing'
        runtime._save(state,dict(op='prepare-native-close',task_id=task_id,operation_id=operation_id))
        return deepcopy(record)


def current(runtime, state, record):
    key = record['task_id']; executor = registered(state,key,require_tree=record['preservation'] is not None)
    runtime._resource_actor(state,record['actor_id'],key); runtime._scope(state,key)
    request = load_bound(record['native_request'])
    require(executor['lease']==('closed' if record['status']=='closed' else 'closing'),'native close lease changed')
    require(record['operation']=='close' and request.get('schema')=='native-close-request/1' and
        request['project_id']==runtime.project_id and request['task_id']==key and
        request['operation_id']==record['operation_id'] and request['executor_id']==executor['id'] and
        request['generation']==record['generation']==executor['generation'] and
        request['executor_routes']==record['executor_routes']==executor['routes'] and
        request['executor_authority']==record['executor_authority']==executor['authority'] and
        request['task_binding']==preserved_binding(state['tasks'][key]) and
        request['controller_id']==state['coordinator_id'] and request['preservation']==record['preservation'] and
        request['scope']==record['proof_scope'], 'native close intent identity or authority changed')
    target = executor['routes']['native_agent']['target_id']
    require(request['close']==dict(tool=CLOSE,arguments=dict(target=target)) and
        request['wait']==dict(tool=WAIT,arguments=dict(targets=[target],timeout_ms=10000)), 'native contract changed')
    from cleanup import eligibility
    if record['preservation'] is None:
        from resource_lifecycle import agent_eligibility
        agent_eligibility(runtime,state,key,record['proof_scope'],closing_operation=record['operation_id'])
    else: eligibility(runtime,state,key,record['preservation'],record['proof_scope'],closing_operation=record['operation_id'],resource_paths=False)
    return request


def claim(runtime, operation_id, actor_id, *, authorized=False):
    require(authorized is True,'actual ROOT bounded tool invocation authority required')
    with writer_lock(runtime.path):
        state=runtime._load();runtime._actor(state,actor_id,control=True)
        record=state['runtime']['operations'][operation_id]
        request=current(runtime,state,record)
        require(request['controller_id']==actor_id,'actual ROOT controller required')
        if record.get('native_dispatch'):
            return dict(dispatch_allowed=False,status=record['status'],request=record['native_request'],
                        next_action='reconcile_original_host_result_without_reissuing')
        require(record['status']=='intent','current native intent required')
        record['native_dispatch']=dict(controller_id=actor_id,request=record['native_request'],status='intent')
        runtime._save(state,dict(op='claim-native-close',operation_id=operation_id,controller_id=actor_id))
        return dict(dispatch_allowed=True,request=record['native_request'],plan=deepcopy(request))


def confirm(runtime, state, record, facts, observation_ref):
    from host_adapter import unwrap
    request=current(runtime,state,record)
    require(record.get('native_dispatch')==dict(controller_id=request['controller_id'],request=record['native_request'],status='intent'),
            'ROOT must claim this intent before the actual call')
    require(facts.get('operation_id')==record['operation_id'] and facts.get('executor_id')==record['executor_id'] and
        facts.get('generation')==record['generation'] and facts.get('request')==record['native_request'] and
        facts.get('controller_id')==request['controller_id'], 'host result belongs to another close intent/controller')
    from cleanup import canonical_path
    roots=[runtime.project_root,state['runtime']['cleanup_root'],state['tasks'][record['task_id']]['worktree']]
    roots += [r['path'] for r in state['runtime'].get('resources',{}).values() if r.get('path')]
    for reference in [observation_ref]+[facts[key]['result'] for key in ('close','wait') if key in facts]:
        path=canonical_path(artifact(reference))
        require(all(not path.is_relative_to(canonical_path(root)) for root in roots),'native results must outlive cleanup')
    for key in ('close','wait'):
        if key not in facts and key=='wait':
            return dict(confirmed=False,reason='post_close_observation_missing')
        step=facts[key]
        require(step.get('tool')==request[key]['tool'] and step.get('arguments')==request[key]['arguments'],
                'host tool or target differs from claimed native request')
        artifact(step['result'])
    raw_close=load_bound(facts['close']['result']);raw_wait=load_bound(facts['wait']['result'])
    try:
        closed=unwrap(raw_close);waited=unwrap(raw_wait)
    except (ValueError,TypeError):
        return dict(confirmed=False,reason='formal_host_response_unstructured_or_rejected')
    previous=closed.get('previous_status')
    known_previous=previous in ('pending_init','running','interrupted','shutdown') if isinstance(previous,str) else (
        isinstance(previous,dict) and len(previous)==1 and set(previous)<= {'completed','errored'})
    if closed.get('error') or closed.get('isError') or not known_previous:
        return dict(confirmed=False,reason='formal_close_result_unconfirmed')
    target=request['close']['arguments']['target']
    if waited.get('error') or waited.get('isError') or waited.get('timed_out') is not False or (
            waited.get('status') not in ({target:'not_found'},{target:'shutdown'})):
        return dict(confirmed=False,reason='post_close_state_unconfirmed')
    return dict(confirmed=True)


def receipt(runtime, state, record):
    value=dict(project_id=runtime.project_id,task_id=record['task_id'],operation_id=record['operation_id'],
        executor_id=record['executor_id'],generation=record['generation'],native_request=record['native_request'],
        host_confirmation=record['host_confirmation'],outcome='confirmed_closed',
        scope=record['proof_scope'],
        local_permit_recorded=bool(record.get('request_id') and record.get('permit_id')),platform_capacity_count_known=False)
    path=io_path(runtime.directory/'receipts'/(fingerprint(value)+'.json'));path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():require(load_bound(ref(path))==value,'immutable native close confirmation changed')
    else:write_atomic(path,value)
    return ref(path)


def verify(runtime, reference):
    try:
        state=runtime._load();value=load_bound(reference);record=state['runtime']['operations'][value['operation_id']]
        require(record.get('close_confirmation')==reference and record['status']=='closed' and
            state['runtime']['executors'][record['executor_id']]['lease']=='closed','no current canonical native close')
        require(all(value.get(key)==record.get(key) for key in ('project_id','task_id','operation_id','executor_id','generation',
                    'native_request','host_confirmation')) and value.get('outcome')=='confirmed_closed' and
                    value.get('platform_capacity_count_known') is False,'native close confirmation differs')
        require(value.get('scope')==record['proof_scope'] and value.get('local_permit_recorded')==
            bool(record.get('request_id') and record.get('permit_id')),'native receipt scope/permit facts differ')
        require(confirm(runtime,state,record,load_bound(record['host_confirmation']),record['host_confirmation'])['confirmed'],
                'native context close unconfirmed')
        return dict(verified=True,native_context_closed=True,scope=record['proof_scope'],platform_capacity_count_known=False)
    except (ValueError,KeyError,TypeError,OSError):return dict(verified=False)
