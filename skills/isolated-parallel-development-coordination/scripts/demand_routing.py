"""Demand intake and bounded dispatch in the canonical ledger; no host tools.

Actors and user_origin are trusted host sideband, never worker payload claims.
Semantic scope judgments are supplied by main, dependency judgments by delivery.
"""
from copy import deepcopy
from pathlib import Path
from coordination import RIGHTS, transition
from evidence import artifact, fingerprint, load_bound, require


def intake(state):
    return state['runtime'].setdefault('intake', dict(requirements={}, plans={}, approvals={},
        instructions={}, routes={}, dependencies={}, events={}))


def main(state, actor):
    require(actor == state['coordinator_id'], 'original main role required')


def delivery(state, actor):
    require(state.get('delivery') and actor == state['delivery']['agent_id'], 'integration role required')


def text(value):
    return isinstance(value, str) and bool(value.strip())


def origin(state, reference, user_origin):
    """Check authenticated sideband and bound content, not JSON self-authorization."""
    require(user_origin and user_origin.get('role') == 'user' and
        user_origin.get('conversation_id') == state['coordinator_id'] and
        user_origin.get('reference') == reference, 'actual user origin in original main required')
    return load_bound(reference)


def current_requirement(state, identity):
    record = intake(state)['requirements'][identity]
    discussion = load_bound(record['discussion'])
    current = record.get('current_spec', record['spec'])
    require(current['sha256']==record['spec']['sha256'], 'current spec content identity differs')
    artifact(current)
    check = load_bound(record['check'])
    require(discussion.get('confirmed') is True and
        check.get('coordinator_id') == state['coordinator_id'] and
        check.get('requirement_id') == identity and check.get('spec') == record['spec'] and
        check.get('discussion') == record['discussion'] and check.get('result') == 'consistent',
        'current spec must be self-checked against confirmed discussion by main')
    return record


def scope_digest(record):
    return fingerprint({k: record[k] for k in ('name', 'scope', 'acceptance')})


def frozen(task):
    return task['user_acceptance'] == 'accepted' or bool(task.get('acceptance_record')) or any(
        h.get('user_acceptance') == 'accepted' for h in task['history'])


def pr_guard(state, task_id, mr):
    """Also called by the existing metadata entrypoint, so it cannot fork a PR."""
    task = state['tasks'][task_id]
    if not task.get('routing'):
        return
    require(text(mr.get('url')) and mr['url'].startswith('https://'), 'actual PR URL required')
    require(not task['mr'] or (task['mr'].get('url'), task['mr']['id']) == (mr['url'], mr['id']), 'maintain original PR')
    require(not any(key != task_id and t.get('mr') and
        (t['mr'].get('url') == mr['url'] or (t['mr'].get('provider'), t['mr'].get('id')) == (mr['provider'], mr['id']))
        for key, t in state['tasks'].items()), 'PR already bound to another requirement')


def plan_item(state, task_id):
    # Latest item for this task only: changing B never invalidates A's approval.
    matches = [(p, item) for p in intake(state)['plans'].values() for item in p['items']
               if item['task_id'] == task_id]
    require(matches, 'task has no presented dispatch summary')
    return matches[-1]


def approved_item(state, task_id):
    plan, item = plan_item(state, task_id)
    record = current_requirement(state, item['requirement_id'])
    require(item['scope_digest'] == scope_digest(record), 'summary scope changed; present and approve affected item again')
    presentation = load_bound(plan['presentation'])
    require(presentation == dict(conversation_id=state['coordinator_id'], plan_id=plan['id'],
        version=plan['version'], items=plan['items']), 'presented summary differs from current plan')
    approval = intake(state)['approvals'].get(fingerprint(item))
    require(approval and approval['decision'] == 'approved', 'dispatch summary pending user approval or rejected')
    raw = load_bound(approval['reference'])
    require(raw.get('plan_id') == approval['plan_id'] and raw.get('presentation') == approval['presentation'] and
        task_id in raw.get('task_ids', []) and raw.get('decision') == 'approved', 'approval binding changed')
    artifact(approval['presentation'])
    return item, record


