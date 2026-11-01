"""Real isolated Git/RESOURCE composition; all native actions are explicit fixtures."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'skills/isolated-parallel-development-coordination/scripts'))
from test_core_runtime import CoreFixture
from demand_routing import scope_digest
from evidence import load_bound, ref
from host_adapter import HostAdapter, NATIVE
from codex_parallel_coordination.bridge import ResourceBridge
from codex_parallel_coordination.dispatch import StandardDispatch
from codex_parallel_coordination.resources import ArtifactCleanup, Controller
from codex_parallel_coordination.worktrees import GitWorktrees


class DispatchTests(CoreFixture):
    def setUp(self):
        super().setUp()
        repo = self.runtime.project_root
        subprocess.run(['git', '-c', 'core.longpaths=true', 'clone', '--shared', '--no-checkout', '--branch', 'main',
                        str(ROOT), str(repo)], check=True, capture_output=True)
        self.managed = Path(self.runtime._load()['runtime']['cleanup_root'])
        self.managed.mkdir(parents=True)
        self.git = GitWorktrees(repo, self.managed)
        self.base = self.git._git('rev-parse', 'refs/heads/main').stdout.decode().strip()
        self.git._git('symbolic-ref', 'HEAD', 'refs/heads/main')
        self.policy = dict(authority=self.json('resource-authority.json', dict(scope='explicit_temporary_managed_resources',
                          launch=True, cleanup_artifacts=True)), max_parallel=1, max_agent_slots=1,
            physical_reserve=1024, commit_reserve=1024,
            projects={self.runtime.project_id: [str(repo), str(self.managed)]},
            cleanup_roots={self.runtime.project_id: [str(self.managed)]}, executables=[sys.executable],
            exclusive_keys=['fixture'], phase_resources={'self_test': ['fixture']})
        self.controller = Controller(self.root / 'resource-control/resources.json')
        self.controller.initialize(self.json('policy.json', self.policy)['path'])
        self.artifacts = ArtifactCleanup(self.controller, {'git_worktree': self.git})
        self.bridge = ResourceBridge(self.runtime, self.controller, self.artifacts, self.root / 'external')
        discussion = self.json('discussion.json', dict(confirmed=True))
        spec = self.file('spec.md', 'Approved synthetic feature')
        check = self.json('check.json', dict(coordinator_id=self.actor, requirement_id='REQ-A',
            discussion=discussion, spec=spec, result='consistent'))
        record = dict(name='A', scope='Approved synthetic feature', acceptance=['Observable fixture'],
                      discussion=discussion, spec=spec, check=check)
        item = dict(task_id='A', requirement_id='REQ-A', name='A', scope=record['scope'],
            acceptance=record['acceptance'], scope_digest=scope_digest(record), split_reason='One confirmed goal',
            assignment='new', executor_id=None, assignment_reason='One new developer')
        presentation = self.json('presentation.json', dict(conversation_id=self.actor, plan_id='PLAN-A', version=1, items=[item]))
        tree = str(self.managed / 'new-feature')
        approval = self.json('approval.json', dict(plan_id='PLAN-A', presentation=presentation, task_ids=['A'],
            decision='approved', text='Approve the dispatch', quote='Approve the dispatch', task_id='A',
            rights=dict(commit=True, push=True, mr=True, merge=False), base_main=self.base,
            specs=[spec],
            integration_agent='integration-fixture', tasks=[dict(id='A', branch='codex/fixture', worktree=tree)]))
        self.request = dict(request_id='dispatch-A', project_id=self.runtime.project_id, task_id='A',
            base_main=self.base, requirement=dict(record, start_reference=approval), approval=approval,
            presentation=presentation, worktree=tree, branch='codex/fixture', integration_agent='integration-fixture',
            acceptance_type='delivery_verified', report_path=str(self.root / 'reports/author.json'))
        self.origin = dict(role='user', conversation_id=self.actor, reference=approval)
        self.calls = []
        self.replies = dict(spawn_agent=dict(agent_id='native-fixture'), wait_agent=dict(status={'native-fixture': 'running'}),
                            send_input=dict(submission_id='accepted-fixture'))
        self.developer = HostAdapter('native_agent', list(NATIVE.values()), self.invoke, self.actor)
        self.integration = HostAdapter('native_agent', list(NATIVE.values()), self.invoke, self.actor)
        self.dispatch = StandardDispatch(self.runtime, self.bridge, self.git, self.developer, self.integration, self.origin,
                                         'integration-fixture')

    def invoke(self, tool, args):
        self.calls.append((tool, args))
        result = self.replies[tool]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def run_dispatch(self):
        return self.dispatch.run(self.request)

    def assert_no_side_effect(self, change, phrase=None):
        before = self.runtime.path.read_bytes(), self.controller.path.read_bytes()
        request = deepcopy(self.request)
        change(request)
        with self.assertRaisesRegex((ValueError, KeyError), phrase or '.'):
            self.dispatch.run(request)
        self.assertEqual((self.runtime.path.read_bytes(), self.controller.path.read_bytes()), before)
        self.assertFalse(Path(self.request['worktree']).exists())
        self.assertEqual(self.calls, [])

    def test_actual_worktree_resource_binding_and_one_handoff_are_idempotent(self):
        first = self.run_dispatch()
        self.assertEqual(first['status'], 'dispatched')
        self.assertEqual(first['owner_agent_id'], 'native_agent:native-fixture')
        again = self.run_dispatch()
        self.assertEqual(first, again)
        self.assertEqual([x[0] for x in self.calls], ['spawn_agent', 'wait_agent', 'send_input'])
        state = self.runtime._load()
        self.assertEqual(set(state['runtime']['resources']), {'A:worktree', 'A:agent'})
        self.assertEqual(state['tasks']['A']['worker_run']['status'], 'running')
        self.assertTrue(Path(self.request['worktree']).is_dir())
        self.assertEqual(len(self.controller.read()[0]['cleanup_resources']), 1)
        self.assertFalse(state['tasks']['A']['authorization']['merge']['allowed'])
        self.assertIn(self.request['report_path'], self.calls[0][1]['message'])
        self.assertIn('Do not merge', self.calls[0][1]['message'])

    def test_wrong_project_actor_and_self_authority_reject_before_effect(self):
        self.assert_no_side_effect(lambda r: r.update(project_id='other'), 'wrong project')
        self.assert_no_side_effect(lambda r: r.update(actor_id=self.actor), 'cannot supply')
        self.developer.actor_id = 'untrusted-developer'
        self.dispatch.actor = self.developer.actor_id
        self.assert_no_side_effect(lambda r: None, 'role required')

    def test_approval_origin_spec_hash_and_main_are_rechecked_before_effect(self):
        self.assert_no_side_effect(lambda r: r['requirement']['spec'].update(sha256='0' * 64), 'hash|check|bound')
        self.assert_no_side_effect(lambda r: r.update(base_main='0' * 40), 'approval base')
        self.dispatch.origin = None
        self.assert_no_side_effect(lambda r: None, 'user origin')

    def test_actual_remote_main_drift_rejects_before_any_creation(self):
        real = self.git.runner
        def runner(argv, **kw):
            if 'ls-remote' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=b'ffffffffffffffffffffffffffffffffffffffff\trefs/heads/main\n')
            return real(argv, **kw)
        self.git.runner = runner
        self.assert_no_side_effect(lambda r: None, 'remote main')

    def test_illegal_and_unowned_paths_and_branch_are_rejected(self):
        self.assert_no_side_effect(lambda r: r.update(worktree=str(self.root / 'outside')), 'approved worktree')
        self.assert_no_side_effect(lambda r: r.update(branch='main'), 'approved worktree')
        Path(self.request['worktree']).mkdir()
        with self.assertRaisesRegex(ValueError, 'unowned'):
            self.run_dispatch()
        self.assertEqual(self.calls, [])

    def test_missing_native_capability_is_real_preflight_not_an_operation_list(self):
        self.developer.tools.clear()
        self.assert_no_side_effect(lambda r: None, 'native spawn/observation capability unavailable')

    def test_resource_queue_does_not_create_a_tree_or_call_host(self):
        self.controller.capacity_acquire(self.json('occupied.json', dict(request_id='occupied',
            project_id=self.runtime.project_id, task_id='occupied', attempt_id='existing', launch_identity='existing',
            resource_class='agent_execution_slot'))['path'])
        self.assertEqual(self.run_dispatch()['status'], 'queued')
        self.assertEqual(self.run_dispatch()['status'], 'queued')
        self.assertFalse(Path(self.request['worktree']).exists())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.runtime._load()['tasks']['A']['owner_agent_id'])

    def test_unknown_spawn_retains_owned_tree_and_never_resends_or_changes_id(self):
        self.replies['spawn_agent'] = TimeoutError('fixture response lost')
        self.assertEqual(self.run_dispatch()['status'], 'unknown')
        self.assertEqual(self.run_dispatch()['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(Path(self.request['worktree']).exists())
        self.assertIn('A:worktree', self.runtime._load()['runtime']['resources'])
        request = dict(self.request, request_id='new-id')
        with self.assertRaisesRegex(ValueError, 'already has a dispatch request'):
            self.dispatch.run(request)
        self.assertEqual(len(self.calls), 1)

    def test_accepted_unconfirmed_binds_actual_agent_without_claiming_running(self):
        self.replies['wait_agent'] = dict(status={'native-fixture': 'completed'})
        result = self.run_dispatch()
        self.assertEqual(result['status'], 'accepted_unconfirmed')
        self.assertEqual(self.run_dispatch()['status'], 'accepted_unconfirmed')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(set(self.runtime._load()['runtime']['resources']), {'A:worktree', 'A:agent'})

    def test_worktree_created_but_response_lost_is_observed_not_recreated(self):
        real = self.git.runner
        actions = []
        def runner(argv, **kw):
            result = real(argv, **kw)
            if 'add' in argv:
                actions.append(argv)
                raise subprocess.TimeoutExpired(argv, 20)
            return result
        self.git.runner = runner
        self.assertEqual(self.run_dispatch()['status'], 'worktree_unknown')
        self.assertTrue(Path(self.request['worktree']).exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_dispatch()['status'], 'dispatched')
        self.assertEqual(len(actions), 1)

    def test_handoff_unknown_does_not_resend_developer_or_integration(self):
        self.replies['send_input'] = TimeoutError('fixture handoff response lost')
        self.assertEqual(self.run_dispatch()['status'], 'handoff_unknown')
        self.assertEqual(self.run_dispatch()['status'], 'handoff_unknown')
        self.assertEqual(len(self.calls), 3)

    def test_conflicting_repeat_preserves_original_request(self):
        self.run_dispatch()
        with self.assertRaisesRegex(ValueError, 'conflicting dispatch'):
            self.dispatch.run(dict(self.request, report_path=str(self.root / 'other-report')))
        self.assertEqual(len(self.calls), 3)

    def test_existing_registered_reservation_is_used_without_creating_again(self):
        self.git._git('worktree', 'add', '-b', self.request['branch'], self.request['worktree'], self.base)
        item = load_bound(self.request['presentation'])['items'][0]
        self.runtime.prepare(dict(task_id='A', worktree=self.request['worktree'], branch=self.request['branch'],
            scope=item['scope'], acceptance=item['acceptance'], user_reference=self.request['approval']['path']), self.actor)
        self.runtime.register_resource('A', dict(id='A:worktree', kind='worktree', path=self.request['worktree']),
                                       self.request['approval'], self.actor)
        self.assertEqual(self.run_dispatch()['status'], 'dispatched')

    def test_spawn_identity_is_durable_before_observation_or_bridge_crash(self):
        real = self.developer.invoke
        def invoke(tool, args):
            if tool == 'wait_agent':
                state = self.runtime._load()
                self.assertEqual(state['tasks']['A']['owner_agent_id'], 'native_agent:native-fixture')
                self.assertEqual(state['runtime']['operations']['dispatch-A:launch']['status'], 'accepted_unconfirmed')
                raise KeyboardInterrupt('fixture bridge crash after actual spawn identity')
            return real(tool, args)
        self.developer.invoke = invoke
        with self.assertRaises(KeyboardInterrupt):
            self.run_dispatch()
        self.assertEqual(self.run_dispatch()['status'], 'accepted_unconfirmed')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('A:agent', self.runtime._load()['runtime']['resources'])

    def test_fixed_bundle_derives_fields_from_frozen_materials(self):
        bundle = {k: self.request[k] for k in ('request_id', 'project_id', 'approval', 'report_path', 'acceptance_type')}
        bundle['spec_check'] = self.request['requirement']['check']
        result = self.dispatch.run(bundle)
        self.assertEqual(result['status'], 'dispatched')
        self.assertEqual(self.dispatch.run(bundle), result)


if __name__ == '__main__':
    unittest.main()
