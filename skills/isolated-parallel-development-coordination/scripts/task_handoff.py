"""Finite observation/receipt continuation over existing task identities and gates.

The caller supplies actual host captures. This module neither invokes tools nor
authenticates JSON; it returns the single minimum call for a trusted host to make.
"""
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import urlparse

from coordination import admit_receipt, consume, receipt_context, receipt_report, writer_lock
from evidence import ERRORS, artifact, fingerprint, load_bound, ref, require, verify_source
from host_adapter import unwrap

APP = 'mcp__codex_app__read_thread'
NATIVE = 'multi_agent_v1__wait_agent'


def launch_binding(state, task_id):
    """An admitted CORE launch is a producer, even without StandardDispatch."""
    from demand_routing import approved_item, intake
    task = state['tasks'][task_id]
    item, _ = approved_item(state, task_id)
    approval_ref = intake(state)['approvals'][fingerprint(item)]['reference']
    owner = state['runtime']['executors'].get(task['owner_agent_id'], {})
    attempt = task.get('worker_run', {}).get('attempt_id')
    launch = state['runtime']['operations'].get(attempt, {})
    require(attempt and launch.get('attempt_id') == attempt and launch.get('task_id') == task_id and
        launch.get('operation') in ('spawn', 'send') and launch.get('executor_id') == task['owner_agent_id'] and
        launch.get('generation') == owner.get('generation') and
        launch.get('status') in ('accepted_unconfirmed','running','reported') and
        task['worker_run'].get('owner_agent_id') == task['owner_agent_id'], 'actual original CORE launch binding required')
    permit = load_bound(launch['admission'])
    require(permit.get('project_id') == state['project']['id'] and permit.get('status') == 'granted' and
        all(permit.get(k) == launch.get(k) for k in ('task_id','attempt_id','request_id','permit_id','launch_identity')),
        'original CORE launch admission changed')
    route = intake(state)['routes'].get(launch.get('route_id'), {})
    require(route.get('task_id') == task_id and route.get('attempt_id') == attempt and
        route.get('spec') == intake(state)['requirements'][item['requirement_id']]['spec'], 'original CORE route differs')
    operation = launch['operation']
    captured = [row for row in launch.get('host_results', []) if row.get('operation') == operation]
    require(len(captured) == 1, 'one actual CORE host result required')
    capture = load_bound(captured[0]['capture'])
    binding = launch.get('callback_captures', {}).get(operation)
    result = capture.get('result', {})
    target = owner.get('routes', {}).get('native_agent', {}).get('target_id')
    require(binding and capture.get('schema') == 'callback-result/1' and capture.get('binding') == binding and
        all(binding.get(k) == value for k,value in dict(project_id=state['project']['id'], task_id=task_id,
            attempt_id=attempt, operation=operation).items()) and result == captured[0]['result'] and
        result.get('status') == 'accepted' and result.get('target_id') == target and
        result.get('channel') == 'native_agent', 'actual CORE host identity/result differs')
    require(operation != 'spawn' or owner.get('authority') == launch['admission'], 'original CORE author authority differs')
    return approval_ref, dict(core_launch=attempt, operation_id=attempt, launch_capture=captured[0]['capture'])