def task_version(state, task_id):
    task = state['tasks'][task_id]
    owner = state['runtime']['executors'].get(task['owner_agent_id'])
    return fingerprint(dict(task={k: task.get(k) for k in
        ('requirements', 'authorization', 'owner_agent_id', 'worktree', 'branch', 'phase',
         'user_acceptance', 'mr', 'routing', 'delivery_revision')},
        executor={k: owner.get(k) for k in ('id', 'generation', 'routes', 'authority')} if owner else None))


def observed_execution(reference, identity):
    from host_adapter import unwrap
    raw = unwrap(load_bound(reference))
    require(raw.get('thread', {}).get('id') == identity, 'observed native thread differs from actual dispatch')
    turn = raw.get('latestTurn') or raw.get('latest_turn') or next(iter(raw.get('turns', [])), {})
    status = turn.get('status')
    if status in ('completed', 'failed', 'interrupted', 'cancelled', 'canceled'):
        return status, turn  # A terminal latest turn wins over active thread metadata.
    thread_status = raw['thread'].get('status', {})
    thread_status = thread_status.get('type') if isinstance(thread_status, dict) else thread_status
    require(status in (None, 'inProgress', 'in_progress') and thread_status in
        ('active', 'running', 'inProgress', 'in_progress'), 'explicit current execution or terminal turn required')
    return 'running', turn


def observed_receipt_origin(state, payload):
    """Recheck the actual owner, approval and terminal source on every use."""
    task_id = payload['task_id']
    binding = state['runtime']['observed_dispatches'][task_id]
    proof = load_bound(binding['proof'])
    approval = load_bound(proof['approval'])
    require(payload.get('origin_dispatch') == binding['proof'] and approval.get('approval') == 'approved' and
        approval.get('schema') == 'approved-dispatch-plan/1' and approval.get('main_conversation') == state['coordinator_id'] and
        any(t.get('id') == task_id for t in approval.get('tasks', [])), 'observed receipt approval or dispatch changed')
    require(payload['owner_agent_id'] == binding['executor_id'] == state['tasks'][task_id]['owner_agent_id'] and
        payload['recipient_agent_id'] == binding['reviewer_id'] == approval['integration_agent'] and
        payload['channel'] == 'native_agent', 'observed receipt owner or task-scoped receiver differs')
    require(state['runtime']['executors'][binding['executor_id']]['generation'] == binding.get('generation', 1),
        'observed receipt executor generation changed')
    for field in ('dispatch_result', 'spec_at_dispatch', 'checklist_at_dispatch'):
        artifact(proof[field])
    outcome, turn = observed_execution(payload['origin_observation'], binding['executor_id'])
    require(outcome != 'running' and payload['kind'] in ('completed', 'blocked') and
        (outcome == 'completed' or payload['kind'] == 'blocked'), 'terminal report kind differs from actual execution outcome')
    finals = [item.get('text', '') for item in turn.get('items', []) if item.get('type') == 'agentMessage' and
        item.get('phase') in ('final', 'final_answer')]
    target = payload['report']['path'].replace('\\', '/').casefold()
    require(any(target in message.replace('\\', '/').casefold() for message in finals),
        'latest actual final does not reference this author report')


