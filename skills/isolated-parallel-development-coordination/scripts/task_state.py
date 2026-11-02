"""Role-limited events in CORE's ledger. Context/callbacks are trusted host sideband.

No actor, role, profile, producer capability or observation is read from a command.
This is an in-process plugin API, not an OS sandbox or a new authorization service.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from coordination import RIGHTS, enqueue, task_base, writer_lock
from evidence import artifact, callback_result, fingerprint, git, io_path, load_bound, require, snapshot
from stage_feedback import store as feedback_store

VERSIONED = {'self-test', 'deliver-pr', 'review', 'technical-acceptance',
             'user-acceptance', 'source-changed', 'rework', 'merge-verified'}
COMMITTED = {'deliver-pr', 'review', 'technical-acceptance', 'user-acceptance', 'merge-verified'}
FIELDS = {
    'register': {'spec', 'scope', 'acceptance', 'acceptance_type', 'integrator_id', 'rights', 'user_reference', 'deliverables'},
    'authorize': {'rights', 'user_reference'}, 'cancel': {'reason', 'user_reference'},
    'progress': {'summary'}, 'problem': {'summary'},
    'self-test': {'status', 'command', 'evidence'}, 'deliver-pr': {'pr', 'summary', 'evidence'},
    'review': {'status', 'summary', 'evidence'}, 'technical-acceptance': {'status', 'summary', 'evidence'},
    'user-acceptance': {'status', 'user_reference'}, 'source-changed': {'summary'},
    'rework': {'summary'}, 'merge-verified': {'operation_id'},
    'user-question': {'title', 'prompt', 'options'},
    'send-intent': {'notification_id', 'attempt_id'},
    'send-result': {'notification_id', 'attempt_id', 'status', 'evidence'},
    'notification-received': {'notification_id'}, 'notification-displayed': {'notification_id'},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def receiver(state, task_id):
    task = state['tasks'][task_id]
    if task.get('state_contract'):
        return task['state_contract']['integrator_id']
    delegation = state.get('delivery') or {}
    if task_id in delegation.get('task_ids', []):
        return delegation['agent_id']
    bound = (state['runtime'].get('observed_dispatches', {}).get(task_id, {}).get('reviewer_id') or
             (task.get('cli_dispatch') or {}).get('reviewer_id'))
    if bound:
        return bound
    # A small authenticated approval reference, never a report/history traversal.
    intake = state['runtime'].get('intake', {})
    for plan in reversed(list(intake.get('plans', {}).values())):
        for item in plan['items']:
            if item['task_id'] == task_id:
                approval = intake.get('approvals', {}).get(fingerprint(item))
                if approval and approval['decision'] == 'approved':
                    return load_bound(approval['reference']).get('integration_agent')
    return None



def scope(state, task_id, actor):
    task = state['tasks'][task_id]
    require(actor and actor in (state['coordinator_id'], task['owner_agent_id'], receiver(state, task_id)),
            'caller outside this task responsibility')
    return task


def events(state):
    return state['runtime'].setdefault('task_state_events', {})


def notify(state, key, kind, text, recipient, version=None, *, recorded_revision=None):
    """One canonical feedback store; technical events never enter human items."""
    task = state['tasks'][key]
    identity = dict(task_id=key, kind=kind, source=version, text=text, recipient_id=recipient,
                    delivery_revision=task['delivery_revision'])
    event_id = fingerprint(identity)
    data = feedback_store(state)
    data['events'].setdefault(event_id, dict(identity, id=event_id, state_tool=True, status='pending',
        technical_owner=receiver(state, key), outcome=kind, scope='registered_fact', evidence=[],
        ledger_revision=recorded_revision if recorded_revision is not None else state['revision']+1, recorded_at=now()))
    return event_id


def source_version(runtime, task, target_sha=None, *, committed=False):
    require(task.get('worktree') and task.get('branch'), 'actual development resource not registered')
    root = Path(task['worktree'])
    require(git(root, '-c', 'core.longpaths=true', 'branch', '--show-current') == task['branch'],
            'actual source branch differs from task')
    head = git(root, '-c', 'core.longpaths=true', 'rev-parse', 'HEAD')
    target = target_sha or git(runtime.project_root, 'rev-parse', 'refs/heads/'+runtime._load()['project']['main_branch'])
    require(git(root, 'merge-base', head, target) == target, 'author must synchronize actual main')
    def clean():
        return not git(root, '-c', 'core.longpaths=true', '--no-optional-locks', 'status',
                       '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
    if committed:
        require(clean(), 'committed source required; staged, unstaged or untracked source cannot be promoted')
    version = dict(head_sha=head, target_sha=target, source_digest=snapshot(root)['digest'])
    if committed:
        require(clean() and git(root, 'rev-parse', 'HEAD') == head, 'committed source changed during observation')
    return version


def reset_results(task):
    if task.get('state_results'):
        task.setdefault('state_result_history', []).append(deepcopy(task['state_results']))
    task.update(self_test='pending', quality_review='pending', review=None,
        technical_acceptance=None, user_acceptance='pending', acceptance_record=None,
        integration=dict(status='pending'), implementation='pending', blocker=None)
    task.pop('state_results', None)


def result_record(command, actor, observed_at):
    return dict(version=deepcopy(command['version']), actor_id=actor, observed_at=observed_at,
        status=command.get('status'), summary=command.get('summary'),
        command=command.get('command'), evidence=deepcopy(command.get('evidence', [])))


class TaskState:
    """Bind once from the installation's Runtime and actual host identity.

    observe_source/observe_merge/verify_display and user_origin come only from
    the trusted caller, like HostAdapter.invoke. Never deserialize them from files.
    Construction does not initialize a ledger or infer a project from cwd.
    """
    def __init__(self, runtime, actor_id, *, observe_source=None, observe_merge=None, verify_display=None):
        runtime._load()  # Existing fixed project only. No fallback initialization.
        require(isinstance(actor_id, str) and actor_id, 'actual host caller required')
        self.runtime, self.actor = runtime, actor_id
        self.custom_source = observe_source
        self.observe_source = observe_source or (lambda task: source_version(runtime, task))
        self.observe_merge, self.verify_display = observe_merge, verify_display

    def query(self, task_id=None):
        state = self.runtime._load()
        keys = [task_id] if task_id else [k for k, t in state['tasks'].items() if
            self.actor in (state['coordinator_id'], t['owner_agent_id'], receiver(state, k))]
        rows = {}
        for key in keys:
            task = scope(state, key, self.actor)
            rows[key] = deepcopy(task)
            rows[key]['resources_detail'] = [deepcopy(r) for r in state['runtime'].get('resources', {}).values()
                                           if r['task_id'] == key]
            rows[key]['archive'] = deepcopy(task.get('archive', {'status': 'not_registered'}))
        return dict(project_id=self.runtime.project_id, ledger=str(self.runtime.path), revision=state['revision'],
                    tasks=rows, freshness='last_registered', queried_at=now(), host_verified_now=False)

    def observe(self, task_id):
        state = self.runtime._load(); task = scope(state, task_id, self.actor)
        version = self.observe_source(deepcopy(task))  # Git/files/host outside writer lock.
        return dict(version=version, observed_at=now(), based_on_revision=state['revision'])

    def pending(self):
        state = self.runtime._load()
        tasks = {k for k in state['tasks'] if self.actor in
            (state['coordinator_id'], state['tasks'][k]['owner_agent_id'], receiver(state, k))}
        values = [deepcopy(e) for e in feedback_store(state)['events'].values() if e['task_id'] in tasks and
                  e['recipient_id'] == self.actor and e['status'] not in ('displayed', 'superseded') and
                  not (e.get('audience')=='technical' and e['status']=='received')]
        return dict(revision=state['revision'], technical=[v for v in values if v.get('audience') == 'technical'],
            notifications=[v for v in values if v.get('audience') != 'technical'],
            human_items=[deepcopy(i) for i in state['items'].values() if self.actor == state['coordinator_id'] and
                         i['status'] not in ('closed', 'superseded')], freshness='last_registered', idle_wakeup=False)

    def record(self, command, *, user_origin=None):
        command = deepcopy(command)
        action = command.get('action')
        require(action in FIELDS and set(command) <= FIELDS[action] | {
            'action', 'task_id', 'event_id', 'expected_revision', 'version'}, 'unknown action or caller-supplied authority/fields')
        require(all(command.get(k) is not None for k in ('task_id', 'event_id', 'expected_revision')) and
                type(command['expected_revision']) is int, 'task, stable event and input revision required')
        state = self.runtime._load()
        self._authorize(state, command, user_origin)
        digest = fingerprint(dict(command={k:v for k,v in command.items() if k!='expected_revision'}, actor=self.actor, user_origin=user_origin))
        prior = events(state).get(command['event_id'])
        if prior:
            require(prior['fingerprint'] == digest, 'conflicting duplicate state event')
            return dict(deepcopy(prior['result']), duplicate=True)
        if command['expected_revision'] != state['revision']:
            return self._conflict(state, command)
        observed, observed_at, external = None, None, None
        if action in VERSIONED:
            task = deepcopy(state['tasks'][command['task_id']])
            observed = (self.observe_source(task) if self.custom_source else source_version(
                self.runtime, task, task.get('source_version', {}).get('target_sha') if action == 'merge-verified' else None,
                committed=action in COMMITTED))
            observed_at = now()
            require(command.get('version') == observed and set(observed) == {'head_sha', 'target_sha', 'source_digest'} and
                    all(isinstance(v, str) and v for v in observed.values()), 'current actual source version required')
        for evidence in command.get('evidence', []):
            artifact(evidence)  # Shallow referenced result only; never report graphs.
        if action == 'register':
            artifact(command['spec'])
            require(isinstance(command.get('deliverables', []), list), 'explicit deliverables list required')
            for item in command.get('deliverables', []):
                require(set(item) == {'path', 'sha256'}, 'deliverables are exact file references')
                artifact(item)
        capture_binding = dict(project_id=self.runtime.project_id, event_id=command['event_id'], fingerprint=digest)
        saved_result = callback_result(self.runtime.directory, capture_binding) if action in ('merge-verified','notification-displayed') else None
        if action == 'merge-verified':
            require(callable(self.observe_merge), 'trusted actual merge observer required')
            external = load_bound(saved_result)['result'] if saved_result else self.observe_merge(
                deepcopy(state['tasks'][command['task_id']]), command['operation_id'])
        if action == 'notification-displayed':
            require(callable(self.verify_display), 'actual host display verifier required')
            event = feedback_store(state)['events'][command['notification_id']]
            external = load_bound(saved_result)['result'] if saved_result else self.verify_display(deepcopy(event))
            require(external.get('visible') is True and external.get('message_id') and
                    external.get('notification_id') == event['id'] and external.get('recipient_id') == self.actor,
                    'accepted is not actual visible display')
        if external is not None and saved_result is None:
            try:
                saved_result = callback_result(self.runtime.directory, capture_binding, result=external, capture=True)
            except OSError as error:
                return dict(status='registration_pending', actual_result=external, event_id=command['event_id'],
                    command=command, reason=str(error), next_action='preserve returned result; reconcile original event')
        with ExitStack() as transaction:
            try:
                transaction.enter_context(writer_lock(self.runtime.path))
            except ValueError as error:
                if not str(error).startswith('writer lock exists;'):
                    raise
                return dict(self._conflict(self.runtime._load(), command, external), reason='writer_busy; do not steal lock')
            state = self.runtime._load()
            self._authorize(state, command, user_origin)
            prior = events(state).get(command['event_id'])
            if prior:
                require(prior['fingerprint'] == digest, 'conflicting duplicate state event')
                return dict(deepcopy(prior['result']), duplicate=True)
            if command['expected_revision'] != state['revision']:
                return dict(self._conflict(state, command, external), actual_result_ref=saved_result)
            result = self._apply(state, command, observed_at, external)
            task = state['tasks'][command['task_id']]
            task['last_registered_at'] = now()
            if observed_at: task['source_observed_at'] = observed_at
            result.update(status='recorded', revision=state['revision']+1, task_id=command['task_id'])
            events(state)[command['event_id']] = dict(fingerprint=digest, result=deepcopy(result), recorded_at=now())
            try:
                self.runtime._save(state, dict(op='task-state', action=action, task_id=command['task_id'],
                                              event_id=command['event_id'], actor_id=self.actor))
            except OSError as error:
                return dict(status='registration_pending', event_id=command['event_id'], command=command,
                            actual_result=external, actual_result_ref=saved_result, reason=str(error), next_action='reconcile original event; do not repeat external action')
            return result

    def _conflict(self, state, command, external=None):
        return dict(status='conflict', revision=state['revision'], expected_revision=command['expected_revision'],
                    event_id=command['event_id'], actual_result=external, next_action='read current record and reconcile same event')

    def _authorize(self, state, c, origin):
        action, key = c['action'], c['task_id']
        if action == 'register':
            require(self.actor == state['coordinator_id'], 'original main required')
        else:
            task = scope(state, key, self.actor)
            if action in ('authorize', 'cancel', 'user-acceptance', 'user-question'):
                require(self.actor == state['coordinator_id'], 'original main required')
            elif action in ('progress', 'problem', 'self-test', 'deliver-pr'):
                require(self.actor == task['owner_agent_id'] and self.actor != receiver(state, key), 'original task author required')
            elif action in ('review', 'technical-acceptance', 'rework', 'merge-verified'):
                require(self.actor == receiver(state, key) and self.actor != task['owner_agent_id'], 'designated independent integrator required')
            if action in ('send-intent', 'send-result', 'notification-received', 'notification-displayed'):
                event = feedback_store(state)['events'][c['notification_id']]
                require(event['task_id'] == key and event.get('state_tool'), 'notification belongs to another task/protocol')
                sender = task['owner_agent_id'] if event.get('audience') == 'technical' and event['recipient_id'] == receiver(state, key) else receiver(state, key)
                require(self.actor == (sender if action.startswith('send-') else event['recipient_id']), 'actual notification sender/recipient required')
            elif task['phase'] in ('completed', 'cancelled'):
                require(action == 'cancel' or c['event_id'] in events(state), 'ended task is immutable')
            elif task.get('cancellation_request'):
                require(action in ('cancel', 'merge-verified') or c['event_id'] in events(state), 'cancel requested; stop work and reconcile existing operations')
        if action in ('register', 'authorize', 'cancel', 'user-acceptance'):
            require(origin and origin.get('role') == 'user' and origin.get('conversation_id') == state['coordinator_id'] and
                    origin.get('reference') == c.get('user_reference') and c.get('user_reference'), 'actual original user decision sideband required')

    def _apply(self, state, c, observed_at, external):
        action, key = c['action'], c['task_id']
        if action == 'register':
            require(key not in state['tasks'] and c['scope'] and c['acceptance'] and
                    c['integrator_id'] != self.actor and c['acceptance_type'] in ('delivery_verified', 'user_required'),
                    'unique approved task, scope, independent integrator and acceptance policy required')
            task = task_base(None, self.runtime.project_root, None, [])
            task.update(worktree=None, phase='backlog', acceptance_type=c['acceptance_type'],
                acceptance_policy_explicit=True, requirements={k: deepcopy(c[k]) for k in ('scope', 'acceptance', 'user_reference', 'acceptance_type')},
                start_reference=c['user_reference'], report_task_id=key,
                progress=dict(title=key, business_task_id=key, role='implementation', facts=[], next_step='await actual resource allocation'),
                state_contract=dict(schema='task-state/1', integrator_id=c['integrator_id'], spec=c['spec'],
                    deliverables=deepcopy(c.get('deliverables', []))),
                worker_run=dict(status='queued', owner_agent_id=None, expected_delivery_revision=1, mode='implementation'))
            state['tasks'][key] = task
            self._rights(task, c)
            return dict(next_owner=c['integrator_id'], resource_status='unallocated')
        task = state['tasks'][key]
        if action in VERSIONED:
            version = c['version']
            if action in ('self-test', 'source-changed'):
                if task.get('source_version') != version:
                    reset_results(task)
                    task['source_version'] = deepcopy(version)
                    task['phase'] = 'developing'
                    for e in feedback_store(state)['events'].values():
                        if e['task_id'] == key and e.get('state_tool') and e['status'] != 'displayed':
                            e['status'] = 'superseded'
            else:
                require(task.get('source_version') == version, 'source/main changed; original author must retest and redeliver')
        if action == 'authorize':
            self._rights(task, c)
        elif action == 'cancel':
            task['cancellation_request'] = dict(user_reference=c['user_reference'], reason=c['reason'], requested_at=now(),
                merge_needs_reconcile=bool(task.get('merge') or state.get('merge_reservation')))
            return dict(next_owner=receiver(state, key), actual_stop='not_observed', task_completed=task['phase']=='completed')
        elif action in ('progress', 'problem'):
            require(c['summary'], 'concrete progress/problem required')
            task.setdefault('progress', {})['last_registered'] = dict(summary=c['summary'], recorded_at=now(), actor_id=self.actor)
            if action == 'problem':
                task['blocker'] = dict(reason=c['summary'], category='technical', owner=receiver(state, key))
                return self._technical(state, key, action, c['summary'])
        elif action == 'self-test':
            require(c['status'] in ('passed', 'failed', 'not_run') and c['command'] and c['evidence'], 'actual test command, outcome and evidence required')
            reset_results(task)
            task['self_test'] = c['status']
            task.setdefault('state_results', {})['self-test'] = result_record(c, self.actor, observed_at)
            task.update(review=None, technical_acceptance=None, integration=dict(status='pending'), quality_review='pending')
        elif action == 'deliver-pr':
            require(task['self_test'] == 'passed' and c['evidence'], 'current passed author tests required')
            pr = c['pr']; require(set(pr) == {'provider', 'id', 'url'} and pr['provider'] == state['project']['provider'] and
                pr['id'] and pr['url'].startswith('https://'), 'actual original PR identity required')
            require(not task['mr'] or all(task['mr'][k] == pr[k] for k in pr), 'maintain original PR')
            require(not any(k != key and t.get('mr') and t['mr']['url'] == pr['url'] for k, t in state['tasks'].items()), 'PR belongs to another task')
            task['delivery_revision'] += 1
            task['mr'] = dict(pr, head_sha=c['version']['head_sha'], target_sha=c['version']['target_sha'])
            task.update(implementation='completed', phase='awaiting_delivery_review', blocker=None)
            task.setdefault('state_results', {})['deliver-pr'] = result_record(c, self.actor, observed_at)
            return self._technical(state, key, action, c['summary'])
        elif action in ('review', 'technical-acceptance'):
            require(task.get('state_results', {}).get('deliver-pr') and task['self_test']=='passed' and
                c['status'] in ('passed', 'failed') and c['evidence'] and c['summary'], 'current delivery and independent result required')
            record = result_record(c, self.actor, observed_at)
            task.setdefault('state_results', {})[action] = record
            if action == 'review':
                task['review'] = dict(record, reviewer_id=self.actor, **c['version'])
                task['quality_review'] = c['status']
            else:
                task['technical_acceptance'] = record
                task['integration'] = dict(status=c['status'], version=c['version'])
            if c['status'] == 'failed':
                reset_results(task)
                task.update(phase='developing', implementation='pending')
                return self._technical(state, key, 'rework', c['summary'], task['owner_agent_id'])
            accepted = task['acceptance_type']=='delivery_verified' or task['user_acceptance']=='accepted'
            task['phase'] = 'awaiting_merge' if task.get('review') and task['quality_review']=='passed' and task['integration']['status']=='passed' and accepted else 'awaiting_acceptance'
            return dict(notification_id=notify(state, key, action, c['summary'], state['coordinator_id'], c['version']))
        elif action == 'user-acceptance':
            require(task.get('state_results', {}).get('deliver-pr') and c['status'] in ('accepted', 'rejected'), 'current delivered source required')
            task['user_acceptance'] = c['status']
            task['acceptance_record'] = dict(version=c['version'], accepted_by=self.actor, user_reference=c['user_reference'])
            if c['status']=='rejected':
                task['phase']='awaiting_acceptance'
            elif task['quality_review']=='passed' and task['integration']['status']=='passed':
                task['phase']='awaiting_merge'
        elif action in ('rework', 'source-changed'):
            reset_results(task)
            task.update(phase='developing', implementation='pending', review=None, quality_review='pending',
                        technical_acceptance=None, integration=dict(status='pending'))
            return self._technical(state, key, 'rework', c['summary'], task['owner_agent_id'])
        elif action == 'merge-verified':
            require(task['phase']=='awaiting_merge' and task['quality_review']=='passed' and task['integration']['status']=='passed' and
                all(task['authorization'][r]['allowed'] for r in ('commit','push','mr','merge')), 'current acceptance/review and actual merge authority required')
            require(external and external.get('contains_source') is True and external.get('source_head')==c['version']['head_sha'] and
                external.get('previous_main')==c['version']['target_sha'] and external.get('main_head') and
                external.get('operation_id')==c['operation_id'] and external.get('task_id')==key and
                external.get('project_id')==self.runtime.project_id and external.get('scope') in ('host_verified','fixture_only'), 'actual original merge observation required')
            task.update(phase='completed', merge=dict(verified=True, head_sha=c['version']['head_sha'], target_sha=c['version']['target_sha'],
                main_sha=external['main_head'], observation=external, observed_at=now()), cleanup=dict(status='pending_cleanup', owner=self.actor))
            return dict(task_completed=True, archive_status=task.get('archive', {}).get('status','not_registered'),
                cleanup_completed=False, notification_id=notify(state, key, 'merge', '实际合入已核对，收尾单独处理', state['coordinator_id'], c['version']))
        elif action == 'user-question':
            enqueue(state, dict(id=c['event_id'], kind='decision', task_id=key, title=c['title'], prompt=c['prompt'], options=c['options']))
        else:
            return self._feedback(state, c, external)
        task['last_registered_at'] = now()
        if observed_at: task['source_observed_at'] = observed_at
        return {}

    def _rights(self, task, c):
        require(isinstance(c['rights'], dict) and set(c['rights']) <= set(RIGHTS) and
                all(type(v) is bool for v in c['rights'].values()), 'explicit bounded rights required')
        for right, allowed in c['rights'].items():
            task['authorization'][right] = dict(allowed=allowed, user_reference=c['user_reference'])

    def _technical(self, state, key, kind, summary, recipient=None):
        target = recipient or receiver(state, key)
        identity = notify(state, key, kind, summary, target, state['tasks'][key].get('source_version'))
        feedback_store(state)['events'][identity]['audience'] = 'technical'
        return dict(notification_id=identity, next_owner=target, requires_user_reply=False)

    def _feedback(self, state, c, external):
        data = feedback_store(state); event = data['events'][c['notification_id']]
        action = c['action']
        require(event['status'] != 'superseded', 'superseded feedback requires current handoff')
        if action == 'send-intent':
            attempt = c['attempt_id']; old = data['sends'].get(attempt)
            require(not old or old.get('notification_ids')==[event['id']], 'send identity differs')
            if old: return dict(send_allowed=False, transport_status=old['status'])
            require(event['status']=='pending', 'unknown send must be observed before retry')
            data['sends'][attempt] = dict(attempt_id=attempt, actor_id=self.actor, notification_ids=[event['id']],
                recipient_id=event['recipient_id'], status='intent', text=event['text'])
            event.update(status='sent_unconfirmed', attempt_id=attempt)
            return dict(send_allowed=True)
        if action == 'send-result':
            send = data['sends'][c['attempt_id']]
            require(send['actor_id']==self.actor and send['notification_ids']==[event['id']] and c['evidence'] and
                c['status'] in ('accepted','unknown','failed','not_sent'), 'bound actual transport result required')
            send.update(status=c['status'], evidence=c['evidence'])
            if event['status'] not in ('received','displayed','superseded'):
                event['status']='pending' if c['status']=='not_sent' else 'sent_unconfirmed'
            return dict(transport_status=c['status'], delivered_to_user=False, business_repeated=False)
        if action == 'notification-received':
            if event['status'] != 'displayed': event['status']='received'
        else:
            event.update(status='displayed', display=external)
        return dict(notification_status=event['status'], delivered_to_user=event['status']=='displayed')


# In-process capabilities, never accepted by the public JSON event entry.
_DISPATCH, _RESOURCE, _ARCHIVE = object(), object(), object()


def record_producer_result(state, capability, task_id, operation_id, result, *, notify_result=False):
    """Stable internal registration seam for the actual producer's verified result.

    Called in its existing transaction, after producer checks. It performs no
    business approval, I/O, resource actions or report traversal. Future archive
    and resource implementations own their own gates and resume logic.
    """
    names = {_DISPATCH: 'dispatch', _RESOURCE: 'resource', _ARCHIVE: 'archive'}
    require(capability in names and task_id in state['tasks'] and operation_id,
            'fixed internal producer capability and original operation required')
    task = state['tasks'][task_id]
    records = task.setdefault('producer_results', {})
    identity = names[capability]+':'+operation_id
    old = records.get(identity)
    if old and old['result'] == result:
        return deepcopy(old)
    row = dict(producer=names[capability], operation_id=operation_id, result=deepcopy(result),
               recorded_at=now(), ledger_revision=state['revision'])
    records[identity] = row
    if task.get('state_contract') or notify_result:
        notify(state, task_id, names[capability], names[capability]+'：结果已登记', state['coordinator_id'], result, recorded_revision=state['revision'])
    return deepcopy(row)


def capture_producer_result(state, operation):
    """Adapt existing producers' committed facts, not caller success claims."""
    original = state['runtime'].get('operations', {}).get(operation.get('operation_id'), {})
    resource = state['runtime'].get('resources', {}).get(operation.get('resource_id'), {})
    key = operation.get('task_id') or original.get('task_id') or resource.get('task_id')
    if key not in state['tasks']:
        return
    name = operation['op']; task = state['tasks'][key]
    if name=='agent-permit-result':
        record_producer_result(state,_RESOURCE,key,operation['operation_id'],dict(agent_permits=deepcopy(task['resource_permits'])),notify_result=True)
    elif name=='resource-flow':
        record_producer_result(state,_RESOURCE,key,operation['operation_id'],deepcopy(original),notify_result=True)
    elif name in ('launch-outcome', 'launch-accepted', 'reconcile-launch', 'bind-observed-dispatch', 'bind-executor'):
        record_producer_result(state, _DISPATCH, key, operation.get('attempt_id') or operation.get('operation_id') or name,
            {k: deepcopy(task.get(k)) for k in ('owner_agent_id', 'worktree', 'branch', 'worker_run')})
    elif name=='task-state' and operation.get('action')=='archive':
        record_producer_result(state, _ARCHIVE, key, operation['operation_id'], task['archive'])
    elif name in ('create-resource-outcome', 'register-resource', 'resource-cleanup-outcome', 'reconcile-resource-cleanup',
                  'cleanup-round', 'reconcile-cleanup-callback-results', 'aggregate-confirmed-agent-close', 'reconcile-close', 'release-receipt'):
        record_producer_result(state, _RESOURCE, key, operation.get('operation_id') or operation.get('resource_id') or name,
            dict(resources=[deepcopy(r) for r in state['runtime'].get('resources', {}).values() if r['task_id']==key],
                 cleanup=deepcopy(task.get('cleanup'))),notify_result=task.get('resource_contract')=='new-task/1' or
                 original.get('operation')=='close' and original.get('preservation') is None)


