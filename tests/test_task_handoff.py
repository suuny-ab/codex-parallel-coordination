"""Single-task handoff against real isolated ledgers; host captures are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch
from evidence import load_bound, pack, ref
from core_runtime import Runtime

APP = 'mcp__codex_app__read_thread'
NATIVE = 'multi_agent_v1__wait_agent'


class TaskHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.replies['wait_agent'] = dict(status={}, timed_out=True)
        self.f.run_dispatch()
        self.owner = 'native_agent:native-fixture'
        self.actor = 'integration-fixture'

    def plan(self, actor=None, tools=(APP,)):
        return self.f.runtime.handoff('A', actor or self.actor, available_tools=tools)

    def observation(self, status='completed', final='', plan=None, identity='native-fixture'):
        plan = plan or self.plan()
        raw = dict(thread=dict(id=identity, status=dict(type='active' if status == 'inProgress' else 'idle')),
            turns=[dict(id='fixture-turn', status=status, items=[dict(type='agentMessage', phase='final_answer', text=final)])])
        return self.f.json('capture-'+str(len(list(self.f.root.glob('capture-*.json'))))+'.json',
            dict(schema='task-handoff-observation/1', binding=plan['binding'], tool=plan['host_call']['tool'],
                 arguments=plan['host_call']['arguments'], result=raw))

    def report(self, revision=1, **changes):
        spec = dict(project_id=self.f.runtime.project_id, task_id='A', owner_agent_id=self.owner,
            delivery_revision=revision, worktree=self.f.request['worktree'], branch=self.f.request['branch'],
            summary='Fixture handoff result', source_root=self.f.request['worktree'],
            evidence=[self.f.file('selftest.log', 'fixture passed')],
            self_test=dict(status='passed', command='fixture selftest'), quality_review=dict(status='pending'),
            git=dict(head_sha=self.f.base, target_sha=self.f.base, pr_url='https://github.com/example/project/pull/1'),
            entry=dict(kind='file', target=self.f.file('entry.txt')['path']),
            steps=['Inspect source and test evidence'], expected='Only this task enters independent review')
        spec.update(changes)
        return self.f.json('report-R'+str(revision)+'.json', pack(spec))

    def deliver(self, report=None, status='completed', actor=None, **options):
        report = report or self.report()
        observation = self.observation(status=status, final=report['path'])
        return self.f.runtime.handoff('A', actor or self.actor, observation_ref=observation, report_ref=report, **options)

    def test_unknown_launch_is_visible_as_dispatched_and_plan_never_respawns(self):
        progress = self.f.runtime.work_cycle(self.f.actor)['progress']['tasks'][0]
        self.assertEqual(progress['category'], 'uncertain')
        self.assertIn('已派发', progress['stage'])
        plan = self.plan()
        self.assertEqual(plan['host_call']['tool'], APP)
        self.assertEqual(plan['host_call']['arguments']['threadId'], 'native-fixture')
        self.assertEqual(plan['next_owner'], self.actor)
        self.assertEqual(len(self.f.calls), 2)
        self.assertNotIn('spawn_agent', json.dumps(plan))

    def test_main_gets_read_only_minimal_step_and_unknown_native_wait_does_not_run(self):
        before = self.f.runtime.path.read_bytes()
        plan = self.plan(actor=self.f.actor, tools=(NATIVE,))
        self.assertEqual(plan['host_call']['arguments'], dict(targets=['native-fixture'], timeout_ms=10000))
        capture = self.f.json('empty-wait.json', dict(schema='task-handoff-observation/1', binding=plan['binding'],
            tool=NATIVE, arguments=plan['host_call']['arguments'], result=dict(status={}, timed_out=True)))
        result = self.f.runtime.handoff('A', self.actor, observation_ref=capture)
        self.assertEqual(result['status'], 'needs_reconcile')
        self.assertEqual(before, self.f.runtime.path.read_bytes())

    def test_running_observation_continues_original_owner_request_and_resources(self):
        before = self.f.runtime._load()
        observation = self.observation(status='inProgress')
        result = self.f.runtime.handoff('A', self.actor, observation_ref=observation)
        state = self.f.runtime._load()
        self.assertEqual(result['status'], 'running')
        self.assertEqual(state['tasks']['A']['owner_agent_id'], self.owner)
        self.assertEqual(state['runtime']['operations']['dispatch-A:launch']['status'], 'running')
        self.assertEqual(state['runtime']['resources'], before['runtime']['resources'])
        self.assertEqual(len(self.f.calls), 2)

    def test_terminal_receipt_enters_existing_review_and_restarts_deduplicate(self):
        report = self.report()
        capture = self.observation(final=report['path'])
        result = self.f.runtime.handoff('A', self.actor, observation_ref=capture, report_ref=report)
        self.assertEqual(result['status'], 'received')
        state = self.f.runtime._load()
        self.assertEqual(state['tasks']['A']['phase'], 'awaiting_delivery_review')
        self.assertEqual(state['tasks']['A']['quality_review'], 'pending')
        self.assertIsNone(state['tasks']['A']['merge'])
        before = self.f.runtime.path.read_bytes()
        restarted = Runtime(self.f.root/'authority', self.f.runtime.project_root)
        again = restarted.handoff('A', self.actor, observation_ref=capture, report_ref=report)
        self.assertTrue(again['duplicate'])
        self.assertEqual(before, self.f.runtime.path.read_bytes())
        self.assertEqual(len(self.f.calls), 2)
        self.assertEqual(restarted.work_cycle(self.actor, task_ids=['A'])['next_action']['owner'], self.actor)

    def test_missing_pr_wrong_owner_recipient_revision_or_held_never_admits(self):
        report = self.report()
        original = load_bound(report)
        for change in (dict(git=dict(head_sha=self.f.base, target_sha=self.f.base)),
                       dict(owner_agent_id='other'), dict(delivery_revision=0), dict(status='held')):
            candidate = self.f.json('bad-'+str(len(list(self.f.root.glob('bad-*'))))+'.json', dict(original, **change))
            before = self.f.runtime.path.read_bytes()
            result = self.deliver(candidate)
            self.assertEqual(result['status'], 'needs_author', result)
            self.assertEqual(before, self.f.runtime.path.read_bytes())
        with self.assertRaises(ValueError):
            self.deliver(report, actor='other-recipient')
        self.assertEqual(self.f.runtime._load()['receipts'], {})

    def test_wrong_target_or_generation_cannot_modify_ledger(self):
        before = self.f.runtime.path.read_bytes()
        for capture in (self.observation(status='inProgress', identity='another-thread'),):
            with self.assertRaises(ValueError):
                self.f.runtime.handoff('A', self.actor, observation_ref=capture)
        plan = self.plan()
        plan['binding']['generation'] += 1
        with self.assertRaises(ValueError):
            self.f.runtime.handoff('A', self.actor, observation_ref=self.observation(status='inProgress', plan=plan))
        self.assertEqual(before, self.f.runtime.path.read_bytes())

    def test_failed_turn_requires_blocked_report_and_does_not_complete(self):
        report = self.report()
        self.assertEqual(self.deliver(report, status='failed')['status'], 'needs_author')
        blocked = self.report(2, status='blocked', blocker=dict(category='technical', owner=self.owner, reason='Fixture failure'))
        self.assertEqual(self.deliver(blocked, status='failed')['status'], 'received')
        task = self.f.runtime._load()['tasks']['A']
        self.assertEqual(task['implementation'], 'blocked')
        self.assertIsNone(task['merge'])

    def test_metadata_r2_reuses_verified_test_binding_without_modifying_r1(self):
        r1 = self.report(git=dict(head_sha=self.f.base, target_sha=self.f.base))
        before = Path(r1['path']).read_bytes()
        self.assertEqual(self.deliver(r1)['status'], 'needs_author')
        r2 = self.report(2)
        self.assertEqual(load_bound(r1)['source'], load_bound(r2)['source'])
        self.assertEqual(load_bound(r1)['self_test'], load_bound(r2)['self_test'])
        self.assertEqual(self.deliver(r2)['status'], 'received')
        self.assertEqual(Path(r1['path']).read_bytes(), before)

    def test_late_running_or_report_cannot_regress_current_delivery(self):
        old = self.report()
        current = self.report(2)
        self.deliver(current)
        before = self.f.runtime.path.read_bytes()
        self.assertEqual(self.deliver(old)['status'], 'stale')
        result = self.f.runtime.handoff('A', self.actor, observation_ref=self.observation(status='inProgress'))
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(before, self.f.runtime.path.read_bytes())

    def test_expired_source_or_report_final_reference_rejects_before_receipt(self):
        report = self.report()
        (Path(self.f.request['worktree'])/'new-feature.txt').write_text('changed', encoding='utf-8')
        self.assertEqual(self.deliver(report)['status'], 'needs_author')
        self.assertEqual(self.f.runtime._load()['receipts'], {})
        with self.assertRaises(ValueError):
            self.f.runtime.handoff('A', self.actor, observation_ref=self.observation(final='accepted'), report_ref=report)

    def test_cli_plan_is_reusable_and_read_only(self):
        script = test_dispatch.ROOT/'skills/isolated-parallel-development-coordination/scripts/core_runtime.py'
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), '--state-root', str(self.f.root/'authority'),
            '--project-root', str(self.f.runtime.project_root), 'handoff', '--task', 'A', '--actor', self.actor,
            '--tool', APP], cwd=test_dispatch.ROOT, capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['host_call']['tool'], APP)

    def test_receiver_mismatch_in_payload_is_rejected_and_other_task_unchanged(self):
        report = self.report()
        self.f.runtime.prepare(dict(task_id='B', worktree=str(self.f.root/'other'), branch='other',
            scope='Other fixture', acceptance=['Untouched'], user_reference='fixture'), self.f.actor)
        before = self.f.runtime._load()['tasks']['B']
        with self.assertRaises(ValueError):
            self.deliver(report, payload=dict(recipient_agent_id='other'))
        self.deliver(report)
        self.assertEqual(before, self.f.runtime._load()['tasks']['B'])

    def test_changed_target_branch_or_self_test_never_admits(self):
        original = load_bound(self.report())
        for change in (dict(git=dict(original['git'], head_sha='f'*40)), dict(branch='other'),
                       dict(git=dict(original['git'], target_sha='f'*40)),
                       dict(self_test=dict(original['self_test'], status='failed'))):
            report = self.f.json('invalid-'+str(len(list(self.f.root.glob('invalid-*'))))+'.json', dict(original, **change))
            self.assertEqual(self.deliver(report)['status'], 'needs_author')
        self.assertEqual(self.f.runtime._load()['receipts'], {})

    def test_scoped_work_cycle_consumes_same_receipt_without_global_drain(self):
        report = self.report()
        self.deliver(report)
        state = self.f.runtime._load()
        receipt = deepcopy(state['receipts']['A:1:completed']['payload'])
        self.f.reload()
        obs = self.f.observation([], [receipt])
        obs['recipient_agent_id'] = self.actor
        result = self.f.runtime.work_cycle(self.actor, task_ids=['A'], observation_ref=self.f.json('work-cycle.json', obs))
        self.assertTrue(result['observation']['current'])
        self.assertEqual(len(self.f.runtime._load()['receipts']), 1)

    def test_expired_final_provenance_holds_existing_review(self):
        from evidence import report_for
        report = self.report()
        capture = self.observation(final=report['path'])
        self.f.runtime.handoff('A', self.actor, observation_ref=capture, report_ref=report)
        state = self.f.runtime._load()
        Path(capture['path']).write_text('changed capture', encoding='utf-8')
        with self.assertRaises(ValueError):
            report_for(state, 'A', ready=False)

    def test_native_terminal_with_exact_report_path_is_received_without_app_alias(self):
        report = self.report()
        plan = self.plan(tools=(NATIVE,))
        capture = self.f.json('native-terminal.json', dict(schema='task-handoff-observation/1', binding=plan['binding'],
            tool=NATIVE, arguments=plan['host_call']['arguments'], result=dict(status={'native-fixture': {'completed': report['path']}})))
        result = self.f.runtime.handoff('A', self.actor, observation_ref=capture, report_ref=report)
        self.assertEqual(result['status'], 'received')
        self.assertEqual(self.f.runtime._load()['receipts']['A:1:completed']['payload']['channel'], 'native_agent')
        before = self.f.runtime.path.read_bytes()
        self.assertTrue(self.deliver(report)['duplicate'])
        self.assertEqual(before, self.f.runtime.path.read_bytes())


class ExternallyBoundHandoff(unittest.TestCase):
    def test_existing_observed_author_uses_same_entry_without_synthetic_launch(self):
        import test_observed_delivery
        fixture = test_observed_delivery.ObservedDeliveryTests('test_running_binding_can_receive_later_terminal_without_rebinding_or_wakeup')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        proof = fixture.observed_proof()
        fixture.runtime.bind_observed_dispatch('A', fixture.json('proof.json', proof), 'integration')
        plan = fixture.runtime.handoff('A', 'integration', available_tools=[APP])
        raw_report = load_bound(fixture.report())
        raw_report.update(owner_agent_id='actual-native', worktree=str(fixture.root/'observed-A'), branch=proof['branch'],
            git=dict(pr_url='https://github.com/example/project/pull/2', head_sha='a'*40, target_sha='b'*40))
        report = fixture.json('canonical-report.json', raw_report)
        raw = load_bound(fixture.terminal(final=report['path']))
        capture = fixture.json('handoff-capture.json', dict(schema='task-handoff-observation/1', binding=plan['binding'],
            tool=APP, arguments=plan['host_call']['arguments'], result=raw))
        result = fixture.runtime.handoff('A', 'integration', observation_ref=capture, report_ref=report)
        self.assertEqual(result['status'], 'received')
        self.assertEqual(fixture.runtime._load()['runtime']['operations'], {})


class RegisteredAuthorHandoff(unittest.TestCase):
    def fixture(self):
        f = test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.replies['spawn_agent'] = TimeoutError('original fixture response lost')
        self.assertEqual(f.run_dispatch()['status'], 'unknown')
        f.runtime.reconcile_launch('dispatch-A:launch', f.json('original-not-started.json', dict(
            project_id=f.runtime.project_id, task_id='A', operation_id='dispatch-A:launch', status='not_started',
            evidence=[f.file('actual-no-start.txt')])), f.actor)
        f.runtime.bind_executor('manual-fixture', {'native_agent':dict(target_id='manual-fixture',host_id='fixture')},
            f.actor, f.request['approval'], task_id='A')
        return f

    def capture(self, f, status, final=''):
        plan = f.runtime.handoff('A', 'integration-fixture', available_tools=[APP])
        return f.json('manual-observation.json', dict(schema='task-handoff-observation/1', binding=plan['binding'],
            tool=APP, arguments=plan['host_call']['arguments'], result=dict(thread=dict(id='manual-fixture'),
                turns=[dict(status=status, items=[dict(type='agentMessage', phase='final_answer', text=final)])])))

    def test_registered_actual_author_continues_without_rewriting_failed_launch(self):
        f = self.fixture()
        before = f.runtime._load()
        row = f.runtime.work_cycle(f.actor)['progress']['tasks'][0]
        self.assertEqual(row['category'], 'uncertain')
        capture = self.capture(f, 'inProgress')
        result = f.runtime.handoff('A', 'integration-fixture', observation_ref=capture)
        self.assertEqual(result['status'], 'running')
        after = f.runtime._load()
        self.assertEqual(after['runtime']['operations'], before['runtime']['operations'])
        self.assertEqual(after['tasks']['A']['owner_agent_id'], 'manual-fixture')
        self.assertEqual(len(f.calls), 1)

    def test_registered_fast_final_proves_terminal_without_inventing_running(self):
        f = self.fixture()
        before = f.runtime._load()['runtime']['operations']
        report = f.json('manual-report.json', pack(dict(project_id=f.runtime.project_id, task_id='A',
            owner_agent_id='manual-fixture', delivery_revision=1, worktree=f.request['worktree'],branch=f.request['branch'],
            summary='Manual fixture final', source_root=f.request['worktree'], evidence=[f.file('selftest.log')],
            self_test=dict(status='passed',command='fixture selftest'),quality_review=dict(status='pending'),
            git=dict(pr_url='https://github.com/example/project/pull/3', head_sha=f.base,target_sha=f.base),
            entry=dict(kind='file',target=f.file('entry.txt')['path']),steps=['Review final'],expected='Original failure retained')))
        capture = self.capture(f, 'completed', report['path'])
        result = f.runtime.handoff('A', 'integration-fixture', observation_ref=capture,report_ref=report)
        self.assertEqual(result['status'], 'received')
        after = f.runtime._load()
        self.assertEqual(after['runtime']['operations'], before)
        self.assertEqual(after['tasks']['A']['worker_run']['status'], 'reported')
        self.assertEqual(after['tasks']['A']['phase'], 'awaiting_delivery_review')
        self.assertEqual(len(f.calls), 1)

    def test_expired_no_start_proof_cannot_attribute_manual_author(self):
        f = self.fixture()
        before = f.runtime.path.read_bytes()
        (f.root/'original-not-started.json').write_text('changed proof',encoding='utf-8')
        with self.assertRaises(ValueError):
            f.runtime.handoff('A', 'integration-fixture', available_tools=[APP])
        self.assertEqual(before, f.runtime.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