def authority(state, task_id):
    task = state['tasks'][task_id]
    observed = state['runtime'].get('observed_dispatches', {}).get(task_id)
    if observed:
        proof = load_bound(observed['proof'])
        approval_ref = proof['approval']
        from real_use_closeout import approval as approved_observed
        approved_observed(state, task_id, approval_ref, observed['reviewer_id'])
        for key in ('dispatch_result', 'spec_at_dispatch', 'checklist_at_dispatch', 'observation'):
            artifact(proof[key])
        require(observed['executor_id'] == task['owner_agent_id'] and
            observed['generation'] == state['runtime']['executors'][task['owner_agent_id']]['generation'],
            'observed owner or generation changed')
        dispatch = dict(observed_dispatch=observed['proof'])
    else:
        from demand_routing import approved_item, intake
        item, _ = approved_item(state, task_id)
        approval_ref = intake(state)['approvals'][fingerprint(item)]['reference']
        matches = [(key, op) for key, op in state['runtime']['operations'].items()
                   if op.get('operation') == 'standard-dispatch' and op.get('task_id') == task_id]
        if not matches:
            approval_ref, dispatch = launch_binding(state, task_id)
        else:
            require(len(matches) == 1, 'one bound standard dispatch required')
            key, operation = matches[0]
            launch = state['runtime']['operations'].get(key+':launch', {})
            owner = state['runtime']['executors'].get(task['owner_agent_id'], {})
            retained = operation.get('retained_host_results', {}).get('spawn')
            registered = False
            if launch.get('status') == 'not_started' and launch.get('executor_id') is None and owner.get('authority') == approval_ref:
                resource = state['runtime'].get('resources', {}).get(task_id+':agent', {})
                require(resource.get('kind') == 'agent' and resource.get('task_id') == task_id and
                    resource.get('project_id') == state['project']['id'] and resource.get('executor_id') == task['owner_agent_id'] and
                    resource.get('generation') == owner.get('generation') and resource.get('authority') == approval_ref,
                    'publicly bound author resource differs from approved task')
                for reference in launch.get('observations', []):
                    facts = load_bound(reference)
                    require(facts.get('project_id') == state['project']['id'] and facts.get('task_id') == task_id and
                        facts.get('operation_id') == key+':launch' and facts.get('status') == 'not_started' and facts.get('evidence'),
                        'original no-execution observation differs')
                    for item in facts['evidence']:
                        artifact(item)
                    registered = True
                require(any(h.get('operation', {}).get('op') == 'bind-executor' and
                    h['operation'].get('executor_id') == task['owner_agent_id'] for h in state['history']),
                    'actual public executor binding required')
            require(operation.get('launch_started') and task['owner_agent_id'] is not None and
                (launch.get('executor_id') == task['owner_agent_id'] or
                 retained is not None and owner.get('authority') == retained or registered),
                'actual original launch identity required; no replacement spawn')
            dispatch = dict(request_id=key, request_digest=operation['request_digest'])
            if registered:
                dispatch.update(registered_executor=task['owner_agent_id'], original_operation_id=key+':launch')
            else:
                dispatch['operation_id'] = key+':launch'
    approval = load_bound(approval_ref)
    receiver = approval.get('integration_agent')
    require(receiver and receiver not in (state['coordinator_id'], task['owner_agent_id']) and
        approval.get('task_id', task_id) == task_id and
        any(t['id'] == task_id and t.get('branch', task['branch']) == task['branch'] and
            Path(t.get('worktree', task['worktree'])).resolve() == Path(task['worktree']).resolve() for t in approval.get('tasks', [])),
        'approved original task/receiver/worktree/branch required')
    require(not state.get('delivery') or (state['delivery']['agent_id'] == receiver and
        task_id in state['delivery']['task_ids']), 'outside current delivery scope')
    executor = state['runtime']['executors'][task['owner_agent_id']]
    artifact(executor['authority'])
    target = executor['routes'].get('native_agent', {}).get('target_id')
    require(target, 'original native target required')
    return dict(project_id=state['project']['id'], task_id=task_id, owner_agent_id=task['owner_agent_id'],
        generation=executor['generation'], target_id=target, recipient_agent_id=receiver,
        approval=approval_ref, **dispatch)


def receivers(state):
    result = {}
    for key in state['tasks']:
        try:
            result[key] = authority(state, key)['recipient_agent_id']
        except ERRORS:
            pass
    return result


def arguments(tool, target):
    if tool == APP:
        return dict(threadId=target, turnLimit=1, includeOutputs=False, maxOutputCharsPerItem=1800)
    require(tool == NATIVE, 'unsupported observation tool; do not substitute or create a chat')
    return dict(targets=[target], timeout_ms=10000)


def observation(state, task_id, reference):
    value = load_bound(reference)
    bound = authority(state, task_id)
    require(value.get('schema') == 'task-handoff-observation/1' and value['binding'] == bound and
        value['arguments'] == arguments(value['tool'], bound['target_id']), 'observation identity/request/recipient binding differs')
    raw = unwrap(value['result'])
    target = bound['target_id']
    if value['tool'] == APP:
        require(raw.get('thread', {}).get('id') == target, 'App observation returned another thread')
        turn = raw.get('latestTurn') or raw.get('latest_turn') or next(iter(raw.get('turns', [])), {})
        status = turn.get('status')
        outcome = 'running' if status in ('inProgress', 'in_progress') else status
        finals = [item.get('text', '') for item in turn.get('items', []) if item.get('type') == 'agentMessage' and
                  item.get('phase') in ('final', 'final_answer')]
    else:
        status = raw.get('status', {})
        require(isinstance(status, dict) and set(status) <= {target}, 'native observation contains another target')
        status = status.get(target)
        outcome = 'completed' if isinstance(status, dict) and 'completed' in status else status
        finals = [status['completed']] if isinstance(status, dict) and isinstance(status.get('completed'), str) else []
    return bound, value['tool'], outcome if isinstance(outcome, str) else 'unknown', finals


