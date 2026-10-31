"""Evidence adoption through the canonical ledger; no execution, merge or cleanup.

Actor and original instruction captures are supplied by the trusted caller, as in
the observed-native adapter. File hashes bind evidence; they do not authenticate
the host. This module never grants a transport or invents historical launches.
"""
from copy import deepcopy
import json
from pathlib import Path

from evidence import (artifact, fingerprint, git, io_path, load_bound, pack, ref,
                      report_for, require, snapshot, write_atomic)

SOURCE = 'direct user message in original main conversation'


def approval(state, key, reference, actor):
    task = state['tasks'][key]
    value = load_bound(reference)
    require(value.get('schema') == 'approved-dispatch-plan/1' and value.get('approval') == 'approved' and
        value.get('source') == SOURCE and value.get('main_conversation') == state['coordinator_id'] and
        value.get('user_quote'), 'original approved dispatch required')
    matches = [t for t in value.get('tasks', []) if t.get('id') == key]
    require(len(matches) == 1 and Path(matches[0].get('worktree', task['worktree'])).resolve() == Path(task['worktree']).resolve() and
        matches[0].get('branch', task['branch']) == task['branch'], 'approved task/worktree/branch differs')
    require(task.get('requirements') and Path(task['requirements']['user_reference']).resolve() ==
        Path(reference['path']).resolve(), 'task must retain its original dispatch approval')
    require(actor == value.get('integration_agent') and actor not in (state['coordinator_id'], task['owner_agent_id']),
        'approved independent integration reviewer required')
    delegation = state.get('delivery')
    require(not delegation or (actor == delegation['agent_id'] and key in delegation['task_ids']),
        'outside current delivery scope')
    return value


def binding(state, key):
    task = state['tasks'][key]
    report = report_for(state, key, ready=False)
    require(task.get('task_kind', 'code') == 'code', 'real-use code task required')
    return dict(project_id=state['project']['id'], task_id=key, owner_agent_id=task['owner_agent_id'],
        delivery_revision=task['delivery_revision'], report=task['delivery'], source_digest=report['source']['digest'],
        head_sha=report.get('git', {}).get('head_sha'), target_sha=report.get('git', {}).get('target_sha'))


def review(state, key, reference, actor):
    """Normalize only known source-bound independent review contracts."""
    expected = binding(state, key)
    value = load_bound(reference)
    if value.get('schema') == 'independent-quality-review/1':
        require(all(value.get(k) == v for k, v in expected.items()) and
            value.get('reviewer_id') == actor and value.get('status') == 'passed' and value.get('command'),
            'independent review task/report/revision/source/Git binding differs')
    else:
        require(value.get('schema') in ('independent-PR-review/1', 'independent-PR-review/2') and
            value.get('task_id') == key and value.get('reviewer') == actor,
            'unsupported independent review or task/reviewer differs')
        pr = value['pr']
        require(pr.get('reviewed_head', pr.get('head')) == expected['head_sha'] and
            pr.get('reviewed_base', pr.get('target_main')) == expected['target_sha'], 'reviewed Git source stale')
        author = value.get('author')
        require((author.get('persistent_thread') if isinstance(author, dict) else author) == expected['owner_agent_id'],
            'review author differs from task owner')
        source = load_bound(expected['report'])['source']
        if value['schema'] == 'independent-PR-review/1':
            require(value.get('status') == 'technical_review_passed_scope_bound' and
                value.get('author_delivery_revision') == expected['delivery_revision'] and
                pr.get('source_digest') == expected['source_digest'] and
                pr.get('source_files_manifest') == source['files'], 'review source inventory/revision differs')
        else:
            imported = load_bound(expected['report']).get('imported_author_report')
            require(value.get('status') == 'technical_review_and_combined_regression_passed' and
                imported and pr.get('author_report') == imported and
                pr.get('source_files_sha256') == {r['relative']: r['sha256'] for r in source['files']},
                'review does not bind the actual author delivery/source')
    require(value.get('evidence'), 'independent review evidence required')
    for item in value['evidence']: artifact(item)
    return expected


def quality(state, key):
    record = state['tasks'][key].get('independent_quality')
    require(record, 'independent quality attachment missing')
    approval(state, key, record['approval'], record['reviewer_id'])
    require(record['binding'] == review(state, key, record['review'], record['reviewer_id']),
        'quality attachment no longer binds current delivery')
    return record