def archive_manifest(state, task_id):
    """Last confirmed exact list from either public task registration path.

    Routing approval was authenticated at intake; reading its recorded facts does
    not grant filesystem permission. Frozen spec/check references stay historical.
    """
    task = state['tasks'][task_id]; contract = task.get('state_contract')
    identity = task.get('routing', {}).get('requirement_id')
    record = None
    if identity:
        data = state['runtime'].get('intake', {})
        require(identity in data.get('requirements', {}) and
            sum(t.get('routing', {}).get('requirement_id')==identity for t in state['tasks'].values())==1,
            'unique canonical requirement binding required')
        matches = [i for p in data.get('plans', {}).values() for i in p['items'] if
                   i['task_id']==task_id and i['requirement_id']==identity]
        require(matches and data.get('approvals', {}).get(fingerprint(matches[-1]), {}).get('decision')=='approved',
                'canonical requirement dispatch approval required')
        record = data['requirements'][identity]
        require(record.get('start_reference') and task.get('start_reference'), 'canonical approved task start required')
        spec = record.get('current_spec', record['spec'])
        require(spec['sha256']==record['spec']['sha256'], 'canonical current spec content identity differs')
        if contract:
            require(contract['spec']==spec and contract.get('deliverables', [])==record.get('deliverables', []),
                    'STATE and canonical confirmed manifests differ')
    else:
        require(contract and contract.get('spec'), 'confirmed STATE or canonical routing contract required')
        spec = contract['spec']
    return dict(spec=deepcopy(spec), deliverables=deepcopy((contract or record).get('deliverables', [])),
                requirement_id=identity)