def receive_observed(state, payload, observation_ref, actor):
    """Receipt-only task authority; reuse canonical admission/consume, never global drain."""
    from coordination import admit_receipt, consume, receipt_context, receipt_report
    from evidence import ERRORS
    packet = deepcopy(payload)
    binding = state['runtime']['observed_dispatches'][packet['task_id']]
    require(actor == binding['reviewer_id'] == packet.get('recipient_agent_id'), 'actual task-scoped integration receiver required')
    packet.update(origin_mode='observed_terminal', origin_dispatch=binding['proof'], origin_observation=observation_ref)
    receipt_context(state, packet)  # Includes current global scope if one exists, plus bound source checks.
    task = state['tasks'][packet['task_id']]
    require(packet['delivery_revision'] >= task['worker_run']['expected_delivery_revision'], 'receipt predates current task execution')
    previous = state['receipts'].get(packet['event_id'])
    if previous:
        require({k: v for k, v in previous['payload'].items() if k != 'origin_observation'} ==
            {k: v for k, v in packet.items() if k != 'origin_observation'}, 'conflicting duplicate observed receipt')
        receipt_context(state, previous['payload']); receipt_report(state, previous['payload'])
        if previous['status'] in ('processed', 'stale'):
            return dict(receipt_status=previous['status'], duplicate=True, origin_mode='observed_terminal')
        packet = previous['payload']  # Retain the original canonical bytes and provenance.
    outcome, turn = observed_execution(observation_ref, binding['executor_id'])
    binding.update(latest_observation=observation_ref, execution_outcome=outcome, terminal_turn_id=turn.get('id'))
    state['runtime']['executors'][binding['executor_id']]['execution'] = 'completed'
    admitted = admit_receipt(state, packet)
    if admitted == 'quarantined':
        return dict(receipt_status='unverified', duplicate=False, origin_mode='observed_terminal')
    candidate = deepcopy(state)
    try:
        status, reason = consume(candidate, packet), None
        state.clear(); state.update(candidate)
    except ERRORS as error:
        status, reason = 'held', str(error)
    receipt = state['receipts'][packet['event_id']]
    if (receipt['status'], receipt.get('reason')) != (status, reason):
        receipt['history'].append(dict(status=status, reason=reason))
    receipt.update(status=status, reason=reason)
    return dict(receipt_status=status, reason=reason, duplicate=False, origin_mode='observed_terminal')


