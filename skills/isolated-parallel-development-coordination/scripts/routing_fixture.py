"""Explicit synthetic approvals for pre-existing runtime regression fixtures only."""
from demand_routing import scope_digest


def enroll(fixture, key):
    runtime, actor = fixture.runtime, fixture.actor
    task = runtime._load()['tasks'][key]
    if task.get('routing') or task.get('task_kind') == 'read_only':
        return
    def record(op, **fields):
        result = runtime.routing(dict(op=op, event_id=f'fixture-{key}-{op}', **fields), actor)
        assert result['status'] == 'recorded', result
    discussion = fixture.json(key+'-discussion.json', dict(confirmed=True))
    spec = fixture.file(key+'-spec.md', task['requirements']['scope'])
    check = fixture.json(key+'-check.json', dict(coordinator_id=actor, requirement_id=key,
        discussion=discussion, spec=spec, result='consistent'))
    record('spec', requirement_id=key, name=key, scope=task['requirements']['scope'],
        acceptance=task['requirements']['acceptance'], discussion=discussion, spec=spec,
        check=check, start_reference=fixture.file(key+'-start.txt'))
    requirement = runtime._load()['runtime']['intake']['requirements'][key]
    item = dict(task_id=key, requirement_id=key, name=key, scope=requirement['scope'],
        acceptance=requirement['acceptance'], scope_digest=scope_digest(requirement),
        split_reason='One synthetic requirement', assignment='new', executor_id=None,
        assignment_reason='Task-specific fixture actor, not module assignment')
    presentation = fixture.json(key+'-presentation.json', dict(conversation_id=actor,
        plan_id=key, version=1, items=[item]))
    record('present', plan_id=key, version=1, items=[item], presentation=presentation)
    approval = fixture.json(key+'-approval.json', dict(plan_id=key, presentation=presentation,
        task_ids=[key], decision='approved', quote='Explicit synthetic user approval'))
    result = runtime.routing(dict(op='approve', event_id=f'fixture-{key}-approve', plan_id=key, reference=approval),
        actor, user_origin=dict(role='user', conversation_id=actor, reference=approval))
    assert result['status'] == 'recorded', result
    record('adopt', task_id=key)


def launch(fixture, key, adapter, **options):
    """Install real guard inputs; never bypass the production guard in a test."""
    enroll(fixture, key)
    state = fixture.runtime._load()
    if state['tasks'][key].get('task_kind') == 'read_only':
        return fixture.runtime.launch(key, adapter, **options)
    if not options.get('spawn') and state['tasks'][key]['owner_agent_id'] is None:
        executor = state['runtime']['executors'][options['executor_id']]
        fixture.runtime.bind_executor(executor['id'], executor['routes'], fixture.actor, executor['authority'], task_id=key)
    event = 'fixture-route-'+options['attempt_id']
    if event not in state['runtime']['intake']['routes']:
        source = fixture.json(event+'-source.json', dict(text=options['message']))
        instruction = fixture.runtime.routing(dict(op='instruction', event_id=event+'-instruction',
            instruction_id=event, source=source, candidates=[key], action='defect' if
            state['tasks'][key]['user_acceptance'] == 'accepted' else 'implement',
            reason='Explicit bounded input for legacy regression fixture'), fixture.actor)
        assert instruction['status'] == 'recorded', instruction
        result = fixture.runtime.routing(dict(op='route', event_id=event, route_id=event,
            instruction_id=event, task_id=key, actions=[dict(kind='spawn' if options.get('spawn') else 'send',
            task_id=key, executor_id=options.get('executor_id'), message=options['message'])]), fixture.actor)
        assert result['status'] == 'recorded', result
    return fixture.runtime.launch(key, adapter, route_id=event, **options)