def receipt_origin(state, payload):
    bound, tool, outcome, finals = observation(state, payload['task_id'], payload['origin_observation'])
    require(payload['owner_agent_id'] == bound['owner_agent_id'] and
        payload['recipient_agent_id'] == bound['recipient_agent_id'] and
        payload['channel'] == ('app_thread' if tool == APP else 'native_agent'), 'final owner/receiver/channel differs')
    require(outcome in ('completed', 'failed', 'interrupted', 'cancelled', 'canceled') and
        (outcome == 'completed' or payload['kind'] == 'blocked'), 'terminal outcome does not support successful receipt')
    target = payload['report']['path'].replace('\\', '/').casefold()
    require(any(target in text.replace('\\', '/').casefold() for text in finals),
        'actual final does not reference this exact report')


def report_contract(state, payload, report, *, verify_live_source=False):
    task = state['tasks'][payload['task_id']]
    require(report.get('owner_agent_id') == task['owner_agent_id'] and
        report.get('project_id') == state['project']['id'] and report.get('task_id') == payload['task_id'] and
        type(report.get('delivery_revision')) is int and report['delivery_revision'] > 0 and
        report['delivery_revision'] == payload['delivery_revision'], 'report author/project/task/revision differs')
    require(Path(report.get('worktree', '')).resolve() == Path(task['worktree']).resolve() and
        report.get('branch') == task['branch'], 'report worktree/branch differs')
    require(report.get('status') != 'held', 'held report must return to author; no completion implied')
    if payload['kind'] == 'completed':
        git = report.get('git', {})
        url = urlparse(git.get('pr_url', ''))
        require(url.scheme == 'https' and url.netloc and re.search(r'/(pull|merge_requests)/[1-9][0-9]*$', url.path),
            'git.pr_url must name the actual PR before review')
        require(all(re.fullmatch(r'[a-f0-9]{40}', git.get(k, '')) for k in ('head_sha', 'target_sha')),
            'git.head_sha and git.target_sha required before review')
        require(not task.get('mr') or task['mr'].get('url') == git['pr_url'], 'maintain original PR')
        if task.get('delivery'):
            previous = load_bound(task['delivery'])
            prior_url = previous.get('git', {}).get('pr_url')
            require(not prior_url or prior_url == git['pr_url'], 'maintain original delivered PR')
        require(report['self_test']['status'] == 'passed' and report['self_test']['command'] and
            report['self_test']['source_digest'] == report['source']['digest'], 'successful source-bound self-test required')
        require(Path(report['source']['root']).resolve() == Path(task['worktree']).resolve(), 'source outside task worktree')
        if verify_live_source:
            verify_source(report['source'])
            if report['source']['kind'] == 'git':
                from evidence import git as read_git
                require(read_git(task['worktree'], '-c', 'core.longpaths=true', 'rev-parse', 'HEAD') == git['head_sha'] and
                    read_git(task['worktree'], '-c', 'core.longpaths=true', 'branch', '--show-current') == task['branch'] and
                    read_git(task['worktree'], '-c', 'core.longpaths=true', 'merge-base', git['head_sha'], git['target_sha']) == git['target_sha'],
                    'report head/branch differs from actual source checkout')
        require(report['evidence'], 'test evidence required')
        for item in report['evidence']:
            artifact(item)
    else:
        require(report.get('status') == 'blocked', 'failed terminal requires a blocked report with an explicit reason')


