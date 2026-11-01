"""Brief/full comparisons on isolated ledgers; no real host display or cleanup."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from evidence import fingerprint
from main_brief import encoded, notification_rows, render
from test_core_runtime import CoreFixture
from test_stage_feedback import FeedbackFixture
import test_work_cycle


class BriefTests(CoreFixture):
    def brief(self, **options):
        return self.runtime.work_cycle(self.actor, view='brief', **options)

    def pages(self, first, **options):
        yield first
        while first['page']['next_cursor']:
            first = self.brief(cursor=first['page']['next_cursor'], **options)
            yield first

    def read_detail(self, snapshot, selector, budget=4096):
        result = self.brief(snapshot=snapshot, detail=selector, budget=budget)
        chunks = [result['content']]
        while result['next_cursor']:
            self.assertLessEqual(len(encoded(result))+1, budget)
            result = self.brief(cursor=result['next_cursor'], detail=selector, budget=budget)
            chunks.append(result['content'])
        self.assertLessEqual(len(encoded(result))+1, budget)
        return json.loads(''.join(chunks))

    def test_focus_priority_selection_pending_count_and_reads_are_unchanged(self):
        self.human('first'); self.human('priority', 'decision', True)
        self.human('later', 'decision', True)
        self.apply(op='priority', item_id='later', user_reference='fixture choice'); self.store()
        for focused in (False, True):
            if focused:
                self.runtime.apply(dict(op='next'), self.actor)
            before = self.runtime.path.read_bytes()
            full = self.runtime.work_cycle(self.actor)
            for _ in range(2):
                brief = self.brief()
                self.assertEqual(brief['user_items']['item']['id'], full['user_item']['id'])
                self.assertEqual(brief['user_items']['pending_total'], 3)
                action = dict(brief['next_action']); action.pop('detail'); action.pop('task_ids')
                self.assertEqual(action, {k: v for k, v in full['next_action'].items() if k != 'task_ids'})
                self.assertFalse(brief['delivered_to_user'])
                self.assertFalse(brief['source']['current'])
            self.assertEqual(before, self.runtime.path.read_bytes())

    def test_no_user_item_still_has_internal_work_and_owner(self):
        test_work_cycle.WorkCycleTests.pending_cleanup(self)
        brief = self.brief()
        self.assertIsNone(brief['user_items']['item'])
        self.assertEqual(brief['next_action']['kind'], 'needs_delivery')
        self.assertFalse(brief['all_work_finished'])
        self.assertTrue(brief['tasks']['items'][0]['closeout'])
        self.assertEqual(brief['tasks']['items'][0]['cleanup']['status'], 'pending_cleanup')

    def test_unicode_many_tasks_are_bounded_continuable_and_repeatable(self):
        for number in range(35):
            self.prepare(str(number), owner='worker-'+str(number))
        self.state['tasks']['0']['progress']['title'] = '中文🙂\\\"\n'*4000
        self.store()
        before = self.runtime.path.read_bytes()
        first = self.brief(budget=4096)
        pages = list(self.pages(first, budget=4096))
        self.assertGreater(len(pages), 1)
        rows = [r for p in pages for r in p['tasks']['items']]
        self.assertEqual({r['task_id'] for r in rows}, set(self.state['tasks']))
        for page in pages:
            self.assertLessEqual(len(encoded(page))+1, 4096)
            self.assertEqual(page['revision'], first['revision'])
            self.assertEqual(page['source']['snapshot'], first['source']['snapshot'])
            if page['page']['remaining']:
                self.assertFalse(page['allow_end'])
                self.assertFalse(page['all_work_finished'])
        self.assertEqual(len(rows), 35)
        self.assertEqual(self.read_detail(first['source']['snapshot'], rows[0]['detail'])['task']['progress']['title'],
                         self.state['tasks']['0']['progress']['title'])
        self.assertEqual(self.brief(budget=4096)['page'], first['page'])
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_full_detail_round_trips_and_keeps_original_references(self):
        self.human('one'); self.store()
        full = self.runtime.work_cycle(self.actor)
        # Runtime's compatibility wrapper adds only these two fields.
        full.pop('ledger'); full.pop('project_id')
        brief = self.brief()
        self.assertEqual(self.read_detail(brief['source']['snapshot'], 'full'), full)
        self.assertEqual(self.read_detail(brief['source']['snapshot'], 'user'), full['user_item'])

    def test_new_revision_rejects_continuation_instead_of_mixing(self):
        first = self.brief()
        self.runtime.apply(dict(op='enqueue', item=dict(id='new', kind='discussion', title='new', prompt='new')), self.actor)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.brief(snapshot=first['source']['snapshot'], detail='full')

    def test_child_blocker_owner_is_not_lost_in_business_task_group(self):
        self.prepare('A', owner='worker-A'); self.prepare('review', owner='worker-review')
        child = self.state['tasks']['review']
        child['progress']['business_task_id'] = 'A'
        child.update(implementation='blocked', blocker=dict(reason='fixture failed'))
        child['blocker_followup'] = dict(status='assigned_technical', owner='worker-review', action='Investigate failure',
            delivery_revision=0, evidence=self.file('assignment.log'))
        self.store()
        rows = [r for page in self.pages(self.brief()) for r in page['tasks']['items']]
        review = next(r for r in rows if r['task_id'] == 'review')
        self.assertTrue(review['blocked'])
        self.assertEqual(review['owner'], 'worker-review')
        self.assertEqual(review['next_step'], 'Investigate failure')

    def test_cursor_cannot_be_reused_for_a_different_detail(self):
        self.human('one'); self.store()
        first = self.brief()
        full = self.brief(snapshot=first['source']['snapshot'], detail='full', budget=4096)
        # Generate a multipage detail even on a very small initial ledger.
        if not full['next_cursor']:
            self.prepare('large', owner='worker-large')
            self.state['tasks']['large']['progress']['title'] = 'large'*2000
            self.store(); first = self.brief()
            full = self.brief(snapshot=first['source']['snapshot'], detail='full', budget=4096)
        self.assertIsNotNone(full['next_cursor'])
        with self.assertRaisesRegex(ValueError, 'different detail'):
            self.brief(cursor=full['next_cursor'], detail='notifications')

    def test_many_attention_rows_expose_remaining_and_never_allow_closeout(self):
        for number in range(12):
            self.prepare(str(number), owner='worker-'+str(number))
            self.state['tasks'][str(number)].update(implementation='blocked', blocker=dict(reason='failure'))
            self.store()
        first = self.brief(budget=4096)
        self.assertGreater(first['page']['remaining_attention'], 0)
        self.assertFalse(first['allow_end'])
        self.assertFalse(first['all_work_finished'])
        self.assertEqual(sum(r['blocked'] for p in self.pages(first, budget=4096) for r in p['tasks']['items']), 12)

    def test_bad_options_and_unauthorized_callers_do_not_collect_or_write(self):
        before = self.runtime.path.read_bytes()
        cases = [dict(budget=0), dict(budget=1000000), dict(budget=True), dict(cursor='broken'),
                 dict(detail='full'), dict(snapshot='l:'+'0'*64, cursor='l:'+'0'*64)]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.brief(**options)
        for actor in ('unknown', '', None):
            with self.assertRaises(ValueError):
                self.runtime.work_cycle(actor, view='brief')
        with self.assertRaises(ValueError):
            self.brief(cursor='l:'+'0'*64, collect=lambda _: self.fail('must not collect'))
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_cli_defaults_to_brief_and_explicit_full_still_works(self):
        command = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).with_name('core_runtime.py')),
                   '--state-root', str(self.root/'authority'), '--project-root', str(self.root/'project'),
                   'work-cycle', '--actor', self.actor]
        before = self.runtime.path.read_bytes()
        for extra, schema in (([], 'coordination-main-brief/1'), (['--view', 'full'], 'coordination-work-cycle/1')):
            process = subprocess.run(command+extra, cwd=self.root, capture_output=True, encoding='utf-8', timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)['schema'], schema)
            if not extra:
                self.assertLessEqual(len(process.stdout.encode()), 8192)
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_observed_snapshot_can_continue_without_reconsuming(self):
        for number in range(10):
            self.prepare(str(number), owner='worker-'+str(number))
        observation = self.json('observation.json', self.observation([]))
        first = self.brief(observation_ref=observation, budget=4096)
        before = self.runtime.path.read_bytes()
        self.assertTrue(first['source']['current'])
        pages = list(self.pages(first, budget=4096))
        for page in pages[1:]:
            self.assertTrue(page['source']['snapshot_replay'])
            self.assertFalse(page['source']['current'])
        self.assertEqual(sum(len(p['tasks']['items']) for p in pages), 10)
        self.assertEqual(before, self.runtime.path.read_bytes())


class FeedbackBriefTests(FeedbackFixture):
    brief = BriefTests.brief
    pages = BriefTests.pages
    read_detail = BriefTests.read_detail
    def test_merge_install_agent_and_failed_worktree_remain_distinct(self):
        self.merged()
        self.resource_fixture(dict(agent='cleaned', worktree='failed', process='retained', database='retained'))
        full = self.runtime.work_cycle(self.actor)
        first = self.brief()
        rows = [r for page in self.pages(first) for r in page['tasks']['items']]
        row = next(r for r in rows if r['task_id'] == 'A')
        self.assertEqual(row['merge'], 'merged')
        self.assertEqual(row['installation'], 'unknown')
        self.assertIn('agent:cleaned', row['cleanup']['resources']['components'])
        self.assertIn('worktree:failed', row['cleanup']['resources']['components'])
        self.assertEqual(row['cleanup']['status'], 'pending_cleanup')
        self.assertEqual(first['notifications']['received_total'], len(full['notifications']['items']))
        notices = [r for page in self.pages(first) for r in page['notifications']['items']]
        self.assertTrue({'merge', 'cleanup'} <= {r['kind'] for r in notices})

    def test_sent_unconfirmed_repeated_reads_and_evidence_expiry(self):
        packet = self.emit()
        ids = self.ids()
        self.runtime.feedback(dict(op='prepare-send', notification_ids=ids, attempt_id='one'), 'delivery-worker')
        first = self.brief()
        notices = [r for page in self.pages(first) for r in page['notifications']['items']]
        self.assertEqual(notices[0]['status'], 'sent_unconfirmed')
        before = self.runtime.path.read_bytes()
        self.brief(); self.brief()
        self.assertEqual(before, self.runtime.path.read_bytes())
        Path(packet['report']['path']).write_text('expired', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.brief(snapshot=first['source']['snapshot'], detail='notifications')
        self.assertEqual(self.brief()['notifications']['received_total'], 0)

    def test_missing_merge_or_cleanup_proof_does_not_claim_success(self):
        self.merged()
        Path(self.state['tasks']['A']['merge']['proof']['path']).write_text('expired', encoding='utf-8')
        rows = [r for page in self.pages(self.brief()) for r in page['tasks']['items']]
        row = next(row for row in rows if row['task_id'] == 'A')
        self.assertEqual(row['merge'], 'last_recorded_unverified')
        self.assertEqual(row['installation'], 'unknown')

    def test_ordinary_success_does_not_hide_existing_unresolved_blocker(self):
        self.emit()
        self.reload(); self.state['tasks']['A']['blocker'] = dict(reason='still failing', owner='worker-A')
        self.store()
        self.runtime.apply(dict(op='describe', task_id='A', title='Unresolved', reference='fixture'), 'delivery-worker')
        self.emit('B')
        first = self.brief()
        notices = [r for page in self.pages(first) for r in page['notifications']['items']]
        self.assertTrue(any(r['kind'] == 'blocker' and r['task_id'] == 'A' and r['attention'] for r in notices))
        self.assertTrue(any(r['kind'] == 'development' and r['task_id'] == 'B' for r in notices))
        self.assertTrue(any(r['task_id'] == 'A' and r['blocked'] for page in self.pages(first) for r in page['tasks']['items']))

    def test_first_page_retains_unfinished_task_beside_closeout_notices(self):
        self.merged()
        first = self.brief(budget=4096)
        self.assertTrue(first['tasks']['items'])
        self.assertEqual(first['tasks']['items'][0]['task_id'], 'B')
        self.assertTrue(first['notifications']['items'])


class AggregationTests(CoreFixture):
    def test_only_ordinary_success_is_aggregated_failures_and_milestones_survive(self):
        events = []
        for i, (kind, outcome) in enumerate((('development', 'delivered'), ('blocker', 'unresolved'),
                ('review', 'failed'), ('review', 'passed'), ('merge', 'merged'),
                ('installation', 'verified'), ('cleanup', dict(status='partial')))):
            events.append(dict(id=str(i), task_id='A', technical_owner='integrator', kind=kind, outcome=outcome,
                               status='pending', text=kind, ledger_revision=i, scope='fixture_only'))
        rows = notification_rows(events)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['kind'], 'review')
        self.assertEqual(rows[0]['count'], 2)
        self.assertEqual(rows[0]['event_indexes'], [0, 3])
        self.assertEqual([r['kind'] for r in rows[1:]], ['blocker', 'review', 'merge', 'installation', 'cleanup'])
        self.assertTrue(all(rows[i]['attention'] for i in (1, 2, 5)))
        self.assertEqual(fingerprint(events), fingerprint(deepcopy(events)))


class ScopedBriefTests(CoreFixture):
    identity = test_work_cycle.ScopedWorkCycleTests.identity
    observed_proof = test_work_cycle.ScopedWorkCycleTests.observed_proof
    report = test_work_cycle.ScopedWorkCycleTests.report
    payload = test_work_cycle.ScopedWorkCycleTests.payload
    terminal = test_work_cycle.ScopedWorkCycleTests.terminal
    bind_terminal = test_work_cycle.ScopedWorkCycleTests.bind_terminal
    observation_ref = test_work_cycle.ScopedWorkCycleTests.observation_ref

    def setUp(self):
        super().setUp()
        self.serial = 0
        self.terminal_ref, self.packet = self.bind_terminal()
    def test_scoped_observed_continuation_keeps_authority_and_coverage(self):
        first = self.runtime.work_cycle('integration', view='brief', observation_ref=self.observation_ref())
        before = self.runtime.path.read_bytes()
        detail = self.runtime.work_cycle('integration', view='brief', snapshot=first['source']['snapshot'], detail='full')
        self.assertTrue(detail['source']['snapshot_replay'])
        self.assertEqual(before, self.runtime.path.read_bytes())
        with self.assertRaises(ValueError):
            self.runtime.work_cycle(self.actor, view='brief', snapshot=first['source']['snapshot'], detail='full')
