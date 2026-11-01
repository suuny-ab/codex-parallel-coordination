"""Finite approved dispatch composition over CORE and RESOURCE, not another queue.

Host adapters, user origin and the fixed profile are trusted host sideband. The
request contains evidence and task choices, never an actor or granted authority.
"""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import os
import re
import subprocess

from coordination import writer_lock
from demand_routing import apply as route_apply, approved_item
from evidence import artifact, fingerprint, load_bound, ref, require, write_atomic


def approved_request(bundle):
    """Expand frozen evidence, not authority; main still chooses task semantics."""
    require(set(bundle) == {'request_id', 'project_id', 'approval', 'spec_check', 'report_path', 'acceptance_type'},
            'only approved bundle fields allowed')
    approval = load_bound(bundle['approval'])
    presentation = load_bound(approval['presentation'])
    check = load_bound(bundle['spec_check'])
    require(len(presentation['items']) == 1 and len(approval['tasks']) == 1, 'one approved spec per call')
    item, task = presentation['items'][0], approval['tasks'][0]
    require(item['task_id'] == task['id'] == approval['task_id'] and
            check['requirement_id'] == item['requirement_id'], 'bundle task or requirement differs')
    record = {k: item[k] for k in ('name', 'scope', 'acceptance')}
    record.update(spec=check['spec'], discussion=check['discussion'], check=bundle['spec_check'], start_reference=bundle['approval'])
    return dict(request_id=bundle['request_id'], project_id=bundle['project_id'], task_id=task['id'],
        base_main=approval['base_main'], requirement=record, approval=bundle['approval'], presentation=approval['presentation'],
        worktree=task['worktree'], branch=task['branch'], integration_agent=approval['integration_agent'],
        report_path=bundle['report_path'], acceptance_type=bundle['acceptance_type'])


