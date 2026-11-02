"""Canonical, finite progress notifications. No human items or host side effects."""
from copy import deepcopy

from evidence import ERRORS, artifact, fingerprint, load_bound, preserved_read, report_for, require


def owners(state):
    from work_cycle import task_receivers
    delivery = state.get('delivery')
    if delivery:
        return {key: delivery['agent_id'] for key in delivery['task_ids']}
    result = task_receivers(state)
    from real_use_closeout import approval, cli_binding
    for key, task in state['tasks'].items():
        if task.get('state_contract'):
            result[key] = task['state_contract']['integrator_id']
            continue
        binding = task.get('cli_dispatch')
        if not binding:
            continue
        try:
            approval(state, key, binding['command']['approval'], binding['reviewer_id'])
            cli_binding(state, key, binding['command'])
            result[key] = binding['reviewer_id']
        except ERRORS:
            continue
    return result


def store(state):
    return state['runtime'].setdefault('stage_feedback', dict(events={}, latest={}, sends={}, installations={}))


def confirmed_close(state, key):
    task = state['tasks'][key]
    executor = state['runtime'].get('executors', {}).get(task['owner_agent_id'], {})
    for operation in state['runtime'].get('operations', {}).values():
        if (executor.get('lease') == 'closed' and operation.get('operation') == 'close' and
            operation.get('status') == 'closed' and operation.get('task_id') == key and
            operation.get('executor_id') == task['owner_agent_id'] and
            operation.get('generation') == executor.get('generation') and
            (operation.get('release_receipt') or operation.get('close_confirmation'))):
            try:
                proof = operation.get('release_receipt') or operation['close_confirmation']
                artifact(proof)
                if not operation.get('release_receipt') and load_bound(proof).get('schema')=='resource-close-confirmation/1':
                    value=load_bound(proof)
                    require(value.get('actual_result')==operation.get('actual_result') and
                        value.get('operation_id')==operation['operation_id'] and
                        load_bound(value['actual_result'])['result'].get('status')=='closed', 'actual close unconfirmed')
                elif not operation.get('release_receipt'):
                    value = load_bound(proof)
                    require(all(value.get(field) == operation.get(field) for field in (
                        'project_id', 'task_id', 'operation_id', 'executor_id', 'generation', 'native_request', 'host_confirmation')) and
                        value.get('outcome') == 'confirmed_closed' and value.get('platform_capacity_count_known') is False,
                        'current canonical native close confirmation required')
                    host = load_bound(operation['host_confirmation'])
                    for step in ('close', 'wait'): artifact(host[step]['result'])
                return proof
            except ERRORS:
                continue
    return None


def cleanup_fact(state, key):
    """Read registered facts only; absence/idle/completed cannot prove release."""
    task = state['tasks'][key]
    resources = [r for r in state['runtime'].get('resources', {}).values() if r['task_id'] == key]
    rows, evidence = [], []
    for resource in resources:
        result = resource.get('cleanup_result') or {}
        status = resource.get('cleanup_status', 'unknown')
        reason = result.get('reason')
        proof = result.get('evidence')
        try:
            if resource['kind'] == 'agent':
                close = confirmed_close(state, key)
                if close and resource.get('executor_id') == task['owner_agent_id'] and resource.get('generation') == state['runtime']['executors'][task['owner_agent_id']]['generation']:
                    status, proof = 'cleaned', close
                elif status != 'cleaned':
                    executor = state['runtime'].get('executors', {}).get(task['owner_agent_id'], {})
                    pending_close = next((o for o in state['runtime'].get('operations', {}).values() if
                        o.get('operation') == 'close' and o.get('task_id') == key and
                        o.get('executor_id') == resource.get('executor_id') == task['owner_agent_id'] and
                        o.get('generation') == resource.get('generation') == executor.get('generation') and
                        o.get('status') in ('intent', 'unknown')), None)
                    if pending_close and executor.get('lease') in ('closing', 'unknown'):
                        status = 'intent' if pending_close['status'] == 'intent' else 'unknown'
                        reason = 'close_pending_confirmation'
            if status == 'cleaned':
                artifact(proof)
                if resource['kind'] == 'agent':
                    require(resource.get('executor_id') == task['owner_agent_id'] and
                            resource.get('generation') == state['runtime']['executors'][task['owner_agent_id']]['generation'] and
                            confirmed_close(state, key), 'current canonical agent close required')
                evidence.append(proof)
        except ERRORS:
            status = 'unknown'
        rows.append(dict(resource_id=resource['id'], kind=resource['kind'], status=status,
                         reason=reason, protected=bool(resource.get('protected')),
                         shared=bool(resource.get('shared'))))
    # Missing registration or observation stays explicit, including temporary data.
    for kind in ('agent', 'process', 'worktree', 'temporary_data'):
        members = [r for r in rows if (r['kind'] in ('database', 'environment', 'artifact')
                   if kind == 'temporary_data' else r['kind'] == kind)]
        if not members:
            proof = confirmed_close(state, key) if kind == 'agent' else None
            if proof:
                evidence.append(proof)
            rows.append(dict(resource_id=None, kind=kind, status='cleaned' if proof else 'unknown',
                             reason=None if proof else 'not_registered_or_observed'))
    statuses = [row['status'] for row in rows]
    cleanup = task.get('cleanup') or {}
    outcome = ('all' if all(s == 'cleaned' for s in statuses) and cleanup.get('status') == 'cleanup_completed' else
               'partial' if 'cleaned' in statuses else 'failed' if 'failed' in statuses else
               'in_progress' if 'intent' in statuses else 'unknown' if 'unknown' in statuses else 'pending')
    labels = dict(agent='开发者上下文', process='进程/测试服务', worktree='工作树', database='临时数据',
                  environment='验收环境', artifact='临时文件', temporary_data='临时数据')
    status_labels = dict(cleaned='已回收', retained='待清理', intent='正在核对回收', failed='回收失败', unknown='待核实')
    details = list(dict.fromkeys(labels[r['kind']] + '：' +
        ('关闭待确认' if r.get('reason') == 'close_pending_confirmation' else status_labels.get(r['status'], '待核实')) +
        ('（受保护）' if r.get('protected') else '（共享引用待核对）' if r.get('shared') else
         '（未登记或未观察）' if r.get('reason') == 'not_registered_or_observed' else
         '（需核对实际效果）' if r['status'] in ('failed', 'unknown') else
         '（等待既有保全及清理门禁）' if r['status'] == 'retained' else '') for r in rows))
    return dict(outcome=outcome, resources=rows, evidence=evidence, details='；'.join(details))