def run(runtime, task_id, actor_id, *, available_tools=(), observation_ref=None, report_ref=None, payload=None):
    state = runtime._load()
    bound = authority(state, task_id)
    receiver = bound['recipient_agent_id']
    require(actor_id in (state['coordinator_id'], receiver), 'actual main or approved task integration receiver required')
    common = dict(task_id=task_id, binding=bound, next_owner=receiver, idle_wakeup=False, automatic_retry=False)
    if observation_ref is None:
        tool = next((t for t in (APP, NATIVE) if t in available_tools), None)
        return dict(common, status='observation_required' if tool else 'capability_unavailable',
            host_call=dict(tool=tool, arguments=arguments(tool, bound['target_id'])) if tool else None,
            next_action='Observe the registered target once; provide its unchanged result and this binding')
    require(actor_id == receiver, 'main stays read-only; hand actual facts to the approved integration receiver')
    with writer_lock(runtime.path):
        state = runtime._load()
        require(authority(state, task_id) == bound, 'handoff binding changed during observation')
        task = state['tasks'][task_id]
        _, tool, outcome, _ = observation(state, task_id, observation_ref)
        if outcome == 'running':
            if task['worker_run']['status'] == 'reported' or task['phase'] in ('completed', 'cancelled'):
                return dict(common, status='stale', next_action='Keep current receipt and original author')
            if task.get('handoff_observation') != observation_ref:
                task.update(phase='developing', implementation='running', handoff_observation=observation_ref)
                task['worker_run'].update(status='running', reason='Original executor observed running')
                state['runtime']['executors'][bound['owner_agent_id']]['execution'] = 'running'
                if bound.get('operation_id'):
                    state['runtime']['operations'][bound['operation_id']]['status'] = 'running'
                    if bound.get('request_id'):
                        state['runtime']['operations'][bound['request_id']].update(status='running_observed', next_owner=receiver)
                runtime._save(state, dict(op='handoff-running', task_id=task_id, observation=observation_ref))
            return dict(common, status='running', next_action='Original author continues; next finite observation on a new fact')
        if outcome not in ('completed', 'failed', 'interrupted', 'cancelled', 'canceled'):
            return dict(common, status='needs_reconcile', next_action='Execution unknown; retain original intent and resources')
        report_ref = report_ref or (ref(task['report_path']) if task.get('report_path') and Path(task['report_path']).exists() else None)
        if report_ref is None:
            return dict(common, status='needs_author', next_owner=bound['owner_agent_id'], reason='terminal report missing')
        try:
            report = load_bound(report_ref)
        except ERRORS as error:
            return dict(common, status='needs_author', next_owner=bound['owner_agent_id'], reason=str(error), report=report_ref)
        kind = 'blocked' if report.get('status') == 'blocked' else 'completed'
        revision = report.get('delivery_revision')
        packet = dict(project_id=runtime.project_id, task_id=task_id, owner_agent_id=bound['owner_agent_id'],
            recipient_agent_id=receiver, channel='app_thread' if tool == APP else 'native_agent',
            kind=kind, delivery_revision=revision, event_id=f'{task_id}:{revision}:{kind}', report=report_ref,
            origin_mode='task_handoff', origin_observation=observation_ref)
        if payload is not None:
            require(all(payload.get(k) == v for k, v in packet.items() if k not in ('origin_mode', 'origin_observation')),
                'provided final payload differs from actual task/report/recipient/revision')
            packet.update(payload, origin_mode='task_handoff', origin_observation=observation_ref)
        # Source identity/provenance mismatch is rejected. A genuine failed turn
        # with an incompatible author report is returned to that author.
        if outcome != 'completed' and kind != 'blocked':
            return dict(common, status='needs_author', next_owner=bound['owner_agent_id'],
                reason='failed terminal requires a blocked report with an explicit reason', report=report_ref)
        receipt_origin(state, packet)
        try:
            report_contract(state, packet, report, verify_live_source=True)
            receipt_report(state, packet)
        except ERRORS as error:
            return dict(common, status='needs_author', next_owner=bound['owner_agent_id'], reason=str(error), report=report_ref)
        previous = state['receipts'].get(packet['event_id'])
        if previous:
            provenance = {'origin_observation', 'origin_mode', 'origin_dispatch', 'channel'}
            require({k: v for k, v in previous['payload'].items() if k not in provenance} ==
                {k: v for k, v in packet.items() if k not in provenance}, 'conflicting duplicate final receipt')
            receipt_context(state, previous['payload']); receipt_report(state, previous['payload'])
            if previous['status'] in ('processed', 'stale'):
                return dict(common, status='received', duplicate=True, report=report_ref)
            packet = previous['payload']
        if revision < max(task['delivery_revision'], task['worker_run']['expected_delivery_revision']):
            return dict(common, status='stale', report=report_ref)
        require(task['phase'] not in ('completed', 'cancelled'), 'ended task cannot receive a new handoff revision')
        admitted = admit_receipt(state, packet)
        if admitted == 'quarantined':
            runtime._save(state, dict(op='handoff-unverified', task_id=task_id))
            return dict(common, status='needs_reconcile', report=report_ref)
        candidate = deepcopy(state)
        try:
            status, reason = consume(candidate, packet), None
            state = candidate
        except ERRORS as error:
            status, reason = 'held', str(error)
        record = state['receipts'][packet['event_id']]
        record.update(status=status, reason=reason)
        record['history'].append(dict(status=status, reason=reason))
        if status == 'processed':
            state['runtime']['executors'][bound['owner_agent_id']]['execution'] = 'completed'
        runtime._save(state, dict(op='handoff-receipt', task_id=task_id, event_id=packet['event_id'], observation=observation_ref))
        return dict(common, status='received' if status == 'processed' else 'needs_reconcile',
            receipt_status=status, reason=reason, report=report_ref, duplicate=False,
            next_action='Independent review and existing acceptance/integration gates; no merge or CI implied')
