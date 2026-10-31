"""Resource API additions; agent host is injected, never actual Codex agents."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import test_resources as fixture
from resources import Controller
from support import fingerprint, load, ref, write_atomic


class AddedResourceTests(unittest.TestCase):
    setUp = fixture.ResourceTests.setUp
    file = fixture.ResourceTests.file
    request = fixture.ResourceTests.request
    start_waiting = fixture.ResourceTests.start_waiting

    def quota(self, request_id, project='alpha', attempt='1'):
        return self.file('capacity-' + request_id + '.json', {
            'request_id': request_id, 'project_id': project, 'task_id': request_id,
            'attempt_id': attempt, 'launch_identity': 'fixture-owned-executor', 'resource_class': 'agent_execution_slot'})

    def receipt(self, request, outcome='confirmed_closed', **changes):
        value = {k: request['spec'][k] for k in ('request_id', 'project_id', 'task_id', 'attempt_id')}
        value.update(permit_id=request['permit_id'], outcome=outcome)
        value.update(changes)
        return ref(self.file('receipt-' + request['spec']['request_id'] + '-' + outcome + '.json', value))

    @staticmethod
    def completed_guard(**changes):
        facts = {k: True for k in ('verified', 'task_completed_on_main', 'preservation_valid', 'no_active_work',
                 'no_active_descendants', 'no_release_dependencies', 'exclusive_task_resource', 'cleanup_authorized')}
        facts['protected'] = False
        facts.update(changes)
        return lambda context, receipt: facts

    def test_capacity_three_limit_failures_then_four_reused_are_protected(self):
        # User incident fixture: failed spawn is not running, then old actors reused.
        policy = dict(self.policy, max_agent_slots=4)
        path = self.root / 'four/resources.json'
        control = Controller(path)
        control.initialize(self.file('four-policy.json', policy))
        actors = [control.capacity_acquire(self.quota('reused-' + str(i))) for i in range(4)]
        self.assertTrue(all(a['status'] == 'granted' for a in actors))
        self.file('reported-spawn-limit.json', {'injected_host_events': ['agent_thread_limit_reached'] * 3,
                  'actual_host_probed': False, 'reuse_now_active_count': 4})
        for i in range(3):
            control.agent_capacity(ref(self.file('limit-' + str(i) + '.json', {
                'category': 'agent_thread_limit', 'event_id': 'fixture-limit-' + str(i), 'host_adapter': 'injected-only'})))
        before = path.read_bytes()
        for actor in actors:
            # CORE authoritative fixture says now running, irrespective of old completed.
            proof = self.receipt(actor)
            with self.assertRaises(ValueError):
                control.capacity_release(actor['spec']['request_id'], actor['permit_id'], proof,
                                         self.completed_guard(task_completed_on_main=False, no_active_work=False))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(control.capacity_acquire(self.quota('fifth'))['status'], 'queued')

    def test_agent_capacity_release_fifo_duplicate_and_resume_new_attempt(self):
        first = self.controller.capacity_acquire(self.quota('old'))
        queued = self.controller.capacity_acquire(self.quota('next', 'beta'))
        self.assertEqual(queued['status'], 'queued')
        proof = self.receipt(first)
        result = self.controller.capacity_release('old', first['permit_id'], proof, self.completed_guard())
        self.assertEqual(result['newly_granted'], ['next'])
        self.assertFalse(result['process_memory_or_acceptance_storage_released'])
        self.assertTrue(self.controller.capacity_release('old', first['permit_id'], proof)['duplicate'])
        resumed = self.controller.capacity_acquire(self.quota('old-resume', attempt='2'))
        self.assertEqual(resumed['status'], 'queued')
        nxt = load(self.path)['capacity_requests']['next']
        self.controller.capacity_release('next', nxt['permit_id'], self.receipt(nxt), self.completed_guard())
        self.assertEqual(load(self.path)['capacity_requests']['old-resume']['status'], 'granted')
        self.assertNotEqual(load(self.path)['capacity_requests']['old-resume']['permit_id'], first['permit_id'])

    def test_agent_failed_unknown_idle_completed_never_release(self):
        first = self.controller.capacity_acquire(self.quota('held'))
        before = self.path.read_bytes()
        for outcome in ('failed', 'unknown', 'idle', 'completed', 'accepted', 'app_chat_idle'):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                self.controller.capacity_release('held', first['permit_id'], self.receipt(first, outcome), self.completed_guard())
        self.assertEqual(self.path.read_bytes(), before)

    def test_agent_premerge_preservation_descendant_release_dependency_scope_authority(self):
        first = self.controller.capacity_acquire(self.quota('guarded'))
        proof = self.receipt(first)
        before = self.path.read_bytes()
        bad = ({'task_completed_on_main': False}, {'preservation_valid': False}, {'no_active_work': False},
               {'no_active_descendants': False}, {'no_release_dependencies': False}, {'exclusive_task_resource': False},
               {'cleanup_authorized': False}, {'protected': True}, {'verified': False})
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.controller.capacity_release('guarded', first['permit_id'], proof, self.completed_guard(**change))
        with self.assertRaises(ValueError):
            self.controller.capacity_release('guarded', first['permit_id'], proof, lambda *args: True)  # Withdrawn draft.
        self.assertEqual(before, self.path.read_bytes())

    def test_agent_changed_identity_or_evidence_and_no_verifier_retains_permit(self):
        first = self.controller.capacity_acquire(self.quota('identity'))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.capacity_release('identity', 'stale-generation', self.receipt(first), self.completed_guard())
        with self.assertRaises(ValueError):
            self.controller.capacity_release('identity', first['permit_id'], self.receipt(first, task_id='other'), self.completed_guard())
        proof = self.receipt(first)
        with self.assertRaises(ValueError):
            self.controller.capacity_release('identity', first['permit_id'], proof)
        Path(proof['path']).write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.controller.capacity_release('identity', first['permit_id'], proof, self.completed_guard())
        self.assertEqual(before, self.path.read_bytes())

    def test_no_agent_started_unused_reservation_release_is_not_cleanup(self):
        first = self.controller.capacity_acquire(self.quota('not-started'))
        proof = self.receipt(first, 'confirmed_not_started')
        result = self.controller.capacity_release('not-started', first['permit_id'], proof,
                                                   lambda *args: {'verified': True, 'not_started': True})
        self.assertFalse(result['native_operation_executed_here'])
        self.assertEqual(load(self.path)['capacity_requests']['not-started']['status'], 'released')

    def test_agent_permit_does_not_release_waiting_user_process_or_storage(self):
        proc, running = self.start_waiting(protection='waiting_user')
        storage = self.file('acceptance-storage.json', {'preserved': True})
        data = storage.read_bytes()
        first = self.controller.capacity_acquire(self.quota('seat-only'))
        self.controller.capacity_release('seat-only', first['permit_id'], self.receipt(first), self.completed_guard())
        self.assertIsNone(proc.poll())
        self.assertEqual(storage.read_bytes(), data)
        self.assertEqual(load(self.path)['requests']['alpha:A']['status'], 'running')
        self.assertEqual(load(self.path)['requests']['alpha:A']['protection'], 'waiting_user')
        proc.communicate(timeout=10)

    def test_capacity_restart_and_duplicate_request_never_creates_new_seat(self):
        path = self.quota('stable')
        first = self.controller.capacity_acquire(path)
        before = self.path.read_bytes()
        second = Controller(self.path).capacity_acquire(path)
        self.assertEqual(second, first)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.controller.capacity_acquire(self.file('bad-stage.json', dict(load(path), task_stage='completed')))
        self.assertEqual(self.path.read_bytes(), before)

    def test_external_memory_change_waits_for_next_explicit_entry(self):
        sample = {'source': 'explicit fixture', 'injected': True, 'physical_available': 1, 'commit_available': 10**12}
        control = Controller(self.path, memory=lambda: sample)
        request = control.submit(self.request())
        self.assertEqual(request['status'], 'queued')
        before = self.path.read_bytes()
        sample['physical_available'] = 10**12
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(control.pump(), ['alpha:A'])

    def test_protected_resource_kinds_reject_reclaim_opt_in(self):
        before = self.path.read_bytes()
        for kind in ('compute', 'acceptance', 'regression', 'shared_tool'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.controller.submit(self.request(resource_kind=kind, protection='none', reclaim_allowed=True))
        self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_actual_tree_deduplicates_alias_and_reclaim_rejects_overlap(self):
        proc, request = self.start_waiting()
        state = load(self.path)
        other = copy.deepcopy(request)
        other['spec'] = load(self.request('beta:B', 'beta'))
        other['source'] = ref(self.root / 'beta-B.json')
        other['spec_digest'] = fingerprint(other['spec'])
        state['requests']['beta:B'] = other  # Explicit alias injection for one actual owned Job.
        write_atomic(self.path, state)
        before = self.path.read_bytes()
        snapshot = self.controller.snapshot()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(len(snapshot['unique_processes']), len({(p['pid'], p['created']) for p in snapshot['unique_processes']}))
        self.assertTrue(all(len(p['requests']) == 2 for p in snapshot['unique_processes']))
        proof = self.file('idle.json', {'project_id': 'alpha', 'task_id': 'A', 'nonce': request['nonce'],
                                       'no_longer_needed': True, 'basis': 'owned auxiliary fixture'})
        self.controller.idle('alpha:A', 'alpha', 'A', request['nonce'], ref(proof))
        with self.assertRaisesRegex(ValueError, 'overlap'):
            self.controller.reclaim('alpha:A', 'alpha', 'A', request['nonce'])
        self.assertIsNone(proc.poll())
        proc.communicate(timeout=10)

    def test_exit_259_is_ended_not_still_active(self):
        result = self.controller.submit(self.request(body='raise SystemExit(259)'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['result']['root_exit_code'], 259)
        self.assertTrue(result['process']['running'])  # Historical registration observation, not current status.
        self.assertTrue(self.controller.recover('alpha:A')['duplicate'])

    def test_legacy_resource_state_not_silently_reinitialized(self):
        state = load(self.path)
        state['schema'] = 'coordination-resources/1'
        write_atomic(self.path, state)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.read()
        self.assertEqual(before, self.path.read_bytes())

    def test_stale_queued_launch_source_cannot_start_and_snapshot_marks_it(self):
        spec = self.request()
        self.controller.submit(spec, run=False)
        spec.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.controller.pump()
        snap = self.controller.snapshot('alpha')
        self.assertTrue(snap['requests'][0]['needs_reconcile'])
        self.assertFalse(snap['requests'][0]['source_current'])
        self.assertIsNone(load(self.path)['requests']['alpha:A']['nonce'])

    def test_two_OS_callers_atomically_reserve_one_agent_quota(self):
        processes = []
        for key, project in (('one', 'alpha'), ('two', 'beta')):
            processes.append(subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(fixture.SCRIPTS / 'resources.py'),
                '--state', str(self.path), 'capacity-acquire', str(self.quota(key, project))],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
        permits = load(self.path)['capacity_requests']
        self.assertEqual(sum(r['status'] == 'granted' for r in permits.values()), 1)
        self.assertEqual(sum(r['status'] == 'queued' for r in permits.values()), 1)

    def test_dedicated_acceptance_process_cleanup_requires_main_and_whole_tree_exit(self):
        authority = self.file('post-main-authority.json', {'scope': 'explicit_temporary_managed_resources', 'launch': True,
                              'reclaim_idle': True, 'cleanup_task_processes': True})
        policy = dict(self.policy, authority=ref(authority))
        path = self.root / 'post-main/resources.json'
        control = Controller(path)
        control.initialize(self.file('post-main-policy.json', policy))
        marker = self.root / 'acceptance-alive'
        body = 'import pathlib,time; pathlib.Path(' + repr(str(marker)) + ").write_text('ready'); time.sleep(3)"
        spec = self.request(body=body, resource_kind='acceptance', protection='waiting_user', reclaim_allowed=False, task_cleanup_allowed=True)
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(fixture.SCRIPTS / 'resources.py'),
            '--state', str(path), 'submit', str(spec)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.communicate(timeout=10))
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)  # Bounded fixture synchronization, no product poller.
        self.assertTrue(marker.exists())
        request = load(path)['requests']['alpha:A']
        proof = ref(self.file('post-main-process-proof.json', {'project_id': 'alpha', 'task_id': 'A',
                    'attempt_id': 'attempt-1', 'nonce': request['nonce']}))
        with self.assertRaises(ValueError):
            control.task_cleanup('alpha:A', 'alpha', 'A', request['nonce'], proof, self.completed_guard(task_completed_on_main=False))
        self.assertIsNone(process.poll())
        result = control.task_cleanup('alpha:A', 'alpha', 'A', request['nonce'], proof, self.completed_guard())
        self.assertTrue(result['termination_requested'])
        self.assertFalse(result['whole_tree_exit_confirmed'])
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(load(path)['requests']['alpha:A']['status'], 'reclaimed')
        self.assertTrue(control.task_cleanup('alpha:A', 'alpha', 'A', request['nonce'], proof, self.completed_guard())['duplicate'])

    def test_capacity_observation_type_and_conflicting_duplicate_rejected(self):
        event = ref(self.file('model-one.json', {'category': 'model_capacity', 'event_id': 'same', 'model': 'fixture-model'}))
        self.controller.model_capacity(event)
        before = self.path.read_bytes()
        wrong = ref(self.file('model-two.json', {'category': 'model_capacity', 'event_id': 'same', 'model': 'other-model'}))
        with self.assertRaises(ValueError):
            self.controller.model_capacity(wrong)
        for value in (True, 1, [], '', ' '):
            invalid = ref(self.file('invalid-model.json', {'category': 'model_capacity', 'event_id': value, 'model': 'model'}))
            with self.assertRaises(ValueError):
                self.controller.model_capacity(invalid)
        self.assertEqual(before, self.path.read_bytes())