def instruction(state, key, reference, schema):
    value = load_bound(reference)
    require(value.get('schema') == schema and value.get('project_id') == state['project']['id'] and
        value.get('task_id') == key and value.get('main_conversation') == state['coordinator_id'] and
        value.get('source') == SOURCE and value.get('user_quote') and value.get('evidence'),
        'explicit original-main scoped instruction required')
    for item in value['evidence']: artifact(item)
    return value


def technical_authority(state, key, reference):
    task = state['tasks'][key]
    value = instruction(state, key, reference, 'technical-acceptance-authorization/1')
    require(value.get('requirements_digest') == fingerprint(task['requirements']) and
        value.get('acceptance_type') == 'delivery_verified', 'confirmed requirements or technical acceptance authority differs')
    # Old prepare encoded all code as user_required. Do not rewrite that scope.
    require(task.get('acceptance_type') == 'delivery_verified' or
        (value.get('legacy_encoding_only') is True and not task.get('acceptance_policy_explicit')),
        'actual user-required acceptance cannot be waived')
    return value


def code_acceptance(state, key):
    """Technical verification and actual human acceptance remain distinct facts."""
    task = state['tasks'][key]
    if task.get('technical_acceptance'):
        record = task['technical_acceptance']
        technical_authority(state, key, record['authority'])
        quality(state, key)
        require(record['binding'] == binding(state, key), 'technical acceptance stale')
        report_for(state, key)
    else:
        require(task['user_acceptance'] == 'accepted', 'functional acceptance missing')
    return True