def facts(state, key):
    try:
        with preserved_read(state, key):
            return task_facts(state, key)
    except ERRORS:
        return []


def task_facts(state, key):
    task = state['tasks'][key]
    result = []
    report = None
    if task['implementation'] == 'completed' and task.get('delivery'):
        try:
            report = report_for(state, key, ready=False)
            result.append(dict(kind='development', outcome='delivered', text='开发已交付，转集成者独审',
                               evidence=[task['delivery']], source=task['delivery']))
        except ERRORS:
            pass  # An unverified report is not a delivery notification.
    if report:
        review = task.get('review')
        independent = task.get('independent_quality')
        try:
            if independent:
                from real_use_closeout import quality
                quality(state, key)
                proof = independent['review']
            else:
                require(review and review['reviewer_id'] == owners(state).get(key) and
                    review['reviewer_id'] != task['owner_agent_id'] and
                    review['source_digest'] == report['source']['digest'] and
                    all(review[field] == task['mr'][field] for field in ('head_sha', 'target_sha')),
                    'current independent review required')
                proof = review['evidence']
                artifact(proof)
            result.append(dict(kind='review', outcome='passed', text='独审已通过', evidence=[proof], source=task['delivery']))
        except ERRORS:
            pass
    merge = task.get('merge') or {}
    if task['phase'] == 'completed' and merge.get('verified') and merge.get('proof'):
        try:
            proof = load_bound(merge['proof'])
            require(proof.get('project_id') == state['project']['id'] and proof.get('task_id') == key and
                proof.get('source_head') == merge.get('head_sha') and proof.get('contains_source') is True and
                proof.get('main_head') == merge.get('main_sha') and proof.get('previous_main') == merge.get('target_sha'),
                'current main inclusion proof required')
            for value in proof['evidence']: artifact(value)
            result.append(dict(kind='merge', outcome='merged', text='合入已完成', evidence=[merge['proof']],
                               source=merge['proof'], scope=merge.get('scope', 'ledger_evidence')))
        except ERRORS:
            pass
    blocker = task.get('blocker')
    if blocker:
        result.append(dict(kind='blocker', outcome=fingerprint(blocker), text='重要受阻，后续由集成者核对',
                           evidence=[task.get('delivery')] if task.get('delivery') else [], source=deepcopy(blocker)))
    if task.get('cleanup') or task['phase'] == 'completed':
        value = cleanup_fact(state, key)
        label = dict(all='全部回收', partial='部分回收', failed='回收失败', in_progress='回收处理中（结果待确认）',
                     unknown='回收待核实', pending='待清理')[value['outcome']]
        result.append(dict(kind='cleanup', outcome=dict(status=value['outcome'], resources=value['resources']),
            text=label+'；'+value['details']+'；后续由集成者负责', evidence=value['evidence'], source=None,
            scope=merge.get('scope', 'ledger_evidence')))
    installation = state['runtime'].get('stage_feedback', {}).get('installations', {}).get(key)
    if installation and installation['binding'] == dict(delivery_revision=task['delivery_revision'], merge=task.get('merge')):
        try:
            artifact(installation['proof'])
            result.append(dict(kind='installation', outcome='verified', text='安装及真实任务验证已完成',
                evidence=[installation['proof']], source=installation['binding'], scope=load_bound(installation['proof'])['scope']))
        except ERRORS:
            pass
    plan = state.get('release_plan')
    if plan and key in plan['included_task_ids'] and plan['completed_steps'] == plan['actions']:
        try:
            from delivery import verify_release_history
            verify_release_history(plan)
            result.append(dict(kind='installation', outcome='verified', text='安装及实际验证已完成',
                evidence=[plan['results']['verify']['result']], source=plan['release_sha']))
        except ERRORS:
            pass
    return result