def bind_observed(state, task_id, proof_ref, actor):
    """Bind an already-spawned native developer, never simulate an admitted launch.

The authenticated caller supplies original host artifacts. This deliberately
limited bootstrap adapter accepts approved-dispatch-plan/1, not worker reports.
"""
    proof = load_bound(proof_ref)
    approval = load_bound(proof['approval'])
    require(approval.get('schema') == 'approved-dispatch-plan/1' and approval.get('approval') == 'approved' and
        approval.get('main_conversation') == state['coordinator_id'] and
        any(t.get('id') == task_id for t in approval.get('tasks', [])) and
        text(approval.get('user_quote')) and approval.get('source') == 'direct user message in original main conversation',
        'original-main approved dispatch plan required')
    reviewer = approval.get('integration_agent')
    require(actor in (state['coordinator_id'], reviewer) and text(reviewer), 'actual approved main or integration caller required')
    require(proof.get('observer_id') == actor and proof.get('project_id') == state['project']['id'] and
        proof.get('task_id') == task_id, 'dispatch proof project, observer or task mismatch')
    previous = state['runtime'].get('observed_dispatches', {}).get(task_id)
    if previous:
        require(previous['proof'] == proof_ref, 'existing dispatch binding cannot be replaced')
        return dict(previous, duplicate=True)
    task = state['tasks'][task_id]
    require(proof.get('based_on_revision') == state['revision'] and proof.get('target_version') == task_version(state, task_id),
        'dispatch binding revision or target version stale')
    require(task['owner_agent_id'] is None and task['worker_run']['status'] == 'queued' and task['phase'] == 'backlog' and
        task.get('task_kind', 'code') == 'code', 'only a pending unowned code task may bind an observed spawn')
    require(str(Path(task['worktree']).resolve()) == str(Path(proof['worktree']).resolve()) and task['branch'] == proof['branch'],
        'dispatch worktree or branch differs')
    require(task['requirements'] and str(Path(task['requirements']['user_reference']).resolve()) ==
        str(Path(proof['approval']['path']).resolve()), 'registered task must reference this original approval')
    dispatch = load_bound(proof['dispatch_result'])
    require(Path(dispatch['authority_file']).resolve() == Path(proof['approval']['path']).resolve(), 'dispatch used another approval')
    matches = [x['value'] for x in dispatch.get('dispatch_attempts', []) if x.get('status') == 'fulfilled' and
        x.get('value', {}).get('task_id') == task_id]
    require(len(matches) == 1, 'one actual successful dispatch result required')
    result = matches[0]
    identity = result.get('agent_id')
    require(text(identity) and identity == proof.get('executor_id') and result.get('branch') == task['branch'] and
        Path(result['worktree']).resolve() == Path(task['worktree']).resolve(), 'actual native dispatch identity or task differs')
    require(identity not in (state['coordinator_id'], reviewer) and
        not any(t['owner_agent_id'] == identity for t in state['tasks'].values()) and
        identity not in state['runtime']['executors'], 'developer identity already owned or is a control role')
    require(not any(o.get('task_id') == task_id and o.get('operation') in ('send', 'spawn')
        for o in state['runtime']['operations'].values()), 'managed launch already exists; reconcile its original intent')
    for field in ('spec_at_dispatch', 'checklist_at_dispatch'):
        artifact(proof[field])
        require(Path(proof[field]['path']).name == Path(result['spec' if field == 'spec_at_dispatch' else 'checklist']).name,
            'dispatch document name differs')
        require(any(x.get('sha256') == proof[field]['sha256'] and
            Path(x['path']).name == Path(proof[field]['path']).name for x in approval.get('specs', [])),
            'dispatch spec/checklist not bound to approved document')
    outcome, turn = observed_execution(proof['observation'], identity)
    running = outcome == 'running'
    route = dict(host_id=proof['host_id'], target_id=identity)
    require(text(route['host_id']) and not any(e.get('routes', {}).get('native_agent') == route
        for e in state['runtime']['executors'].values()), 'native route missing or already owned')
    state['runtime']['executors'][identity] = dict(id=identity, routes={'native_agent': route}, generation=1,
        lease='open', execution='running' if running else 'completed', active_children=None, pending_work=None,
        authority=proof['approval'], observation=None)
    task.update(owner_agent_id=identity, phase='developing', implementation='running' if running else 'pending')
    task['worker_run'].update(owner_agent_id=identity, status='running' if running else 'awaiting_receipt',
        reason='Verified externally observed execution; terminal attribution awaits canonical author receipt')
    record = dict(executor_id=identity, generation=1, proof=proof_ref, observer_id=actor, reviewer_id=reviewer,
        receiver_id=reviewer, receipt_scope=[task_id], execution_outcome=outcome, terminal_turn_id=turn.get('id'),
        admission_reconstructed=False, host_actions=[], observation=proof['observation'])
    state['runtime'].setdefault('observed_dispatches', {})[task_id] = record
    return deepcopy(record)


def prepare_binding(state, spec):
    """Queue is harmless; when a plan exists its identity/scope must match exactly."""
    if not any(i['task_id'] == spec['task_id'] for p in intake(state)['plans'].values() for i in p['items']):
        return None  # Historical prepare remains recoverable, but code launch has no bypass.
    _, item = plan_item(state, spec['task_id'])
    record = current_requirement(state, item['requirement_id'])
    require(spec.get('task_kind', 'code') == 'code', 'queries and analysis stay with main')
    require(spec['scope'] == record['scope'] and spec['acceptance'] == record['acceptance'], 'prepared scope differs from spec')
    require(not any(t.get('routing', {}).get('requirement_id') == item['requirement_id']
        for t in state['tasks'].values()), 'one requirement has one task')
    return dict(requirement_id=item['requirement_id'])


def bind_guard(state, task_id, executor_id):
    item, _ = approved_item(state, task_id)
    task = state['tasks'][task_id]
    require(task['owner_agent_id'] in (None, executor_id), 'preserve original task owner')
    require(not any(key != task_id and t['owner_agent_id'] == executor_id for key, t in state['tasks'].items()),
        'task developer cannot be reassigned to another requirement')
    require(item['assignment'] == 'new' or item['executor_id'] == executor_id, 'approved recipient differs')