def cli_log(reference, identity):
    events = [json.loads(line) for line in artifact(reference).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    threads = [e.get('thread_id') for e in events if e.get('type') == 'thread.started']
    require(threads and all(x == identity for x in threads), 'actual CLI thread missing or differs')
    status, finals = 'unknown', []
    for event in events:
        kind = event.get('type')
        if kind == 'turn.started': status, finals = 'running', []
        elif kind == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            finals.append(event['item'].get('text', ''))
        elif kind in ('turn.completed', 'turn.failed'):
            status = 'completed' if kind == 'turn.completed' else 'failed'
        elif kind == 'process.exited' and status == 'running': status = 'unknown'
    return status, finals


def cli_binding(state, key, command):
    task = state['tasks'][key]
    launch = load_bound(command['launch'])
    identity = command['thread_id']
    require(launch.get('task_id') == key and launch.get('transport') == 'codex_exec_child_process' and
        launch.get('native_agent_id') is None and type(launch.get('pid')) is int and launch['pid'] > 0 and
        Path(launch['worktree']).resolve() == Path(task['worktree']).resolve() and launch['branch'] == task['branch'] and
        Path(launch['authority']).resolve() == Path(command['approval']['path']).resolve() and
        Path(launch['stdout']).resolve() == Path(command['launch_log']['path']).resolve(), 'original CLI dispatch differs')
    cli_log(command['launch_log'], identity)
    plan = load_bound(command['approval'])
    approved_task = next(t for t in plan['tasks'] if t['id'] == key)
    for name in ('spec', 'checklist'):
        if approved_task.get(name) or plan.get('specs'):
            captured = command['approved_inputs'][name]
            artifact(captured)
            require(Path(captured['path']).name == Path(approved_task.get(name, captured['path'])).name and any(
                r['sha256'] == captured['sha256'] and Path(r['path']).name == Path(captured['path']).name
                for r in plan['specs']), 'CLI dispatch document differs from approved bytes')
    if command.get('continuation'):
        continuation = load_bound(command['continuation'])
        require(continuation.get('task_id') == key and continuation.get('thread_id') == identity and
            type(continuation.get('pid')) is int and continuation['pid'] > 0, 'CLI continuation identity differs')
        if continuation.get('stdout'):
            require(Path(continuation['stdout']).resolve() == Path(command['log']['path']).resolve(),
                'CLI continuation log differs')
    else: require(command['log'] == command['launch_log'], 'later CLI log needs its actual process continuation')
    return cli_log(command['log'], identity)


def cli_observation(reference, kind, identity):
    if kind == 'codex_exec': return cli_log(reference, identity)
    require(kind == 'app_thread', 'explicit CLI/App observation kind required')
    from host_adapter import unwrap
    raw = unwrap(load_bound(reference))
    if 'polls' in raw:
        require(len(raw['polls']) == 1, 'one bounded App observation required')
        raw = raw['polls'][0]
    require(raw.get('thread', {}).get('id') == identity, 'App observation is for another CLI thread')
    turn = raw.get('latestTurn') or next(iter(raw.get('turns', [])), {})
    final = raw.get('latestAssistantMessage', {})
    if final:
        require(final.get('turnId') == turn.get('id') and final.get('phase') in ('final', 'final_answer'),
            'App final belongs to another turn')
        finals = [final.get('text', '')]
    else:
        finals = [i.get('text', '') for i in turn.get('items', []) if i.get('type') == 'agentMessage' and
            i.get('phase') in ('final', 'final_answer')]
    return turn.get('status', 'unknown'), finals


def cli_origin(state, payload):
    key = payload['task_id']; task = state['tasks'][key]
    record = task['cli_dispatch']; command = record['command']
    approval(state, key, command['approval'], record['reviewer_id'])
    cli_binding(state, key, command)
    require(payload.get('channel') == 'codex_exec' and payload.get('origin_mode') == 'observed_codex_exec' and
        payload['owner_agent_id'] == command['thread_id'] == task['owner_agent_id'] and
        payload.get('recipient_agent_id') == record['reviewer_id'], 'CLI origin/owner/receiver differs')
    status, finals = cli_observation(payload['origin_observation'], payload['observation_kind'], command['thread_id'])
    require(status in ('completed', 'failed', 'interrupted', 'cancelled', 'canceled') and
        (payload['kind'] == 'blocked' or status == 'completed'), 'CLI terminal outcome does not prove this delivery')
    report = load_bound(payload['report'])
    target = report.get('imported_author_report', payload['report'])
    mentioned = any(target['path'].replace('\\', '/').casefold() in t.replace('\\', '/').casefold() for t in finals)
    git_info = report.get('git', {})
    exact_git_final = git_info.get('head_sha') and git_info.get('target_sha') and any(
        git_info['head_sha'] in t and git_info['target_sha'] in t for t in finals)
    require(mentioned or exact_git_final, 'actual latest final does not identify this report or exact Git result')
    if report.get('imported_author_report'):
        verify_imported_report(report, task['owner_agent_id'])


def author_tests(raw):
    commands = raw['tests']['commands']
    require(commands and all(c.get('exit_code') == 0 for c in commands) and
        raw['tests']['core'].get('failures') == [] and raw['tests']['core'].get('errors') == [] and
        raw['tests']['root'].get('failures') == 0 and raw['tests']['root'].get('errors') == 0,
        'author tests did not pass')
    return commands


def imported_report(state, key, reference):
    """Explicit adapter for the preserved development-delivery/2 author contract."""
    raw = load_bound(reference); task = state['tasks'][key]
    if raw.get('schema') != 'development-delivery/2': return raw
    require(raw.get('task_id') == key and raw.get('revision') and raw.get('status') ==
        'same_PR_synchronized_selftested_awaiting_independent_review' and
        Path(raw['worktree']).resolve() == Path(task['worktree']).resolve() and raw['branch'] == task['branch'] and
        raw.get('developer', {}).get('persistent_thread') == task['owner_agent_id'], 'actual CLI author report identity differs')
    manifest_ref = ref(Path(reference['path']).parent/raw['source_binding']['manifest'])
    source = snapshot(task['worktree'])
    require(load_bound(manifest_ref) == {r['relative']: r['sha256'] for r in source['files']}, 'author source manifest stale')
    commands = author_tests(raw)
    result = pack(dict(project_id=state['project']['id'], task_id=key, delivery_revision=raw['revision'],
        summary=key+' actual CLI delivery', source_root=task['worktree'], evidence=[reference, manifest_ref],
        self_test=dict(status='passed', command=commands), quality_review=dict(status='pending'),
        entry=dict(kind='file', target=reference['path']), steps=['Inspect actual author delivery and bound tests'],
        expected='Approved technical acceptance criteria verified independently',
        git=dict(head_sha=raw['head_sha'], target_sha=raw['base_sha'], branch=raw['branch'], pr_url=raw['pr']['pr_url']),
        imported_author_report=reference, author_source_manifest=manifest_ref,
        adapter='development-delivery/2 to canonical report; original author bytes preserved'))
    return result


def verify_imported_report(report, owner):
    raw = load_bound(report['imported_author_report'])
    require(raw.get('schema') == 'development-delivery/2' and raw['task_id'] == report['task_id'] and
        raw['revision'] == report['delivery_revision'] and raw['head_sha'] == report['git']['head_sha'] and
        raw['base_sha'] == report['git']['target_sha'] and raw.get('status') ==
        'same_PR_synchronized_selftested_awaiting_independent_review' and
        raw.get('developer', {}).get('persistent_thread') == owner and
        Path(raw['worktree']).resolve() == Path(report['source']['root']).resolve() and
        report['self_test']['status'] == 'passed' and report['self_test']['command'] == author_tests(raw) and
        load_bound(report['author_source_manifest']) ==
        {r['relative']: r['sha256'] for r in report['source']['files']}, 'imported author report binding differs')


def verify_ci(state, key, command):
    ci = command['ci']; head = command['source_head']
    required = (state.get('delivery') or {}).get('required_ci_jobs', [])
    if ci.get('policy') == 'required':
        from delivery import verify_ci as configured_ci
        configured_ci(ci['result'], head, required or ci.get('required_jobs'))
    else:
        require(ci.get('policy') == 'not_configured' and not required, 'configured CI cannot be bypassed')
        def observed(reference):
            raw = load_bound(reference)
            require(not raw.get('isError'), 'CI observation failed')
            return raw.get('structuredContent', raw)
        statuses, runs = observed(ci['statuses']), observed(ci['runs'])
        context = load_bound(command['integration'])
        bound = context.get('ci') == ci or all(x in context.get('evidence', []) for x in (ci['statuses'], ci['runs']))
        require(ci.get('head_sha') == head and bound and statuses.get('sha', head) == head and
            statuses.get('statuses') == [] and statuses.get('total_count', 0) == 0 and
            runs.get('workflow_runs') == [] and runs.get('total_count', 0) == 0,
            'actual absence of configured CI evidence required; pending is not passed')


def merged(state, key, command, actor):
    task = state['tasks'][key]
    expected = binding(state, key)
    code_acceptance(state, key); quality(state, key)
    require(task['implementation'] == 'completed' and task.get('worker_run', {}).get('status') == 'reported' and
        not task.get('blocker') and task['phase'] not in ('completed', 'cancelled'), 'current fully delivered task required')
    require(state['items'].get(state['active_human_item'], {}).get('task_id') != key and
        not state.get('merge_reservation'), 'active user item or merge reservation protects task')
    authority = instruction(state, key, command['merge_authority'], 'code-merge-authorization/1')
    require(authority.get('merge_allowed') is True and all(authority.get(k) == expected[k] for k in ('head_sha', 'target_sha')),
        'exact source/base merge authorization required')
    review(state, key, command['integration'], actor)
    verify_ci(state, key, command)
    require(command.get('source_head') == expected['head_sha'] and command.get('previous_main') == expected['target_sha'] and
        command.get('main_ref') == state['project']['main_branch'] and command.get('scope') in ('fixture_only', 'host_verified') and
        command.get('evidence') and command.get('pr_url') == load_bound(task['delivery'])['git']['pr_url'],
        'actual merge source/base/main/PR differs')
    mr = command['mr']
    require(mr.get('url') == command['pr_url'] and mr.get('provider') == state['project']['provider'] and mr.get('id') and
        mr.get('source_branch') == task['branch'] and mr.get('target_branch') == command['main_ref'] and
        mr.get('head_sha') == expected['head_sha'] and mr.get('target_sha') == expected['target_sha'], 'actual PR metadata differs')
    require(not task.get('mr') or all(task['mr'].get(k) == mr[k] for k in ('provider','id','url')), 'maintain the original PR')
    require(not any(k != key and t.get('mr', {}).get('url') == mr['url'] for k,t in state['tasks'].items() if t.get('mr')),
        'PR belongs to another task')
    from demand_routing import pr_guard
    pr_guard(state,key,mr)
    for item in command['evidence']: artifact(item)
    if command['scope'] == 'host_verified':
        observed = load_bound(command['merge_observation'])
        require(observed.get('schema') == 'verified-PR-merge/1' and observed.get('task_id') == key and
            observed.get('status') == 'merged_remote_and_local_verified' and
            observed.get('reviewed_head') == command['source_head'] and observed.get('previous_main') == command['previous_main'] and
            observed.get('merge_sha') == command['merge_commit'] and observed.get('pr_url') == command['pr_url'] and
            observed.get('independent_review') == command['integration'] and observed.get('evidence'),
            'actual observed remote merge does not match independently reviewed task')
        for item in observed['evidence']: artifact(item)
    for item in state['project']['protected']: artifact(item)
    root = task['worktree']
    require(git(root, 'branch', '--show-current') == task['branch'] and not git(root, 'status', '--porcelain') and
        git(root, 'rev-parse', 'HEAD') == command['source_head'], 'author branch/head/source changed')
    repository = state['project'].get('local_main_repo') or state['project']['root']
    # Use the task repository only when explicitly requested (e.g. an isolated replay).
    if command.get('git_repository') == 'task': repository = root
    branch = state['project']['main_branch']
    require(command.get('git_ref') in ('refs/heads/'+branch, 'refs/remotes/origin/'+branch), 'exact target main ref required')
    require(git(repository, 'rev-parse', command['git_ref']) == command['main_head'], 'actual target main differs')
    for ancestor, descendant in ((command['previous_main'], command['source_head']),
                                 (command['source_head'], command['merge_commit']),
                                 (command['previous_main'], command['merge_commit']),
                                 (command['merge_commit'], command['main_head'])):
        require(git(repository, 'merge-base', ancestor, descendant) == ancestor, 'actual Git main does not contain tested source')
    if authority.get('merge_order') or command.get('merge_order'):
        order = authority.get('merge_order')
        require(order and command.get('merge_order', order) == order and key in order and all(state['tasks'][p]['phase'] == 'completed'
            for p in order[:order.index(key)]), 'authorized serial merge order required')
    if state.get('delivery'):
        order = state['delivery']['merge_order']
        require(all(state['tasks'][p]['phase'] == 'completed' for p in order[:order.index(key)]), 'honor delegated merge order')
    return expected


def apply(runtime, command, actor):
    from coordination import writer_lock, admit_receipt, consume, receipt_context, receipt_report
    key, action = command['task_id'], command['action']
    require(action in ('bind-cli', 'receive-cli', 'quality', 'technical-acceptance', 'record-merge'), 'unknown real-use action')
    with writer_lock(runtime.path):
        state = runtime._load(); task = state['tasks'][key]
        approval(state, key, command['approval'], actor)
        original = deepcopy(state)
        if action == 'bind-cli':
            command = deepcopy(command); command.setdefault('launch_log', command['log'])
            outcome, _ = cli_binding(state, key, command)
            previous = task.get('cli_dispatch')
            if previous:
                require(previous['command'] == command, 'CLI dispatch cannot be replaced')
                return dict(duplicate=True, revision=state['revision'])
            identity = command['thread_id']
            require(task['owner_agent_id'] is None and task['phase'] == 'backlog' and task['worker_run']['status'] == 'queued' and
                not any(t['owner_agent_id'] == identity for t in state['tasks'].values()) and
                identity not in state['runtime']['executors'] and identity not in (state['coordinator_id'], actor),
                'only the approved unowned queued task can adopt this CLI author')
            require(not any(o.get('task_id') == key for o in state['runtime']['operations'].values()),
                'reconcile existing host effects before CLI adoption')
            state['runtime']['executors'][identity] = dict(id=identity, generation=1, lease='open',
                routes={'codex_exec': dict(host_id='local', target_id=identity)}, origin='codex_exec_child_process',
                authority=command['approval'], observation=None, execution=outcome, pending_work=None, active_children=None)
            task.update(owner_agent_id=identity, phase='developing', implementation='running' if outcome == 'running' else 'pending',
                cli_dispatch=dict(command=command, reviewer_id=actor, admission_reconstructed=False))
            task['worker_run'].update(owner_agent_id=identity, status='running' if outcome == 'running' else
                ('awaiting_receipt' if outcome in ('completed', 'failed') else 'unknown'), reason='Actual CLI process/thread observed; author receipt required')
            if outcome in ('unknown', 'failed'):
                task['blocker'] = dict(category='evidence', owner=actor,
                    reason='Actual CLI outcome '+outcome+'; preserve context and obtain a verified author report')
            result = dict(status='bound', origin='codex_exec_child_process', admission_reconstructed=False)
        elif action == 'receive-cli':
            report = imported_report(state, key, command['report'])
            if report.get('imported_author_report'):
                target = io_path(runtime.directory/'imported-reports'/ (fingerprint(report)+'.json'))
                if target.exists(): require(load_bound(ref(target)) == report, 'immutable imported report differs')
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(target, report)
                report_ref = ref(target)
            else: report_ref = command['report']
            revision = report['delivery_revision']; kind = command.get('kind', 'completed')
            packet = dict(project_id=state['project']['id'], task_id=key, owner_agent_id=task['owner_agent_id'],
                recipient_agent_id=actor, channel='codex_exec', origin_mode='observed_codex_exec',
                origin_observation=command['observation'], observation_kind=command['observation_kind'],
                event_id=f'{key}:{revision}:{kind}', delivery_revision=revision, kind=kind, report=report_ref)
            receipt_context(state, packet); receipt_report(state, packet)
            require(revision >= task['worker_run']['expected_delivery_revision'], 'CLI receipt predates current work')
            prior = state['receipts'].get(packet['event_id'])
            if prior:
                def facts(value): return {k:v for k,v in value.items() if k not in ('origin_observation','observation_kind')}
                require(facts(prior['payload']) == facts(packet), 'conflicting duplicate CLI receipt')
                receipt_context(state, prior['payload']); receipt_report(state, prior['payload'])
                if prior['status'] in ('processed','stale'):
                    return dict(receipt_status=prior['status'], duplicate=True, revision=state['revision'])
                packet = prior['payload']
            status = admit_receipt(state, packet)
            if status != 'duplicate':
                status = consume(state, packet)
                state['receipts'][packet['event_id']]['status'] = status
            result = dict(receipt_status=status, origin='codex_exec_child_process')
        elif action == 'quality':
            expected = review(state, key, command['review'], actor)
            record = dict(approval=command['approval'], review=command['review'], reviewer_id=actor, binding=expected)
            previous = task.get('independent_quality')
            if previous and previous['binding'] == expected:
                require(previous == record, 'conflicting review for same delivery')
            else:
                require(task['phase'] not in ('completed', 'cancelled'), 'ended delivery immutable')
                task['independent_quality'] = record
            quality(state, key)
            task['quality_review'] = 'passed'
            report_for(state, key)
            if (task.get('blocker') or {}).get('category') == 'evidence': task['blocker'] = None
            result = dict(quality_review='passed')
        elif action == 'technical-acceptance':
            technical_authority(state, key, command['authority']); quality(state, key); report_for(state, key)
            require(task['implementation'] == 'completed' and task.get('worker_run', {}).get('status') == 'reported' and
                state['items'].get(state['active_human_item'], {}).get('task_id') != key,
                'current delivered task and no active user item required')
            record = dict(authority=command['authority'], binding=binding(state, key), reviewer_id=actor)
            old = task.get('technical_acceptance')
            if old: require(old == record, 'technical acceptance binding differs')
            else:
                require(task['phase'] not in ('completed', 'cancelled'), 'ended task immutable')
                task.update(technical_acceptance=record, phase='awaiting_integration', blocker=None)
                for item in state['items'].values():
                    if item.get('task_id') == key and item.get('kind') == 'acceptance' and item['status'] == 'queued':
                        item.update(status='closed', outcome='technical_verified', resolution_reference=command['authority'])
            result = dict(technical_accepted=True, user_acceptance=task['user_acceptance'])
        else:
            if task['phase'] == 'completed':
                require(task.get('real_use_completion', {}).get('command') == command, 'different historical completion proof')
                return dict(status='completed', duplicate=True, revision=state['revision'], cleanup_authorized=False)
            expected = merged(state, key, command, actor)
            proof = dict(command, project_id=state['project']['id'], contains_source=True, binding=expected)
            target = io_path(runtime.directory/'merge-proofs'/(fingerprint(proof)+'.json'))
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(target, proof)
            proof_ref = ref(target)
            task.update(phase='completed', merge=dict(verified=True, head_sha=command['source_head'],
                target_sha=command['previous_main'], main_sha=command['main_head'], scope=command['scope'],
                proof=proof_ref, basis='observed_actual_git'),
                real_use_completion=dict(command=deepcopy(command), binding=expected, proof=proof_ref),
                mr=deepcopy(command['mr']),
                integration=dict(status='passed', basis='observed_independent_checks', report=command['integration']),
                cleanup=dict(status='pending_cleanup', owner=actor, next_action='preserve_and_reclaim'))
            result = dict(status='completed', cleanup_authorized=False)
        changed = state != original
        if changed: runtime._save(state, dict(op='real-use', task_id=key, action=action, actor_id=actor, evidence=deepcopy(command)))
        return dict(result, duplicate=not changed, revision=state['revision'])
