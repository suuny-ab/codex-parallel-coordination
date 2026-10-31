"""Resource completion-proof contract; CORE/native facts explicitly injected.

The actual CORE revision/acceptance verifier is covered by CORE and integration.
These tests exercise real owned files and quota persistence, without native calls.
"""
from pathlib import Path
import unittest
import test_cleanup as cleanup_fixture
import test_resource_additions as quota_fixture
from support import load, load_bound, ref, write_atomic


class ReadOnlyCompletionTests(unittest.TestCase):
    setUp = cleanup_fixture.CleanupTests.setUp
    file = cleanup_fixture.CleanupTests.file
    register = cleanup_fixture.CleanupTests.register
    quota = quota_fixture.AddedResourceTests.quota
    receipt = quota_fixture.AddedResourceTests.receipt

    @staticmethod
    def completed_guard(**changes):
        facts = quota_fixture.AddedResourceTests.completed_guard()({}, {})
        facts.update(task_kind='read_only', task_completion_verified=True,
                     task_completed_on_main=False, read_only_completion_verified=True)
        facts.update(changes)
        return lambda context, proof: facts

    def proof(self, record):
        # No fabricated merge/MR/CI proof for a read-only task.
        payload = {k: record['spec'][k] for k in ('resource_id', 'project_id', 'task_id', 'attempt_id')}
        payload['handoff'] = ref(self.handoff)
        return ref(self.file('readonly-proof-' + payload['resource_id'] + '.json', payload))

    def test_read_only_completion_reclaims_owned_directory_without_fake_main(self):
        record = self.register('readonly-owned')
        before = self.core_state.read_bytes()
        proof = self.proof(record)
        self.assertNotIn('merge', load(proof['path']))
        result = self.cleaner.cleanup('readonly-owned', proof, self.completed_guard())
        self.assertEqual(result['status'], 'reclaimed')
        self.assertFalse(Path(record['spec']['path']).exists())
        self.assertFalse(result['task_completed_changed'])
        self.assertTrue(self.cleaner.cleanup('readonly-owned', proof)['duplicate'])
        self.assertEqual(self.core_state.read_bytes(), before)

    def test_read_only_completion_releases_quota_and_drains_other_project_fifo(self):
        first = self.controller.capacity_acquire(self.quota('readonly-A'))
        self.assertEqual(self.controller.capacity_acquire(self.quota('readonly-B', 'beta'))['status'], 'queued')
        proof = self.receipt(first)  # Injected confirmed native close, never merely completed.
        result = self.controller.capacity_release('readonly-A', first['permit_id'], proof, self.completed_guard())
        self.assertEqual(result['newly_granted'], ['readonly-B'])
        self.assertFalse(result['process_memory_or_acceptance_storage_released'])
        self.assertFalse(result['native_operation_executed_here'])
        self.assertTrue(self.controller.capacity_release('readonly-A', first['permit_id'], proof)['duplicate'])

    def test_read_only_missing_completion_or_common_guards_cannot_remove_or_release(self):
        invalid = ({'read_only_completion_verified': False}, {'task_completion_verified': False},
                   {'task_completed_on_main': True}, {'verified': False}, {'preservation_valid': False},
                   {'no_active_work': False}, {'no_active_descendants': False}, {'no_release_dependencies': False},
                   {'exclusive_task_resource': False}, {'cleanup_authorized': False}, {'protected': True})
        for index, changes in enumerate(invalid):
            for endpoint in ('file', 'quota'):
                with self.subTest(changes=changes, endpoint=endpoint):
                    key = 'held-' + str(index) + '-' + endpoint
                    if endpoint == 'file':
                        record = self.register(key)
                        proof = self.proof(record)
                        before = self.control.path.read_bytes()
                        with self.assertRaises(ValueError):
                            self.cleaner.cleanup(key, proof, self.completed_guard(**changes))
                        self.assertTrue(Path(record['spec']['path']).exists())
                        self.assertEqual(self.control.path.read_bytes(), before)
                    else:
                        # A fresh store avoids one unexpected release masking later guard cases.
                        from resources import Controller
                        control = Controller(self.root / (key + '/resources.json'))
                        control.initialize(self.file(key + '-policy.json', self.policy))
                        actor = control.capacity_acquire(self.quota(key))
                        before = control.path.read_bytes()
                        with self.assertRaises(ValueError):
                            control.capacity_release(key, actor['permit_id'], self.receipt(actor),
                                                     self.completed_guard(**changes))
                        self.assertEqual(control.path.read_bytes(), before)

    def test_code_cannot_borrow_read_only_completion_flag_and_legacy_main_still_works(self):
        for key, kind in (('explicit-code', 'code'), ('legacy-code', None)):
            record = self.register(key)
            proof = self.proof(record)
            bad = self.completed_guard(task_kind=kind)
            with self.assertRaises(ValueError):
                self.cleaner.cleanup(key, proof, bad)
            self.assertTrue(Path(record['spec']['path']).exists())
            facts = quota_fixture.AddedResourceTests.completed_guard()({}, {})
            if kind is not None:
                facts.update(task_kind=kind, task_completion_verified=True, read_only_completion_verified=False)
            self.assertEqual(self.cleaner.cleanup(key, proof, lambda *args: facts)['status'], 'reclaimed')

    def test_unknown_kind_is_not_a_new_cleanup_authorization(self):
        for index, kind in enumerate(('review', 'exploration', '', True)):
            with self.subTest(kind=kind):
                key = 'unknown-kind-' + str(index)
                record = self.register(key)
                with self.assertRaises(ValueError):
                    self.cleaner.cleanup(key, self.proof(record),
                                         self.completed_guard(task_kind=kind, task_completed_on_main=True))
                self.assertTrue(Path(record['spec']['path']).exists())

    def test_read_only_completed_unknown_or_untrusted_receipt_does_not_free_native_quota(self):
        actor = self.controller.capacity_acquire(self.quota('readonly-native'))
        before = self.controller.path.read_bytes()
        for outcome in ('completed', 'idle', 'unknown', 'failed', 'accepted'):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                self.controller.capacity_release('readonly-native', actor['permit_id'], self.receipt(actor, outcome),
                                                 self.completed_guard())
        with self.assertRaises(ValueError):
            self.controller.capacity_release('readonly-native', actor['permit_id'], self.receipt(actor), lambda *args: True)
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_read_only_changed_release_receipt_keeps_quota(self):
        actor = self.controller.capacity_acquire(self.quota('readonly-evidence'))
        proof = self.receipt(actor)
        Path(proof['path']).write_text('{}', encoding='utf-8')
        before = self.controller.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.capacity_release('readonly-evidence', actor['permit_id'], proof, self.completed_guard())
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_missing_or_inconsistent_new_completion_fields_retain_quota(self):
        actor = self.controller.capacity_acquire(self.quota('missing-completion'))
        proof = self.receipt(actor)
        valid = self.completed_guard()({}, {})
        cases = []
        for key in ('task_kind', 'task_completion_verified', 'read_only_completion_verified', 'task_completed_on_main'):
            missing = dict(valid)
            del missing[key]
            cases.append(missing)
        code = dict(valid, task_kind='code', task_completed_on_main=True, read_only_completion_verified=False)
        missing = dict(code)
        del missing['task_completion_verified']
        cases.extend((missing, dict(code, task_completion_verified=False),
                      dict(code, read_only_completion_verified=True), dict(valid, task_completion_verified='true')))
        before = self.controller.path.read_bytes()
        for facts in cases:
            with self.subTest(facts=facts), self.assertRaises(ValueError):
                self.controller.capacity_release('missing-completion', actor['permit_id'], proof, lambda *args: facts)
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_read_only_label_in_receipt_cannot_replace_canonical_completion(self):
        actor = self.controller.capacity_acquire(self.quota('readonly-label'))
        proof = self.receipt(actor, task_kind='read_only', task_completion_verified=True,
                             read_only_completion_verified=True, task_completed=True)
        before = self.controller.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.capacity_release('readonly-label', actor['permit_id'], proof,
                quota_fixture.AddedResourceTests.completed_guard(task_completed_on_main=False))
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_long_current_completion_evidence_roundtrips_atomically(self):
        parent = self.root
        while len(str(parent / 'current-delivery-proof.json')) <= 300:
            parent /= 'completion-' + 'x' * 60
        path = parent / 'current-delivery-proof.json'
        self.assertGreater(len(str(path)), 300)
        write_atomic(path, {'revision': 4, 'verified': True})
        original = ref(path)
        self.assertEqual(original['path'], str(path.resolve()))
        self.assertEqual(load_bound(original), {'revision': 4, 'verified': True})
        write_atomic(path, {'revision': 5, 'verified': True})
        self.assertEqual(load(path)['revision'], 5)
        self.assertNotEqual(ref(path)['sha256'], original['sha256'])
        with self.assertRaises(ValueError):
            load_bound(original)

    def test_atomic_replace_coexists_with_a_readonly_snapshot_handle(self):
        import json
        import support
        path = self.file('concurrent-snapshot.json', {'revision': 1})
        # Baseline readers used Path.open; fallback permits exact old-source replay.
        open_read = getattr(support, '_open_read', lambda value: Path(value).open('rb'))
        with open_read(path) as reader:
            write_atomic(path, {'revision': 2})
            self.assertEqual(json.loads(reader.read()), {'revision': 1})
        self.assertEqual(load(path), {'revision': 2})
        # An unrelated restrictive reader still refuses replacement; never force/retry it.
        with path.open('rb') as external_reader:
            with self.assertRaises(PermissionError):
                write_atomic(path, {'revision': 3})
            self.assertEqual(json.loads(external_reader.read()), {'revision': 2})
        self.assertEqual(load(path), {'revision': 2})

    def test_uncertain_native_replace_keeps_fsynced_snapshot_without_retry(self):
        import ctypes
        from unittest.mock import patch
        for code in (1176, 1177):
            with self.subTest(winerror=code):
                directory = self.root / ('uncertain-' + str(code))
                path = directory / 'current.json'
                write_atomic(path, {'revision': 1})
                with patch('support._replace_atomic', side_effect=ctypes.WinError(code)) as replace:
                    with self.assertRaises(OSError) as failure:
                        write_atomic(path, {'revision': 2})
                self.assertIn('preserved snapshot:', str(failure.exception))
                self.assertEqual(replace.call_count, 1)
                retained = list(directory.glob('.res-*.tmp'))
                self.assertEqual(len(retained), 1)
                self.assertEqual(load(retained[0]), {'revision': 2})
                self.assertEqual(load(path), {'revision': 1})
