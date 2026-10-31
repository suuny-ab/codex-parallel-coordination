"""Approved routing matrix: isolated ledger and recording host callbacks only.

The historical quote is real regression input; host identities and effects here
are synthetic. No current executor receives the historical misdispatch.
"""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

from demand_routing import scope_digest, task_version
from evidence import load_bound
from test_core_runtime import CoreFixture


class RoutingTests(CoreFixture):
    def setUp(self):
        super().setUp()
        self.serial = 0

    def identity(self):
        self.serial += 1
        return 'event-'+str(self.serial)

    def command(self, op, actor=None, user_origin=None, **fields):
        return self.runtime.routing(dict(op=op, event_id=self.identity(), **fields),
            actor or self.actor, user_origin=user_origin)

    def record(self, op, **fields):
        result = self.command(op, **fields)
        self.assertEqual(result['status'], 'recorded', result)
        return result['result']

    def spec(self, key='A', scope=None, consistent=True, start=True):
        scope = scope or 'Confirmed goal '+key
        prefix = self.identity()
        discussion = self.json(prefix+'-discussion.json', dict(confirmed=True, text='Confirmed discussion: '+scope))
        spec = self.file(prefix+'-spec.md', scope)
        check = self.json(prefix+'-check.json', dict(coordinator_id=self.actor, requirement_id=key,
            spec=spec, discussion=discussion, result='consistent' if consistent else 'mismatch'))
        return self.command('spec', requirement_id=key, name=key, scope=scope,
            acceptance=['Observable goal '+key], discussion=discussion, spec=spec, check=check,
            start_reference=self.file(prefix+'-start.txt') if start else None)

    def present(self, keys=('A',), assignment='new', executor=None):
        data = self.runtime.routing_status()['intake']
        items = []
        for key in keys:
            spec = data['requirements'][key]
            items.append(dict(task_id=key, requirement_id=key, name=spec['name'], scope=spec['scope'],
                acceptance=spec['acceptance'], scope_digest=scope_digest(spec), split_reason='Independently verifiable goal',
                assignment=assignment, executor_id=executor, assignment_reason='One developer for this requirement'))
        plan = self.identity()
        presentation = self.json(plan+'-presentation.json', dict(conversation_id=self.actor, plan_id=plan, version=1, items=items))
        self.record('present', plan_id=plan, version=1, items=items, presentation=presentation)
        return plan

    def approve(self, plan, keys=('A',), role='user', decision='approved', origin=True):
        presentation = self.runtime.routing_status()['intake']['plans'][plan]['presentation']
        reference = self.json(self.identity()+'-approval.json', dict(plan_id=plan, presentation=presentation,
            task_ids=list(keys), decision=decision, quote='Approve this split and dispatch summary'))
        return self.command('approve', plan_id=plan, reference=reference,
            user_origin=dict(role=role, conversation_id=self.actor, reference=reference) if origin else None)

    def task(self, key='A', approve=True):
        self.assertEqual(self.spec(key)['status'], 'recorded')
        plan = self.present((key,))
        requirement = self.runtime.routing_status()['intake']['requirements'][key]
        work = self.root/key
        work.mkdir()
        self.runtime.prepare(dict(task_id=key, worktree=str(work), branch='task/'+key, scope=requirement['scope'],
            acceptance=requirement['acceptance'], user_reference='Explicit fixture task start'), self.actor)
        if approve:
            self.assertEqual(self.approve(plan, (key,))['status'], 'recorded')
        return plan

    def bind_task(self, key='A', owner=None):
        owner = owner or 'worker-'+key
        self.runtime.bind_executor(owner, {'native_agent': dict(target_id='native-'+key, host_id='fixture')},
            self.actor, self.file(self.identity()+'-binding.txt'), task_id=key)
        self.observed(owner)
        return owner

    def instruction(self, key='A', action='implement', candidates=None, changes=None, source=None):
        identity = self.identity()
        self.record('instruction', instruction_id=identity, source=source or self.json(identity+'-source.json',
            dict(text='Explicit instruction '+identity)), action=action, candidates=candidates if candidates is not None else [key],
            reason='Unique demand from current discussion', changes=changes or {})
        return identity

    def route(self, key='A', action='implement', instruction=None, spawn=False):
        instruction = instruction or self.instruction(key, action)
        route = self.identity()
        owner = None if spawn else self.runtime.routing_status()['tasks'][key]['owner_agent_id']
        result = self.command('route', route_id=route, instruction_id=instruction, task_id=key,
            actions=[dict(kind='spawn' if spawn else 'send', task_id=key, executor_id=owner, message='Bounded '+instruction)])
        return route, result

    def launch(self, route, adapter=None, attempt=None):
        decision = self.runtime.routing_status()['intake']['routes'][route]
        if adapter is None:
            adapter, _ = self.adapter({'send_input': {'submission_id': 'fixture'},
                'spawn_agent': {'agent_id': 'new-fixture'}, 'wait_agent': {'status': 'running'}})
        attempt = attempt or self.identity()
        return self.runtime.launch(decision['task_id'], adapter, attempt_id=attempt,
            admission=self.permit(task=decision['task_id'], attempt=attempt,
                executor='spawn:'+attempt if decision['spawn'] else decision['executor_id']),
            authorized=True, message=decision['message'], spawn=decision['spawn'],
            executor_id=decision['executor_id'], route_id=route)

    def accepted(self, key='A'):
        # Explicit synthetic acceptance state: does not claim a real user accepted.
        self.reload()
        self.state['tasks'][key].update(user_acceptance='accepted', phase='awaiting_integration')
        self.store()

    def test_T01_queries_remain_main_without_tasks_or_host_calls(self):
        for action in ('query', 'discuss'):
            self.instruction(action=action, candidates=[])
        self.assertEqual(self.runtime.routing_status()['tasks'], {})
        self.assertTrue(all(i['status'] == 'main_handled' for i in self.runtime.routing_status()['intake']['instructions'].values()))

    def test_T02_approved_new_demand_binds_actual_identity_worktree_and_one_pr(self):
        self.task()
        self.assertIsNone(self.runtime.routing_status()['tasks']['A']['owner_agent_id'])
        route, result = self.route(spawn=True)
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(self.launch(route)['status'], 'running')
        self.assertEqual(self.runtime.routing_status()['tasks']['A']['owner_agent_id'], 'native_agent:new-fixture')
        self.runtime.apply(dict(op='authorize', task_id='A', rights=['mr'], user_reference='Synthetic explicit PR authorization'), self.actor)
        mr = dict(id='fixture-1', url='https://github.com/example/fixture/pull/1', provider='github',
            source_branch='task/A', target_branch='main', head_sha='a'*40, target_sha='b'*40)
        self.record('bind-pr', task_id='A', mr=mr)
        self.assertEqual(self.runtime.routing_status()['tasks']['A']['mr'], mr)
        self.assertEqual(self.command('bind-pr', task_id='A', mr=dict(mr, id='different'))['status'], 'rejected')

    def test_T03_same_unfinished_goal_reuses_owner_worktree_and_pr(self):
        self.task(); self.bind_task()
        self.runtime.apply(dict(op='authorize', task_id='A', rights=['mr'], user_reference='fixture'), self.actor)
        mr = dict(id='one', url='https://github.com/example/fixture/pull/1', provider='github',
            source_branch='task/A', target_branch='main', head_sha='a'*40, target_sha='b'*40)
        self.record('bind-pr', task_id='A', mr=mr)
        before = self.runtime.routing_status()['tasks']['A']
        route, result = self.route(action='defect')
        self.assertEqual(result['status'], 'recorded')
        adapter, calls = self.adapter({'send_input': {'id': 'native-A'}, 'wait_agent': {'status': 'running'}})
        self.launch(route, adapter)
        after = self.runtime.routing_status()['tasks']['A']
        self.assertEqual([t for t, _ in calls], ['send_input', 'wait_agent'])
        for field in ('owner_agent_id', 'worktree', 'branch', 'mr'):
            self.assertEqual(before[field], after[field])

    def test_T04_same_module_does_not_allow_reusing_another_demands_developer(self):
        self.task(); owner = self.bind_task(); self.task('B')
        with self.assertRaisesRegex(ValueError, 'another requirement'):
            self.runtime.bind_executor(owner, self.runtime._load()['runtime']['executors'][owner]['routes'],
                self.actor, self.file('authority-B.txt'), task_id='B')

    def test_T05_ambiguous_pause_holds_only_instruction(self):
        self.task(); self.task('B')
        before = self.runtime.routing_status()['tasks']
        identity = self.instruction(candidates=['A', 'B'], action='adjust')
        self.assertEqual(self.runtime.routing_status()['intake']['instructions'][identity]['status'], 'held')
        self.assertEqual(before, self.runtime.routing_status()['tasks'])

    def test_T06_mixed_message_routes_clear_parts_and_holds_ambiguous_part(self):
        self.task(); self.bind_task(); self.task('B')
        held = self.instruction(candidates=['A', 'B'])
        clear = self.instruction('A')
        _, result = self.route(instruction=clear)
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(self.runtime.routing_status()['intake']['instructions'][held]['status'], 'held')

    def test_T07_cross_module_goal_keeps_one_task_and_developer(self):
        self.task(); self.bind_task()
        source = self.json('cross-module.json', dict(text='Implement state, intake and dispatch for the same goal'))
        instruction = self.instruction(source=source)
        _, result = self.route(instruction=instruction)
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(len(self.runtime.routing_status()['tasks']), 1)

    def dependency(self, disposition):
        self.task(); self.bind_task(); self.task('B'); self.bind_task('B')
        self.runtime.apply(dict(op='delegate', agent_id='integration', task_ids=['A', 'B'], merge_order=['A', 'B'],
            required_ci_jobs=['fixture-unit'], routes={owner: dict(target_id='native-'+key, existing_work_agent=True,
                evidence=self.file('route-'+key)) for key, owner in [('A','worker-A'), ('B','worker-B')]},
            authority_evidence=self.file('integration-authority'), user_reference='Synthetic explicit integration role'), self.actor)
        fields = dict(source_task='A', target_task='B', contract='Payload schema v2', actual_change='Required field added', required_action='Read the confirmed schema field')
        self.record('dependency', actor='worker-A', dependency_id='dep', evidence=self.file('actual-contract-diff'), **fields)
        proof = self.json('verified-dependency.json', dict(fields, verifier='integration', disposition=disposition,
            basis=[self.file('concrete-schema-proof')]))
        before = self.runtime.routing_status()['tasks']
        result = self.record('verify-dependency', actor='integration', dependency_id='dep', evidence=proof)
        return before, result

    def test_T08_integration_verified_dependency_adds_todo_without_interrupt(self):
        before, result = self.dependency('within_scope')
        after = self.runtime.routing_status()['tasks']
        self.assertEqual(result['host_actions'], [])
        self.assertFalse(result['interrupt'])
        self.assertEqual(after['B']['routing']['dependency_todos'], ['dep'])
        for field in ('phase', 'owner_agent_id', 'worker_run'):
            self.assertEqual(before['B'][field], after['B'][field])

    def test_T09_same_file_without_dependency_evidence_changes_no_task(self):
        before, result = self.dependency('unsubstantiated')
        self.assertEqual(result['status'], 'unsubstantiated')
        self.assertEqual(before, self.runtime.routing_status()['tasks'])

    def test_T10_out_of_scope_dependency_waits_for_main_user_alignment(self):
        before, result = self.dependency('needs_user')
        self.assertEqual(result['status'], 'needs_user')
        self.assertEqual(before, self.runtime.routing_status()['tasks'])

    def test_T11_authorization_changes_only_explicit_rights_of_one_demand(self):
        self.task(); self.task('B')
        before = deepcopy(self.runtime._load()['tasks']['B'])
        rights = dict(commit=True, push=True, mr=True)
        source = self.json('scoped-rights.json', dict(text='Allow commit, push and PR for A', task_id='A', rights=rights))
        identity = self.instruction(action='authorize', source=source, changes=dict(rights=rights))
        self.record('apply-instruction', instruction_id=identity,
            user_origin=dict(role='user', conversation_id=self.actor, reference=source))
        state = self.runtime._load()
        self.assertTrue(all(state['tasks']['A']['authorization'][r]['allowed'] for r in rights))
        self.assertFalse(state['tasks']['A']['authorization']['deploy']['allowed'])
        self.assertEqual(before, state['tasks']['B'])

    def test_T12_accepted_defect_returns_to_original_pr_author(self):
        self.task(); self.bind_task(); self.accepted()
        route, result = self.route(action='defect')
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(self.launch(route)['executor_id'], 'worker-A')

    def test_T13_new_capability_after_acceptance_is_a_new_requirement(self):
        self.task(); self.accepted()
        before = self.runtime.routing_status()['tasks']
        identity = self.instruction(action='adjust')
        self.assertEqual(self.runtime.routing_status()['intake']['instructions'][identity]['status'], 'new_requirement')
        self.assertEqual(before, self.runtime.routing_status()['tasks'])
        self.assertEqual(self.spec(scope='New independently testable capability')['status'], 'rejected')

    def test_T18_stale_rights_owner_scope_or_repeated_event_cannot_repeat_host_effect(self):
        self.task(); self.bind_task()
        route, result = self.route()
        self.assertEqual(result['status'], 'recorded')
        adapter, calls = self.adapter({'send_input': {'id': 'native-A'}, 'wait_agent': {'status': 'running'}})
        self.runtime.apply(dict(op='authorize', task_id='A', rights=['push'], user_reference='new scoped right'), self.actor)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.launch(route, adapter)
        self.assertEqual(calls, [])
        route, _ = self.route()
        attempt = 'one-attempt'
        first = self.launch(route, adapter, attempt)
        second = self.launch(route, adapter, attempt)
        self.assertEqual(first['status'], second['status'])
        self.assertEqual(len(calls), 2)
        duplicate_route, result = self.route(instruction=self.runtime.routing_status()['intake']['routes'][route]['instruction_id'])
        self.assertEqual(result['status'], 'rejected')
        self.assertNotIn(duplicate_route, self.runtime.routing_status()['intake']['routes'])

    def test_T19_developer_cannot_direct_another_task_or_verify_dependency(self):
        self.task(); self.bind_task(); self.task('B')
        before = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):
            self.command('route', actor='worker-A', route_id='bad')
        with self.assertRaises(ValueError):
            self.command('verify-dependency', actor='worker-A', dependency_id='bad')
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_T21_current_spec_self_check_still_requires_summary_approval(self):
        self.task(approve=False)
        route, result = self.route(spawn=True)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('approval', result['reason'])
        self.assertNotIn(route, self.runtime.routing_status()['intake']['routes'])

    def test_T22_unaligned_ideas_create_no_spec_or_developer(self):
        for _ in range(3):
            self.instruction(action='discuss', candidates=[])
        data = self.runtime.routing_status()
        self.assertEqual(data['intake']['requirements'], {})
        self.assertEqual(data['tasks'], {})

    def test_T23_main_proposes_split_and_merge_with_reasons_then_user_approves_count(self):
        self.assertEqual(self.spec('A')['status'], 'recorded')
        self.assertEqual(self.spec('B')['status'], 'recorded')
        plan = self.present(('A', 'B'))
        self.assertEqual(len(self.runtime.routing_status()['intake']['plans'][plan]['items']), 2)
        self.assertEqual(self.approve(plan, ('A', 'B'))['status'], 'recorded')
        self.assertEqual(self.runtime.routing_status()['tasks'], {})

    def test_T24_mismatched_spec_is_retained_as_rejection_not_dispatchable(self):
        result = self.spec(consistent=False)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.runtime.routing_status()['intake']['requirements'], {})
        self.assertTrue(any(e['result']['status'] == 'rejected' for e in self.runtime.routing_status()['intake']['events'].values()))

    def test_T25_changed_spec_invalidates_check_technical_recheck_retains_scope_approval(self):
        self.task(); self.bind_task()
        route, _ = self.route()
        record = self.runtime.routing_status()['intake']['requirements']['A']
        Path(record['spec']['path']).write_text('Changed after self-check', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.launch(route)
        self.assertEqual(self.spec()['status'], 'recorded')
        route, result = self.route()
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(self.launch(route)['status'], 'running')

    def test_T26_spec_check_is_neither_acceptance_nor_deployment_permission(self):
        self.task()
        task = self.runtime._load()['tasks']['A']
        self.assertEqual(task['user_acceptance'], 'pending')
        self.assertEqual(task['self_test'], 'pending')
        self.assertFalse(task['authorization']['deploy']['allowed'])

    def test_T27_no_presented_dispatch_summary_prevents_host_dispatch(self):
        self.prepare()
        adapter, calls = self.adapter({'spawn_agent': {'agent_id': 'must-not-create'}})
        with self.assertRaisesRegex(ValueError, 'routing decision'):
            self.runtime.launch('A', adapter, attempt_id='unapproved', admission=self.permit(attempt='unapproved'),
                authorized=True, message='unapproved', spawn=True)
        self.assertEqual(calls, [])

    def test_T28_changed_scope_needs_new_summary_approval_unaffected_task_continues(self):
        self.task(); self.bind_task(); self.task('B'); self.bind_task('B')
        self.assertEqual(self.spec(scope='Confirmed expanded goal A')['status'], 'recorded')
        _, result = self.route()
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('scope changed', result['reason'])
        route_b, result_b = self.route('B')
        self.assertEqual(result_b['status'], 'recorded')
        plan = self.present()
        self.assertEqual(self.approve(plan)['status'], 'recorded')
        self.record('sync-spec', task_id='A')
        _, result = self.route()
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(self.launch(route_b)['status'], 'running')

    def test_T29_partial_approval_dispatches_only_selected_task(self):
        self.task(approve=False); self.task('B', approve=False)
        plan = self.present(('A', 'B'))
        self.assertEqual(self.approve(plan, ('A',))['status'], 'recorded')
        route, approved = self.route(spawn=True)
        _, pending = self.route('B', spawn=True)
        self.assertEqual(approved['status'], 'recorded')
        self.assertEqual(pending['status'], 'rejected')
        self.assertEqual(self.launch(route)['status'], 'running')

    def test_T30_worker_claim_unrelated_yes_or_presentation_is_not_user_approval(self):
        plan = self.task(approve=False)
        self.assertEqual(self.approve(plan, role='worker')['status'], 'rejected')
        self.assertEqual(self.approve(plan, origin=False)['status'], 'rejected')
        reference = self.json('unrelated-yes.json', dict(quote='yes', task_ids=['A'], decision='approved', plan_id='unrelated'))
        result = self.command('approve', plan_id=plan, reference=reference,
            user_origin=dict(role='user', conversation_id=self.actor, reference=reference))
        self.assertEqual(result['status'], 'rejected')
        _, result = self.route(spawn=True)
        self.assertEqual(result['status'], 'rejected')

    def test_historical_misdispatch_replay_only_updates_adoption_and_never_interrupts(self):
        self.task('ADOPTION'); self.bind_task('ADOPTION'); self.task('CORE'); self.bind_task('CORE')
        before = deepcopy(self.runtime._load()['tasks']['CORE'])
        executor_before = deepcopy(self.runtime._load()['runtime']['executors']['worker-CORE'])
        quote = '我认为我们以应用到目前这个真实的开发环境为首要目标，这样开发的结果就能获得真实的反馈。而不是模拟测试。'
        source = self.json('historical-source.json', dict(text=quote))
        identity = self.instruction('ADOPTION', action='adjust', source=source)
        for actions in ([dict(kind='pause', task_id='CORE', executor_id='worker-CORE', message='All development pauses')],
                        [dict(kind='send', task_id='ADOPTION', executor_id='worker-ADOPTION', message='Adopt'),
                         dict(kind='send', task_id='CORE', executor_id='worker-CORE', message='Review now')]):
            result = self.command('route', route_id=self.identity(), instruction_id=identity, task_id='ADOPTION', actions=actions)
            self.assertEqual(result['status'], 'rejected')
        route, result = self.route('ADOPTION', instruction=identity)
        self.assertEqual(result['status'], 'recorded')
        adapter, calls = self.adapter({'send_input': {'id': 'native-ADOPTION'}, 'wait_agent': {'status': 'running'}})
        self.launch(route, adapter)
        self.assertEqual(calls[0][1]['id'], 'native-ADOPTION')
        self.assertFalse(calls[0][1]['interrupt'])
        self.assertEqual([t for t, _ in calls], ['send_input', 'wait_agent'])
        self.assertEqual(before, self.runtime._load()['tasks']['CORE'])
        self.assertEqual(executor_before, self.runtime._load()['runtime']['executors']['worker-CORE'])
        self.assertEqual(load_bound(source)['text'], quote)

    def test_event_dedup_conflict_and_public_status_cli_are_current(self):
        command = dict(op='instruction', event_id='stable', instruction_id='stable', action='query', candidates=[],
            source=self.json('query.json', dict(text='Explain routing')), reason='Main handles query')
        first = self.runtime.routing(command, self.actor)
        before = self.runtime.path.read_bytes()
        self.assertEqual(first, self.runtime.routing(command, self.actor))
        self.assertEqual(before, self.runtime.path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.runtime.routing(dict(command, reason='conflict'), self.actor)
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('core_runtime.py')),
            '--state-root', str(self.root/'authority'), '--project-root', str(self.root/'project'), 'routing-status'],
            capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stable', result.stdout)
        rejected = self.json('rejected-cli.json', dict(op='route', event_id='cli-rejected', route_id='no-task'))
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('core_runtime.py')),
            '--state-root', str(self.root/'authority'), '--project-root', str(self.root/'project'),
            'routing', rejected['path'], '--actor', self.actor], capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn('rejected', result.stdout)
        self.assertIn('cli-rejected', self.runtime.routing_status()['intake']['events'])

    def test_ended_demand_does_not_reuse_developer_for_new_goal(self):
        self.task(); self.bind_task(); self.reload()
        self.state['tasks']['A'].update(phase='completed', merge=dict(verified=True, head_sha='a'*40))
        self.store(); self.task('B')
        with self.assertRaisesRegex(ValueError, 'another requirement'):
            self.runtime.bind_executor('worker-A', self.runtime._load()['runtime']['executors']['worker-A']['routes'],
                self.actor, self.file('no-recycle-authority'), task_id='B')

    def test_explicitly_rejected_summary_and_missing_start_right_never_dispatch(self):
        plan = self.task(approve=False)
        self.assertEqual(self.approve(plan, decision='rejected')['status'], 'recorded')
        _, rejected = self.route(spawn=True)
        self.assertEqual(rejected['status'], 'rejected')
        self.approve(plan)
        self.assertEqual(self.spec(start=False)['status'], 'recorded')
        _, result = self.route(spawn=True)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('start authorization', result['reason'])

    def test_owner_generation_or_recipient_change_invalidates_route_before_host(self):
        self.task(); self.bind_task()
        route, _ = self.route()
        self.reload()
        self.state['runtime']['executors']['worker-A']['routes']['native_agent']['target_id'] = 'different'
        self.store()
        adapter, calls = self.adapter({})
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.launch(route, adapter)
        self.assertEqual(calls, [])

    def test_old_spec_route_cannot_survive_a_technical_recheck(self):
        self.task(); self.bind_task()
        route, _ = self.route()
        self.spec()
        adapter, calls = self.adapter({})
        with self.assertRaisesRegex(ValueError, 'spec changed'):
            self.launch(route, adapter)
        self.assertEqual(calls, [])

    def test_only_explicit_condition_is_replaced_without_changing_other_constraints(self):
        self.task()
        first = self.instruction(action='adjust', changes=dict(conditions=dict(delivery='file', deploy='forbidden')))
        self.record('apply-instruction', instruction_id=first)
        second = self.instruction(action='adjust', changes=dict(conditions=dict(delivery='real project')))
        self.record('apply-instruction', instruction_id=second)
        self.assertEqual(self.runtime.routing_status()['tasks']['A']['routing']['conditions'],
            dict(delivery='real project', deploy='forbidden'))
        self.accepted()
        third = self.instruction(action='defect', changes=dict(conditions=dict(new_capability='x')))
        self.assertEqual(self.command('apply-instruction', instruction_id=third)['status'], 'rejected')

    def test_duplicate_requirement_worktree_or_pr_binding_is_rejected(self):
        self.task(); self.bind_task()
        self.task('B'); self.bind_task('B')
        for key in ('A', 'B'):
            self.runtime.apply(dict(op='authorize', task_id=key, rights=['mr'], user_reference='fixture PR right'), self.actor)
        mr = dict(id='one', url='https://github.com/example/fixture/pull/1', provider='github',
            source_branch='task/A', target_branch='main', head_sha='a'*40, target_sha='b'*40)
        self.record('bind-pr', task_id='A', mr=mr)
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'another requirement'):
            self.runtime.apply(dict(op='mr', task_id='B', mr=dict(mr, source_branch='task/B')), self.actor)
        self.assertEqual(before, self.runtime.path.read_bytes())
        with self.assertRaises(ValueError):
            self.runtime.prepare(dict(task_id='overlap', worktree=str(self.root/'A'), branch='different',
                scope='x', acceptance=['x'], user_reference='fixture'), self.actor)

    def test_new_read_only_developer_is_rejected_but_history_is_not_rewritten(self):
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'stay with main'):
            self.runtime.prepare(dict(task_id='query', task_kind='read_only', acceptance_type='delivery_verified',
                worktree=str(self.root/'query'), branch='query', scope='Explain', acceptance=['Answer'], user_reference='fixture'), self.actor)
        self.assertEqual(before, self.runtime.path.read_bytes())

    def observed_proof(self):
        spec = self.file('approved-spec.md')
        checklist = self.file('approved-checklist.md')
        approval = self.json('approved-plan.json', dict(schema='approved-dispatch-plan/1', approval='approved',
            source='direct user message in original main conversation', user_quote='Approve new developer for A',
            main_conversation=self.actor, integration_agent='integration', tasks=[dict(id='A')],
            specs=[spec, checklist]))
        work = self.root/'observed-A'; work.mkdir()
        self.runtime.prepare(dict(task_id='A', worktree=str(work), branch='task/A', scope='Approved A',
            acceptance=['Verified A'], user_reference=approval['path']), self.actor)
        dispatch = self.json('original-dispatch.json', dict(authority_file=approval['path'], dispatch_attempts=[
            dict(status='fulfilled', value=dict(task_id='A', worktree=str(work), branch='task/A',
                agent_id='actual-native', spec='approved-spec.md', checklist='approved-checklist.md'))]))
        observation = self.json('original-observation.json', dict(thread=dict(id='actual-native', status=dict(type='active')),
            turns=[dict(status='inProgress')]))
        return dict(project_id=self.runtime.project_id, task_id='A', based_on_revision=self.runtime._load()['revision'],
            target_version=task_version(self.runtime._load(), 'A'), worktree=str(work), branch='task/A',
            executor_id='actual-native', host_id='fixture-host', observer_id='integration',
            approval=approval, dispatch_result=dispatch, observation=observation,
            spec_at_dispatch=spec, checklist_at_dispatch=checklist)

    def test_observed_external_spawn_binds_pending_task_without_host_or_fake_admission(self):
        proof = self.json('bind-proof.json', self.observed_proof())
        result = self.runtime.bind_observed_dispatch('A', proof, 'integration')
        state = self.runtime._load()
        self.assertEqual(state['tasks']['A']['owner_agent_id'], 'actual-native')
        self.assertEqual(state['tasks']['A']['worker_run']['status'], 'running')
        self.assertEqual(state['runtime']['operations'], {})
        self.assertFalse(result['admission_reconstructed'])
        self.assertEqual(result['host_actions'], [])
        before = self.runtime.path.read_bytes()
        self.assertTrue(self.runtime.bind_observed_dispatch('A', proof, 'integration')['duplicate'])
        self.assertEqual(before, self.runtime.path.read_bytes())

    def test_observed_spawn_rejects_stale_identity_or_unapproved_caller_without_mutation(self):
        raw = self.observed_proof()
        for change, actor in [(dict(based_on_revision=0), 'integration'), (dict(branch='another'), 'integration'),
            (dict(executor_id='fake'), 'integration'), ({}, 'worker-A'), (dict(observer_id='main-session'), 'integration'),
            (dict(target_version='stale'), 'integration')]:
            with self.subTest(change=change, actor=actor):
                proof = self.json(self.identity()+'-bad-binding.json', dict(raw, **change))
                before = self.runtime.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.runtime.bind_observed_dispatch('A', proof, actor)
                self.assertEqual(before, self.runtime.path.read_bytes())

    def test_observed_spawn_rejects_wrong_or_nonrunning_observation_and_changed_approval(self):
        raw = self.observed_proof()
        for thread in (dict(id='different', status=dict(type='active')), dict(id='actual-native', status=dict(type='idle'))):
            proof = self.json(self.identity()+'-bad-observation-proof.json', dict(raw,
                observation=self.json(self.identity()+'-bad-observation.json', dict(thread=thread, turns=[]))))
            before = self.runtime.path.read_bytes()
            with self.assertRaises(ValueError):
                self.runtime.bind_observed_dispatch('A', proof, 'integration')
            self.assertEqual(before, self.runtime.path.read_bytes())
        Path(raw['approval']['path']).write_text('revoked', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.runtime.bind_observed_dispatch('A', self.json('revoked-proof.json', raw), 'integration')
