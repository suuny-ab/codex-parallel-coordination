"""Validate a trusted terminal host observation at the original CORE boundary."""
from pathlib import Path

import dispatch_lock
from dispatch_lock import dispatch_binding
from evidence import fingerprint, load_bound, ref, require


def check(runtime, state, attempt, observation, actor, evidence_root):
    proof = load_bound(observation)
    root = Path(evidence_root).resolve()
    require(Path(observation['path']).resolve().is_relative_to(root), 'prehost proof outside trusted host root')
    binding = proof['binding']
    request = load_bound(binding['request_ref'])
    if 'spec_check' in request:
        from codex_parallel_coordination.dispatch import approved_request
        request = approved_request(request)
    require(binding == dispatch_binding(runtime, request, actor, binding['request_ref']) and
        attempt == request['request_id']+':launch', 'prehost request/actor binding differs')
    owner_ref = proof['owner']
    require(Path(owner_ref['path']).resolve().is_relative_to(root), 'prehost owner outside trusted host root')
    owner = load_bound(owner_ref)
    process = owner['process']
    require(owner == dict(schema='dispatch-writer-owner/1', binding=binding, process=process) and
        process.get('status') == 'alive' and process.get('creation') and process.get('pid') == proof.get('child_pid'),
        'original host process source required')
    require(proof.get('schema') == 'dispatch-prehost-terminal/1' and proof.get('closed') is True and
        (type(proof.get('exit_code')) is int or isinstance(proof.get('signal'), str) and proof['signal']) and
        type(proof.get('host_calls')) is int and proof['host_calls'] == 0 and
        proof.get('pending', False) is None and proof.get('result_refs') == [] and
        proof.get('scope') in ('fixture_only', 'host_verified'), 'terminal zero-host observation required')
    actual = dispatch_lock.process_identity(process['pid'])
    require(actual['status'] == 'dead' and actual['creation'] in (None, process['creation']),
        'original host active, unknown or PID reused')
    operations = state['runtime']['operations']
    dispatch = operations[request['request_id']]
    record = operations[attempt]
    task = state['tasks'][request['task_id']]
    require(dispatch.get('operation') == 'standard-dispatch' and dispatch.get('task_id') == request['task_id'] and
        dispatch.get('request_digest') == fingerprint(request) and dispatch.get('launch_started') is True and
        dispatch.get('driver_owner_pid') == process['pid'] and not dispatch.get('retained_host_results') and
        not dispatch.get('handoff_intent'), 'original prehost dispatch differs')
    require(record.get('operation') == 'spawn' and record.get('status') == 'intent' and
        record.get('task_id') == request['task_id'] and record.get('attempt_id') == attempt and
        record.get('request_id') == request['request_id']+':capacity' and
        record.get('route_id') == request['request_id']+':route' and record.get('executor_id') is None and
        record.get('generation') is None and not record.get('callback_captures') and not record.get('host_results') and
        not record.get('prehost_recoveries') and
        record.get('admission') == dispatch['admission']['admission'] and
        task['owner_agent_id'] is None and task['worker_run']['status'] == 'queued' and
        task['worker_run'].get('attempt_id') in (None, attempt), 'callback, identity or unknown effect blocks recovery')
    permit = load_bound(record['admission'])
    require(permit.get('status') == 'granted' and all(record.get(key) == permit.get(key) for key in
        ('request_id', 'permit_id', 'launch_identity', 'task_id', 'attempt_id')) and
        permit.get('project_id') == runtime.project_id and record['launch_identity'] == 'spawn:'+attempt,
        'original permit/launch identity differs')
    route = state['runtime']['intake']['routes'][record['route_id']]
    require(route.get('task_id') == request['task_id'] and route.get('attempt_id') == attempt and
        not any(item.get('task_id') == request['task_id'] and item.get('kind') == 'agent'
                for item in state['runtime'].get('resources', {}).values()), 'route/actual author blocks recovery')
    require(not any(key not in (request['request_id'], attempt) and op.get('task_id') == request['task_id'] and
        op.get('operation') in ('spawn', 'send') for key, op in operations.items()), 'another host attempt blocks recovery')
    for tool in ('multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent', 'send_input',
                 'mcp__codex_app__send_message_to_thread'):
        for kind in ('intent', 'result'):
            require(not (root/('host-'+fingerprint([request['request_id'], tool, kind])+'.json')).exists(),
                    'host intent/result blocks prehost recovery')
    # An unrecorded late return is still a host effect. Never infer zero from
    # missing CORE callbacks when the durable transport already knows a result.
    for path in root.glob('host-result-*.json'):
        journal = load_bound(ref(path))
        require(journal.get('request_id') != request['request_id'], 'retained host result blocks recovery')
    return request
