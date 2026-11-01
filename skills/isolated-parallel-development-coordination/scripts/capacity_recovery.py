"""A known native capacity rejection is distinct from an unknown host effect."""
from evidence import callback_result, load_bound, require
from host_adapter import capacity_error


def check(runtime, state, attempt, actor):
    runtime._actor(state, actor, control=True)
    record = state['runtime']['operations'][attempt]
    task = state['tasks'][record['task_id']]
    require(record.get('operation')=='spawn' and record.get('status')=='capacity' and
        record.get('code')=='agent_capacity_exceeded' and not record.get('executor_id') and
        not record.get('generation') and task['owner_agent_id'] is None and
        task['worker_run']['status']=='queued', 'only an explicit unowned capacity rejection can continue')
    returns = record.get('host_results', [])
    binding = record.get('callback_captures', {}).get('spawn')
    require(len(returns)==1 and returns[0].get('operation')=='spawn' and binding and
        set(record.get('callback_captures', {}))=={'spawn'}, 'one original spawn rejection required')
    reference = callback_result(runtime.directory, binding)
    require(reference and reference==returns[0].get('capture'), 'original callback return required')
    captured = load_bound(reference)
    result = returns[0]['result']
    require(captured['result']==result and result.get('status')=='capacity' and
        result.get('code')=='agent_capacity_exceeded' and capacity_error(result.get('error', {})) and
        not result.get('target_id') and binding.get('actor_id')==actor and
        binding.get('attempt_id')==attempt and binding.get('task_id')==record['task_id'] and
        binding.get('project_id')==runtime.project_id and binding.get('operation')=='spawn',
        'verified original host capacity failure required')
    require(all(record.get(k)==result.get(k) for k in ('code','error','response_digest','tool')),
            'canonical capacity outcome differs from original callback')
    require(all(binding['launch'].get(k)==record.get(k) for k in
        ('operation','request_id','permit_id','admission','executor_id','generation','launch_identity','route_id')),
        'original capacity launch identity changed')
    require(not any(r.get('task_id')==record['task_id'] and r.get('kind')=='agent'
        for r in state['runtime'].get('resources', {}).values()), 'existing author resource blocks retry')
    return record
