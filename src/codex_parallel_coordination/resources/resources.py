#!/usr/bin/env python3
"""One explicitly authorized local resource instance, finite launches and exit events."""
import argparse
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import time
import uuid

if __package__:
    from .support import ERRORS, artifact, fingerprint, load, load_bound, ref, require, write_atomic, writer_lock, phase_keys, project_roots, text, cleanup_verified
    from .instance_lock import instance_lock
    from .windows_resources import Job, observe_job, pid_identity, reclaim_idle_job, system_memory
else:
    from support import ERRORS, artifact, fingerprint, load, load_bound, ref, require, write_atomic, writer_lock, phase_keys, project_roots, text, cleanup_verified
    from instance_lock import instance_lock
    from windows_resources import Job, observe_job, pid_identity, reclaim_idle_job, system_memory


class Controller:
    def __init__(self, path, memory=None):
        self.path = Path(path).resolve()
        self.memory = memory or system_memory

    @contextmanager
    def transaction(self):
        with instance_lock(self.path), writer_lock(self.path):
            yield

    def read(self):
        state = load(self.path)  # Missing/corrupt state is never initialized silently.
        require(state['schema'] == 'coordination-resources/2' and isinstance(state['requests'], dict), 'resource state invalid/legacy; explicit copy migration required')
        require(type(state.get('revision')) is int and state['revision'] >= 0 and isinstance(state.get('history'), list)
                and isinstance(state.get('capacity_requests'), dict) and isinstance(state.get('cleanup_resources'), dict),
                'corrupt resource bookkeeping; preserve without reinitializing')
        policy = load_bound(state['policy'])
        authority = load_bound(policy['authority'])
        require(authority['scope'] == 'explicit_temporary_managed_resources' and authority['launch'] is True,
                'temporary process authority missing or changed')
        phase_keys(policy)
        for request in state['requests'].values():
            phase_keys(policy, request['spec'])
            require(request['spec_digest'] == fingerprint(request['spec']), 'stored request content changed; reconcile')
            require(request['status'] in ('queued', 'starting', 'running', 'idle', 'launch_unknown', 'completed', 'failed', 'reclaimed', 'recovered_exit'),
                    'unknown resource lifecycle state')
            if request['status'] != 'queued':
                require(text(request.get('nonce')) and request.get('job_name') == 'Local\\CoordinationPlugin-' + request['nonce'],
                        'execution/Job identity missing or changed')
        return state, policy

    def save(self, state, operation, **details):
        state['revision'] += 1
        state['history'].append(dict(revision=state['revision'], operation=operation, observed_at=time.time(), **details))
        write_atomic(self.path, state)

    def initialize(self, policy_path):
        policy = load(policy_path)
        require(type(policy['max_parallel']) is int and policy['max_parallel'] > 0 and
                all(type(policy[k]) is int and policy[k] >= 0 for k in ('physical_reserve', 'commit_reserve')),
                'explicit instance concurrency and memory margins required')
        require(policy['projects'] and policy['executables'] and isinstance(policy['exclusive_keys'], list), 'instance project/executable/resource scope required')
        phase_keys(policy)
        authority = load_bound(policy['authority'])
        require(authority['scope'] == 'explicit_temporary_managed_resources' and authority['launch'] is True,
                'temporary process authority missing')
        scopes = [(key, path) for key in policy['projects'] for path in project_roots(policy, key)]
        for key, root in scopes:
            require(root.is_dir(), 'existing absolute allowed project/work root required')
            require(not any(key != other and (root.is_relative_to(path) or path.is_relative_to(root)) for other, path in scopes),
                    'project resource roots overlap; refuse cross-project authority mixing')
        for exe in policy['executables']:
            require(Path(exe).is_absolute() and Path(exe).is_file(), 'absolute allowed executable required')
        with self.transaction():
            require(not self.path.exists(), 'resource instance exists; recover instead of replacing')
            state = {'schema': 'coordination-resources/2', 'revision': 0, 'policy': ref(policy_path),
                     'requests': {}, 'capacity_requests': {}, 'cleanup_resources': {}, 'history': [], 'model_capacity_events': [], 'agent_capacity_events': []}
            write_atomic(self.path, state)
            self.read()
        return {'initialized': str(self.path), 'scope': 'registered temporary jobs only'}

    def submit(self, spec_path, run=True):
        spec = load(spec_path)
        with self.transaction():
            state, policy = self.read()
            key = spec['request_id']
            require(all(text(spec.get(k)) for k in ('request_id', 'project_id', 'task_id', 'attempt_id', 'launch_identity', 'phase', 'purpose'))
                    and spec['project_id'] in policy['projects'], 'nonempty project/task/attempt/executor/request required')
            roots = project_roots(policy, spec['project_id'])
            cwd = Path(spec['cwd'])
            require(cwd.is_absolute() and any(cwd.resolve().is_relative_to(root) for root in roots) and cwd.is_dir(), 'request outside authorized project/work roots')
            require(spec.get('task_id') and spec.get('phase') and spec.get('purpose') and spec.get('launch_identity'), 'task/stage/owner/purpose missing')
            require(isinstance(spec['argv'], list) and spec['argv'] and all(isinstance(x, str) for x in spec['argv']) and
                    spec['argv'][0] in policy['executables'], 'executable/argv not authorized')
            require(type(spec['peak_bytes']) is int and spec['peak_bytes'] > 0 and
                    isinstance(spec['exclusive_keys'], list) and
                    set(spec['exclusive_keys']).issubset(policy['exclusive_keys']) and
                    len(set(spec['exclusive_keys'])) == len(spec['exclusive_keys']), 'memory budget/resource keys invalid')
            phase_keys(policy, spec)
            require(spec['protection'] in ('running', 'waiting_user', 'none') and type(spec['reclaim_allowed']) is bool,
                    'explicit protection/reclaim permission required')
            require(spec.get('resource_kind') in ('temporary_auxiliary', 'compute', 'acceptance', 'regression', 'shared_tool'), 'explicit resource kind required')
            require(spec['resource_kind'] == 'temporary_auxiliary' or spec['protection'] != 'none' and not spec['reclaim_allowed'],
                    'compute/acceptance/regression/shared tools are always protected')
            require(not spec['reclaim_allowed'] or load_bound(policy['authority']).get('reclaim_idle') is True,
                    'reclaim scope not authorized')
            require(type(spec.get('task_cleanup_allowed', False)) is bool and
                    (not spec.get('task_cleanup_allowed') or load_bound(policy['authority']).get('cleanup_task_processes') is True),
                    'persistent task process cleanup needs explicit authority')
            if key in state['requests']:
                require(state['requests'][key]['spec_digest'] == fingerprint(spec), 'conflicting duplicate request; no restart')
                return copy.deepcopy(state['requests'][key])
            require(not any(tuple(r['spec'][k] for k in ('project_id', 'task_id', 'attempt_id')) ==
                            tuple(spec[k] for k in ('project_id', 'task_id', 'attempt_id')) for r in state['requests'].values()),
                    'same project/task/attempt already registered under another request id')
            state['requests'][key] = {'spec': spec, 'spec_digest': fingerprint(spec), 'source': ref(spec_path),
                'status': 'queued', 'sequence': len(state['requests']), 'nonce': None, 'reason': 'awaiting admission',
                'protection': spec['protection'], 'observations': []}
            self.save(state, 'submitted', request_id=key)
        if run:
            self.pump()
        return self.read()[0]['requests'][key]

    def admission(self, state, policy, request):
        sample = self.memory()
        require(sample.get('injected') is False or policy.get('allow_test_memory_injection') is True,
                'injected memory is test-only and needs explicit instance permission')
        require(all(type(sample.get(k)) is int and sample[k] >= 0 for k in ('physical_available', 'commit_available')),
                'physical and commit availability required')
        active = [r for r in state['requests'].values() if r['status'] in ('starting', 'running', 'launch_unknown', 'idle')]
        reasons = []
        if len(active) >= policy['max_parallel']:
            reasons.append('local_process_slots')
        used_keys = {k for r in active for k in r['spec']['exclusive_keys']}
        if used_keys.intersection(request['spec']['exclusive_keys']):
            reasons.append('exclusive_stage_resource')
        # Deliberately conservative budget headroom, not summed/shared working sets.
        reserved = sum(r['spec']['peak_bytes'] for r in active)
        for kind, margin in (('physical', 'physical_reserve'), ('commit', 'commit_reserve')):
            if sample[kind + '_available'] - reserved - request['spec']['peak_bytes'] < policy[margin]:
                reasons.append(kind + '_headroom')
        return {'sample': sample, 'budget_reserved': reserved, 'reasons': reasons,
                'allowed': not reasons, 'model_capacity': 'separate/not inferred', 'agent_slots': 'not measured here'}

    def claim(self):
        with self.transaction():
            state, policy = self.read()
            pending = sorted((r for r in state['requests'].values() if r['status'] == 'queued'), key=lambda r: r['sequence'])
            if not pending:
                return None
            request = pending[0]  # FIFO; an exclusive/low-memory first item is not silently bypassed.
            admission = self.admission(state, policy, request)
            request['observations'].append(admission)
            request['reason'] = ','.join(admission['reasons'])
            if not admission['allowed']:
                self.save(state, 'queued_after_admission', request_id=request['spec']['request_id'])
                return None
            artifact(request['source'])
            require(load_bound(request['source']) == request['spec'], 'queued launch specification changed')
            request.update(status='starting', nonce=str(uuid.uuid4()), launcher=pid_identity(os.getpid()))
            request['job_name'] = 'Local\\CoordinationPlugin-' + request['nonce']
            self.save(state, 'permit_reserved', request_id=request['spec']['request_id'], nonce=request['nonce'])
            return copy.deepcopy(request)

    def pump(self):
        completed = []
        while True:  # Finite queue drain driven by this request/exit, no periodic wakeup.
            request = self.claim()
            if request is None:
                return completed
            key, nonce = request['spec']['request_id'], request['nonce']
            logs = self.path.parent / 'process-evidence' / nonce
            logs.mkdir(parents=True)
            job = None
            try:
                job = Job(request['job_name'])
                identity = job.launch(request['spec']['argv'], request['spec']['cwd'], logs)
                with self.transaction():
                    state, _ = self.read()
                    current = state['requests'][key]
                    require(current['nonce'] == nonce and current['status'] == 'starting', 'launch reservation changed')
                    current.update(status='running', process=identity, logs=str(logs))
                    self.save(state, 'process_registered_before_resume', request_id=key, process=identity)
                job.start()
                events = []
                result = job.wait(lambda event, sample: events.append({'event': event, 'sample': sample}))
                result.update(request_id=key, nonce=nonce, process=identity)
                write_atomic(logs / 'process-events.json', events)
                write_atomic(logs / 'tree-exit.json', result)
                with self.transaction():
                    state, _ = self.read()
                    current = state['requests'][key]
                    require(current['nonce'] == nonce, 'exit belongs to another execution')
                    current['exit_evidence'] = ref(logs / 'tree-exit.json')
                    self.save(state, 'tree_exit_evidence_bound', request_id=key)
                self.finish(key, nonce, result, ref(logs / 'tree-exit.json'))
                completed.append(key)
            except BaseException:
                # Never retry/release an uncertain launch. Recovery inspects actual Job/PID identity.
                with self.transaction():
                    state, _ = self.read()
                    state['requests'][key]['status'] = 'launch_unknown'
                    self.save(state, 'launch_or_monitor_exception', request_id=key)
                raise
            finally:
                if job is not None:
                    job.close()

    def finish(self, key, nonce, result, evidence):
        require(result.get('tree_empty') is True, 'process tree is not proven empty')
        with self.transaction():
            state, _ = self.read()
            request = state['requests'][key]
            require(request['nonce'] == nonce, 'another execution owns this permit')
            if request['status'] in ('completed', 'failed', 'reclaimed', 'recovered_exit'):
                return {'duplicate': True}
            observed = observe_job(request['job_name'])
            require(observed['idle_proven'], 'actual Job still has active/unknown processes')
            require(request.get('exit_evidence') == evidence and load_bound(evidence) == result and result.get('request_id') == key and result.get('nonce') == nonce
                    and result.get('process') == request['process'], 'exit evidence does not bind this execution/result')
            request.update(status='reclaimed' if request.get('reclaim_request') else 'completed' if result['root_exit_code'] == 0 else 'failed',
                result=result, result_evidence=evidence, reason='tree exit event verified')
            self.save(state, 'permit_released', request_id=key, nonce=nonce)
        return {'released': key}

    def recover(self, key):
        with self.transaction():
            state, _ = self.read()
            request = state['requests'][key]
            if request['status'] in ('completed', 'failed', 'reclaimed', 'recovered_exit'):
                return {'duplicate': True, 'status': request['status']}
            require(request.get('job_name') and request.get('process'), 'uncertain pre-registration launch; no automatic restart/release')
            observed = observe_job(request['job_name'])
            if observed['members']:
                root = [x for x in observed['members'] if x['pid'] == request['process']['pid']]
                require(not root or root[0]['created'] == request['process']['created'], 'PID identity changed; no reclaim')
                return {'status': 'still_running', 'observation': observed, 'permit_retained': True}
            try:
                actual = pid_identity(request['process']['pid'])
                require(actual['created'] == request['process']['created'], 'PID identity changed; no release')
            except OSError as error:
                require(error.winerror == 87, 'process lookup unavailable; do not infer exit')
                actual = None
            if actual and actual['running']:
                return {'status': 'still_running', 'observation': observed, 'root': actual, 'permit_retained': True}
            exit_path = Path(request['logs']) / 'tree-exit.json'
            if not observed['idle_proven'] and not exit_path.is_file():
                # On this Windows host the *name* can disappear after the monitor's
                # last handle closes while a member survives. Missing name/PID alone
                # is therefore never sufficient proof that descendants are gone.
                return {'status': 'unknown_process_tree', 'permit_retained': True,
                        'next_owner': request['spec']['launch_identity'],
                        'required_action': 'restore actual whole-tree exit evidence; no restart or timeout-based release'}
            recorded = load_bound(request['exit_evidence']) if request.get('exit_evidence') else None
            if exit_path.is_file() and not recorded:
                return {'status': 'unknown_process_tree', 'permit_retained': True,
                        'reason': 'unbound exit file is not trusted whole-tree completion evidence'}
            if recorded:
                require(recorded.get('tree_empty') is True and recorded.get('request_id') == key and
                        recorded.get('nonce') == request['nonce'] and recorded.get('process') == request['process'],
                        'exit record source/identity does not match this execution')
            result = {'job': observed, 'root_identity': actual, 'tree_empty': True,
                      'root_exit_code': recorded['root_exit_code'] if recorded else actual['exit_code'] if actual else None,
                      'recorded_exit_evidence': ref(exit_path) if recorded else None,
                      'outcome': 'resource lifecycle only; no business/test pass inferred'}
            path = self.path.parent / 'process-evidence' / request['nonce'] / ('recovery-' + str(uuid.uuid4()) + '.json')
            write_atomic(path, result)
            request.update(status='recovered_exit', recovery_evidence=ref(path), result=result,
                           reason='registered Job empty/destroyed and original root ended; actual one-shot recovery')
            self.save(state, 'permit_released_after_actual_recovery', request_id=key, nonce=request['nonce'])
        return {'released': key, 'status': 'recovered_exit', 'next_started_from_release_event': self.pump()}

    def idle(self, key, project, task, nonce, evidence):
        with self.transaction():
            state, policy = self.read()
            request = state['requests'][key]
            spec = request['spec']
            require((spec['project_id'], spec['task_id'], request['nonce']) == (project, task, nonce),
                    'another project/task/execution owns this resource')
            require(request['status'] == 'running' and request['protection'] == 'none' and
                    spec['reclaim_allowed'] and load_bound(policy['authority']).get('reclaim_idle') is True,
                    'computing/waiting-user/protected or unapproved resource cannot be reclaimed')
            proof = load_bound(evidence)
            require(proof['project_id'] == project and proof['task_id'] == task and proof['nonce'] == nonce and
                    proof['no_longer_needed'] is True and proof.get('basis'),
                    'actual owner release/use evidence required; CPU idleness is not evidence')
            actual = pid_identity(request['process']['pid'])
            require(actual['created'] == request['process']['created'] and actual['running'], 'PID identity changed')
            request.update(status='idle', idle_evidence=evidence)
            self.save(state, 'owner_released_use', request_id=key)
        return {'idle_recorded': key, 'permit_retained_until_tree_exit': True}

    def reclaim(self, key, project, task, nonce):
        with self.transaction():
            state, policy = self.read()
            request = state['requests'][key]
            spec = request['spec']
            require((spec['project_id'], spec['task_id'], request['nonce']) == (project, task, nonce),
                    'another project/task/execution owns this resource')
            require(request['status'] == 'idle' and request['protection'] == 'none' and spec['reclaim_allowed'] and
                    load_bound(policy['authority']).get('reclaim_idle') is True, 'resource still protected/in use/not authorized')
            require(spec['resource_kind'] == 'temporary_auxiliary', 'only temporary auxiliary resources may be reclaimed')
            artifact(request['idle_evidence'])
            if request.get('reclaim_request'):
                return {'duplicate': True, 'termination_requested': True, 'whole_tree_exit_confirmed': False}
            members = observe_job(request['job_name'])['members']
            identities = {(m['pid'], m['created']) for m in members}
            for other in state['requests'].values():
                if other is request or other['status'] not in ('starting', 'running', 'idle', 'launch_unknown') or not other.get('job_name'):
                    continue
                overlap = identities.intersection((m['pid'], m['created']) for m in observe_job(other['job_name'])['members'])
                require(not overlap, 'process tree overlaps another permit; no reclaim')
            result = reclaim_idle_job(request['job_name'], request['process'])
            request['reclaim_request'] = result
            self.save(state, 'authorized_idle_reclaim_requested', request_id=key)
        return result

    def task_cleanup(self, key, project, task, nonce, proof_ref, verifier=None):
        """Canonical completed-task environment cleanup; not ordinary process exit."""
        with self.transaction():
            state, policy = self.read()
            request = state['requests'][key]
            spec = request['spec']
            require((spec['project_id'], spec['task_id'], request['nonce']) == (project, task, nonce), 'another task/execution owns this process')
            if request['status'] in ('completed', 'failed', 'reclaimed', 'recovered_exit'):
                return {'duplicate': True, 'status': 'process_tree_already_released'}
            require(request['status'] in ('running', 'idle') and spec['resource_kind'] != 'shared_tool' and spec.get('task_cleanup_allowed') is True
                    and load_bound(policy['authority']).get('cleanup_task_processes') is True,
                    'task process cleanup not authorized; pending_cleanup')
            proof = load_bound(proof_ref)
            require(callable(verifier) and all(proof.get(k) == value for k, value in
                    (('project_id', project), ('task_id', task), ('attempt_id', spec['attempt_id']), ('nonce', nonce))),
                    'canonical task cleanup proof required')
            cleanup_verified(verifier(copy.deepcopy(spec), copy.deepcopy(proof)))
            if request.get('reclaim_request'):
                return {'duplicate': True, 'termination_requested': True, 'whole_tree_exit_confirmed': False}
            own = {(m['pid'], m['created']) for m in observe_job(request['job_name'])['members']}
            for other in state['requests'].values():
                if other is not request and other.get('job_name') and other['status'] in ('starting', 'running', 'idle', 'launch_unknown'):
                    require(not own.intersection((m['pid'], m['created']) for m in observe_job(other['job_name'])['members']),
                            'shared process tree; pending_cleanup')
            result = reclaim_idle_job(request['job_name'], request['process'])
            request.update(reclaim_request=result, cleanup_proof=proof_ref, protection='none')
            self.save(state, 'task_completed_process_cleanup_requested', request_id=key)
            return dict(result, whole_tree_exit_confirmed=False, task_completed_changed=False)

    def model_capacity(self, source):
        value = load_bound(source)
        require(value['category'] == 'model_capacity' and text(value.get('model')) and text(value.get('event_id')), 'explicit model event required')
        with self.transaction():
            state, _ = self.read()
            previous = next((x for x in state['model_capacity_events'] if x['event_id'] == value['event_id']), None)
            if previous:
                require(previous['evidence'] == source, 'conflicting duplicate model capacity event')
            else:
                state['model_capacity_events'].append(dict(value, evidence=source))
                self.save(state, 'model_capacity_observed_no_model_switch')
        return {'category': 'model_capacity', 'local_permits_released': False, 'model_switched': False}

    def agent_capacity(self, source):
        value = load_bound(source)
        require(value.get('category') == 'agent_thread_limit' and text(value.get('event_id')) and text(value.get('host_adapter')),
                'explicit native agent capacity event required; not model or memory pressure')
        with self.transaction():
            state, _ = self.read()
            previous = next((x for x in state['agent_capacity_events'] if x['event_id'] == value['event_id']), None)
            if previous:
                require(previous['evidence'] == source, 'conflicting duplicate capacity event')
            else:
                state['agent_capacity_events'].append({'event_id': value['event_id'], 'evidence': source})
                self.save(state, 'agent_capacity_observed_no_running_or_release_inferred')
        return {'category': 'agent_execution_slot', 'permits_released': False, 'agent_started': False,
                'model_or_process_memory_changed': False}

    def snapshot(self, project_id=None):
        """Read-only OS-mutex snapshot, no lock file/state write or task stage."""
        with instance_lock(self.path):
            require(not self.path.with_suffix(self.path.suffix + '.lock').exists(),
                    'resource writer lock present; snapshot requires reconcile, no trusted projection')
            state, policy = self.read()
            require(project_id is None or project_id in policy['projects'], 'unknown snapshot project')
            requests, unique, measured_complete = [], {}, True
            for key, r in sorted(state['requests'].items(), key=lambda pair: pair[1]['sequence']):
                spec = r['spec']
                if project_id is not None and spec['project_id'] != project_id:
                    continue
                view = {k: spec[k] for k in ('project_id', 'task_id', 'attempt_id', 'launch_identity', 'resource_kind')}
                view.update(request_id=key, permit_id=r['nonce'], resource_status=r['status'], reason=r.get('reason'),
                            protection=r['protection'], sequence=r['sequence'])
                try:
                    artifact(r['source'])
                    view['source_current'] = True
                except ERRORS:
                    view.update(source_current=False, needs_reconcile=True, reason='registered launch evidence changed/missing')
                if r.get('job_name') and r['status'] in ('starting', 'running', 'idle', 'launch_unknown'):
                    observation = observe_job(r['job_name'], measure=True)
                    measured_complete = measured_complete and observation['exists'] and 'race' not in observation
                    view['tree'] = observation
                    for member in observation['members']:
                        identity = (member['pid'], member['created'])
                        unique.setdefault(identity, dict(member, requests=[]))['requests'].append(key)
                requests.append(view)
            return {'schema': 'coordination-resource-snapshot/1', 'component_version': '1.1.2', 'resource_revision': state['revision'],
                    'observed_at': time.time(), 'project_id': project_id, 'requests': requests,
                    'system_memory': self.memory(), 'unique_processes': list(unique.values()),
                    'capacity_permits': [copy.deepcopy(r) for r in state['capacity_requests'].values()
                                         if project_id is None or r['spec']['project_id'] == project_id],
                    'resource_categories': ['agent_execution_slot', 'model_request_capacity', 'managed_process_cpu_memory', 'acceptance_environment_storage'],
                    'model_capacity_observation_count': len(state['model_capacity_events']),
                    'agent_capacity_observation_count': len(state['agent_capacity_events']),
                    'global_native_agent_capacity': 'Unknown; max_agent_slots governs plugin-owned permits only',
                    'agent_runtime_authority': 'CORE only; these are quota permits, not native agent leases',
                    'acceptance_storage_accounting': 'Registered owned artifacts only; usage not measured or whole-machine managed',
                    'cleanup_resources': [{'resource_id': key, 'project_id': r['spec']['project_id'], 'kind': r['spec']['kind'],
                                           'cleanup_status': r['status'], 'references': r['spec']['references'], 'protected': r['spec']['protected']}
                                          for key, r in state['cleanup_resources'].items() if project_id is None or r['spec']['project_id'] == project_id],
                    'measurement_complete': measured_complete,
                    'private_commit_total': sum(x['private_commit'] for x in unique.values()) if measured_complete and all('private_commit' in x for x in unique.values()) else None,
                    'shared_working_sets_summed': False, 'no_task_stage_stored': True,
                    'external_memory_events': 'No subscription; rechecked at next explicit entry or managed exit event'}

    def _capacity_drain(self, state, policy):
        limit = policy.get('max_agent_slots')
        require(type(limit) is int and limit > 0, 'explicit plugin-owned agent slot quota required')
        requests = state['capacity_requests']
        used = sum(r['status'] == 'granted' for r in requests.values())
        granted = []
        for key, request in sorted(requests.items(), key=lambda pair: pair[1]['sequence']):
            if used >= limit:
                break
            if request['status'] == 'queued':
                request.update(status='granted', permit_id=str(uuid.uuid4()))
                used += 1
                granted.append(key)
        return granted

    def capacity_acquire(self, spec_path):
        """Reserve only a plugin-owned slot; never creates/resumes an agent."""
        spec = load(spec_path)
        allowed = {'request_id', 'project_id', 'task_id', 'attempt_id', 'launch_identity', 'resource_class'}
        require(isinstance(spec, dict) and set(spec) == allowed and all(text(spec[k]) for k in allowed),
                'capacity identity only; no task stages/native lease fields')
        require(spec['resource_class'] == 'agent_execution_slot', 'unsupported capacity resource class')
        with self.transaction():
            state, policy = self.read()
            require(spec['project_id'] in policy['projects'], 'unknown capacity project')
            requests = state['capacity_requests']
            key = spec['request_id']
            if key in requests:
                require(requests[key]['spec_digest'] == fingerprint(spec), 'conflicting capacity request')
                return copy.deepcopy(requests[key])
            require(not any(tuple(r['spec'][k] for k in ('project_id', 'task_id', 'attempt_id')) ==
                            tuple(spec[k] for k in ('project_id', 'task_id', 'attempt_id')) for r in requests.values()),
                    'same capacity attempt already registered')
            requests[key] = {'spec': spec, 'spec_digest': fingerprint(spec), 'status': 'queued',
                             'permit_id': None, 'sequence': len(requests)}
            grants = self._capacity_drain(state, policy)
            self.save(state, 'capacity_requested', request_id=key, newly_granted=grants)
            return copy.deepcopy(requests[key])

    def capacity_release(self, request_id, permit_id, receipt_ref, verifier=None):
        """CORE-confirmed result only. Verifier reads canonical state, no host I/O."""
        with self.transaction():
            state, policy = self.read()
            request = state['capacity_requests'][request_id]
            require(request['permit_id'] == permit_id and text(permit_id), 'another capacity permit owns this request')
            if request['status'] == 'released':
                require(request['release_receipt'] == receipt_ref, 'different duplicate capacity release evidence')
                artifact(receipt_ref)
                return {'duplicate': True, 'released': request_id, 'newly_granted': []}
            require(request['status'] == 'granted' and callable(verifier), 'canonical CORE receipt verifier required')
            receipt = load_bound(receipt_ref)
            context = dict(request['spec'], permit_id=permit_id)
            require(all(receipt.get(k) == context[k] for k in ('request_id', 'project_id', 'task_id', 'attempt_id', 'permit_id'))
                    and receipt.get('outcome') in ('confirmed_closed', 'confirmed_not_started'),
                    'idle/completed/failed/unknown is not confirmed native close/not-started')
            verified = verifier(copy.deepcopy(context), copy.deepcopy(receipt))
            require(isinstance(verified, dict) and verified.get('verified') is True,
                    'canonical verification required; old boolean-only cleanup draft withdrawn')
            if receipt['outcome'] == 'confirmed_closed':
                cleanup_verified(verified)
            else:
                require(verified.get('not_started') is True, 'unused reservation must be confirmed never started')
            request.update(status='released', release_receipt=receipt_ref)
            grants = self._capacity_drain(state, policy)
            self.save(state, 'capacity_released_after_CORE_confirmation', request_id=request_id, newly_granted=grants)
            return {'released': request_id, 'newly_granted': grants,
                    'process_memory_or_acceptance_storage_released': False, 'native_operation_executed_here': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'submit', 'model-capacity', 'agent-capacity'):
        sub.add_parser(name).add_argument('input', type=Path)
    sub.add_parser('pump')
    sub.add_parser('show')
    sub.add_parser('snapshot').add_argument('--project')
    sub.add_parser('enqueue').add_argument('input', type=Path)
    sub.add_parser('capacity-acquire').add_argument('input', type=Path)
    sub.add_parser('recover').add_argument('request_id')
    for name in ('idle', 'reclaim'):
        p = sub.add_parser(name)
        p.add_argument('request_id')
        p.add_argument('--project', required=True)
        p.add_argument('--task', required=True)
        p.add_argument('--nonce', required=True)
        if name == 'idle':
            p.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    require(args.state.is_absolute(), 'absolute instance resource file required')
    controller = Controller(args.state)
    result = controller.initialize(args.input) if args.command == 'init' else \
        controller.submit(args.input) if args.command == 'submit' else \
        controller.submit(args.input, run=False) if args.command == 'enqueue' else \
        controller.snapshot(args.project) if args.command == 'snapshot' else \
        controller.capacity_acquire(args.input) if args.command == 'capacity-acquire' else \
        controller.pump() if args.command == 'pump' else \
        controller.recover(args.request_id) if args.command == 'recover' else \
        controller.idle(args.request_id, args.project, args.task, args.nonce, ref(args.evidence)) if args.command == 'idle' else \
        controller.reclaim(args.request_id, args.project, args.task, args.nonce) if args.command == 'reclaim' else \
        controller.model_capacity(ref(args.input)) if args.command == 'model-capacity' else \
        controller.agent_capacity(ref(args.input)) if args.command == 'agent-capacity' else controller.read()[0]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except ERRORS as error:
        print(json.dumps({'error': str(error), 'no_permission_or_budget_expansion': True}), file=sys.stderr)
        sys.exit(1)