def launch_guard(state, task_id, route_id, *, spawn, executor_id, message):
    task = state['tasks'][task_id]
    # Existing historical read-only workers may finish through their old protocol;
    # intake cannot create them and never permits a new spawn for analysis.
    if task.get('task_kind') == 'read_only' and not task.get('routing'):
        require(not spawn, 'queries and analysis do not spawn developers')
        return None
    data = intake(state)
    require(route_id in data['routes'], 'bounded routing decision required before host dispatch')
    route = data['routes'][route_id]
    require(route['task_id'] == task_id and route['message'] == message and route['spawn'] == spawn and
        route['executor_id'] == executor_id, 'dispatch differs from bounded routing decision')
    require(route.get('attempt_id') is None, 'instruction already dispatched; reconcile its original attempt')
    require(route['version'] == task_version(state, task_id), 'routing decision stale; reassess current target, rights and owner')
    item, record = approved_item(state, task_id)
    require(task.get('routing', {}).get('requirement_id') == item['requirement_id'] and
        task['requirements']['scope'] == record['scope'] and task['requirements']['acceptance'] == record['acceptance'],
        'task and approved requirement differ')
    require(route['spec'] == record['spec'], 'spec changed after routing; reassess')
    instruction = data['instructions'][route['instruction_id']]
    require(instruction['task_id'] == task_id and instruction['status'] == 'assigned', 'instruction is not uniquely assigned')
    artifact(instruction['source'])
    require(instruction['action'] in ('implement', 'adjust', 'defect'), 'instruction cannot dispatch a developer')
    require(task['phase'] != 'completed', 'ended requirement needs a new task linked to its predecessor')
    require(not frozen(task) or instruction['action'] == 'defect',
        'accepted scope frozen; new capability needs a separate requirement')
    require(record['start_reference'], 'task start authorization required')
    artifact(record['start_reference'])
    if spawn:
        require(item['assignment'] == 'new' and task['owner_agent_id'] is None, 'same requirement reuses original developer')
    else:
        bind_guard(state, task_id, executor_id)
        require(task['owner_agent_id'] == executor_id, 'bind actual task developer before sending')
    return route


def feedback_tasks(state, command):
    """Derive affected tasks from the validated routing decision, never caller scope."""
    data, op = intake(state), command['op']
    if op == 'spec':
        return [key for key, task in state['tasks'].items()
                if task.get('routing', {}).get('requirement_id') == command['requirement_id']]
    if op == 'present':
        return [item['task_id'] for item in data['plans'][command['plan_id']]['items']]
    if op == 'approve':
        return load_bound(command['reference'])['task_ids']
    if op == 'instruction':
        return data['instructions'][command['instruction_id']]['candidates']
    if op == 'apply-instruction':
        return [data['instructions'][command['instruction_id']]['task_id']]
    if op in ('dependency', 'verify-dependency'):
        item = data['dependencies'][command['dependency_id']]
        return [item['source_task'], item['target_task']]
    return [command['task_id']]


