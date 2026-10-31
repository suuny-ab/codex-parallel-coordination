"""Development bindings and explicit cancellation in the existing task ledger."""
from copy import deepcopy

from coordination import writer_lock
from evidence import artifact, load_bound, require
from task_completion import definition

ENDED = ('completed', 'cancelled')


def binding_fields(task):
    return {k: deepcopy(task[k]) for k in ('owner_agent_id', 'worktree', 'branch')}


def validate_bindings(state):
    """No I/O: called by canonical validation, including legacy transition paths."""
    bindings = []
    for key, task in state['tasks'].items():
        bound = task.get('development_binding')
        if not bound:
            continue
        require(definition(task)[0] == 'code' and all(bound[k] == v for k, v in binding_fields(task).items()),
                'development identity/worktree/branch is immutable')
        require(task['owner_agent_id'] not in (state['coordinator_id'], state.get('delivery', {}).get('agent_id')),
                'main/delivery role is not a development executor')
        for other_key, other in state['tasks'].items():
            if other_key != key:
                require(other['owner_agent_id'] != task['owner_agent_id'], 'development executor belongs to one requirement')
                if task.get('mr') and other.get('mr'):
                    require((task['mr']['provider'], task['mr']['id']) != (other['mr']['provider'], other['mr']['id']),
                            'PR already belongs to another requirement')
        for old in bindings:
            require(old['spec']['sha256'] != bound['spec']['sha256'], 'spec already belongs to another requirement')
        bindings.append(bound)


def bind(runtime, task_id, spec_ref, actor_id, *, predecessor_id=None):
    artifact(spec_ref)
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._actor(state, actor_id, control=True); runtime._scope(state, task_id)
        task = state['tasks'][task_id]
        require(definition(task)[0] == 'code' and task['phase'] not in ENDED and
                task['requirements'] and task['start_reference'], 'authorized unfinished development task required')
        executor = state['runtime']['executors'].get(task['owner_agent_id'])
        require(executor and executor['lease'] == 'open' and executor['routes'], 'actual registered executor required')
        artifact(executor['authority'])
        if predecessor_id is not None:
            require(predecessor_id != task_id and state['tasks'][predecessor_id]['phase'] in ENDED,
                    'predecessor must be a different ended requirement')
        value = dict(binding_fields(task), spec=spec_ref, predecessor_id=predecessor_id)
        old = task.get('development_binding')
        require(not old or old == value, 'development binding cannot be replaced')
        if not old:
            task['development_binding'] = value
            validate_bindings(state)
            runtime._save(state, dict(op='bind-development', task_id=task_id))
        return deepcopy(value)


def cancellation_fields(task):
    return dict(binding_fields(task), delivery_revision=task['delivery_revision'], delivery=task.get('delivery'),
                mr=deepcopy(task.get('mr')))


def cancelled(state, task_id, scope=None):
    task = state['tasks'][task_id]; record = task.get('cancellation') or {}
    proof = load_bound(record['proof'])
    require(task['phase'] == 'cancelled' and not task.get('merge') and
            proof.get('project_id') == state['project']['id'] and proof.get('task_id') == task_id and
            proof.get('explicit_cancel') is True and proof.get('user_reference') and proof.get('artifacts') and
            proof.get('scope') in ('fixture_only', 'host_verified') and
            (scope is None or proof['scope'] == scope) and
            all(proof.get(k) == v for k, v in cancellation_fields(task).items()), 'current explicit cancellation required')
    for item in proof['evidence']:
        artifact(item)
    return record


