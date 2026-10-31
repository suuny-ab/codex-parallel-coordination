"""R2: terminal attribution and receipt-only authority, using explicit fixtures."""
from copy import deepcopy

from evidence import fingerprint, pack
from coordination import readiness
from test_core_runtime import CoreFixture
import test_demand_routing as routing_tests


class ObservedDeliveryTests(CoreFixture):
    identity = routing_tests.RoutingTests.identity
    observed_proof = routing_tests.RoutingTests.observed_proof

    def setUp(self):
        super().setUp()
        self.serial = 0

    def terminal(self, status='completed', thread_status='idle', final=None):
        return self.json(self.identity()+'-terminal.json', dict(thread=dict(id='actual-native', status=dict(type=thread_status)),
            turns=[dict(id='turn-1', status=status, items=[] if final is None else [
                dict(type='agentMessage', phase='final_answer', text=final)])]))

    def report(self, blocked=False):
        work = self.root/'observed-A'
        (work/'feature.py').write_text('explicit synthetic source\n', encoding='utf-8')
        spec = dict(project_id=self.runtime.project_id, task_id='A', delivery_revision=1,
            summary='Synthetic observed delivery', source_root=str(work), evidence=[self.file('selftest.log')],
            self_test=dict(status='failed' if blocked else 'passed', command='fixture selftest'),
            quality_review=dict(status='pending'), entry=dict(kind='file', target=self.file('entry.html')['path']),
            steps=['Review the recorded result'], expected='Only A is received')
        if blocked:
            spec.update(status='blocked', blocker=dict(category='technical', owner='actual-native', reason='Actual fixture failure'))
        return self.json('author-report.json', pack(spec))

    def payload(self, report, blocked=False):
        kind = 'blocked' if blocked else 'completed'
        return dict(project_id=self.runtime.project_id, task_id='A', owner_agent_id='actual-native',
            recipient_agent_id='integration', channel='native_agent', kind=kind, delivery_revision=1,
            event_id='A:1:'+kind, report=report)

    def bind_terminal(self, status='completed', thread_status='idle', blocked=False):
        proof = self.observed_proof()
        report = self.report(blocked)
        observation = self.terminal(status, thread_status, final=report['path'])
        proof['observation'] = observation
        self.runtime.bind_observed_dispatch('A', self.json('proof.json', proof), 'integration')
        return observation, self.payload(report, blocked)

    def test_idle_completed_binds_ownership_without_inventing_running_or_task_completion(self):
        self.bind_terminal()
        state = self.runtime._load()
        self.assertEqual(state['tasks']['A']['owner_agent_id'], 'actual-native')
        self.assertEqual(state['tasks']['A']['worker_run']['status'], 'awaiting_receipt')
        self.assertNotEqual(state['tasks']['A']['phase'], 'completed')
        self.assertEqual(state['runtime']['executors']['actual-native']['execution'], 'completed')
        self.assertEqual(state['runtime']['operations'], {})

    def test_latest_terminal_turn_overrides_active_thread_metadata(self):
        self.bind_terminal(thread_status='active')
        self.assertEqual(self.runtime._load()['tasks']['A']['worker_run']['status'], 'awaiting_receipt')
        self.assertEqual(self.runtime._load()['runtime']['executors']['actual-native']['execution'], 'completed')

    def test_failed_terminal_is_attributed_and_received_as_blocked_without_completion(self):
        observation, payload = self.bind_terminal(status='failed', blocked=True)
        result = self.runtime.receive_observed_delivery(payload, observation, 'integration')
        self.assertEqual(result['receipt_status'], 'processed')
        task = self.runtime._load()['tasks']['A']
        self.assertEqual(task['implementation'], 'blocked')
        self.assertNotEqual(task['phase'], 'completed')
        self.assertEqual(task['worker_run']['status'], 'reported')
        self.assertIsNone(task['merge'])

    def test_scoped_receiver_consumes_only_its_actual_report_without_global_delegate_or_ci(self):
        observation, payload = self.bind_terminal()
        self.prepare('B', owner='worker-B')
        self.reload()
        # Another task's already buffered arrival must not be consumed by A's receiver.
        other = dict(payload, task_id='B', owner_agent_id='worker-B', event_id='B:1:completed')
        self.state['receipts']['B:1:completed'] = dict(payload=other, status='buffered', report_verified=False,
            fingerprint=fingerprint(other), history=[])
        self.store()
        before = deepcopy(self.runtime._load())
        result = self.runtime.receive_observed_delivery(payload, observation, 'integration')
        state = self.runtime._load()
        self.assertEqual(result['receipt_status'], 'processed')
        self.assertEqual(state['tasks']['A']['phase'], 'awaiting_acceptance')
        self.assertEqual(state['tasks']['A']['delivery_revision'], 1)
        self.assertEqual(before['tasks']['B'], state['tasks']['B'])
        self.assertEqual(before['receipts']['B:1:completed'], state['receipts']['B:1:completed'])
        self.assertFalse(state.get('delivery'))
        self.assertFalse(state['tasks']['A']['authorization']['merge']['allowed'])
        self.assertEqual(state['runtime']['operations'], {})
        receipt = state['receipts']['A:1:completed']['payload']
        self.assertEqual(receipt['origin_mode'], 'observed_terminal')

    def test_receiver_wrong_actor_target_report_or_running_observation_is_rejected(self):
        observation, payload = self.bind_terminal()
        changes = [(payload, observation, self.actor), (dict(payload, recipient_agent_id='other'), observation, 'integration'),
            (dict(payload, owner_agent_id='other'), observation, 'integration'),
            (dict(payload, task_id='not-A'), observation, 'integration'),
            (payload, self.terminal(status='inProgress', thread_status='active'), 'integration')]
        for packet, source, actor in changes:
            before = self.runtime.path.read_bytes()
            with self.subTest(actor=actor, packet=packet), self.assertRaises((ValueError, KeyError)):
                self.runtime.receive_observed_delivery(packet, source, actor)
            self.assertEqual(before, self.runtime.path.read_bytes())
        wrong_report = self.json('unmentioned-report.json', dict(task_id='A', delivery_revision=1))
        with self.assertRaises(ValueError):
            self.runtime.receive_observed_delivery(dict(payload, report=wrong_report), observation, 'integration')

    def test_receiver_deduplicates_and_revalidates_bound_approval_without_rewriting_origin(self):
        observation, payload = self.bind_terminal()
        self.runtime.receive_observed_delivery(payload, observation, 'integration')
        before = self.runtime.path.read_bytes()
        duplicate = self.runtime.receive_observed_delivery(payload, observation, 'integration')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(before, self.runtime.path.read_bytes())
        (self.root/'approved-plan.json').write_text('revoked', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.runtime.receive_observed_delivery(payload, observation, 'integration')

    def test_terminal_attribution_without_final_report_does_not_fake_a_delivery(self):
        proof = self.observed_proof()
        proof['observation'] = self.terminal(status='interrupted')
        self.runtime.bind_observed_dispatch('A', self.json('interrupted-proof.json', proof), 'integration')
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery_revision'], 0)
        self.assertEqual(self.runtime._load()['receipts'], {})
        self.assertEqual(self.runtime._load()['tasks']['A']['worker_run']['status'], 'awaiting_receipt')

    def test_source_change_is_retained_as_acceptance_blocker_not_bypassed(self):
        observation, payload = self.bind_terminal()
        (self.root/'observed-A/feature.py').write_text('changed after report', encoding='utf-8')
        self.runtime.receive_observed_delivery(payload, observation, 'integration')
        state = self.runtime._load()
        self.assertEqual(state['tasks']['A']['delivery_revision'], 1)
        self.assertIn('source inventory changed', state['tasks']['A']['blocker']['reason'])
        self.assertTrue(readiness(state, state['items']['A:1:completed']))
        self.assertEqual(state['tasks']['A']['user_acceptance'], 'pending')
        self.assertIsNone(state['tasks']['A']['merge'])

    def test_running_binding_can_receive_later_terminal_without_rebinding_or_wakeup(self):
        proof = self.observed_proof()
        self.runtime.bind_observed_dispatch('A', self.json('running-proof.json', proof), 'integration')
        report = self.report()
        result = self.runtime.receive_observed_delivery(self.payload(report),
            self.terminal(final=report['path']), 'integration')
        self.assertEqual(result['receipt_status'], 'processed')
        self.assertEqual(self.runtime._load()['tasks']['A']['worker_run']['status'], 'reported')
        self.assertEqual(self.runtime._load()['runtime']['executors']['actual-native']['execution'], 'completed')
        self.assertEqual(self.runtime._load()['runtime']['operations'], {})

    def test_stored_provenance_is_rechecked_after_consumption(self):
        observation, payload = self.bind_terminal()
        self.runtime.receive_observed_delivery(payload, observation, 'integration')
        from pathlib import Path
        Path(observation['path']).write_text('changed original capture', encoding='utf-8')
        state = self.runtime._load()
        reasons = readiness(state, state['items']['A:1:completed'])
        self.assertTrue(any('hash mismatch' in reason for reason in reasons), reasons)

    def test_failed_turn_cannot_be_relabelled_as_successful_author_delivery(self):
        observation, payload = self.bind_terminal(status='failed', blocked=True)
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'outcome'):
            self.runtime.receive_observed_delivery(dict(payload, kind='completed', event_id='A:1:completed'), observation, 'integration')
        self.assertEqual(before, self.runtime.path.read_bytes())