class StandardDispatch:
    def __init__(self, runtime, bridge, git, developer, integration, user_origin, integration_target):
        self.runtime, self.bridge, self.git = runtime, bridge, git
        self.developer, self.integration, self.origin = developer, integration, user_origin
        self.actor = developer.actor_id
        self.integration_target = integration_target

    def _record(self, identity, *, _expected_revision=None, **changes):
        with writer_lock(self.runtime.path):
            state = self.runtime._load()
            self.runtime._actor(state, self.actor, control=True)
            require(_expected_revision is None or state['revision'] == _expected_revision,
                    'queued baseline state changed; recheck original request')
            value = state['runtime']['operations'].setdefault(identity, {})
            value.update(deepcopy(changes))
            self.runtime._save(state, dict(op='standard-dispatch', request_id=identity, task_id=value['task_id']))
            return deepcopy(value)

    def _evidence(self, identity, name, value):
        path = self.bridge.evidence / ('dispatch-' + fingerprint(identity) + '-' + name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            require(load_bound(ref(path)) == value, 'immutable dispatch evidence changed')
        else:
            write_atomic(path, value)
        return ref(path)

    def _commands(self, request):
        presentation = load_bound(request['presentation'])
        task = request['task_id']
        matches = [i for i in presentation['items'] if i['task_id'] == task]
        require(len(matches) == 1, 'one presented task required')
        item = matches[0]
        require(item['assignment'] == 'new' and item.get('executor_id') is None,
                'new dispatch only; rework stays with the original author and PR')
        record = request['requirement']
        approval = load_bound(request['approval'])
        require(any(x.get('sha256') == record['spec']['sha256'] and
                    Path(x['path']).resolve() == Path(record['spec']['path']).resolve()
                    for x in approval.get('specs', [])), 'spec not bound to approved dispatch')
        check = load_bound(record['check'])
        if check.get('review'):
            artifact(check['review'])
        commands = [dict(op='spec', requirement_id=item['requirement_id'], **record),
            dict(op='present', plan_id=presentation['plan_id'], version=presentation['version'],
                 items=presentation['items'], presentation=request['presentation']),
            dict(op='approve', plan_id=presentation['plan_id'], reference=request['approval'])]
        require(len(presentation['items']) == 1,
                'one approved spec per call; use an already registered batch plan through routing')
        require(approval.get('base_main') == request['base_main'] and
                approval.get('integration_agent') == request['integration_agent'] and
                approval.get('task_id') == task, 'approval base, integration or task differs')
        selected = [t for t in approval.get('tasks', []) if t.get('id') == task]
        require(len(selected) == 1 and selected[0]['branch'] == request['branch'] and
                Path(selected[0]['worktree']).resolve() == Path(request['worktree']).resolve(),
                'approved worktree or branch differs')
        rights = approval.get('rights', {})
        require(set(rights) <= {'commit', 'push', 'mr', 'merge'} and
                all(type(v) is bool for v in rights.values()), 'scoped Git rights required')
        require(record['start_reference'] == request['approval'], 'start must reference original approval')
        return item, commands, rights

    def _base(self, request, previous):
        base = request['base_main']
        for reference in (previous or {}).get('base_revisions', []):
            proof = load_bound(reference)
            expected = dict(schema='queued-dispatch-baseline/1', project_id=self.runtime.project_id,
                task_id=request['task_id'], request_id=request['request_id'], request_digest=fingerprint(request),
                approval=request['approval'], original_base_main=request['base_main'], previous_base_main=base,
                worktree=request['worktree'], branch=request['branch'], actor_id=self.actor)
            require(all(proof.get(k) == v for k, v in expected.items()) and
                re.fullmatch(r'[a-f0-9]{40}', proof.get('effective_base_main', '')) and
                proof['local_main'] == proof['remote_main'] == proof['effective_base_main'],
                'queued baseline evidence differs from original request')
            base = proof['effective_base_main']
        return base

    def rebase_queued(self, request, *, authorized=False):
        """Append one observed technical baseline, never dispatch or edit frozen input.

        authorized is current trusted-host sideband, not a task/file permission.
        Only a never-started RESOURCE queue may use this separate finite action.
        """
        require(authorized is True, 'current scoped queued-rebase authorization required')
        if 'spec_check' in request:
            request = approved_request(request)
        with writer_lock(self.runtime.directory / 'standard-dispatch-driver'):
            state = self.runtime._load()
            self.runtime._actor(state, self.actor, control=True)
            identity, task_id = request['request_id'], request['task_id']
            old = state['runtime']['operations'].get(identity, {})
            require(old.get('operation') == 'standard-dispatch' and old.get('task_id') == task_id and
                old.get('request_digest') == fingerprint(request) and old.get('status') == 'queued' and
                old.get('admission') == dict(status='queued', request_id=identity+':capacity', admission=None),
                'same original RESOURCE-queued request required')
            require(not any(old.get(k) for k in ('launch_started', 'worktree_intent', 'handoff_intent', 'retained_host_results')) and
                not any(key != identity and op.get('task_id') == task_id for key, op in state['runtime']['operations'].items()) and
                not any(r.get('task_id') == task_id for r in state['runtime'].get('resources', {}).values()) and
                not any(r.get('task_id') == task_id for r in state['runtime'].get('intake', {}).get('routes', {}).values()),
                'queued rebase excludes every tree/resource/host intent')
            task = state['tasks'][task_id]
            require(task['owner_agent_id'] is None and task['phase'] == 'backlog' and
                task['worker_run']['status'] == 'queued', 'queued rebase cannot replace an actual author')
            require(not Path(request['worktree']).exists(), 'queued worktree already exists')
            capacity = dict(request_id=identity+':capacity', project_id=self.runtime.project_id, task_id=task_id,
                attempt_id=identity+':launch', launch_identity='spawn:'+identity+':launch', resource_class='agent_execution_slot')
            capacity_ref = ref(self.bridge.evidence/('dispatch-'+fingerprint(identity)+'-capacity.json'))
            require(load_bound(capacity_ref) == capacity, 'original queued capacity source differs')
            resource, _ = self.bridge.controller.read()
            observed = resource['capacity_requests'].get(identity+':capacity', {})
            from .resources.support import fingerprint as resource_fingerprint
            require(observed.get('spec') == capacity and observed.get('spec_digest') == resource_fingerprint(capacity) and
                observed.get('status') in ('queued', 'granted'), 'same active RESOURCE request required; no new permit')
            previous = self._base(request, old)
            current = self.git._git('rev-parse', 'refs/heads/main').stdout.decode().strip()
            # This reuses all original scope, approval, host, path, branch, policy
            # and local/remote guards; only its technical expected base differs.
            self._preflight(request, old, base_main=current)
            try:
                self.git._git('merge-base', '--is-ancestor', previous, current)
            except subprocess.CalledProcessError as error:
                raise ValueError('previous queued baseline must be an ancestor of current main') from error
            if previous == current:
                return dict(self.status(identity), effective_base_main=current, duplicate=True, host_actions=[])
            proof = dict(schema='queued-dispatch-baseline/1', project_id=self.runtime.project_id, task_id=task_id,
                request_id=identity, request_digest=fingerprint(request), approval=request['approval'],
                original_base_main=request['base_main'], previous_base_main=previous, effective_base_main=current,
                local_main=current, remote_main=current, worktree=request['worktree'], branch=request['branch'],
                based_on_revision=state['revision'],
                actor_id=self.actor, capacity_source=capacity_ref, observed_capacity=deepcopy(observed),
                authorization='Current scoped trusted-host call; this artifact does not grant authority', scope=self.developer.scope)
            evidence = self._evidence(identity, 'baseline-'+current+'-'+str(state['revision']), proof)
            self._record(identity, _expected_revision=state['revision'],
                         base_revisions=[*old.get('base_revisions', []), evidence])
            return dict(self.status(identity), effective_base_main=current, duplicate=False, host_actions=[])

    def _preflight(self, request, previous, *, base_main=None):
        state = self.runtime._load()
        self.runtime._actor(state, self.actor, control=True)
        require(self.integration.actor_id == self.actor, 'host adapter actor mismatch')
        require(request['project_id'] == self.runtime.project_id, 'wrong project')
        require(self.git.repository == self.runtime.project_root, 'trusted repository differs')
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', request['request_id']), 'stable request id required')
        require(not {'actor_id', 'user_origin', 'rights', 'state_root'}.intersection(request),
                'request cannot supply host identity, profile or authority')
        item, commands, rights = self._commands(request)
        # Reuse the production routing guards on a throwaway copy before mutations.
        probe = deepcopy(state)
        for command in commands:
            if command['op'] == 'present' and command['plan_id'] in probe['runtime'].get('intake', {}).get('plans', {}):
                plan = probe['runtime']['intake']['plans'][command['plan_id']]
                require(plan['presentation'] == command['presentation'] and plan['items'] == command['items'],
                        'existing plan differs')
                continue
            route_apply(probe, command, self.actor, self.origin if command['op'] == 'approve' else None)
        approved_item(probe, request['task_id'])
        require(self.integration.channel in ('native_agent', 'app_thread') and
                self.integration.capabilities()['existing_message'], 'integration message capability unavailable')
        require(request['integration_agent'] != self.actor, 'independent integration recipient required')
        require(request['integration_agent'] == self.integration_target, 'integration differs from trusted host recipient')
        require(not state.get('delivery') or state['delivery']['agent_id'] == self.actor,
                'current global delivery delegation excludes main launch; integration must verify task authority')
        require(request['acceptance_type'] in ('delivery_verified', 'user_required'), 'explicit acceptance type required')
        report = Path(request['report_path'])
        require(Path(request['worktree']).is_absolute(), 'absolute worktree path required')
        tree = Path(request['worktree']).resolve()
        require(report.is_absolute() and not report.resolve().is_relative_to(tree) and
                not report.resolve().is_relative_to(self.runtime.project_root), 'report must outlive checkout')
        require(tree != self.git.root and tree.is_relative_to(self.git.root) and
                self.git.root == Path(state['runtime']['cleanup_root']).resolve(), 'worktree outside canonical managed root')
        _, policy = self.bridge.controller.read()
        roots = policy.get('cleanup_roots', {}).get(self.runtime.project_id, [])
        require(any(tree != Path(p).resolve() and tree.is_relative_to(Path(p).resolve()) for p in roots),
                'worktree outside current RESOURCE policy')
        require(not any(report.resolve().is_relative_to(Path(p).resolve()) for p in roots),
                'report inside managed cleanup root')
        base_main = base_main or self._base(request, previous)
        require(self.git._git('rev-parse', 'refs/heads/main').stdout.decode().strip() == base_main, 'stale local main')
        remote = self.git._git('ls-remote', 'origin', 'refs/heads/main').stdout.decode().split()
        require(remote and remote[0] == base_main, 'stale remote main')
        require(re.fullmatch(r'codex/[A-Za-z0-9][A-Za-z0-9_./-]*', request['branch']) and
                '..' not in request['branch'], 'invalid task branch')
        self.git._git('check-ref-format', '--branch', request['branch'])
        # Inspect every lexical parent; resolve() alone would conceal junctions.
        for parent in (Path(request['worktree']), *Path(request['worktree']).parents):
            require(not parent.is_symlink() and not parent.is_junction(), 'linked worktree path refused')
        resources = state['runtime'].get('resources', {})
        reservation = resources.get(request['task_id'] + ':worktree')
        owned = bool(previous and previous.get('worktree_intent')) or bool(reservation and
            reservation['task_id'] == request['task_id'] and Path(reservation['path']).resolve() == tree and
            reservation['authority'] == request['approval'])
        entries = self.git._entries()
        matches = [e for e in entries if Path(e['worktree']).resolve() == tree]
        branch_used = [e for e in entries if e.get('branch') == 'refs/heads/' + request['branch']]
        if tree.exists() or matches or branch_used:
            require(owned and len(matches) == 1 and matches == branch_used and
                    matches[0].get('HEAD') == base_main, 'unowned or changed worktree')
        elif not previous or not previous.get('worktree_intent'):
            exists = self.git.runner(['git', '-c', 'core.longpaths=true', '-C', str(self.git.repository),
                                     'show-ref', '--verify', '--quiet', 'refs/heads/' + request['branch']],
                                    capture_output=True, timeout=20)
            require(exists.returncode == 1, 'branch already exists or cannot be verified')
        task = state['tasks'].get(request['task_id'])
        if task:
            require((previous or reservation) and task['owner_agent_id'] is None and
                    task['worker_run']['status'] == 'queued' and task['branch'] == request['branch'] and
                    Path(task['worktree']).resolve() == tree and task['requirements']['scope'] == item['scope'] and
                    task['requirements']['acceptance'] == item['acceptance'], 'task already owned or differs')
            require(not task.get('routing') or task['routing']['requirement_id'] == item['requirement_id'],
                    'task belongs to another requirement')
        require(self.developer.channel == 'native_agent' and self.developer.capabilities()['spawn'] and
                self.developer.capabilities()['observation'], 'native spawn/observation capability unavailable')
        running = sum(t.get('worker_run', {}).get('status') == 'running' for t in state['tasks'].values())
        require(running < state['project']['worker_capacity'], 'project worker capacity unavailable')
        return item, commands, rights

    def _route(self, identity, command, origin=None):
        result = self.runtime.routing(dict(command, event_id=identity + ':' + command['op']),
                                      self.actor, user_origin=origin)
        require(result['status'] == 'recorded', result.get('reason', 'routing rejected'))
        return result

    def run(self, request):
        """Repeat the same request to continue known steps; never retry host effects."""
        if 'spec_check' in request:
            request = approved_request(request)
        try:
            return self._run(request)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
            state = self.runtime._load()
            old = state['runtime']['operations'].get(request.get('request_id'))
            if old and old.get('operation') == 'standard-dispatch' and old['request_digest'] == fingerprint(request):
                self._record(request['request_id'], last_error=str(error), next_owner=self.integration_target)
            raise

    def _run(self, request):
        with ExitStack() as locks:
            locks.enter_context(writer_lock(self.runtime.directory / 'standard-dispatch-driver'))
            identity, task_id = request['request_id'], request['task_id']
            state = self.runtime._load()
            self.runtime._actor(state, self.actor, control=True)
            operations = state['runtime']['operations']
            old = operations.get(identity)
            base_main = self._base(request, old)
            if old:
                require(old.get('operation') == 'standard-dispatch' and old['request_digest'] == fingerprint(request),
                        'conflicting dispatch request')
                # Re-entry after launch must first preserve actual owner/resources;
                # it does not re-run preflight as if this were an unowned task.
                if old.get('launch_started'):
                    locks.close()
                    intent = self.bridge.evidence / ('host-' + fingerprint([identity, 'multi_agent_v1__spawn_agent', 'intent']) + '.json')
                    if identity + ':launch' not in operations and old.get('status') == 'launch_pending' and not intent.exists() and not old.get('retained_host_results'):
                        # CORE persists its own launch intent before any native call.
                        # A driver exit in this gap can continue that same attempt.
                        route = state['runtime']['intake']['routes'][identity + ':route']
                        self.runtime.launch(task_id, self.developer, attempt_id=identity + ':launch',
                            admission=old['admission']['admission'], authorized=True, message=route['message'],
                            spawn=True, route_id=identity + ':route')
                    return self._finish(request, old)
            require(not any(o.get('operation') == 'standard-dispatch' and o.get('task_id') == task_id and
                            key != identity for key, o in operations.items()), 'task already has a dispatch request')
            # Git/network waits must not hold this project's shared driver lock.
            locks.close()
            item, commands, rights = self._preflight(request, old)
            locks.enter_context(writer_lock(self.runtime.directory / 'standard-dispatch-driver'))
            require(self.runtime._load()['revision'] == state['revision'],
                    'dispatch state changed during preflight; recheck original request')
            if not old:
                self._record(identity, operation='standard-dispatch', task_id=task_id,
                             request_digest=fingerprint(request), status='preparing')
            if not old or old.get('driver_owner_pid')!=os.getpid():
                self._record(identity,driver_owner_pid=os.getpid())
            for command in commands:
                # A previously registered identical plan is already guarded above.
                if command['op'] == 'present' and command['plan_id'] in self.runtime._load()['runtime'].get('intake', {}).get('plans', {}):
                    continue
                self._route(identity, command, self.origin if command['op'] == 'approve' else None)
            if task_id not in self.runtime._load()['tasks']:
                self.bridge.prepare_development(dict(task_id=task_id, title=item['name'], worktree=request['worktree'],
                    branch=request['branch'], task_kind='code', acceptance_type=request['acceptance_type'],
                    scope=item['scope'], acceptance=item['acceptance'], user_reference=request['approval']['path'],
                    report_path=request['report_path']), self.actor, authority_ref=request['approval'])
            elif not self.runtime._load()['tasks'][task_id].get('routing'):
                self._route(identity, dict(op='adopt', task_id=task_id))
            if rights:
                auth = dict(op='instruction', instruction_id=identity + ':rights', source=request['approval'],
                    action='authorize', candidates=[task_id], reason='Explicit approved task-only rights', changes=dict(rights=rights))
                self._route(identity + ':rights', auth)
                self._route(identity + ':rights', dict(op='apply-instruction', instruction_id=identity + ':rights'), self.origin)
            attempt = identity + ':launch'
            source = self._evidence(identity, 'capacity', dict(request_id=identity + ':capacity',
                project_id=self.runtime.project_id, task_id=task_id, attempt_id=attempt,
                launch_identity='spawn:' + attempt, resource_class='agent_execution_slot'))
            admission = self.bridge.admit(source)
            self._record(identity, admission=admission, status=admission['status'])
            if admission['status'] != 'granted':
                return self.status(identity)
            old = self.runtime._load()['runtime']['operations'][identity]
            if not old.get('worktree_intent'):
                reserved = any(Path(e['worktree']).resolve() == Path(request['worktree']).resolve()
                               for e in self.git._entries())
                self._record(identity, worktree_intent=dict(path=request['worktree'], branch=request['branch'], base=base_main))
                if not reserved:
                    expected_revision = self.runtime._load()['revision']
                    locks.close()
                    try:
                        self.git.create(dict(path=request['worktree'], metadata=dict(repository_root=str(self.git.repository))),
                                        request['branch'], base_main)
                    except (subprocess.SubprocessError, OSError) as error:
                        locks.enter_context(writer_lock(self.runtime.directory / 'standard-dispatch-driver'))
                        self._record(identity, status='worktree_unknown', error_type=type(error).__name__)
                        return self.status(identity)
                    locks.enter_context(writer_lock(self.runtime.directory / 'standard-dispatch-driver'))
                    require(self.runtime._load()['revision'] == expected_revision,
                            'dispatch state changed during tree creation; observe original tree')
            entries = [e for e in self.git._entries() if Path(e['worktree']).resolve() == Path(request['worktree']).resolve()]
            require(len(entries) == 1 and entries[0].get('branch') == 'refs/heads/' + request['branch'] and
                    entries[0]['HEAD'] == base_main, 'worktree intent needs observation; never blindly recreate')
            self._register(request, dict(id=task_id + ':worktree', kind='worktree', path=request['worktree']))
            self.bridge.artifacts.register(dict(resource_id=task_id + ':worktree', project_id=self.runtime.project_id,
                task_id=task_id, attempt_id=attempt, kind='git_worktree', path=request['worktree'], references=[task_id],
                protected=False, metadata=dict(repository_root=str(self.git.repository), branch='refs/heads/' + request['branch'])))
            message = '\n'.join([f"Task {task_id}: {item['name']}", f"Project: {self.runtime.project_root}",
                f"Worktree: {request['worktree']}\nBranch: {request['branch']}\nBase main: {base_main}",
                f"Read spec: {request['requirement']['spec']['path']}", item['scope'],
                'Acceptance:\n' + '\n'.join(item['acceptance']),
                f"Approval: {request['approval']['path']}\nGit rights: {rights}",
                f"Report: {request['report_path']}\nIntegration: {request['integration_agent']}",
                'Implement and self-test; maintain one PR. Do not merge, install, change global settings or create agents/chats.',
                'Use explicit workdir. Preserve real failures, reruns, source/evidence hashes and fixture/host limits.',
                *request.get('protection', [])])
            self._route(identity + ':implement', dict(op='instruction', instruction_id=identity + ':implement',
                source=request['approval'], action='implement', candidates=[task_id], reason='Approved fixed dispatch'))
            route_id = identity + ':route'
            result = self.runtime.routing(dict(op='route', event_id=route_id, route_id=route_id,
                instruction_id=identity + ':implement', task_id=task_id,
                actions=[dict(kind='spawn', task_id=task_id, executor_id=None, message=message)]), self.actor)
            require(result['status'] == 'recorded', result.get('reason', 'routing rejected'))
            self._record(identity, launch_started=True, status='launch_pending')
            locks.close()  # Canonical launch intent/attempt now owns dedup, not a lock held across native calls.
            self.runtime.launch(task_id, self.developer, attempt_id=attempt, admission=admission['admission'],
                authorized=True, message=message, spawn=True, route_id=route_id)
            return self._finish(request, self.runtime._load()['runtime']['operations'][identity])

    def _register(self, request, resource):
        state = self.runtime._load()
        existing = state['runtime'].get('resources', {}).get(resource['id'])
        if existing:
            authorities = [request['approval']]
            if resource['kind'] == 'agent':
                authorities.append(state['runtime']['executors'][resource['executor_id']]['authority'])
            require(existing['task_id'] == request['task_id'] and existing['authority'] in authorities and
                    all(Path(existing[k]).resolve() == Path(v).resolve() if k == 'path' else existing.get(k) == v
                        for k, v in resource.items()), 'existing resource differs')
            return
        self.runtime.register_resource(request['task_id'], resource, request['approval'], self.actor)

    def resume_prehost(self, request, observation, *, authorized=False):
        """Explicit original-host continuation, never file-derived authorization."""
        require(authorized is True, 'trusted terminal host sideband required')
        if 'spec_check' in request:
            request = approved_request(request)
        identity = request['request_id']
        state = self.runtime._load()
        self.runtime._actor(state, self.actor, control=True)
        old = state['runtime']['operations'][identity]
        require(old.get('request_digest') == fingerprint(request), 'conflicting recovery request')
        if old.get('status') == 'dispatched':
            return self.status(identity)
        from prehost_recovery import check
        require(check(self.runtime, state, identity+':launch', observation, self.actor, self.bridge.evidence) == request,
                'prehost observation differs from original request')
        self._preflight(request, old)
        source = load_bound(old['admission']['admission'])
        resource, _ = self.bridge.controller.read()
        permit = resource['capacity_requests'][identity+':capacity']
        from .resources.support import fingerprint as resource_fingerprint
        require(permit['status'] == 'granted' and permit['permit_id'] == source['permit_id'] and
            permit['spec'] == load_bound(ref(self.bridge.evidence/('dispatch-'+fingerprint(identity)+'-capacity.json'))) and
            permit['spec_digest'] == resource_fingerprint(permit['spec']),
            'original RESOURCE admission no longer granted')
        route = state['runtime']['intake']['routes'][identity+':route']
        self.runtime.launch(request['task_id'], self.developer, attempt_id=identity+':launch',
            admission=old['admission']['admission'], authorized=True, message=route['message'], spawn=True,
            route_id=identity+':route', prehost_recovery=observation, dispatch_evidence_root=self.bridge.evidence)
        return self._finish(request, self.runtime._load()['runtime']['operations'][identity])

    def _finish(self, request, old):
        identity, task_id = request['request_id'], request['task_id']
        if old.get('retained_host_results'):
            return self.status(identity)  # Evidence retention never resumes host actions.
        state = self.runtime._load()
        outcome = state['runtime']['operations'].get(identity + ':launch')
        if not outcome:
            self._record(identity, status='needs_reconcile', reason='launch boundary interrupted; no automatic retry')
            return self.status(identity)
        owner = state['tasks'][task_id]['owner_agent_id']
        if owner:
            executor = state['runtime']['executors'][owner]
            self._register(request, dict(id=task_id + ':agent', kind='agent', executor_id=owner,
                                        generation=executor['generation']))
        if outcome['status'] != 'running':
            self._record(identity, status=outcome['status'],
                reason=outcome.get('reason') or 'Observe original launch; do not resend or change id',
                next_action='Resolve native capacity; RESOURCE permit is separate; no automatic retry' if
                    outcome['status'] == 'capacity' else 'Observe original launch; do not resend or change id')
            return self.status(identity)
        state=self.runtime._load();old=state['runtime']['operations'][identity]
        if old.get('handoff_intent'):
            return self.status(identity)
        # Authority is checked again before the distinct external send.
        approved_item(state, task_id)
        require(self.origin and self.origin.get('role') == 'user' and
                self.origin.get('conversation_id') == state['coordinator_id'] and
                self.origin.get('reference') == request['approval'], 'current handoff user origin required')
        require(load_bound(request['approval'])['integration_agent'] == request['integration_agent'], 'integration authority changed')
        require(request['integration_agent'] == self.integration_target, 'integration differs from trusted host recipient')
        self._record(identity, _expected_revision=state['revision'], handoff_intent=True, status='handoff_unknown')
        message = self._handoff_message(request, owner)
        try:
            result = self.integration.call('send', target=request['integration_agent'], message=message, authorized=True)
        except Exception as error:
            result = dict(status='unknown', error_type=type(error).__name__)
        evidence = self._evidence(identity, 'handoff', result)
        self._record(identity, handoff=result, handoff_evidence=evidence,
            status='dispatched' if result['status'] == 'accepted' else 'handoff_unknown')
        return self.status(identity)

    def _handoff_message(self, request, owner):
        return f"Dispatched {request['task_id']}; author {owner}; worktree {request['worktree']}; branch {request['branch']}; report {request['report_path']}. Independently review the final source/evidence; maintain the original PR and approved gates."

    def reconcile_host_result(self, request, result_ref):
        """Retain a known result of this current uncertain dispatch; no host calls.

        The trusted caller supplies its original request and durable transport
        receipt. A late spawn can bind its actual owner, never running/finished.
        """
        if 'spec_check' in request:
            request = approved_request(request)
        with writer_lock(self.runtime.directory / 'standard-dispatch-driver'):
            state = self.runtime._load()
            self.runtime._actor(state, self.actor, control=True)
            identity, task_id = request['request_id'], request['task_id']
            old = state['runtime']['operations'][identity]
            require(old.get('operation') == 'standard-dispatch' and old['request_digest'] == fingerprint(request) and
                    old.get('launch_started') and old['status'] != 'dispatched' and
                    state['tasks'][task_id]['phase'] not in ('completed', 'cancelled'), 'current uncertain dispatch required')
            require(Path(result_ref['path']).resolve().is_relative_to(self.bridge.evidence.resolve()),
                    'host result outside trusted external evidence root')
            journal = load_bound(result_ref)
            packet, response = journal['pending_packet'], journal['response']
            context = packet['context']
            require(journal.get('schema') == 'dispatch-host-result/1' and journal['actor_id'] == self.actor and
                    journal['request_id'] == context['request_id'] == identity and context['project_id'] == self.runtime.project_id and
                    context['task_id'] == task_id and context['request_digest'] == fingerprint(request), 'host result dispatch binding differs')
            original_request = load_bound(context['request_ref'])
            if 'spec_check' in original_request:
                original_request = approved_request(original_request)
            require(original_request == request, 'host result references another request')
            intent = load_bound(context['intent'])
            require(intent['tool'] == packet['tool'] == response['tool'] and intent['arguments'] == packet['arguments'] and
                    intent['context'] == {k: v for k, v in context.items() if k != 'intent'} and
                    response['kind'] == 'host_result', 'host result differs from original pending intent')
            launch = state['runtime']['operations'][identity + ':launch']
            require(launch['task_id'] == task_id and launch['operation'] == 'spawn', 'original spawn intent missing')
            route = state['runtime']['intake']['routes'][launch['route_id']]
            owner = state['tasks'][task_id]['owner_agent_id']
            stage, actual = context['stage'], journal['actual_agent_id']
            capacity_detail = None
            require(context['operation_id'] == identity + (':handoff' if stage == 'handoff' else ':launch'),
                    'host result operation differs')
            if stage == 'spawn':
                require(packet['tool'] == 'multi_agent_v1__spawn_agent' and intent['canonical_tool'] == 'spawn_agent' and
                        packet['arguments'] == dict(message=route['message']), 'spawn result differs from original route')
                from host_adapter import unwrap
                if response.get('error'):
                    raw = {}
                elif isinstance(response.get('result'), dict) and response['result'].get('isError'):
                    rejected = response['result']
                    raw = dict(error=dict(message='\n'.join(b['text'] for b in rejected.get('content', [])
                        if b.get('type') == 'text'), raw=rejected))
                else:
                    raw = unwrap(response['result'])
                returned = raw.get('agent_id', raw.get('id')) if not raw.get('error') else None
                require(returned == actual and (actual is None or isinstance(actual, str) and actual.strip()),
                        'actual spawn identity differs from raw host result')
                from host_adapter import capacity_error
                detail = response.get('error') or raw.get('error')
                detail = detail if isinstance(detail, dict) else dict(message=str(detail))
                if actual is None and capacity_error(detail):
                    capacity_detail = detail
            elif stage == 'observe':
                require(owner is not None, 'observation has no known executor')
                target = state['runtime']['executors'][owner]['routes']['native_agent']['target_id']
                require(packet['tool'] == 'multi_agent_v1__wait_agent' and intent['canonical_tool'] == 'wait_agent' and
                        packet['arguments'] == dict(targets=[target], timeout_ms=10000) and actual == target,
                        'observation differs from registered executor')
            else:
                message = self._handoff_message(request, owner)
                expected = dict(threadId=self.integration_target, prompt=message) if self.integration.channel == 'app_thread' else dict(
                    id=self.integration_target, message=message, interrupt=False)
                require(stage == 'handoff' and old.get('handoff_intent') and packet['tool'] == self.integration.names['send'] and
                        packet['arguments'] == expected and actual is None and request['integration_agent'] == self.integration_target,
                        'handoff result differs from original recipient')
            retained = deepcopy(old.get('retained_host_results', {}))
            if stage in retained:
                require(retained[stage] == result_ref, 'conflicting retained host result')
                return self.status(identity)
            if stage == 'spawn' and actual:
                executor_id = 'native_agent:' + actual
                require(owner in (None, executor_id), 'late result cannot replace an existing owner')
                require(executor_id not in state['runtime']['executors'] or owner==executor_id and
                    state['runtime']['operations'][identity+':launch'].get('executor_id')==executor_id,
                    'late spawn cannot adopt another existing executor or reconstruct a historical mapping')
                self.runtime.bind_executor(executor_id, {'native_agent': dict(target_id=actual, host_id='current')},
                    self.actor, result_ref, task_id=task_id, attempt_id=identity+':launch')
                executor = self.runtime._load()['runtime']['executors'][executor_id]
                self._register(request, dict(id=task_id + ':agent', kind='agent', executor_id=executor_id,
                                             generation=executor['generation']))
            retained[stage] = result_ref
            if capacity_detail:
                reason = capacity_detail.get('message') or capacity_detail.get('code') or 'native capacity refused'
                self._record(identity+':launch', status='capacity', error=capacity_detail,
                    code='agent_capacity_exceeded', reason=reason)
                self._record(identity, retained_host_results=retained, status='capacity', reason=reason,
                    next_action='Resolve native capacity; RESOURCE permit is separate; no automatic retry',
                    next_owner=self.integration_target)
                return self.status(identity)
            self._record(identity, retained_host_results=retained, status='needs_reconcile',
                reason='Known host result retained; execution/delivery not inferred', next_action='observe_existing_executor_or_delivery',
                next_owner=self.integration_target)
            return self.status(identity)

    def status(self, identity):
        state = self.runtime._load()
        record = state['runtime']['operations'][identity]
        task = state['tasks'].get(record['task_id'], {})
        return dict(request_id=identity, task_id=record['task_id'], status=record['status'],
            owner_agent_id=task.get('owner_agent_id'), worktree=task.get('worktree'), branch=task.get('branch'),
            ledger=str(self.runtime.path), revision=state['revision'], record=deepcopy(record),
            resources=[deepcopy(r) for r in state['runtime'].get('resources', {}).values() if r['task_id'] == record['task_id']],
            next_owner=self.integration_target, scope=self.developer.scope, handoff_is_delivery=False, idle_wakeup=False)
