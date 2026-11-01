"""One finite receipt/recovery/progress entry; no second state or host side effects."""
from copy import deepcopy

from coordination import checkpoint, next_item, presentation, readiness, summary, writer_lock
from evidence import ERRORS, artifact, load_bound, require, write_atomic
from progress import build_progress, round_handoff
from reconcile import closeout


def task_receivers(state):
    """Reuse already-bound observed dispatch authority; never mint a global role."""
    from real_use_closeout import approval
    receivers = {}
    for key, binding in state.get('runtime', {}).get('observed_dispatches', {}).items():
        try:
            proof = load_bound(binding['proof'])
            reviewer = binding['reviewer_id']
            approval(state, key, proof['approval'], reviewer)
            require(binding['receipt_scope'] == [key] and proof['task_id'] == key and
                proof['project_id'] == state['project']['id'] and
                proof['executor_id'] == binding['executor_id'] == state['tasks'][key]['owner_agent_id'] and
                state['runtime']['executors'][binding['executor_id']]['generation'] == binding['generation'],
                'current observed task receiver binding required')
            for field in ('dispatch_result', 'spec_at_dispatch', 'checklist_at_dispatch', 'observation'):
                artifact(proof[field])
            receivers[key] = reviewer
        except ERRORS:
            continue  # Expired authority is a gap, never a new role.
    return receivers


def project(state, *, current=False, reconciled=None, receiver_scope=None):
    """Project exactly the loaded/saved revision. Old observations are historical."""
    check = reconciled if reconciled is not None else checkpoint(state)
    handoff = round_handoff(state, check)
    progress = build_progress(state)
    issues = list(progress['issues'])
    if not current:
        issues.append('本次未取得新观察；最后登记的收件及执行通路待核实')
    task_scope = (state.get('last_reconcile') or {}).get('task_scope') if current else None
    if task_scope is not None and set(task_scope) != set(state['tasks']):
        issues.append('本轮仅核对已授权任务；其它任务保留最后登记')
    verified = current and not issues
    progress['verified'] = verified
    progress['scope'] = progress['scope'] if current else 'last_recorded'
    progress['issues'] = issues
    if not verified:
        progress['counts'] = {key: None for key in progress['counts']}
        for row in progress['tasks']:
            row['certainty'] = 'last_recorded'

    receivers = task_receivers(state)
    if receiver_scope is not None:
        receivers = {key: owner for key, owner in receivers.items() if key in receiver_scope}
    delivery_owner = state.get('delivery', {}).get('agent_id')
    if not delivery_owner and len(set(receivers.values())) == 1:
        delivery_owner = next(iter(receivers.values()))
    owner_tasks = (list(state['delivery']['task_ids']) if state.get('delivery') else
                   [key for key, owner in receivers.items() if owner == delivery_owner])
    cleanup_pending = False
    for row in progress['tasks']:
        task_id = row['task_id']
        task = state['tasks'][task_id]
        members = [t for key, t in state['tasks'].items()
                   if t.get('progress', {}).get('business_task_id', key) == task_id]
        ended = all(t['phase'] in ('completed', 'cancelled') for t in members)
        cleanup = [(t.get('cleanup') or {}).get('status') for t in members]
        cleanup_status = ('cleanup_completed' if all(s == 'cleanup_completed' for s in cleanup) else
                          'pending_cleanup' if 'pending_cleanup' in cleanup else
                          'unknown' if ended else 'not_due')
        # Missing cleanup records never prove release, including old completed tasks.
        cleanup_pending |= cleanup_status in ('pending_cleanup', 'unknown')
        row['development'] = dict(status=task['implementation'], self_test=task['self_test'])
        row['delivery'] = dict(status=task['phase'], quality_review=task['quality_review'],
                               integration=task['integration']['status'], merge=deepcopy(task.get('merge')))
        row['cleanup'] = dict(status=cleanup_status, owner=delivery_owner if task_id in owner_tasks else None,
                              next_step='按既有授权保全并核对资源清理' if cleanup_status != 'cleanup_completed' else '已登记清理完成')
        if task['phase'] == 'cancelled':
            row.update(category='completed', stage='已取消，后续按原生命周期核对')

    item = None
    active = state['items'].get(state['active_human_item'])
    if active and not readiness(state, active):
        item = presentation(state, active)
    elif handoff['kind'] == 'human_item':
        item = check.get('next') or presentation(state, next_item(state))
    # This is a proposal to present, never a wait, acceptance or activation.
    visible = {k: v for k, v in (item or {}).items() if k in (
        'id', 'kind', 'title', 'prompt', 'options', 'entry', 'steps', 'expected',
        'request_text', 'recipient', 'required_materials', 'status')}
    technical = bool(issues or cleanup_pending or check.get('delivery_evidence_issues') or
                 any(row['category'] not in ('in_progress', 'queued', 'external_wait', 'completed')
                     for row in progress['tasks']))
    release = state.get('release_plan')
    technical |= bool(release and release['completed_steps'] != release['actions'])
    waiting = any(t['phase'] not in ('completed', 'cancelled') for t in state['tasks'].values())
    if item:
        action = dict(kind='needs_user', owner=state['coordinator_id'], role='main',
                      reason='保持当前人事项' if active else '按原优先级及FIFO返回一项就绪事项',
                      next_step='由主会话实际呈现；呈现后沿用next/wait，答复后resolve-next')
    elif technical:
        action = dict(kind='needs_delivery', owner=delivery_owner, role='delivery',
                      reason='观察或交付依据待核对' if issues else '仍有交付或清理工作',
                      next_step='由当前授权集成者核对收件、执行通路及当前报告' if issues else
                                '由当前授权集成者按既有门禁接续交付与清理')
    elif waiting:
        action = dict(kind='waiting', owner=delivery_owner, role='delivery',
                      reason='已登记执行、排队或外部等待', next_step='等待本次执行或资料结果，下次工作时有限收件')
    else:
        action = dict(kind='no_pending_work', owner=None, role=None,
                      reason='本轮覆盖范围内没有待处理工作', next_step='本轮结束，后续新事实在下次入口核对')
    if action['role'] == 'delivery' and not delivery_owner:
        action['next_step'] = '先核实当前授权集成者及收件通路；不由主会话冒用集成身份'
    action['task_ids'] = owner_tasks if action['role'] == 'delivery' else []
    if action['role'] == 'delivery' and delivery_owner and set(owner_tasks) != set(state['tasks']):
        action['next_step'] += '，仅接续已授权任务，其它任务责任待核实'
    labels = dict(pending_cleanup='待清理', cleanup_completed='已登记清理完成', unknown='清理待核实', not_due='完成后按门禁清理')
    development_labels = dict(pending='待开工', running='执行已登记', queued='排队', blocked='受阻', completed='已交付')
    lines = [('需要你处理：'+str(visible.get('title', '当前事项'))+'。') if item else '本轮没有已核实可递交的用户事项。']
    lines.append(action['next_step']+'。')
    for row in progress['tasks']:
        prefix = '' if verified else '最后登记：'
        lines.append(f"{row['title']}：{prefix}{row['stage']}；开发：{development_labels[row['development']['status']]}；"
                     f"审核/合入：{row['stage']}；{labels[row['cleanup']['status']]}。")
    if not verified:
        lines.append('当前收件覆盖及执行状态待核实。')
    elif not progress['tasks']:
        lines.append('当前没有已登记的业务任务。')
    text = '\n'.join(lines)
    progress['text'] = text
    audit = state.get('last_reconcile') or {}
    notification = deepcopy(check.get('notify_main'))
    if notification and notification['kind'] == 'batch_complete':
        notification.update(scope='development_delivery', cleanup_pending=cleanup_pending,
                            all_work_finished=action['kind']=='no_pending_work')
    from stage_feedback import pending
    notifications = pending(state)
    if notifications['display_required']:
        text = notifications['text'] + '\n' + text
    return dict(schema='coordination-work-cycle/1', revision=state['revision'], summary=summary(state),
        user_item=visible or None, human_focus=state['active_human_item'], delivered_to_user=False,
        next_action=action, progress=progress, text=text,
        notifications=notifications, display_required=notifications['display_required'],
        allow_end=check['allow_end'] and not notifications['display_required'],
        all_work_finished=action['kind']=='no_pending_work' and not notifications['display_required'],
        observation=dict(current=current, verified=verified,
            scope=check.get('scope', 'ledger_only') if current else 'last_recorded',
            source=audit.get('observation'), inbox_mode=audit.get('mode'),
            observed_task_ids=list(audit.get('workers', {})), task_scope=task_scope, issues=issues),
        reconciliation=deepcopy(audit) or None,
        notify_main=notification, idle_wakeup=False,
        limitation='Point-in-time coverage only; caller assertions are not host authentication. No automatic execution or cleanup.')