def apply(state, command, actor, user_origin=None):
    """A finite ledger mutation; Runtime persists successes and rejected instructions."""
    data = intake(state)
    op = command['op']
    if op == 'spec':
        main(state, actor)
        identity = command['requirement_id']
        require(all(text(command.get(k)) for k in ('name', 'scope')) and
            isinstance(command.get('acceptance'), list) and command['acceptance'] and
            all(text(x) for x in command['acceptance']), 'confirmed name, scope and acceptance required')
        record = {k: deepcopy(command[k]) for k in ('name', 'scope', 'acceptance', 'discussion', 'spec', 'check')}
        record['start_reference'] = command.get('start_reference')
        require(isinstance(command.get('deliverables', []), list), 'explicit deliverables list required')
        record['deliverables'] = deepcopy(command.get('deliverables', []))
        for item in record['deliverables']:
            require(set(item)=={'path','sha256'}, 'deliverables are exact file references')
            artifact(item)
        previous = data['requirements'].get(identity)
        bound = [t for t in state['tasks'].values() if t.get('routing', {}).get('requirement_id') == identity]
        require(not previous or scope_digest(previous) == scope_digest(record) or
            all(not frozen(t) and t['phase'] != 'completed' for t in bound),
            'accepted requirement scope frozen; new goal requires a new requirement')
        if previous and any(t['phase'] in ('completed', 'cancelled') or t.get('cancellation_request') for t in bound):
            require(record['spec'] == previous['spec'] and
                record['deliverables'] == previous.get('deliverables', []),
                'ended/cancel-requested requirement archive manifest frozen; spec and deliverables cannot be replaced')
            # STATE archive relocation is not a new declaration or approval.
            for field in ('current_spec', 'location_history'):
                if field in previous: record[field] = deepcopy(previous[field])
        data['requirements'][identity] = record
        current_requirement(state, identity)
        return dict(requirement_id=identity, self_check='consistent', functional_acceptance=False)
    if op == 'present':
        main(state, actor)
        require(command['plan_id'] not in data['plans'], 'plan version already recorded; use new plan id')
        items = deepcopy(command['items'])
        require(items and len({i['task_id'] for i in items}) == len(items) and
            len({i['requirement_id'] for i in items}) == len(items), 'unique spec/task per summary required')
        for item in items:
            record = current_requirement(state, item['requirement_id'])
            require(item['scope_digest'] == scope_digest(record) and item['name'] == record['name'] and
                item['scope'] == record['scope'] and item['acceptance'] == record['acceptance'] and
                all(text(item.get(k)) for k in ('split_reason', 'assignment_reason')) and
                item['assignment'] in ('new', 'reuse'), 'complete split/merge and dispatch summary required')
            task = state['tasks'].get(item['task_id'])
            if item['assignment'] == 'reuse':
                require(task and task['phase'] != 'completed' and task['owner_agent_id'] == item['executor_id'] and
                    task.get('routing', {}).get('requirement_id') == item['requirement_id'],
                    'reuse belongs to the same unfinished requirement, not a module worker')
            else:
                require(item.get('executor_id') is None, 'new developer identity stays unknown until actual binding')
        presentation = load_bound(command['presentation'])
        require(presentation == dict(conversation_id=state['coordinator_id'], plan_id=command['plan_id'],
            version=command['version'], items=items), 'summary must be presented in original main')
        data['plans'][command['plan_id']] = dict(id=command['plan_id'], version=command['version'],
            items=items, presentation=command['presentation'])
        return dict(plan_id=command['plan_id'], spec_count=len(items), approval='pending')
    if op == 'approve':
        main(state, actor)
        raw = origin(state, command['reference'], user_origin)
        plan = data['plans'][command['plan_id']]
        require(raw.get('plan_id') == plan['id'] and raw.get('presentation') == plan['presentation'] and
            raw.get('decision') in ('approved', 'rejected') and text(raw.get('quote')) and
            raw.get('task_ids') and set(raw['task_ids']) <= {i['task_id'] for i in plan['items']},
            'explicit approval of this summary and selected tasks required')
        for item in plan['items']:
            if item['task_id'] in raw['task_ids']:
                data['approvals'][fingerprint(item)] = dict(plan_id=plan['id'], presentation=plan['presentation'],
                    reference=command['reference'], decision=raw['decision'])
        return dict(decision=raw['decision'], task_ids=raw['task_ids'])
    if op == 'instruction':
        main(state, actor)
        require(command['instruction_id'] not in data['instructions'], 'instruction id already recorded')
        source = load_bound(command['source'])
        require(text(source.get('text')) and text(command.get('reason')), 'original instruction and attribution reason required')
        candidates = command.get('candidates', [])
        require(len(candidates) == len(set(candidates)) and set(candidates) <= set(state['tasks']), 'known unique candidate tasks required')
        action = command['action']
        require(action in ('query', 'discuss', 'implement', 'adjust', 'defect', 'new', 'authorize'), 'unknown instruction action')
        target = candidates[0] if len(candidates) == 1 else None
        status = 'main_handled' if action in ('query', 'discuss') else ('assigned' if target else 'held')
        if action == 'new':
            status = 'new_requirement' if len(candidates) <= 1 else 'held'
        if target and action in ('adjust', 'defect', 'implement'):
            task = state['tasks'][target]
            if task['phase'] == 'completed' or (frozen(task) and action != 'defect'):
                status = 'new_requirement'
        record = dict(source=command['source'], action=action, candidates=candidates, task_id=target,
            reason=command['reason'], status=status, changes=deepcopy(command.get('changes', {})))
        data['instructions'][command['instruction_id']] = record
        return deepcopy(record)
    if op == 'adopt':
        main(state, actor)
        task = state['tasks'][command['task_id']]
        item, record = approved_item(state, command['task_id'])
        require(not task.get('routing') and task.get('task_kind', 'code') == 'code', 'only an unbound existing code task may be adopted')
        require(task['requirements']['scope'] == record['scope'] and task['requirements']['acceptance'] == record['acceptance'], 'existing task scope differs')
        require(not any(t.get('routing', {}).get('requirement_id') == item['requirement_id'] for t in state['tasks'].values()), 'requirement already bound')
        task['routing'] = dict(requirement_id=item['requirement_id'])
        return dict(task_id=command['task_id'], adopted=True, execution_observed=False)
    if op == 'sync-spec':
        main(state, actor)
        task = state['tasks'][command['task_id']]
        item, record = approved_item(state, command['task_id'])
        require(task['phase'] != 'completed' and not frozen(task) and
            task.get('routing', {}).get('requirement_id') == item['requirement_id'], 'accepted scope frozen or different requirement')
        task['requirements'].update(scope=record['scope'], acceptance=deepcopy(record['acceptance']))
        return dict(task_id=command['task_id'], scope_updated=True)
    if op == 'apply-instruction':
        main(state, actor)
        instruction = data['instructions'][command['instruction_id']]
        require(instruction['status'] == 'assigned', 'only uniquely assigned instruction may change its task')
        task = state['tasks'][instruction['task_id']]
        changes = instruction['changes']
        require(set(changes) <= {'conditions', 'rights'} and changes, 'only explicit task conditions or rights can change')
        if 'rights' in changes:
            require(instruction['action'] == 'authorize', 'rights change requires an authorization instruction')
            raw = origin(state, instruction['source'], user_origin)
            require(raw.get('task_id') == instruction['task_id'] and raw.get('rights') == changes['rights'], 'scoped user rights differ')
            require(set(changes['rights']) <= set(RIGHTS) - {'deploy'} and
                all(type(v) is bool for v in changes['rights'].values()), 'deployment requires independent existing authority entry')
            for right, allowed in changes['rights'].items():
                task['authorization'][right] = dict(allowed=allowed, user_reference=instruction['source'])
        if 'conditions' in changes:
            require(not frozen(task) and task['phase'] != 'completed', 'accepted scope frozen')
            require(isinstance(changes['conditions'], dict) and changes['conditions'], 'explicit conditions required')
            task.setdefault('routing', {}).setdefault('conditions', {}).update(deepcopy(changes['conditions']))
        instruction['status'] = 'applied'
        return dict(task_id=instruction['task_id'], changed=deepcopy(changes))
    if op == 'route':
        main(state, actor)
        identity = command['route_id']
        require(identity not in data['routes'], 'route id already recorded')
        instruction = data['instructions'][command['instruction_id']]
        task_id = instruction['task_id']
        require(instruction['status'] == 'assigned' and task_id == command['task_id'], 'instruction must uniquely own the target')
        actions = command['actions']
        require(len(actions) == 1 and set(actions[0]) == {'kind', 'task_id', 'executor_id', 'message'} and
            actions[0]['task_id'] == task_id and actions[0]['kind'] in ('send', 'spawn') and
            text(actions[0]['message']), 'one bounded spawn/send; no broadcast, interruption or project reordering')
        action = actions[0]
        route = dict(task_id=task_id, instruction_id=command['instruction_id'], message=action['message'],
            spawn=action['kind'] == 'spawn', executor_id=action['executor_id'], reason=instruction['reason'],
            version=task_version(state, task_id), spec=current_requirement(state,
                state['tasks'][task_id]['routing']['requirement_id'])['spec'], attempt_id=None)
        data['routes'][identity] = route
        launch_guard(state, task_id, identity, spawn=route['spawn'], executor_id=route['executor_id'], message=route['message'])
        require(not any(r['instruction_id'] == route['instruction_id'] and r.get('attempt_id') and
            state['runtime']['operations'][r['attempt_id']]['status'] not in ('capacity', 'unsupported', 'queued', 'not_started')
            for key, r in data['routes'].items() if key != identity), 'instruction already dispatched')
        return deepcopy(route)
    if op == 'dependency':
        source = command['source_task']
        target = command['target_task']
        require(actor == state['tasks'][source]['owner_agent_id'], 'dependency request belongs to actual source developer')
        require(source != target and target in state['tasks'], 'concrete dependency target required')
        require(all(text(command.get(k)) for k in ('contract', 'actual_change', 'required_action')), 'four-part technical dependency required')
        artifact(command['evidence'])
        require(command['dependency_id'] not in data['dependencies'], 'dependency already recorded')
        record = {k: deepcopy(command[k]) for k in ('source_task', 'target_task', 'contract', 'actual_change', 'required_action', 'evidence')}
        record.update(requester=actor, status='pending_integration', source_version=task_version(state, source), target_version=task_version(state, target))
        data['dependencies'][command['dependency_id']] = record
        return deepcopy(record)
    if op == 'verify-dependency':
        delivery(state, actor)
        record = data['dependencies'][command['dependency_id']]
        require(record['status'] == 'pending_integration', 'dependency already dispositioned')
        require(record['source_version'] == task_version(state, record['source_task']) and
            record['target_version'] == task_version(state, record['target_task']), 'dependency target changed; verify a new request')
        proof = load_bound(command['evidence'])
        require(all(proof.get(k) == record[k] for k in ('source_task', 'target_task', 'contract', 'actual_change', 'required_action')) and
            proof.get('verifier') == actor and proof.get('basis') and
            proof.get('disposition') in ('within_scope', 'needs_user', 'unsubstantiated'), 'concrete integration evidence required')
        for evidence in proof['basis']:
            artifact(evidence)
        record.update(status=proof['disposition'], verification=command['evidence'], verifier=actor)
        if record['status'] == 'within_scope':
            task = state['tasks'][record['target_task']]
            require(task['phase'] != 'completed', 'ended target needs a new requirement')
            task.setdefault('routing', {}).setdefault('dependency_todos', []).append(command['dependency_id'])
        return dict(status=record['status'], interrupt=False, host_actions=[])
    if op == 'bind-pr':
        main(state, actor)
        task = state['tasks'][command['task_id']]
        require(task.get('routing') and task['owner_agent_id'], 'actual task developer must be bound first')
        mr = command['mr']
        require(mr.get('source_branch') == task['branch'] and mr.get('target_branch') == state['project']['main_branch'] and
            mr.get('provider') == state['project']['provider'], 'PR branch/provider differs')
        pr_guard(state, command['task_id'], mr)
        updated, result = transition(state, dict(op='mr', task_id=command['task_id'], mr=mr, actor_id=actor))
        state.clear(); state.update(updated)
        return result
    raise ValueError('unsupported routing operation; lifecycle and cleanup use their existing engines')
