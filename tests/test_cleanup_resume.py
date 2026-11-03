"""Same CORE cleanup operation; real disposable Git, fixture host/merge facts."""
from pathlib import Path
import unittest

import test_resource_closed_loop as closed_loop
from codex_parallel_coordination.worktrees import GitWorktrees
from evidence import load_bound


class RealGitResumeTests(unittest.TestCase):
    def setUp(self):
        self.case = c = closed_loop.ResourceClosedLoop('test_native_root_handoff_preserves_real_local_permit_release')
        c.setUp()
        self.addCleanup(c.doCleanups)
        self.original = c.bridge.cleanup

        def unwired(resource):
            if resource['kind'] == 'worktree':
                c.artifacts.adapters.pop('git_worktree', None)
                stored = c.controller.read()[0]['cleanup_resources'][resource['id']]['spec']
                c.artifacts.update_usage(resource['id'], stored['project_id'], 'A', stored['attempt_id'], ['A'], False)
                resource = dict(resource)
                resource.pop('metadata', None)  # Reproduce the original unconnected host callback.
            return self.original(resource)

        c.bridge.cleanup = unwired
        with self.assertRaisesRegex(AssertionError, 'pending_cleanup'):
            c.normal_cycle(scoped=True, native=True, durable=True)
        self.state = state = c.runtime._load()
        self.resource = state['runtime']['resources']['A:worktree']
        self.operation = state['tasks']['A']['cleanup']['operation_id']
        self.proof = state['tasks']['A']['preservation']
        self.assertEqual(self.resource['cleanup_status'], 'unknown')
        self.assertIn('no trusted adapter', self.resource['cleanup_result']['reason'])
        stored = c.controller.read()[0]['cleanup_resources']['A:worktree']
        self.assertEqual(stored['status'], 'registered')
        self.assertNotIn('operation_id', stored)
        self.tree = Path(self.resource['path'])
        self.assertTrue(self.tree.exists())
        self.git = GitWorktrees(c.runtime.project_root, state['runtime']['cleanup_root'])
        self.removes = []
        remove = self.git.remove

        def counted(spec, identity):
            self.removes.append(spec['resource_id'])
            return remove(spec, identity)

        self.git.remove = counted
        c.artifacts.adapters['git_worktree'] = self.git
        self.spec = dict(stored['spec'], references=[])
        observation = self.git.observe(self.spec)
        self.assertTrue(observation['safe'])
        self.assertEqual(observation['identity'], stored['identity'])
        self.assertEqual(observation['head'], state['tasks']['A']['merge']['head_sha'])
        c.artifacts.update_usage('A:worktree', self.spec['project_id'], 'A', self.spec['attempt_id'], [], False)

    def reconcile_retained(self):
        c = self.case
        observation = self.git.observe(self.spec)
        receipt = c.json('actual-retained.json', dict(resource_id='A:worktree', status='retained',
            evidence=[c.json('actual-before-delete.json', dict(observation=observation,
                resource_status='registered', no_delete_started=True))]))
        c.runtime.reconcile_cleanup('A', 'A:worktree', receipt, 'delivery-worker')
        return receipt

    def invoke(self, resource):
        resource = dict(resource)
        resource.pop('metadata', None)  # Keep the explicitly registered, counted trusted adapter.
        return self.original(resource)

    def resume(self, invoke=None, **options):
        return self.case.runtime.cleanup_task('A', self.operation, self.proof,
            invoke or self.invoke, 'delivery-worker', authorized=options.get('authorized', True), scope='fixture_only')

    def test_unwired_retained_same_operation_removes_real_git_once(self):
        receipt = self.reconcile_retained()
        before = set(self.state['runtime']['operations'])
        sibling = self.case.root/'other-project'/'untouched.txt'
        sibling.write_bytes(b'other project remains')
        result = self.resume()
        self.assertEqual(sibling.read_bytes(), b'other project remains')
        self.assertEqual(result['status'], 'cleanup_completed', result.get('results'))
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.removes, ['A:worktree'])
        after = self.case.runtime._load()
        self.assertEqual(before, set(after['runtime']['operations']))
        self.assertEqual(after['tasks']['A']['cleanup']['operation_id'], self.operation)
        self.assertIn(receipt, after['runtime']['resources']['A:worktree']['cleanup_observations'])
        recovery = after['runtime']['operations'][self.operation]['resumptions'][0]
        self.assertEqual(recovery['previous_result'], self.resource['cleanup_result'])
        self.assertEqual(recovery['observation'], receipt)
        self.assertEqual(load_bound(recovery['callback_result'])['result'], self.resource['cleanup_result'])
        self.assertEqual(recovery['callback_capture'], self.state['runtime']['operations'][self.operation]['callback_captures']['A:worktree'])
        self.assertEqual(self.resume(lambda _: self.fail('duplicate delete'))['status'], 'cleanup_completed')
        self.assertEqual(self.removes, ['A:worktree'])
        import json
        print(json.dumps(dict(cleanup_resume_trace=dict(scope='fixture_only', operation_id=self.operation,
            same_operation=True, remove_calls=len(self.removes), target_exists=self.tree.exists(),
            original_failure=recovery['previous_result'], retained_observation_preserved=True,
            original_callback_capture_preserved=True, duplicate_did_not_delete=True,
            unrelated_project_unchanged=True, native_calls=0, live_ledger_writes=0))))

    def test_unknown_and_missing_return_only_observe_until_reconciled(self):
        for status in ('unknown', 'intent'):
            with self.subTest(status=status):
                state = self.case.runtime._load()
                if status == 'intent':
                    from copy import deepcopy
                    state['runtime']['resources']['A:worktree'] = deepcopy(
                        self.state['runtime']['operations'][self.operation]['callback_captures']['A:worktree']['resource'])
                    state['runtime']['operations'][self.operation].pop('callback_captures', None)
                state['runtime']['resources']['A:worktree']['cleanup_status'] = status
                from evidence import write_atomic
                write_atomic(self.case.runtime.path, state)  # Isolated crash/receipt-loss injection only.
                result = self.resume(lambda _: self.fail('uncertain effect retried'))
                self.assertEqual(result['status'], 'pending_cleanup')
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())

    def test_reconciled_absence_never_invokes_remove_again(self):
        c = self.case
        self.assertEqual(self.git.remove(self.spec, self.git.observe(self.spec)['identity'])['status'], 'removed')
        receipt = c.json('actual-absence.json', dict(resource_id='A:worktree', status='cleaned',
            evidence=[c.json('absence.json', dict(exists=self.tree.exists()))]))
        c.runtime.reconcile_cleanup('A', 'A:worktree', receipt, 'delivery-worker')
        self.assertEqual(self.resume(lambda _: self.fail('confirmed absence retried'))['status'], 'cleanup_completed')
        self.assertEqual(self.removes, ['A:worktree'])

    def test_resume_rechecks_authority_preservation_and_activity(self):
        from copy import deepcopy
        from evidence import write_atomic
        self.reconcile_retained()
        baseline = self.case.runtime._load()
        mutations = (
            lambda s: s['runtime']['operations'][self.operation].update(cleanup_authorized=False),
            lambda s: s['tasks']['A'].update(publication_dependencies=['release']),
            lambda s: s['runtime']['executors']['worker-A'].update(active_children=['active-child']),
            lambda s: s['runtime']['executors']['worker-A'].update(pending_work=True),
            lambda s: s['runtime']['executors']['worker-A'].update(protected=True),
            lambda s: s['runtime']['executors']['worker-A'].update(generation=2),
            lambda s: s['tasks']['A']['merge'].update(verified=False),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                state = deepcopy(baseline); mutate(state)
                write_atomic(self.case.runtime.path, state)
                with self.assertRaises(ValueError):
                    self.resume(lambda _: self.fail('changed gate invoked'))
        write_atomic(self.case.runtime.path, baseline)
        with self.assertRaises(ValueError):
            self.resume(authorized=False)
        proof = Path(self.proof['path']); saved = proof.read_bytes()
        proof.write_bytes(b'{}')
        try:
            with self.assertRaises(ValueError): self.resume(lambda _: self.fail('changed preservation'))
        finally:
            proof.write_bytes(saved)
        authority = Path(baseline['tasks']['A']['resource_authority']['path']); saved = authority.read_bytes()
        authority.write_bytes(b'{}')
        try:
            with self.assertRaises(ValueError): self.resume(lambda _: self.fail('changed authority'))
        finally:
            authority.write_bytes(saved)
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())

    def test_protection_paths_identity_and_sharing_still_refuse(self):
        from copy import deepcopy
        from evidence import write_atomic
        self.reconcile_retained()
        baseline = self.case.runtime._load()
        mutations = (
            dict(protected=True), dict(shared=True), dict(path=str(self.case.runtime.project_root)),
            dict(path_identity=[-1, -1]), dict(attempt_id='other-attempt'),
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                state = deepcopy(baseline)
                state['runtime']['resources']['A:worktree'].update(changes)
                write_atomic(self.case.runtime.path, state)
                if 'path' in changes or 'path_identity' in changes:
                    with self.assertRaises(ValueError): self.resume()
                else:
                    self.assertEqual(self.resume()['status'], 'pending_cleanup')
                self.assertTrue(self.tree.exists())
        write_atomic(self.case.runtime.path, baseline)
        self.case.artifacts.update_usage('A:worktree', self.spec['project_id'], 'A', self.spec['attempt_id'], ['A'], False)
        self.assertEqual(self.resume()['status'], 'pending_cleanup')
        self.assertEqual(self.removes, [])

    def test_current_head_must_still_match_tested_main(self):
        import subprocess
        self.reconcile_retained()
        subprocess.run(['git', '-c', 'core.longpaths=true', '-C', str(self.tree), 'checkout', '--detach', 'HEAD~1'],
            check=True, capture_output=True)
        # The normal bridge observes current HEAD even when branch metadata is present.
        self.assertEqual(self.resume(self.original)['status'], 'pending_cleanup')
        self.assertTrue(self.tree.exists())
        self.assertEqual(self.removes, [])

    def test_concurrent_revision_before_adapter_verification_refuses(self):
        from evidence import write_atomic
        self.reconcile_retained()
        def changed(resource):
            self.assertFalse(self.case.runtime.path.with_suffix('.json.lock').exists())
            state = self.case.runtime._load()
            state['revision'] += 1
            write_atomic(self.case.runtime.path, state)
            return self.invoke(resource)
        self.assertEqual(self.resume(changed)['status'], 'pending_cleanup')
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())

    def test_retained_without_public_observation_cannot_resume(self):
        from evidence import write_atomic
        state = self.case.runtime._load()
        state['runtime']['resources']['A:worktree']['cleanup_status'] = 'retained'
        write_atomic(self.case.runtime.path, state)  # Unobserved fixture state is not permission.
        self.assertEqual(self.resume()['status'], 'pending_cleanup')
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())

    def test_concurrent_duplicate_does_not_delete(self):
        self.reconcile_retained()
        def raced(resource):
            self.assertEqual(self.resume(lambda _: self.fail('parallel duplicate'))['status'], 'pending_cleanup')
            return self.invoke(resource)
        self.assertEqual(self.resume(raced)['status'], 'pending_cleanup')
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())

    def test_resource_unknown_remains_observe_only_even_after_core_retained(self):
        self.reconcile_retained()
        remove = self.git.remove
        def uncertain(spec, identity):
            self.removes.append(spec['resource_id'])
            return dict(status='unknown', reason='ack lost; no automatic retry')
        self.git.remove = uncertain
        first = self.resume()
        self.assertEqual(first['status'], 'pending_cleanup')
        self.assertEqual(self.removes, ['A:worktree'])
        self.git.remove = remove
        self.reconcile_retained()
        self.assertEqual(self.resume()['status'], 'pending_cleanup')
        self.assertEqual(self.removes, ['A:worktree'])
        self.assertTrue(self.tree.exists())


    def test_missing_original_return_after_retained_does_not_delete(self):
        from evidence import write_atomic
        self.reconcile_retained()
        state = self.case.runtime._load()
        state['runtime']['operations'][self.operation].pop('callback_results')
        write_atomic(self.case.runtime.path, state)
        self.assertEqual(self.resume()['status'], 'pending_cleanup')
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())

    def test_revision_change_before_intent_rejects_without_stale_save(self):
        from unittest.mock import patch
        import cleanup
        from evidence import write_atomic
        self.reconcile_retained()
        eligibility = cleanup.eligibility
        current = []
        def changed(*args, **options):
            eligibility(*args, **options)
            state = self.case.runtime._load()
            state['revision'] += 1
            write_atomic(self.case.runtime.path, state)  # Fault injection: stale locked snapshot must never be saved.
            current.append(self.case.runtime.path.read_bytes())
        with patch.object(cleanup, 'eligibility', changed):
            with self.assertRaisesRegex(ValueError, 'revision changed'):
                self.resume(lambda _: self.fail('stale intent invoked'))
        self.assertEqual(self.case.runtime.path.read_bytes(), current[0])
        self.assertEqual(self.removes, [])
        self.assertTrue(self.tree.exists())
