"""Real disposable directory/SQLite cleanup, CORE guard injected, no real Git/agent operations."""
import copy
from pathlib import Path
import sqlite3
import unittest
import test_resources as fixture
from resources import Controller
from cleanup import ArtifactCleanup, TemporaryFilesystem
from support import load, load_bound, ref


class CleanupTests(unittest.TestCase):
    file = fixture.ResourceTests.file

    def setUp(self):
        fixture.ResourceTests.setUp(self)
        roots = {p: str(self.root / p / 'cleanup') for p in ('alpha', 'beta')}
        for path in roots.values():
            Path(path).mkdir()
        authority = self.file('cleanup-authority.json', {'scope': 'explicit_temporary_managed_resources',
                             'launch': True, 'reclaim_idle': True, 'cleanup_artifacts': True})
        policy = dict(self.policy, authority=ref(authority), cleanup_roots={p: [r] for p, r in roots.items()})
        self.control = Controller(self.root / 'cleanup-state/resources.json')
        self.control.initialize(self.file('cleanup-policy.json', policy))
        self.cleaner = ArtifactCleanup(self.control)
        self.core_state = self.file('canonical-core-fixture.json', {'task_completed': True, 'cleanup_completed': False})
        self.merge = self.file('merge-proof.json', {'kind': 'fixture_only_not_real_git', 'target_main_sha': 'fixture-target-main',
                                                  'source_sha': 'fixture-source', 'verified': True})
        self.handoff = self.file('handoff.json', {'minimal_record': 'fixture commit, decision, acceptance and reproduction'})

    def register(self, key='owned', kind='temporary_directory', **changes):
        path = self.root / 'alpha/cleanup' / key
        if kind in ('temporary_directory', 'acceptance_environment'):
            path.mkdir()
            (path / 'fixture.txt').write_text('disposable fixture', encoding='utf-8')
        elif kind == 'temporary_database':
            db = sqlite3.connect(path)
            try:
                db.execute('CREATE TABLE fixture(value TEXT)')
                db.execute("INSERT INTO fixture VALUES ('synthetic-only')")
                db.commit()
            finally:
                db.close()
        else:
            path.write_text('disposable fixture', encoding='utf-8')
        spec = {'resource_id': key, 'project_id': 'alpha', 'task_id': 'task', 'attempt_id': 'attempt-1',
                'kind': kind, 'path': str(path), 'references': [], 'protected': False}
        spec.update(changes)
        return self.cleaner.register(spec)

    def proof(self, record):
        proof = {k: record['spec'][k] for k in ('resource_id', 'project_id', 'task_id', 'attempt_id')}
        proof.update(merge=ref(self.merge), handoff=ref(self.handoff))
        return ref(self.file('proof-' + proof['resource_id'] + '.json', proof))

    def guard(self, **changes):
        def verify(context, proof):
            merge, handoff = load_bound(proof['merge']), load_bound(proof['handoff'])
            facts = {k: True for k in ('verified', 'task_completed_on_main', 'preservation_valid', 'no_active_work',
                     'no_active_descendants', 'no_release_dependencies', 'exclusive_task_resource', 'cleanup_authorized')}
            facts['protected'] = False
            facts['task_completed_on_main'] = merge['verified'] and merge['target_main_sha'] == 'fixture-target-main'
            facts['preservation_valid'] = bool(handoff['minimal_record'])
            facts.update(changes)
            return facts
        return verify

    def test_premerge_wrong_main_handoff_active_children_dependencies_refused(self):
        record = self.register()
        proof = self.proof(record)
        before = self.core_state.read_bytes()
        for change in ({'task_completed_on_main': False}, {'preservation_valid': False}, {'no_active_work': False},
                       {'no_active_descendants': False}, {'no_release_dependencies': False}, {'cleanup_authorized': False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.cleaner.cleanup('owned', proof, self.guard(**change))
            self.assertTrue(Path(record['spec']['path']).exists())
        self.assertEqual(self.core_state.read_bytes(), before)
        self.merge.write_text('{"verified":true,"target_main_sha":"wrong-main"}', encoding='utf-8')
        proof = self.proof(record)
        with self.assertRaises(ValueError):
            self.cleaner.cleanup('owned', proof, self.guard())
        self.assertTrue(Path(record['spec']['path']).exists())

    def test_preservation_evidence_changed_is_not_ready(self):
        record = self.register()
        proof = self.proof(record)
        self.handoff.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.cleaner.cleanup('owned', proof, self.guard())
        self.assertTrue(Path(record['spec']['path']).exists())

    def test_shared_or_protected_excluded_then_explicit_usage_release(self):
        record = self.register(references=['other-task'], protected=True)
        proof = self.proof(record)
        with self.assertRaises(ValueError):
            self.cleaner.cleanup('owned', proof, self.guard())
        with self.assertRaises(ValueError):
            self.cleaner.update_usage('owned', 'beta', 'task', 'attempt-1', [], False)
        self.cleaner.update_usage('owned', 'alpha', 'task', 'attempt-1', [], False)
        self.assertEqual(self.cleaner.cleanup('owned', proof, self.guard())['status'], 'reclaimed')

    def test_actual_directory_and_database_reclaimed_without_losing_core_task(self):
        before = self.core_state.read_bytes()
        for key, kind in (('directory', 'temporary_directory'), ('database', 'temporary_database'), ('environment', 'acceptance_environment')):
            record = self.register(key, kind)
            result = self.cleaner.cleanup(key, self.proof(record), self.guard())
            self.assertEqual(result['status'], 'reclaimed')
            self.assertFalse(Path(record['spec']['path']).exists())
            self.assertFalse(result['task_completed_changed'])
            self.assertTrue(self.cleaner.cleanup(key, self.proof(record), self.guard())['duplicate'])
        self.assertEqual(before, self.core_state.read_bytes())

    def test_default_filesystem_refuses_git_marker_and_outside_root(self):
        path = self.root / 'alpha/cleanup/git-marker'
        path.mkdir()
        (path / '.git').write_text('fixture not a real worktree', encoding='utf-8')
        spec = {'resource_id': 'git', 'project_id': 'alpha', 'task_id': 'task', 'attempt_id': 'attempt',
                'kind': 'temporary_directory', 'path': str(path), 'references': [], 'protected': False}
        with self.assertRaises(ValueError):
            self.cleaner.register(spec)
        spec.update(resource_id='outside', path=str(self.root / 'beta'))
        with self.assertRaises(ValueError):
            self.cleaner.register(spec)
        self.assertTrue((path / '.git').exists())

    def test_identity_replaced_object_not_deleted(self):
        record = self.register('file', 'temporary_file')
        path = Path(record['spec']['path'])
        path.rename(self.root / 'old-original-file')
        path.write_text('new owner object', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.cleaner.cleanup('file', self.proof(record), self.guard())
        self.assertEqual(path.read_text(), 'new owner object')

    def test_partial_failure_preserves_success_and_explicit_retry_only(self):
        class FailOnce(TemporaryFilesystem):
            calls = 0
            def remove(self, spec, expected):
                self.calls += 1
                if self.calls == 1:
                    return {'status': 'failed_no_change'}
                return super().remove(spec, expected)
        adapter = FailOnce()
        self.cleaner = ArtifactCleanup(self.control, {'acceptance_environment': adapter})
        a = self.register('a')
        b = self.register('b', 'acceptance_environment')
        self.assertEqual(self.cleaner.cleanup('a', self.proof(a), self.guard())['status'], 'reclaimed')
        self.assertEqual(self.cleaner.cleanup('b', self.proof(b), self.guard())['status'], 'pending_cleanup')
        self.assertEqual(self.cleaner.cleanup('b', self.proof(b), self.guard())['status'], 'pending_cleanup')
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(self.cleaner.cleanup('b', self.proof(b), self.guard(), retry=True)['status'], 'reclaimed')
        self.assertEqual(adapter.calls, 2)
        self.assertTrue(load(self.core_state)['task_completed'])
        self.assertEqual(load(self.control.path)['cleanup_resources']['a']['status'], 'reclaimed')

    def test_unknown_after_effect_recovery_observes_absence_no_retry(self):
        class UnknownAfter(TemporaryFilesystem):
            calls = 0
            def remove(self, spec, expected):
                self.calls += 1
                super().remove(spec, expected)
                raise TimeoutError('injected after removal')
        adapter = UnknownAfter()
        self.cleaner = ArtifactCleanup(self.control, {'temporary_directory': adapter})
        record = self.register()
        proof = self.proof(record)
        self.assertEqual(self.cleaner.cleanup('owned', proof, self.guard())['status'], 'pending_cleanup')
        recovered = ArtifactCleanup(Controller(self.control.path), {'temporary_directory': adapter})
        self.assertEqual(recovered.observe('owned')['status'], 'reclaimed')
        self.assertEqual(adapter.calls, 1)

    def test_unknown_before_effect_never_blindly_retries(self):
        class UnknownBefore(TemporaryFilesystem):
            calls = 0
            def remove(self, spec, expected):
                self.calls += 1
                raise TimeoutError('injected before effect')
        adapter = UnknownBefore()
        self.cleaner = ArtifactCleanup(self.control, {'temporary_directory': adapter})
        record = self.register()
        proof = self.proof(record)
        self.assertEqual(self.cleaner.cleanup('owned', proof, self.guard())['status'], 'pending_cleanup')
        self.assertEqual(self.cleaner.cleanup('owned', proof, self.guard(), retry=True)['status'], 'pending_cleanup')
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(Path(record['spec']['path']).exists())
        with self.assertRaises(ValueError):
            self.cleaner.update_usage('owned', 'alpha', 'task', 'attempt-1', ['new-user'], False)

    def test_open_database_failure_is_pending_then_explicit_safe_retry(self):
        record = self.register('open-db', 'temporary_database')
        proof = self.proof(record)
        db = sqlite3.connect(record['spec']['path'])
        try:
            db.execute('SELECT * FROM fixture').fetchall()
            result = self.cleaner.cleanup('open-db', proof, self.guard())
            self.assertEqual(result['status'], 'pending_cleanup')
            self.assertEqual(result['result']['status'], 'failed_no_change')
            self.assertTrue(Path(record['spec']['path']).exists())
        finally:
            db.close()
        self.assertEqual(self.cleaner.cleanup('open-db', proof, self.guard(), retry=True)['status'], 'reclaimed')