def capture(state, task_ids=None):
    """Called within existing canonical writes, never on main's read-only cycle."""
    if 'runtime' not in state:
        return
    for key, owner in owners(state).items():
        if task_ids is not None and key not in task_ids:
            continue
        task = state['tasks'][key]
        if task.get('state_contract'):
            continue  # Producers/events already registered these facts, no report rescan.
        values = facts(state, key)
        data = store(state)
        for event in data['events'].values():
            if event['task_id'] == key and event['status'] != 'displayed' and event['delivery_revision'] < task['delivery_revision']:
                event['status'] = 'superseded'
        blocker_id = data['latest'].get(key+':blocker')
        if (not task.get('blocker') and blocker_id and data['events'][blocker_id]['outcome'] != 'resolved' and
            task['self_test'] == 'passed' and any(f['kind'] == 'development' for f in values)):
            values.append(dict(kind='blocker', outcome='resolved', text='此前阻塞已解除',
                               evidence=[task['delivery']], source=task['delivery']))
        for fact in values:
            identity = dict(task_id=key, delivery_revision=task['delivery_revision'], kind=fact['kind'],
                            outcome=fact['outcome'], source=fact['source'], owner=owner,
                            scope=fact.get('scope', 'ledger_evidence'))
            event_id = fingerprint(identity)
            slot = key+':'+fact['kind']
            previous = data['events'].get(data['latest'].get(slot))
            if previous and all(previous.get(field) == value for field, value in identity.items()):
                continue
            if previous and previous['status'] != 'displayed':
                previous['status'] = 'superseded'
            # A repeated state after a real transition is a fresh change, not a replay.
            if event_id in data['events']:
                event_id = fingerprint(dict(identity, previous=data['latest'].get(slot)))
            data['events'][event_id] = dict(identity, id=event_id, ledger_revision=state['revision'],
                recipient_id=state['coordinator_id'], technical_owner=owner, evidence=fact['evidence'],
                text=('隔离夹具：' if identity['scope'] == 'fixture_only' else '')+
                    task.get('progress', {}).get('title', key)+'：'+fact['text']+'。', status='pending')
            data['latest'][slot] = event_id


def pending(state):
    data = state.get('runtime', {}).get('stage_feedback', {})
    current_owners = owners(state)
    current_facts = {}
    events = []
    for event in data.get('events', {}).values():
        key = event['task_id']
        if event.get('state_tool'):
            if event['recipient_id'] == state['coordinator_id'] and event['status'] not in ('displayed','superseded'):
                events.append(deepcopy(event))
            continue
        if (event['status'] in ('displayed', 'superseded') or current_owners.get(key) != event['technical_owner'] or
            event['delivery_revision'] != state['tasks'][key]['delivery_revision']):
            continue
        # Recheck current facts; expired source/review/cleanup proof cannot be announced.
        if key not in current_facts:
            current_facts[key] = facts(state, key)
        if not any(fact['kind'] == event['kind'] and fact['outcome'] == event['outcome'] and
                   fact['source'] == event['source'] for fact in current_facts[key]):
            task = state['tasks'][key]
            if not (event['kind'] == 'blocker' and event['outcome'] == 'resolved' and not task.get('blocker') and
                    task['self_test'] == 'passed' and event['source'] == task['delivery'] and
                    any(f['kind'] == 'development' for f in current_facts[key])):
                continue
        events.append(deepcopy(event))
    return dict(items=events, text='\n'.join(e['text'] for e in events), requires_reply=False,
                recipient_id=state['coordinator_id'], display_required=bool(events), idle_wakeup=False)