def run(runtime, actor_id, *, observation_ref=None, collect=None, task_ids=None):
    require(isinstance(actor_id, str) and actor_id.strip(), 'actual nonempty caller identity required')
    state = runtime._load()  # Recover the existing project; never initialize/reset.
    delivery = state.get('delivery', {}).get('agent_id')
    receivers = task_receivers(state)
    if task_ids is not None:
        require(not delivery and task_ids and len(task_ids)==len(set(task_ids)) and
            set(task_ids).issubset(state['tasks']), 'explicit existing task-receiver scope required; global cycle is separate')
        receivers = {key: owner for key, owner in receivers.items() if key in task_ids}
    require(actor_id in (state['coordinator_id'], delivery) or actor_id in receivers.values(),
            'current main/delivery role or approved task receiver required')
    require(observation_ref is None or collect is None, 'provide observation or trusted collector, not both')
    if observation_ref is not None or collect is not None:
        scoped = not delivery and bool(state.get('runtime', {}).get('observed_dispatches'))
        scope = [key for key, owner in receivers.items() if owner == actor_id] if scoped else None
        if scoped:
            require(scope and actor_id != state['coordinator_id'], 'approved task-scoped receiver required; main stays read-only')
        else:
            runtime._actor(state, actor_id)
        if collect is not None:
            # Host-owned callback collects already-received facts once. No JSON code/tool authority.
            observation_ref = collect(dict(project_id=runtime.project_id, coordinator_id=state['coordinator_id'],
                recipient_agent_id=actor_id, based_on_revision=state['revision'],
                task_scope=scope, tasks={key: deepcopy(task) for key, task in state['tasks'].items()
                                        if scope is None or key in scope}))
        if observation_ref is not None:
            with writer_lock(runtime.path):
                state = runtime._load()
                if scoped:
                    current_receivers = task_receivers(state)
                    require(not state.get('delivery') and all(current_receivers.get(key)==actor_id for key in scope),
                            'task receiver authority changed during collection')
                else:
                    runtime._actor(state, actor_id)
                state, result = closeout(state, load_bound(observation_ref), observation_ref,
                                        actor_id=actor_id, task_scope=scope)
                write_atomic(runtime.path, state)
                view = project(state, current=True, reconciled=result, receiver_scope=scope)
                return dict(view, ledger=str(runtime.path), project_id=runtime.project_id)
    return dict(project(state, receiver_scope=task_ids), ledger=str(runtime.path), project_id=runtime.project_id)