def archive_binding(task):
    """Frozen task facts; moving current file locators must not rewrite reviews."""
    return {k: deepcopy(task.get(k)) for k in ('owner_agent_id', 'worktree', 'branch',
        'delivery_revision', 'source_version', 'phase', 'merge', 'cancellation_request', 'cancellation', 'routing')}


def record_archive(runtime, capability, actor_id, task_id, operation_id, expected_revision, *, plan=None, result=None):
    """ARCHIVE's bounded same-ledger transaction. No file/host work under this lock.

    The actual producer supplies an internally verified plan/result. Public JSON
    events cannot call this seam or supply its in-process capability.
    """
    require(capability is _ARCHIVE and bool(plan) != bool(result), 'internal archive capability and one update required')
    with ExitStack() as transaction:
        try:
            transaction.enter_context(writer_lock(io_path(runtime.path)))
        except ValueError as error:
            if not str(error).startswith('writer lock exists;'):
                raise
            return dict(status='conflict', revision=runtime._load()['revision'], reason='writer_busy')
        state = runtime._load(); task = state['tasks'][task_id]
        require(actor_id == receiver(state, task_id) and actor_id != task['owner_agent_id'],
                'designated independent integrator required for archive')
        if state['revision'] != expected_revision:
            return dict(status='conflict', revision=state['revision'])
        operations = state['runtime']['operations']; old = operations.get(operation_id)
        if plan is not None:
            require(plan['project_id'] == runtime.project_id and plan['task_id'] == task_id and
                plan['operation_id'] == operation_id and plan['binding'] == archive_binding(task), 'archive plan binding differs')
            policy = state['project'].get('archive_policy')
            require(policy is None or policy == plan['policy'], 'fixed project archive policy differs')
            require(not old and not task.get('archive'), 'resume original archive operation; no replacement id')
            archive_manifest(state, task_id)
            state['project']['archive_policy'] = deepcopy(plan['policy'])
            task['spec_location'] = deepcopy(plan['files'][0]['original'])
            operations[operation_id] = dict(operation='archive', task_id=task_id, operation_id=operation_id,
                owner=actor_id, plan=deepcopy(plan), status='intent')
            task['archive'] = dict(schema='task-archive/1', project_id=runtime.project_id, task_id=task_id,
                operation_id=operation_id, status='intent', plan=deepcopy(plan),
                protected_directories=sorted({str(Path(f['original']['path']).parent) for f in plan['files']}))
        else:
            require(old and old['operation']=='archive' and old['task_id']==task_id and
                old['plan']['binding'] == archive_binding(task), 'original archive/task binding differs')
            plan = old['plan']
            require(result['project_id']==runtime.project_id and result['task_id']==task_id and
                result['operation_id']==operation_id and len(result['files'])==len(plan['files']) and
                all(all(row[k]==item[k] for k in ('kind','original','destination'))
                    for row,item in zip(result['files'],plan['files'])), 'verified archive result identity differs')
            manifest = archive_manifest(state, task_id)
            contract = task.get('state_contract')
            canonical_record = (state['runtime']['intake']['requirements'][manifest['requirement_id']]
                                if manifest['requirement_id'] else None)
            for item, row in zip(plan['files'], result['files']):
                if row['status'] != 'archived':
                    continue
                current = manifest['spec'] if item['kind']=='spec' else manifest['deliverables'][item['index']]
                require(current in (item['original'], item['destination']), 'current confirmed file identity changed')
                if current != item['destination']:
                    change = dict(operation_id=operation_id, kind=item['kind'], original=deepcopy(current),
                                  current=deepcopy(item['destination']), recorded_at=now())
                    for container in (contract, canonical_record):
                        if container is None: continue
                        container.setdefault('location_history', []).append(deepcopy(change))
                        if item['kind']=='spec':
                            container['current_spec' if container is canonical_record else 'spec']=deepcopy(item['destination'])
                        else:
                            container['deliverables'][item['index']]=deepcopy(item['destination'])
                    task.setdefault('archive_location_history', []).append(deepcopy(change))
                if item['kind']=='spec':task['spec_location']=deepcopy(item['destination'])
            old.update(status=result['status'], result=deepcopy(result))
            task['archive'] = dict(deepcopy(result), plan=deepcopy(plan))
        task['last_registered_at'] = now()
        try:
            runtime._save(state, dict(op='task-state', action='archive', task_id=task_id, operation_id=operation_id))
        except OSError as error:
            return dict(status='registration_pending', reason=str(error), actual_result=deepcopy(result),
                next_action='observe original archive operation; do not repeat completed moves')
        return dict(status='recorded', revision=state['revision'])
