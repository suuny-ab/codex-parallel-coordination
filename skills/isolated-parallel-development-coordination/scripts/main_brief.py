"""Bounded, read-only views of one validated work-cycle projection (stdlib)."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from evidence import fingerprint, require

DEFAULT_BUDGET = 8192
MIN_BUDGET = 4096
MAX_BUDGET = 65536


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def options(view, budget, cursor, snapshot, detail, observing=False):
    require(view in ('full', 'brief'), 'view must be full or brief')
    require(type(budget) is int and MIN_BUDGET <= budget <= MAX_BUDGET,
            'budget must be an integer from 4096 to 65536 bytes')
    require(not (cursor and snapshot), 'use cursor or snapshot, not both')
    require(not detail or cursor or snapshot, 'detail requires its snapshot or cursor')
    require(not (cursor or snapshot or detail) or view == 'brief', 'continuation requires brief view')
    require(not observing or not (cursor or snapshot or detail), 'continuation is read-only; do not resubmit observations')


def pointer(value):
    """Pointers select a view, never grant identity, observation or write authority."""
    parts = value.split(':')
    require(len(parts) in (2, 4) and parts[0] in ('l', 'c') and len(parts[1]) == 64 and
            all(c in '0123456789abcdef' for c in parts[1]), 'invalid brief snapshot/cursor')
    offset = 0
    if len(parts) == 4:
        require(parts[2].isdigit() and len(parts[2]) <= 12, 'invalid brief offset')
        offset = int(parts[2])
    return ':'.join(parts[:2]), offset, parts[3] if len(parts) == 4 else None


def compact(value, limit=120):
    """Clipping is explicit; the shared snapshot and detail selector retain all bytes."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + '…[detail]'
    if isinstance(value, dict):
        return {k: compact(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [compact(v, limit) for v in value]
    return value


def notification_rows(events):
    rows = []
    ordinary = {}
    for index, event in enumerate(events):
        # Only successful ordinary stages share a slot. Milestones, blockers,
        # failures and uncertain outcomes retain independent rows and identities.
        group = event['kind'] in ('development', 'review') and event['outcome'] in ('delivered', 'passed')
        slot = (event['task_id'], event['technical_owner'])
        if group and slot in ordinary:
            row = rows[ordinary[slot]]
            row['event_indexes'].append(index)
            row['count'] += 1
            row['statuses'][event['status']] = row['statuses'].get(event['status'], 0)+1
            if event['ledger_revision'] >= row['ledger_revision']:
                row.update(kind=event['kind'], text=event['text'], ledger_revision=event['ledger_revision'])
            if row['status'] != event['status']:
                row['status'] = 'mixed_unconfirmed'
            continue
        row = dict(task_id=event['task_id'], kind=event['kind'], text=event['text'],
                   status=event['status'], owner=event['technical_owner'], scope=event.get('scope'),
                   ledger_revision=event['ledger_revision'], count=1, event_indexes=[index],
                   statuses={event['status']: 1},
                   attention=event['kind'] == 'blocker' and event['outcome'] != 'resolved' or
                       event['kind'] == 'cleanup' and event['outcome'].get('status') != 'all' or
                       event['kind'] in ('development', 'review') and not group)
        if group:
            ordinary[slot] = len(rows)
        else:
            row['outcome'] = deepcopy(event['outcome'])
        rows.append(row)
    return rows


def task_rows(state, full):
    """Reuse validated notification facts; component outcomes never imply each other."""
    from stage_feedback import facts, cleanup_fact
    from stage_feedback import owners
    from progress import task_row
    receivers = owners(state)
    rows, details = [], []
    grouped = {row['task_id']: row for row in full['progress']['tasks']}
    for key, task in state['tasks'].items():
        progress = deepcopy(grouped.get(key))
        if progress is None:
            progress = task_row(state, key, [key], (state.get('last_reconcile') or {}).get('workers', {}))
            if not full['observation']['verified']:
                progress['certainty'] = 'last_recorded'
        current = facts(state, key)
        cleanup = cleanup_fact(state, key)
        ended = task['phase'] in ('completed', 'cancelled')
        cleanup_status = (task.get('cleanup') or {}).get('status', 'unknown' if ended else 'not_due')
        if ended and cleanup_status == 'cleanup_completed' and cleanup['outcome'] == 'all':
            continue
        owner = receivers.get(key)
        if progress['category'] in ('in_progress', 'queued'):
            owner = task.get('owner_agent_id')
        followup = task.get('blocker_followup') or {}
        if followup.get('delivery_revision') == task['delivery_revision']:
            owner = followup.get('owner') or owner
        row = dict(task_id=key, title=progress['title'], stage=progress['stage'], category=progress['category'],
                   certainty=progress['certainty'], blocked=bool(task.get('blocker') or task['implementation'] == 'blocked' or
                       progress['category'] == 'technical_blocked'),
                   owner=owner, next_step=(followup.get('action') if followup.get('delivery_revision') == task['delivery_revision'] else None)
                       or progress['next_step'], closeout=ended,
                   blocker=deepcopy(task.get('blocker')),
                   development=dict(status=task['implementation'], self_test=task['self_test']),
                   merge='merged' if any(f['kind'] == 'merge' for f in current) else
                       'last_recorded_unverified' if task.get('merge') else 'not_recorded',
                   installation='verified' if any(f['kind'] == 'installation' for f in current) else 'unknown',
                   cleanup=dict(status=cleanup_status, outcome=cleanup['outcome'], resources=cleanup['resources']),
                   cleanup_attention=ended and cleanup['outcome'] != 'all' or any(r['status'] == 'failed' for r in cleanup['resources']),
                   issues=progress['issues'])
        details.append(dict(progress=progress, task=task, validated_facts=current, cleanup=cleanup))
        row['detail'] = 'task/'+str(len(details)-1)
        rows.append(row)
    return rows, details


def render(state, full, *, actor_id, task_ids=None, budget=DEFAULT_BUDGET, cursor=None, snapshot=None, detail=None,
           replay=False):
    """All selection, readiness, pending and evidence checks already ran in project()."""
    budget -= 1  # Include the CLI's trailing newline in the transport ceiling.
    notices = notification_rows(full['notifications']['items'])
    tasks, task_details = task_rows(state, full)
    details = dict(user=full['user_item'], notifications=full['notifications'], tasks=task_details,
                   observation=full['observation'], full=full)
    binding = fingerprint(dict(state=state, full=full, task_details=task_details, actor=actor_id, task_ids=task_ids))
    token = ('c:' if full['observation']['current'] else 'l:') + binding
    supplied, offset, selector = pointer(cursor or snapshot) if cursor or snapshot else (token, 0, None)
    require(supplied == token, 'brief source/revision/evidence changed; refresh instead of joining snapshots')
    require(selector is None or selector == (detail or 'brief'), 'cursor belongs to a different detail/brief section')
    source = dict(snapshot=token, ledger_revision=state['revision'], ledger_fingerprint=fingerprint(state),
                  scope='last_recorded' if replay else full['observation']['scope'],
                  current=full['observation']['current'] and not replay, snapshot_replay=replay,
                  verified=full['observation']['verified'],
                  verification_scope='snapshot_evidence_only',
                  checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  observation=compact(full['observation']['source'], 160),
                  issues=dict(count=len(full['observation']['issues']), detail='observation'))
    base = dict(schema='coordination-main-brief/1', revision=state['revision'], project_id=state['project']['id'],
                source=source, delivered_to_user=False, idle_wakeup=False)
    if detail:
        if detail.startswith('task/'):
            number = detail[5:]
            require(number.isdigit() and int(number) < len(task_details), 'unknown task detail')
            value = task_details[int(number)]
        elif detail.startswith('notification/'):
            number = detail[13:]
            require(number.isdigit() and int(number) < len(notices), 'unknown notification detail')
            value = [full['notifications']['items'][i] for i in notices[int(number)]['event_indexes']]
        else:
            require(detail in details, 'unknown brief detail')
            value = details[detail]
        content = encoded(value).decode('utf-8')
        require(offset < len(content) or offset == 0, 'detail offset out of range')
        # JSON escapes count towards the transport budget. Binary search a Unicode
        # character boundary; each returned page remains complete JSON.
        def page(length):
            end = offset + length
            return dict(base, detail=detail, encoding='json-text', content=content[offset:end],
                        remaining_characters=len(content)-end,
                        next_cursor=token+':'+str(end)+':'+detail if end < len(content) else None)
        low, high = 0, len(content)-offset
        while low < high:
            middle = (low+high+1)//2
            if len(encoded(page(middle))) <= budget:
                low = middle
            else:
                high = middle-1
        result = page(low)
        require(low or not content, 'brief metadata exceeds budget')
        return result

    pending = sum(i['status'] in ('queued', 'active', 'waiting_user') for i in state['items'].values())
    user = full['user_item']
    visible_user = {key: user[key] for key in ('id', 'kind', 'title', 'status') if key in user} if user else None
    if visible_user:
        visible_user['detail'] = 'user'
        visible_user['prompt'] = compact(user.get('prompt'), 160)
    result = dict(base,
        user_items=dict(item=compact(visible_user), pending_total=pending, detail='user'),
        notifications=dict(items=[], received_total=len(full['notifications']['items']), total=len(notices),
                           requires_reply=False, display_required=full['display_required'], detail='notifications'),
        tasks=dict(items=[], total=len(tasks), closeout_total=sum(t['closeout'] for t in tasks), detail='tasks'),
        next_action=dict(compact({k: v for k, v in full['next_action'].items() if k != 'task_ids'}),
                         task_ids=dict(count=len(full['next_action']['task_ids']), detail='full'), detail='full'),
        allow_end=full['allow_end'], all_work_finished=full['all_work_finished'])
    notification_records = [('notifications', dict(row, detail='notification/'+str(i))) for i, row in enumerate(notices)]
    notification_records.sort(key=lambda record: not record[1]['attention'])
    task_records = [('tasks', row) for row in sorted(tasks, key=lambda row: (row['closeout'], not row['blocked']))]
    # Give both changing progress and unfinished work space on the first page,
    # even when many historical closeout notices remain unconfirmed.
    records = []
    for index in range(max(len(task_records), len(notification_records))):
        if index < len(task_records):
            records.append(task_records[index])
        if index < len(notification_records):
            records.append(notification_records[index])
    require(offset < len(records) or offset == 0, 'brief offset out of range')

    def pagination(end):
        remaining = records[end:]
        result['page'] = dict(offset=offset, returned=end-offset, remaining=len(remaining),
            remaining_notifications=sum(k == 'notifications' for k, _ in remaining),
            remaining_tasks=sum(k == 'tasks' for k, _ in remaining),
            remaining_attention=sum(bool(r.get('attention') or r.get('blocked') or r.get('cleanup_attention')) for _, r in remaining),
            next_cursor=token+':'+str(end)+':brief' if remaining else None)
        result['allow_end'] = full['allow_end'] and not remaining
        result['all_work_finished'] = full['all_work_finished'] and not remaining

    pagination(offset)
    require(len(encoded(result)) <= budget, 'brief metadata exceeds budget; read full diagnostic or increase budget')
    for index in range(offset, len(records)):
        section, row = records[index]
        small = compact(row)
        # Unbounded lists are detail-only, with exact counts; never silently slice.
        if section == 'notifications':
            small['event_indexes'] = dict(count=len(row['event_indexes']), detail=small['detail'])
            if isinstance(small.get('outcome'), (dict, list)):
                small['outcome'] = dict(status=row['outcome'].get('status') if isinstance(row['outcome'], dict) else None,
                                        detail=small['detail'])
        else:
            components = {}
            for resource in row['cleanup']['resources']:
                key = resource['kind']+':'+resource['status']
                components[key] = components.get(key, 0)+1
            small['cleanup']['resources'] = dict(count=len(row['cleanup']['resources']), components=components, detail=small['detail'])
            small['issues'] = dict(count=len(row['issues']), detail=small['detail'])
            if isinstance(small['blocker'], (dict, list)):
                small['blocker'] = dict(present=True, detail=small['detail'])
        result[section]['items'].append(small)
        pagination(index+1)
        if len(encoded(result)) > budget:
            result[section]['items'].pop()
            pagination(index)
            if index == offset:
                # Even exceptionally large identifiers/component sets have a
                # navigable row. Exact content remains in this snapshot's detail.
                small = {k: small[k] for k in ('task_id', 'kind', 'stage', 'blocked', 'owner', 'detail', 'attention', 'cleanup_attention') if k in small}
                small['summary_in_detail'] = True
                result[section]['items'].append(small)
                pagination(index+1)
                require(len(encoded(result)) <= budget, 'brief metadata exceeds budget')
            break
    require(result['page']['returned'] or not records, 'one brief row exceeds budget; increase budget or read its detail')
    return result