def command(state, value, actor, verify_display=None):
    data = store(state)
    op = value['op']
    if op == 'record-installation':
        key = value['task_id']; task = state['tasks'][key]
        require(actor == owners(state).get(key) and actor != task['owner_agent_id'], 'current independent integrator required')
        proof = load_bound(value['proof'])
        require(task['phase'] == 'completed' and task.get('merge', {}).get('verified') and
            proof.get('schema') == 'stage-installation-verification/1' and proof.get('project_id') == state['project']['id'] and
            proof.get('status') == 'passed' and
            proof.get('task_id') == key and proof.get('delivery_revision') == task['delivery_revision'] and
            proof.get('source_head') == task['merge'].get('head_sha') and task['merge'].get('main_sha') and
            proof.get('installed_sha') == task['merge']['main_sha'] and proof.get('reviewer_id') == actor and
            proof.get('scope') in ('host_verified', 'fixture_only') and proof.get('environment') and
            proof.get('backup_evidence') and proof.get('installation_evidence') and proof.get('real_task_evidence'),
            'merged source, backup, installation and real-task evidence required')
        for field in ('backup_evidence', 'installation_evidence', 'real_task_evidence'):
            for item in proof[field]: artifact(item)
        record = dict(proof=value['proof'], binding=dict(delivery_revision=task['delivery_revision'], merge=task['merge']))
        duplicate = data['installations'].get(key) == record
        data['installations'][key] = deepcopy(record)
        return dict(status='recorded', duplicate=duplicate)
    ids = value['notification_ids']
    require(not any(data['events'].get(i, {}).get('state_tool') for i in ids), 'state-tool feedback uses the bound TaskState entry')
    require(ids and len(ids) == len(set(ids)), 'nonempty unique notification ids required')
    if op == 'confirm-display' and all(data['events'].get(i, {}).get('status') == 'displayed' for i in ids):
        require(actor == state['coordinator_id'] and all(data['events'][i]['display']['proof'] == value['proof'] for i in ids),
                'display replay identity differs')
        artifact(value['proof'])
        return dict(status='displayed', duplicate=True)
    current = {event['id']: event for event in pending(state)['items']}
    # Exact replay is allowed, but never a conflicting attempt or another recipient.
    if op == 'prepare-send' and value['attempt_id'] in data['sends']:
        old = data['sends'][value['attempt_id']]
        require(old['notification_ids'] == ids and old['actor_id'] == actor and
                all(owners(state).get(data['events'][i]['task_id']) == actor for i in ids), 'send replay identity differs')
        return dict(deepcopy(old), duplicate=True, send_allowed=False)
    require(set(ids).issubset(current), 'current undisplayed notifications required')
    events = [data['events'][i] for i in ids]
    text = '\n'.join(event['text'] for event in events)
    if op == 'confirm-display':
        require(actor == state['coordinator_id'] and callable(verify_display), 'actual main and trusted host display verifier required')
        proof = value['proof']; artifact(proof)
        context = dict(project_id=state['project']['id'], coordinator_id=actor, notification_ids=ids, text=text)
        observed = verify_display(deepcopy(context), deepcopy(proof))
        require(isinstance(observed, dict) and all(observed.get(k) == v for k, v in context.items()) and
            observed.get('visible') is True and observed.get('message_id') and
            observed.get('scope') in ('host_verified', 'fixture_only'), 'actual visible message confirmation required; accepted is not displayed')
        for event in events:
            event.update(status='displayed', display=dict(proof=proof, observation=deepcopy(observed)))
        return dict(status='displayed', notification_ids=ids, scope=observed['scope'])
    require(all(actor == event['technical_owner'] for event in events), 'current notification integrator required')
    attempt = value['attempt_id']
    if op == 'prepare-send':
        require(all(event['status'] == 'pending' for event in events), 'uncertain send must be reconciled, never blindly retried')
        record = dict(attempt_id=attempt, actor_id=actor, notification_ids=ids, recipient_id=state['coordinator_id'],
                      status='intent', text=text)
        data['sends'][attempt] = record
        for event in events: event.update(status='sent_unconfirmed', attempt_id=attempt)
        return dict(deepcopy(record), send_allowed=True)
    require(op == 'record-send' and attempt in data['sends'], 'prepare send intent first')
    record = data['sends'][attempt]
    require(record['actor_id'] == actor and record['notification_ids'] == ids, 'send identity differs')
    proof = load_bound(value['proof'])
    require(all(proof.get(k) == record[k] for k in ('attempt_id', 'notification_ids', 'recipient_id')) and
            proof.get('status') in ('accepted', 'unknown', 'failed'), 'bound send result required')
    if record.get('proof'):
        require(record['proof'] == value['proof'], 'conflicting send result')
        return dict(status='sent_unconfirmed', delivered_to_user=False, duplicate=True)
    record.update(status=proof['status'], proof=value['proof'])
    return dict(status='sent_unconfirmed', transport_status=proof['status'], delivered_to_user=False)
