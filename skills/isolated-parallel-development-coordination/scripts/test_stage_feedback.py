"""Isolated canonical-ledger trajectories; host display/cleanup are explicit fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from core_runtime import Runtime
from delivery import CONTROL
from evidence import digest, io_path, load_bound, snapshot
from stage_feedback import cleanup_fact
from test_core_runtime import CoreFixture
import test_delivery
import test_work_cycle
import test_native_control
import test_real_use_closeout


class FeedbackFixture(CoreFixture):
    def apply(self, **op):
        if self.state.get('delivery'):
            op.setdefault('actor_id', self.actor if op['op'] in CONTROL else 'delivery-worker')
        return super().apply(**op)

    def setUp(self):
        super().setUp()
        for key in ('A', 'B'):
            self.dispatch(key, acceptance_type='delivery_verified')
        self.store()
        self.runtime.apply(dict(op='delegate', agent_id='delivery-worker', task_ids=['A', 'B'], merge_order=['A', 'B'],
            required_ci_jobs=['test'], routes={
                'worker-'+k: dict(target_id='existing-thread-'+k, existing_work_agent=True, evidence=self.file(k+'-route.txt'))
                for k in ('A', 'B')}, authority_evidence=self.file('delegation.txt'), user_reference='explicit isolated fixture'), self.actor)
        self.reload()
        self.runtime.apply(dict(op='delivery-transport', transport=test_delivery.DeliveryFixture.contacts(self)), 'delivery-worker')
        self.reload()

    def emit(self, key='A', revision=1):
        self.reload()
        packet = dict(self.delivery(key, revision), recipient_agent_id='delivery-worker')
        self.store()
        self.runtime.receive(packet, 'delivery-worker')
        self.runtime.apply(dict(op='drain'), 'delivery-worker')
        self.reload()
        return packet

    def view(self):
        return self.runtime.work_cycle(self.actor)

    def ids(self):
        return [e['id'] for e in self.view()['notifications']['items']]

    def confirm(self, ids=None, visible=True):
        ids = ids or self.ids()
        proof = self.file('visible-'+str(self.state['revision'])+'.txt', 'Fixture visible message observation')
        command = dict(op='confirm-display', notification_ids=ids, proof=proof)
        result = self.runtime.feedback(command, self.actor, verify_display=lambda context, _: dict(
            context, visible=visible, message_id='fixture-message', scope='fixture_only'))
        self.reload()
        return result, command

    def merged(self):
        self.emit()
        self.reload()
        task = self.state['tasks']['A']
        proof = self.json('merged.json', dict(project_id=self.runtime.project_id, task_id='A',
            source_head='a'*40, previous_main='b'*40, main_head='c'*40, contains_source=True,
            scope='fixture_only', evidence=[self.file('merge.log')]))
        task.update(phase='completed', merge=dict(verified=True, head_sha='a'*40, target_sha='b'*40,
                    main_sha='c'*40, proof=proof, scope='fixture_only'),
                    cleanup=dict(status='pending_cleanup', owner='delivery-worker'))
        self.store()
        self.runtime.apply(dict(op='describe', task_id='A', reference='fixture', title='需求甲'), 'delivery-worker')
        self.reload()

    def resource_fixture(self, statuses):
        self.reload()
        self.state['runtime']['executors']['worker-A'] = dict(id='worker-A', generation=1, lease='open', execution='completed')
        resources = {}
        for kind, status in statuses.items():
            rid = 'resource-'+kind
            resources[rid] = dict(id=rid, task_id='A', kind=kind, cleanup_status=status,
                executor_id='worker-A', generation=1,
                cleanup_result=dict(status=status, evidence=self.file(kind+'-result.log')))
        self.state['runtime']['resources'] = resources
        if statuses.get('agent') == 'cleaned':
            self.state['runtime']['executors']['worker-A']['lease'] = 'closed'
            self.state['runtime']['operations']['close'] = dict(operation='close', status='closed', task_id='A',
                executor_id='worker-A', generation=1, release_receipt=self.file('close-proof.log'))
        self.state['tasks']['A']['cleanup']['status'] = ('cleanup_completed' if all(s == 'cleaned' for s in statuses.values()) else 'pending_cleanup')
        self.store()
        self.runtime.apply(dict(op='describe', task_id='A', reference='fixture', title='需求甲'), 'delivery-worker')
        self.reload()


    def branch_fixture(self, statuses):
        self.resource_fixture(dict(agent='cleaned', process='cleaned', worktree='cleaned', database='cleaned'))
        for kind, status in statuses.items():
            rid = 'resource-'+kind
            self.state['runtime']['resources'][rid] = dict(id=rid, task_id='A', kind=kind,
                branch='codex/branch-fixture', cleanup_status=status,
                cleanup_result=dict(status=status, evidence=self.file(kind+'-result.log')))
        self.state['tasks']['A']['cleanup']['status'] = (
            'cleanup_completed' if all(s == 'cleaned' for s in statuses.values()) else 'pending_cleanup')
        self.store()
        self.runtime.apply(dict(op='describe', task_id='A', reference='fixture', title='需求甲'), 'delivery-worker')
        self.reload()


class BranchFeedbackTests(FeedbackFixture):
    def test_each_branch_kind_can_be_rendered_without_the_other(self):
        self.merged()
        for kind, name in (('local_branch', '本地分支'), ('remote_branch', '远端分支')):
            with self.subTest(kind=kind):
                self.branch_fixture({kind: 'retained'})
                self.assertIn(name+'（codex/branch-fixture）：待清理', cleanup_fact(self.state, 'A')['details'])

    def test_branch_names_and_cleaned_pending_unknown_statuses_are_read_only(self):
        self.merged()
        for status, label in (('cleaned', '已回收'), ('retained', '待清理'), ('unknown', '待核实')):
            with self.subTest(status=status):
                self.branch_fixture(dict(local_branch=status, remote_branch=status))
                before = self.runtime.path.read_bytes()
                state = deepcopy(self.state)
                fact = cleanup_fact(self.state, 'A')
                for kind, name in (('local_branch', '本地分支'), ('remote_branch', '远端分支')):
                    self.assertIn(name+'（codex/branch-fixture）：'+label, fact['details'])
                    row = next(r for r in fact['resources'] if r['kind'] == kind)
                    self.assertEqual(row['branch'], 'codex/branch-fixture')
                    self.assertEqual(row['status'], status)
                self.assertEqual(fact['outcome'], 'all' if status == 'cleaned' else 'partial')
                notifications = self.view()['notifications']['items']
                cleanup = next(n for n in notifications if n['task_id'] == 'A' and n['kind'] == 'cleanup')
                self.assertIn(fact['details'], cleanup['text'])
                self.assertEqual(cleanup['outcome']['status'], fact['outcome'])
                self.assertEqual(before, self.runtime.path.read_bytes())
                self.assertEqual(state, self.state)

    def test_expired_branch_cleanup_proof_stays_unknown_without_rewriting_facts(self):
        self.merged()
        self.branch_fixture(dict(local_branch='cleaned', remote_branch='cleaned'))
        for kind in ('local_branch', 'remote_branch'):
            resource = self.state['runtime']['resources']['resource-'+kind]
            Path(resource['cleanup_result']['evidence']['path']).write_text('expired', encoding='utf-8')
        before = self.runtime.path.read_bytes()
        fact = cleanup_fact(self.state, 'A')
        self.assertEqual(fact['outcome'], 'partial')
        for kind, name in (('local_branch', '本地分支'), ('remote_branch', '远端分支')):
            self.assertIn(name+'（codex/branch-fixture）：待核实（需核对实际效果）', fact['details'])
            self.assertEqual(self.state['runtime']['resources']['resource-'+kind]['cleanup_status'], 'cleaned')
        self.assertEqual(before, self.runtime.path.read_bytes())


class StageFeedbackTests(FeedbackFixture):
    def test_development_notifies_now_both_without_and_with_active_decision(self):
        self.emit()
        self.assertIn('开发已交付', self.view()['notifications']['text'])
        self.assertIsNone(self.view()['user_item'])  # Technical acceptance remains internal.
        self.reload(); self.human('current', 'decision'); self.apply(op='next'); self.store()
        before = deepcopy(self.runtime._load()['items'])
        self.emit('B')
        result = self.view()
        self.assertEqual(result['human_focus'], 'current')
        self.assertEqual(result['user_item']['id'], 'current')
        self.assertEqual(result['next_action']['kind'], 'needs_user')
        self.assertEqual(len(result['notifications']['items']), 2)
        self.assertFalse(result['notifications']['requires_reply'])
        self.assertTrue(result['display_required']); self.assertFalse(result['allow_end'])
        self.assertEqual(self.runtime._load()['items'], before)
        self.assertEqual(result['revision'], result['summary']['revision'])

    def test_author_quality_is_not_independent_review_and_verified_review_is_separate(self):
        self.emit()
        self.assertEqual([e['kind'] for e in self.view()['notifications']['items']], ['development'])
        self.reload()
        task = self.state['tasks']['A']
        task.update(phase='awaiting_merge', mr=dict(head_sha='a'*40, target_sha='b'*40))
        self.store()
        self.runtime.apply(dict(op='review', task_id='A', evidence=self.file('independent.log')), 'delivery-worker')
        self.assertIn('独审已通过', self.view()['notifications']['text'])
        self.reload(); self.state['tasks']['A']['review']['head_sha'] = 'c'*40; self.store()
        self.assertNotIn('独审已通过', self.view()['notifications']['text'])

    def test_merge_cleanup_and_installation_have_separate_evidence_and_notifications(self):
        self.merged()
        items = self.view()['notifications']['items']
        self.assertTrue({'development', 'merge', 'cleanup'} <= {e['kind'] for e in items})
        proof = dict(schema='stage-installation-verification/1', status='passed', project_id=self.runtime.project_id, task_id='A',
            delivery_revision=1, source_head='a'*40, installed_sha='c'*40, reviewer_id='delivery-worker', scope='fixture_only', environment='isolated',
            backup_evidence=[self.file('backup.log')], installation_evidence=[self.file('install.log')], real_task_evidence=[self.file('real-task-fixture.log')])
        command = dict(op='record-installation', task_id='A', proof=self.json('installation.json', proof))
        self.runtime.feedback(command, 'delivery-worker')
        text = self.view()['notifications']['text']
        self.assertIn('安装及真实任务验证已完成', text)
        self.assertIn('隔离夹具', text)
        self.assertTrue(self.runtime.feedback(command, 'delivery-worker')['duplicate'])
        bad = dict(proof, source_head='b'*40)
        with self.assertRaises(ValueError):
            self.runtime.feedback(dict(command, proof=self.json('wrong-installation.json', bad)), 'delivery-worker')
        with self.assertRaises(ValueError):
            self.runtime.feedback(dict(command, proof=self.json('pending-installation.json', dict(proof, status='pending'))), 'delivery-worker')

    def test_generated_accepted_and_unknown_send_are_never_displayed_or_blindly_retried(self):
        self.emit(); ids = self.ids()
        self.assertTrue(self.view()['display_required'])
        command = dict(op='prepare-send', notification_ids=ids, attempt_id='send-1')
        self.assertTrue(self.runtime.feedback(command, 'delivery-worker')['send_allowed'])
        self.assertFalse(self.runtime.feedback(command, 'delivery-worker')['send_allowed'])
        proof = self.json('send.json', dict(attempt_id='send-1', notification_ids=ids, recipient_id=self.actor, status='accepted'))
        result = self.runtime.feedback(dict(op='record-send', attempt_id='send-1', notification_ids=ids, proof=proof), 'delivery-worker')
        self.assertFalse(result['delivered_to_user'])
        restarted = Runtime(self.root/'authority', self.root/'project')
        self.assertEqual(restarted.work_cycle(self.actor)['notifications']['items'][0]['status'], 'sent_unconfirmed')
        with self.assertRaises(ValueError):
            restarted.feedback(dict(command, attempt_id='blind-retry'), 'delivery-worker')
        with self.assertRaises(ValueError):
            self.confirm(visible=False)
        result, confirmation = self.confirm()
        self.assertEqual(result['scope'], 'fixture_only')
        self.assertEqual(self.ids(), [])
        self.assertTrue(self.runtime.feedback(confirmation, self.actor)['duplicate'])

    def test_duplicate_cycles_restart_and_late_revision_do_not_repeat_or_overwrite(self):
        old = self.emit(); first = self.ids()
        for _ in range(2):
            self.runtime.apply(dict(op='drain'), 'delivery-worker')
            self.assertEqual(self.ids(), first)
        self.confirm()
        before = self.runtime.path.read_bytes()
        self.assertFalse(Runtime(self.root/'authority', self.root/'project').work_cycle(self.actor)['display_required'])
        self.assertEqual(before, self.runtime.path.read_bytes())
        self.emit(revision=2)
        newer = self.ids()
        self.runtime.receive(old, 'delivery-worker'); self.runtime.apply(dict(op='drain'), 'delivery-worker')
        self.assertEqual(newer, self.ids())
        self.assertTrue(all(e['delivery_revision'] == 2 for e in self.view()['notifications']['items']))
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery_revision'], 2)

    def test_partial_failed_unknown_and_all_cleanup_never_infer_release_from_execution(self):
        self.merged()
        for status in ('completed', 'idle', 'not_found'):
            self.resource_fixture(dict(agent='unknown', process='unknown', worktree='retained', database='retained'))
            self.state['runtime']['executors']['worker-A']['execution'] = status; self.store()
            self.assertNotEqual(cleanup_fact(self.state, 'A')['outcome'], 'all')
        self.resource_fixture(dict(agent='unknown', process='failed', worktree='cleaned', database='retained'))
        result = cleanup_fact(self.state, 'A')
        self.assertEqual(result['outcome'], 'partial')
        self.assertIn('工作树：已回收', result['details'])
        self.assertIn('进程/测试服务：回收失败', result['details'])
        partial = self.ids(); self.confirm()
        self.resource_fixture(dict(agent='cleaned', process='cleaned', worktree='cleaned', database='cleaned'))
        self.assertEqual(cleanup_fact(self.state, 'A')['outcome'], 'all')
        self.assertNotEqual(self.ids(), partial)
        self.assertIn('全部回收', self.view()['notifications']['text'])
        self.assertNotIn('RAM', self.view()['notifications']['text'])

    def test_missing_registration_and_missing_evidence_stay_unknown(self):
        self.merged()
        self.resource_fixture(dict(worktree='cleaned'))
        result = cleanup_fact(self.state, 'A')
        self.assertEqual(result['outcome'], 'partial')
        self.assertIn('未登记或未观察', result['details'])
        Path(self.state['runtime']['resources']['resource-worktree']['cleanup_result']['evidence']['path']).unlink()
        self.assertEqual(cleanup_fact(self.state, 'A')['outcome'], 'unknown')

    def test_cleanup_pending_running_failed_have_distinct_actual_outcomes(self):
        self.merged()
        for status, outcome in (('retained', 'pending'), ('intent', 'in_progress'), ('failed', 'failed')):
            with self.subTest(status=status):
                self.resource_fixture(dict.fromkeys(('agent', 'process', 'worktree', 'database'), status))
                self.assertEqual(cleanup_fact(self.state, 'A')['outcome'], outcome)
                self.assertEqual(self.view()['notifications']['items'][-1]['outcome']['status'], outcome)

    def test_unknown_and_failed_transport_preserve_pending_and_deny_new_attempts(self):
        for revision, transport in enumerate(('unknown', 'failed'), 1):
            self.emit(revision=revision); ids = self.ids(); attempt = 'attempt-'+transport
            self.runtime.feedback(dict(op='prepare-send', notification_ids=ids, attempt_id=attempt), 'delivery-worker')
            proof = self.json(transport+'.json', dict(attempt_id=attempt, notification_ids=ids, recipient_id=self.actor, status=transport))
            command = dict(op='record-send', notification_ids=ids, attempt_id=attempt, proof=proof)
            self.runtime.feedback(command, 'delivery-worker')
            self.assertTrue(self.runtime.feedback(command, 'delivery-worker')['duplicate'])
            self.assertTrue(self.view()['display_required'])
            with self.assertRaises(ValueError):
                self.runtime.feedback(dict(op='prepare-send', notification_ids=ids, attempt_id='blind-'+transport), 'delivery-worker')
            self.confirm()

    def test_text_or_recipient_mismatch_cannot_confirm_visible_message(self):
        self.emit(); before = self.runtime.path.read_bytes()
        command = dict(op='confirm-display', notification_ids=self.ids(), proof=self.file('mismatch.log'))
        for change in (dict(text='some other message'), dict(coordinator_id='another-main'), dict(visible=False)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.runtime.feedback(command, self.actor, verify_display=lambda context, _: dict(
                    context, visible=True, message_id='fixture', scope='fixture_only') | change)
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_long_source_file_is_bound_instead_of_omitted_or_marked_deleted(self):
        root = self.root/'A'
        directory = root/('long-'*20)
        io_path(directory).mkdir()
        source = directory/'actual-source.txt'
        io_path(source).write_text('real isolated source bytes', encoding='utf-8')
        inventory = snapshot(root)
        files = {row['relative']: row['sha256'] for row in inventory['files']}
        if source.is_file():
            self.assertEqual(files[source.relative_to(root).as_posix()], digest(source))
        else:
            from evidence import ref
            self.assertIn(ref(source), inventory['extra'])

    def test_identity_authority_focus_and_permissions_cannot_be_changed_by_notifications(self):
        self.emit(); ids = self.ids()
        before = self.runtime.path.read_bytes()
        for actor in (self.actor, 'worker-A', 'unrelated', ''):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.runtime.feedback(dict(op='prepare-send', notification_ids=ids, attempt_id='wrong'), actor)
        proof = self.file('assertion.json', '{"visible": true}')
        for actor in ('worker-A', 'delivery-worker', self.actor):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.runtime.feedback(dict(op='confirm-display', notification_ids=ids, proof=proof), actor)
        self.assertEqual(self.runtime.path.read_bytes(), before)
        state = self.runtime._load()
        self.confirm()
        after = self.runtime._load()
        for field in ('tasks', 'items', 'active_human_item', 'delivery', 'coordinator_id'):
            self.assertEqual(state[field], after[field])

    def test_important_block_and_resolution_are_changes_without_human_todos(self):
        self.emit(); self.confirm()
        self.reload(); self.state['tasks']['A']['blocker'] = dict(category='technical', owner='worker-A', reason='fixture failure')
        self.store()
        self.runtime.apply(dict(op='describe', task_id='A', reference='fixture', title='需求甲'), 'delivery-worker')
        self.assertIn('重要受阻', self.view()['notifications']['text'])
        self.assertEqual(self.runtime._load()['items'], {})
        self.confirm(); self.reload(); self.state['tasks']['A']['blocker'] = None; self.store()
        self.state['tasks']['A']['implementation'] = 'running'; self.store()
        self.runtime.apply(dict(op='drain'), 'delivery-worker')
        self.assertNotIn('阻塞已解除', self.view()['notifications']['text'])
        self.reload(); self.state['tasks']['A']['implementation'] = 'completed'; self.store()
        self.runtime.apply(dict(op='drain'), 'delivery-worker')
        self.assertIn('阻塞已解除', self.view()['notifications']['text'])
        ids = self.ids(); self.runtime.apply(dict(op='drain'), 'delivery-worker')
        self.assertEqual(ids, self.ids())

    def test_cli_recovers_unshown_notifications_but_json_cannot_assert_display(self):
        self.emit()
        cmd = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).with_name('core_runtime.py')),
               '--state-root', str(self.root/'authority'), '--project-root', str(self.root/'project')]
        result = subprocess.run(cmd+['work-cycle', '--actor', self.actor, '--view', 'full'], cwd=self.root, capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['display_required'])
        command = self.json('cli-fake-display.json', dict(op='confirm-display', notification_ids=self.ids(), proof=self.file('fake.log')))
        result = subprocess.run(cmd+['feedback', command['path'], '--actor', self.actor], cwd=self.root, capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 1)


class ScopedFeedbackTests(CoreFixture):
    identity = test_work_cycle.ScopedWorkCycleTests.identity
    observed_proof = test_work_cycle.ScopedWorkCycleTests.observed_proof
    report = test_work_cycle.ScopedWorkCycleTests.report
    payload = test_work_cycle.ScopedWorkCycleTests.payload
    terminal = test_work_cycle.ScopedWorkCycleTests.terminal
    bind_terminal = test_work_cycle.ScopedWorkCycleTests.bind_terminal
    observation_ref = test_work_cycle.ScopedWorkCycleTests.observation_ref

    def test_task_scoped_delivery_notifies_main_without_global_role_or_extra_human_item(self):
        self.serial = 0
        self.terminal_ref, self.packet = self.bind_terminal()
        self.reload(); self.human('current', 'decision'); self.apply(op='next'); self.store()
        result = self.runtime.work_cycle('integration', observation_ref=self.observation_ref())
        self.assertIn('开发已交付', result['notifications']['text'])
        self.assertEqual(result['human_focus'], 'current')
        before = self.runtime.path.read_bytes()
        view = self.runtime.work_cycle(self.actor)
        self.assertTrue(view['display_required'])
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertEqual(len(self.runtime._load()['items']), 2)  # Active decision plus explicit user-required acceptance only.
        self.assertFalse(self.runtime._load().get('delivery'))
        ids = [e['id'] for e in view['notifications']['items']]
        (self.root/'approved-plan.json').write_text('revoked', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.runtime.feedback(dict(op='prepare-send', notification_ids=ids, attempt_id='stale'), 'integration')


class NativeReceiptFeedbackTests(CoreFixture):
    ready = test_native_control.NativeControlTests.ready

    def test_latest_main_native_confirmation_is_consumed_without_a_quota_receipt(self):
        proof, _ = self.ready()
        prepared = self.runtime.prepare_native_close('A', 'root-close', proof, self.actor, authorized=True, scope='fixture_only')
        pending = cleanup_fact(self.runtime._load(), 'A')
        self.assertIn('关闭待确认', pending['details'])
        self.assertNotIn('开发者上下文：已回收', pending['details'])
        self.runtime.claim_native_close('root-close', self.actor, authorized=True)
        observed = test_native_control.NativeControlTests.observation(self, prepared['native_request'])
        self.runtime.reconcile_lifecycle('root-close', observed, self.actor)
        state = self.runtime._load()
        operation = state['runtime']['operations']['root-close']
        self.assertNotIn('release_receipt', operation)
        fact = cleanup_fact(state, 'A')
        self.assertIn('开发者上下文：已回收', fact['details'])
        self.assertNotEqual(fact['outcome'], 'all')
        self.assertEqual(state['runtime']['resources']['A:agent']['cleanup_status'], 'retained')
        Path(load_bound(operation['host_confirmation'])['close']['result']['path']).write_text('changed', encoding='utf-8')
        self.assertNotIn('开发者上下文：已回收', cleanup_fact(state, 'A')['details'])


class CliFeedbackTests(CoreFixture):
    cli = test_real_use_closeout.RealUseTests.cli
    cli_report = test_real_use_closeout.RealUseTests.cli_report

    def test_existing_approved_cli_receiver_can_relay_but_stale_approval_cannot(self):
        command, _ = self.cli()
        self.runtime.real_use(command, 'integration')
        self.runtime.real_use(self.cli_report(command), 'integration')
        notifications = self.runtime.work_cycle(self.actor)['notifications']
        self.assertIn('开发已交付', notifications['text'])
        self.assertEqual(notifications['items'][0]['technical_owner'], 'integration')
        ids = [e['id'] for e in notifications['items']]
        Path(command['approval']['path']).write_text('revoked', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.runtime.feedback(dict(op='prepare-send', notification_ids=ids, attempt_id='stale-cli'), 'integration')
