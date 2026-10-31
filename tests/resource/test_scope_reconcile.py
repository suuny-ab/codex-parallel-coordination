"""Author-found reconciliation/scope regressions; no new processes or real projects."""
from pathlib import Path
import unittest
import test_cleanup as fixture
from cleanup import ArtifactCleanup
from resources import Controller
from support import load_bound, load, ref, write_atomic


class ScopeReconcileTests(unittest.TestCase):
    setUp = fixture.CleanupTests.setUp
    file = fixture.CleanupTests.file

    def test_stale_writer_lock_snapshot_refuses_precise_historical_projection(self):
        path = self.controller.path
        lock = path.with_suffix(path.suffix + '.lock')
        lock.write_text('fixture interrupted writer', encoding='utf-8')
        try:
            with self.assertRaisesRegex(ValueError, 'lock|reconcile'):
                observed = self.controller.snapshot('alpha')
                self.file('untrusted-snapshot-actually-returned.json', observed)
        finally:
            lock.unlink()  # Only this test's exact created marker.

    def test_other_project_cleanup_root_misconfiguration_rejected(self):
        policy = load_bound(load(self.control.path)['policy'])
        policy['cleanup_roots']['alpha'] = [str(self.root / 'beta/cleanup')]
        control = Controller(self.root / 'bad-scope/resources.json')
        control.initialize(self.file('bad-scope-policy.json', policy))
        target = self.root / 'beta/cleanup/owned-by-beta'
        target.mkdir()
        spec = {'resource_id': 'wrong-owner', 'project_id': 'alpha', 'task_id': 'task', 'attempt_id': '1',
                'kind': 'temporary_directory', 'path': str(target), 'references': [], 'protected': False}
        with self.assertRaisesRegex(ValueError, 'project|scope'):
            ArtifactCleanup(control).register(spec)
        self.assertTrue(target.exists())

    def test_explicit_external_temp_root_not_owned_by_another_project_is_allowed(self):
        policy = load_bound(load(self.control.path)['policy'])
        external = self.root / 'explicit-external-alpha'
        external.mkdir()
        policy['cleanup_roots']['alpha'] = [str(external)]
        control = Controller(self.root / 'external-temp/resources.json')
        control.initialize(self.file('external-temp-policy.json', policy))
        target = external / 'disposable'
        target.mkdir()
        record = ArtifactCleanup(control).register({'resource_id': 'external', 'project_id': 'alpha', 'task_id': 'task',
                    'attempt_id': '1', 'kind': 'temporary_directory', 'path': str(target), 'references': [], 'protected': False})
        self.assertEqual(record['status'], 'registered')

    def test_parent_child_registration_cannot_hide_protected_other_owner(self):
        parent = self.root / 'alpha/cleanup/parent'
        parent.mkdir()
        child = parent / 'protected-child'
        child.write_text('other task fixture', encoding='utf-8')
        common = {'project_id': 'alpha', 'task_id': 'A', 'attempt_id': '1', 'references': [], 'protected': False}
        self.cleaner.register(dict(common, resource_id='parent', kind='temporary_directory', path=str(parent)))
        with self.assertRaisesRegex(ValueError, 'overlap|registered'):
            self.cleaner.register(dict(common, resource_id='child', task_id='B', kind='temporary_file', path=str(child),
                                       protected=True, references=['B-active-reference']))
        self.assertTrue(child.exists())

    def test_state_control_subtree_sibling_cannot_register_for_cleanup(self):
        policy = load_bound(load(self.control.path)['policy'])
        policy['cleanup_roots']['alpha'] = [str(self.root / 'alpha')]
        control = Controller(self.root / 'alpha/control/resources.json')
        control.initialize(self.file('control-scope-policy.json', policy))
        sibling = control.path.parent / 'protected-metadata.json'
        sibling.write_text('control metadata', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'state|authority|protected'):
            ArtifactCleanup(control).register({'resource_id': 'control-data', 'project_id': 'alpha', 'task_id': 'A',
                'attempt_id': '1', 'kind': 'temporary_file', 'path': str(sibling), 'references': [], 'protected': False})
        self.assertTrue(sibling.exists())

    def test_uppercase_git_marker_is_still_a_git_boundary_on_windows(self):
        target = self.root / 'alpha/cleanup/git-case'
        target.mkdir()
        (target / '.GIT').write_text('synthetic marker, no actual Git operation', encoding='utf-8')
        self.assertTrue((target / '.git').exists())
        with self.assertRaises(ValueError):
            self.cleaner.register({'resource_id': 'git-case', 'project_id': 'alpha', 'task_id': 'A', 'attempt_id': '1',
                'kind': 'temporary_directory', 'path': str(target), 'references': [], 'protected': False})
        self.assertTrue((target / '.GIT').exists())

    def test_legacy_nested_registration_blocks_parent_cleanup(self):
        import copy
        from support import fingerprint
        from cleanup import TemporaryFilesystem
        parent = self.root / 'alpha/cleanup/legacy-parent'
        parent.mkdir()
        child = parent / 'B-protected'
        child.write_text('synthetic protected fixture', encoding='utf-8')
        common = {'project_id': 'alpha', 'task_id': 'A', 'attempt_id': '1', 'references': [], 'protected': False}
        original = self.cleaner.register(dict(common, resource_id='parent', kind='temporary_directory', path=str(parent)))
        child_spec = dict(common, resource_id='child', task_id='B', kind='temporary_file', path=str(child),
                          references=['B-active-reference'], protected=True)
        state = load(self.control.path)
        state['cleanup_resources']['child'] = {'spec': child_spec, 'spec_digest': fingerprint(child_spec),
                'identity': TemporaryFilesystem().observe(child_spec)['identity'], 'status': 'registered'}
        write_atomic(self.control.path, state)  # Explicit legacy-state fixture injection.
        proof = fixture.CleanupTests.proof(self, original)
        with self.assertRaisesRegex(ValueError, 'overlap|registered'):
            self.cleaner.cleanup('parent', proof, fixture.CleanupTests.guard(self))
        self.assertTrue(child.exists())