def cancel(runtime, proof_ref, actor_id):
    proof = load_bound(proof_ref)
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._actor(state, actor_id, control=True)
        key = proof['task_id']; runtime._scope(state, key); task = state['tasks'][key]
        if task['phase'] == 'cancelled':
            cancelled(state, key)
            require(task['cancellation']['proof'] == proof_ref, 'different duplicate cancellation')
            return deepcopy(task['cancellation'])
        require(definition(task)[0]=='code' and task['phase'] not in ENDED and not task.get('merge') and
                proof.get('based_on_revision') == state['revision'], 'current unfinished task required')
        require(not state.get('merge_reservation') or state['merge_reservation']['task_id']!=key,
                'reconcile/release outstanding merge reservation before cancellation')
        require(proof.get('evidence') and proof.get('artifacts'), 'cancellation instruction and work preservation sources required')
        for item in proof['artifacts']:
            artifact(item)
        task.update(phase='cancelled', cancellation=dict(proof=proof_ref, binding=cancellation_fields(task),
                    pr_status='pending' if task.get('mr') else 'not_required'),
                    cleanup=dict(status='pending_cleanup', owner=actor_id))
        affected=sorted({other for resource in state.get('runtime',{}).get('resources',{}).values()
            if key in resource.get('references',[]) for other in resource['references']
            if other!=key and state['tasks'][other]['phase'] not in ENDED})
        task['cancellation']['dependency_review']=dict(task_ids=affected,
            owner=state.get('delivery',{}).get('agent_id',state['coordinator_id']),
            status='pending' if affected else 'no_registered_dependencies')
        cancelled(state, key)
        for item in state['items'].values():
            if item.get('task_id') == key and item['status'] in ('queued', 'active', 'waiting_user'):
                item['status'] = 'superseded'
                if state['active_human_item'] == item['id']: state['active_human_item'] = None
                if state.get('priority_item') == item['id']: state['priority_item'] = None
        task['blocker'] = None
        runtime._save(state, dict(op='cancel-task', task_id=key, proof=proof_ref))
        return deepcopy(task['cancellation'])


def close_pr(runtime, task_id, operation_id, preservation_ref, invoke, actor_id, *, authorized=False, scope='fixture_only'):
    from cleanup import eligibility
    require(authorized is True and operation_id, 'current exact PR close authorization required')
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._actor(state, actor_id); runtime._scope(state, task_id)
        record = cancelled(state, task_id, scope)
        eligibility(runtime, state, task_id, preservation_ref, scope, require_pr_closed=False)
        if record['pr_status'] in ('closed', 'not_required'):
            if record['pr_status'] == 'closed': artifact(record['pr_result']['evidence'])
            return deepcopy(record)
        operations = state['runtime']['operations']; previous = operations.get(operation_id)
        if previous:
            require(previous.get('operation') == 'cancel-pr' and previous.get('task_id') == task_id, 'operation ID reused')
            return deepcopy(previous)
        require(record['pr_status'] not in ('intent', 'unknown'), 'observe uncertain PR close before retry')
        task = state['tasks'][task_id]
        operation = dict(operation='cancel-pr', operation_id=operation_id, task_id=task_id, mr=deepcopy(task['mr']),
                         status='intent', preservation=preservation_ref, scope=scope)
        operations[operation_id] = operation; record.update(pr_status='intent', pr_operation_id=operation_id)
        runtime._save(state, dict(op='cancel-pr-intent', task_id=task_id))
        try:
            result = invoke(deepcopy(operation))
            require(result.get('status') in ('closed', 'failed', 'unknown'), 'explicit PR close result required')
            if result['status'] == 'closed': artifact(result['evidence'])
        except Exception as error:
            result = dict(status='unknown', reason=type(error).__name__)
        operation.update(result); record.update(pr_status=result['status'], pr_result=deepcopy(result))
        runtime._save(state, dict(op='cancel-pr-outcome', task_id=task_id))
        return deepcopy(operation)


def reconcile_pr(runtime, task_id, observation_ref, actor_id):
    facts = load_bound(observation_ref)
    with writer_lock(runtime.path):
        state = runtime._load(); runtime._actor(state, actor_id); runtime._scope(state, task_id)
        record = cancelled(state, task_id); task = state['tasks'][task_id]
        require(record['pr_status'] in ('intent', 'unknown') and facts.get('task_id') == task_id and
                facts.get('operation_id') == record['pr_operation_id'] and facts.get('mr') == task['mr'] and
                facts.get('scope') == state['runtime']['operations'][record['pr_operation_id']]['scope'] and
                facts.get('status') in ('closed', 'retained') and facts.get('evidence'), 'actual PR identity/outcome observation required')
        for item in facts['evidence']: artifact(item)
        status = 'closed' if facts['status'] == 'closed' else 'failed'
        record.update(pr_status=status, pr_result=dict(status=status, evidence=observation_ref))
        state['runtime']['operations'][record['pr_operation_id']].update(status=status, observation=observation_ref)
        runtime._save(state, dict(op='reconcile-cancel-pr', task_id=task_id))
        return deepcopy(record)
