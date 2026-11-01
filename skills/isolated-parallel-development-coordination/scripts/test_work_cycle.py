"""Finite WORK-CYCLE calls on isolated ledgers; host observations are fixtures."""
import json
from pathlib import Path
import subprocess
import sys

from coordination import ref, summary, write_atomic
from evidence import load_bound
from core_runtime import Runtime
from test_core_runtime import CoreFixture
import test_external_followup
import test_progress_fallback
import test_delivery
import test_observed_delivery


class WorkCycleTests(CoreFixture):
    observed_worker = test_progress_fallback.ProgressFallbackTests.observed_worker

    def run_cycle(self, workers=(), messages=(), mode='fixture'):
        self.reload()
        obs = self.observation(workers, messages, mode)
        if self.state.get('delivery'):
            obs['recipient_agent_id'] = self.state['delivery']['agent_id']
        return self.runtime.work_cycle(self.actor, observation_ref=self.json(
            'cycle-'+str(self.state['revision'])+'.json', obs))

    def pending_cleanup(self):
        event = self.delivery(); self.receive(event)
        self.apply(op='next'); self.close(event['event_id'], 'accepted')
        self.state['tasks']['A'].update(phase='completed', merge=dict(verified=True, scope='fixture_only'))
        self.state['tasks']['A']['cleanup'] = dict(status='pending_cleanup', owner=self.actor)
        self.store()

    def external_event(self, reviewed=False):
        event, request, review = test_external_followup.ExternalFollowupTests.external_event(self, reviewed=reviewed)
        report = load_bound(event['report']); report['project_id'] = self.runtime.project_id
        write_atomic(event['report']['path'], report)
        event.update(project_id=self.runtime.project_id, report=ref(event['report']['path']))
        review['report_sha256'] = event['report']['sha256']
        return event, request, review

    def test_completed_pending_cleanup_has_delivery_owner_and_three_dimensions(self):
        self.pending_cleanup()
        self.assertEqual(summary(self.state)['action'], 'idle')  # Existing machine meaning.
        result = self.run_cycle()
        self.assertEqual(result['next_action']['kind'], 'needs_delivery')
        self.assertEqual(result['progress']['tasks'][0]['cleanup']['status'], 'pending_cleanup')
        self.assertEqual(result['progress']['tasks'][0]['development']['status'], 'completed')
        self.assertEqual(result['progress']['tasks'][0]['delivery']['status'], 'completed')
        self.assertIn('待清理', result['text'])
        self.assertFalse(result['all_work_finished'])
        self.assertEqual(result['revision'], result['summary']['revision'])
        self.assertEqual(result['revision'], result['progress']['revision'])
        self.assertEqual(result['revision'], self.runtime._load()['revision'])

    def test_same_receipt_is_consumed_once_and_current_focus_is_not_interrupted(self):
        self.human('current'); self.human('next'); self.apply(op='next')
        event = self.delivery(); self.store()
        worker = self.observed_worker(status='completed', revision=1)
        for _ in range(2):
            result = self.run_cycle([worker], [event])
            self.assertEqual(result['user_item']['id'], 'current')
            self.assertFalse(result['delivered_to_user'])
        state = self.runtime._load()
        self.assertEqual(len(state['receipts']), 1)
        self.assertEqual(state['tasks']['A']['delivery_revision'], 1)
        self.assertEqual(len(state['items']), 3)
        self.assertEqual(state['active_human_item'], 'current')

    def test_priority_fifo_one_item_and_summary_does_not_present_or_accept(self):
        self.human('normal'); self.human('decision', 'decision', True)
        self.human('later', 'decision', True)
        self.apply(op='priority', item_id='later', user_reference='fixture user choice'); self.store()
        result = self.run_cycle()
        self.assertEqual(result['user_item']['id'], 'later')
        self.assertEqual(result['next_action']['kind'], 'needs_user')
        self.assertIsNone(self.runtime._load()['active_human_item'])
        self.assertFalse(result['delivered_to_user'])
        self.runtime.apply(dict(op='next'), self.actor)
        self.runtime.resolve_and_next(dict(item_id='later', outcome='answered', user_reference='fixture answer'), self.actor)
        result = self.runtime.work_cycle(self.actor)
        self.assertEqual(result['user_item']['id'], 'decision')

    def test_main_is_read_only_and_cannot_collect_or_submit_delivery_observations(self):
        self.prepare(owner='worker-A'); self.reload()
        # Minimal existing delegation; no host route is asserted by this fixture.
        self.state['delivery'] = dict(agent_id='delivery-worker', task_ids=['A'], merge_order=['A'],
            required_ci_jobs=[], routes={}, authority_evidence=self.file('delegation.txt'),
            user_reference='fixture', transport=None, last_notification=None)
        self.store()
        before = self.runtime.path.read_bytes()
        result = self.runtime.work_cycle(self.actor)
        self.assertEqual(result['next_action']['owner'], 'delivery-worker')
        self.assertEqual(self.runtime.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.runtime.work_cycle(self.actor, collect=lambda _: self.fail('main must not collect'))
        with self.assertRaises(ValueError):
            self.runtime.work_cycle(self.actor, observation_ref=self.json('forbidden.json', {}))
        for actor in ('worker-A', None, ''):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.runtime.work_cycle(actor)
        self.assertEqual(self.runtime.path.read_bytes(), before)

    def test_collect_once_and_revision_race_rejects_without_consuming(self):
        calls = []
        def collect(context):
            calls.append(context)
            self.reload()
            return self.json('collected.json', self.observation([]))
        result = self.runtime.work_cycle(self.actor, collect=collect)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result['observation']['current'])
        self.assertEqual(result['next_action']['kind'], 'no_pending_work')
        def racing(context):
            self.reload(); obs = self.observation([])
            self.runtime.apply(dict(op='enqueue', item=dict(id='new', kind='discussion',
                title='New', prompt='Question')), self.actor)
            return self.json('old-revision.json', obs)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.runtime.work_cycle(self.actor, collect=racing)
        self.assertIn('new', self.runtime._load()['items'])

    def test_actual_delegated_writer_and_batch_notification_keep_cleanup_visible(self):
        self.pending_cleanup(); self.prepare('B', 'worker-B'); self.reload()
        self.state['tasks']['B'].update(phase='completed', merge=dict(verified=True, scope='fixture_only'))
        self.state['tasks']['B']['cleanup'] = dict(status='pending_cleanup')
        self.store()
        routes = {owner: dict(target_id='existing-thread-'+key, existing_work_agent=True,
            evidence=self.file(key+'-route.txt')) for key, owner in [('A', 'worker-A'), ('B', 'worker-B')]}
        self.runtime.apply(dict(op='delegate', agent_id='delivery-worker', task_ids=['A', 'B'],
            merge_order=['A', 'B'], required_ci_jobs=['test'], routes=routes,
            authority_evidence=self.file('delegate.txt'), user_reference='explicit fixture authorization'), self.actor)
        self.runtime.apply(dict(op='delivery-transport', transport=test_delivery.DeliveryFixture.contacts(self)), 'delivery-worker')
        self.actor = 'delivery-worker'
        result = self.run_cycle()
        self.assertEqual(result['next_action']['owner'], 'delivery-worker')
        self.assertEqual(result['notify_main']['kind'], 'batch_complete')
        self.assertTrue(result['notify_main']['cleanup_pending'])
        self.assertFalse(result['notify_main']['all_work_finished'])
        self.assertEqual(result['notify_main']['scope'], 'development_delivery')

    def test_recovery_does_not_reuse_old_running_observation_as_current(self):
        self.dispatch(); self.store()
        result = self.run_cycle([self.observed_worker()])
        self.assertEqual(result['progress']['counts']['in_progress'], 1)
        before = self.runtime.path.read_bytes()
        restarted = Runtime(self.root/'authority', self.root/'project')
        result = restarted.work_cycle(self.actor)
        self.assertFalse(result['observation']['current'])
        self.assertIsNone(result['progress']['counts']['in_progress'])
        self.assertIn('最后登记', result['text'])
        self.assertEqual(self.runtime.path.read_bytes(), before)

    def test_queued_blocked_external_delivery_and_merge_are_distinct(self):
        self.prepare('QUEUED'); result = self.run_cycle()
        self.assertEqual(result['progress']['tasks'][0]['category'], 'queued')
        self.assertEqual(result['next_action']['kind'], 'waiting')
        self.reload()
        event, _, _ = self.external_event()
        self.receive(event)
        self.apply(op='route-blocker', task_id='A', delivery_revision=4, owner='worker-A',
            action='Inspect field source', evidence=self.file('assigned.txt'))
        self.store()
        result = self.run_cycle([self.observed_worker(status='blocked', revision=4)])
        self.assertEqual(result['progress']['tasks'][1]['category'], 'technical_blocked')
        self.assertEqual(result['next_action']['kind'], 'needs_delivery')

    def test_external_wait_and_awaiting_integration_merge_remain_separate(self):
        event, request, _ = self.external_event(reviewed=True)
        self.receive(event); self.apply(op='next')
        self.apply(op='close', item_id=request['id'], outcome='requested', user_reference='fixture sent',
            request_evidence=self.file('request-sent.txt'))
        self.store()
        result = self.run_cycle([self.observed_worker(status='blocked', revision=4)])
        self.assertEqual(result['progress']['tasks'][0]['category'], 'external_wait')
        self.assertEqual(result['next_action']['kind'], 'waiting')
        self.reload()
        event = self.delivery('B'); self.receive(event); self.apply(op='next'); self.close(event['event_id'], 'accepted')
        self.store()
        workers = [self.observed_worker(status='blocked', revision=4), self.observed_worker('B', 'completed', 1)]
        result = self.run_cycle(workers)
        self.assertEqual(result['progress']['tasks'][1]['category'], 'awaiting_integration')
        self.reload(); self.state['tasks']['B']['phase'] = 'awaiting_merge'; self.store()
        result = self.run_cycle(workers)
        self.assertEqual(result['progress']['tasks'][1]['category'], 'awaiting_merge')
        self.assertEqual(result['next_action']['kind'], 'needs_delivery')

    def test_completed_cleanup_is_explicit_and_stale_observation_cannot_write(self):
        self.pending_cleanup(); self.reload()
        self.state['tasks']['A']['cleanup']['status'] = 'cleanup_completed'; self.store()
        obs = self.json('stale.json', self.observation([]))
        result = self.run_cycle()
        self.assertTrue(result['all_work_finished'])
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.runtime.work_cycle(self.actor, observation_ref=obs)
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_expired_report_or_proof_suppresses_acceptance_and_current_counts(self):
        event = self.delivery(); self.store()
        result = self.run_cycle([self.observed_worker(status='completed', revision=1)], [event])
        self.assertEqual(result['user_item']['id'], event['event_id'])
        self.runtime.apply(dict(op='next'), self.actor)
        Path(event['report']['path']).write_text('{changed', encoding='utf-8')
        result = self.runtime.work_cycle(self.actor)
        self.assertIsNone(result['user_item'])
        self.assertEqual(result['human_focus'], event['event_id'])
        self.assertEqual(result['next_action']['kind'], 'needs_delivery')
        self.assertIsNone(result['progress']['counts']['in_progress'])

    def test_report_arrival_or_invalid_report_is_not_a_ready_user_item(self):
        event = self.delivery(); self.store()
        result = self.run_cycle([self.observed_worker(status='completed', revision=1)])
        self.assertIsNone(result['user_item'])
        self.assertEqual(result['next_action']['kind'], 'needs_delivery')
        self.assertFalse(result['observation']['verified'])
        Path(event['report']['path']).write_text('{bad', encoding='utf-8')
        result = self.run_cycle([self.observed_worker(status='completed', revision=1)], [event])
        self.assertIsNone(result['user_item'])
        self.assertFalse(result['all_work_finished'])

    def test_missing_observation_or_capability_does_not_claim_no_work(self):
        for actor in (None, ''):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.runtime.work_cycle(actor)
        result = self.runtime.work_cycle(self.actor, collect=lambda _: None)
        self.assertEqual(result['next_action']['kind'], 'needs_delivery')
        self.assertFalse(result['all_work_finished'])
        result = self.run_cycle(mode='unavailable')
        self.assertFalse(result['observation']['verified'])
        self.assertEqual(result['observation']['scope'], 'incomplete_native_coverage')

    def test_documented_cli_read_and_observed_calls(self):
        obs = self.json('cli-obs.json', self.observation([]))
        command = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).with_name('core_runtime.py')),
            '--state-root', str(self.root/'authority'), '--project-root', str(self.root/'project'),
            'work-cycle', '--actor', self.actor]
        before = self.runtime.path.read_bytes()
        read = subprocess.run(command, cwd=self.root, capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertFalse(json.loads(read.stdout)['observation']['current'])
        self.assertEqual(self.runtime.path.read_bytes(), before)
        observed = subprocess.run(command+['--observation', obs['path']], cwd=self.root,
            capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(json.loads(observed.stdout)['next_action']['kind'], 'no_pending_work')


class ScopedWorkCycleTests(CoreFixture):
    identity = test_observed_delivery.ObservedDeliveryTests.identity
    observed_proof = test_observed_delivery.ObservedDeliveryTests.observed_proof
    report = test_observed_delivery.ObservedDeliveryTests.report
    payload = test_observed_delivery.ObservedDeliveryTests.payload
    terminal = test_observed_delivery.ObservedDeliveryTests.terminal
    bind_terminal = test_observed_delivery.ObservedDeliveryTests.bind_terminal

    def setUp(self):
        super().setUp(); self.serial = 0
        self.terminal_ref, self.packet = self.bind_terminal()

    def observation_ref(self, packets=()):
        self.reload()
        obs = self.observation([dict(task_id='A', owner_agent_id='actual-native', status='completed',
            last_delivery_revision=1, evidence=self.terminal_ref, receipt=self.packet)])
        obs['recipient_agent_id'] = 'integration'
        obs['inbox']['messages'] = list(packets)
        return self.json('scoped-'+str(self.state['revision'])+'.json', obs)

    def test_real_shaped_approved_binding_without_global_delivery_can_read_and_receive(self):
        before = self.runtime.path.read_bytes()
        root = self.runtime.work_cycle(self.actor)
        self.assertEqual(root['next_action']['owner'], 'integration')
        self.assertEqual(root['next_action']['task_ids'], ['A'])
        self.runtime.work_cycle('integration')
        self.assertEqual(self.runtime.path.read_bytes(), before)
        result = self.runtime.work_cycle('integration', observation_ref=self.observation_ref())
        self.assertEqual(result['revision'], self.runtime._load()['revision'])
        self.assertEqual(result['observation']['task_scope'], ['A'])
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery_revision'], 1)
        self.assertFalse(self.runtime._load().get('delivery'))
        self.runtime.work_cycle('integration', task_ids=['A'], observation_ref=self.observation_ref())
        self.assertEqual(len(self.runtime._load()['receipts']), 1)
        before = self.runtime.path.read_bytes()
        for tasks in (['not-A'], [], ['A', 'A']):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                self.runtime.work_cycle('integration', task_ids=tasks)
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_no_global_drain_or_other_task_authority_and_no_main_or_author_impersonation(self):
        self.prepare('B', owner='worker-B'); self.reload()
        other = dict(self.packet, task_id='B', owner_agent_id='worker-B', event_id='B:1:completed')
        from evidence import fingerprint
        self.state['receipts']['B:1:completed'] = dict(payload=other, status='buffered', report_verified=False,
            fingerprint=fingerprint(other), history=[])
        self.store(); before = self.runtime._load()
        result = self.runtime.work_cycle('integration', observation_ref=self.observation_ref())
        after = self.runtime._load()
        self.assertEqual(before['tasks']['B'], after['tasks']['B'])
        self.assertEqual(before['receipts']['B:1:completed'], after['receipts']['B:1:completed'])
        self.assertFalse(result['all_work_finished'])
        frozen = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):
            self.runtime.work_cycle('integration', observation_ref=self.observation_ref([other]))
        with self.assertRaises(ValueError):
            self.runtime.work_cycle('integration', task_ids=['B'], collect=lambda _: self.fail('other task'))
        for actor in (self.actor, 'actual-native', 'unrelated'):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.runtime.work_cycle(actor, collect=lambda _: self.fail('unauthorized collector'))
        self.assertEqual(frozen, self.runtime.path.read_bytes())

    def test_expired_approval_does_not_authorize_collect_or_claim_integrator(self):
        (self.root/'approved-plan.json').write_text('revoked', encoding='utf-8')
        before = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):
            self.runtime.work_cycle('integration', collect=lambda _: self.fail('stale authority'))
        result = self.runtime.work_cycle(self.actor)
        self.assertIsNone(result['next_action']['owner'])
        self.assertEqual(before, self.runtime.path.read_bytes())
