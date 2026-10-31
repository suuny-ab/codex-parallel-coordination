"""Win32 long generated proof paths through the real cleanup/release entrypoints."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from evidence import artifact, load_bound, load, ref, write_atomic
import test_cleanup_runtime as cleanup_tests
import coordination_v1


@unittest.skipUnless(os.name == 'nt', 'Win32 generated artifact path regression')
class GeneratedArtifactPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        parent = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(self.base.resolve().is_relative_to(parent))
        self.assertNotEqual(self.base.resolve(), parent)
        self.temp.name = '\\\\?\\' + str(self.base)

    def fixture(self, parent_chars):
        parent = self.base / ('p' * max(1, parent_chars - len(str(self.base)) - 1))
        parent.mkdir()
        fixture = cleanup_tests.CleanupTests('test_merge_then_preserve_then_real_isolated_database_environment_cleanup')
        with patch.object(tempfile, 'tempdir', str(parent)):
            fixture.setUp()
        self.addCleanup(self.remove_generated_then_fixture, fixture)
        return fixture

    def remove_generated_then_fixture(self, fixture):
        # These two exact owned artifact directories may contain >MAX_PATH names.
        for name in ('cleanup-proofs', 'receipts'):
            directory = Path('\\\\?\\' + str(fixture.runtime.directory / name))
            if directory.exists():
                for path in directory.iterdir():
                    path.unlink()
        fixture.doCleanups()

    def test_long_cleanup_proof_full_hash_verification_cleanup_and_retry(self):
        fixture = self.fixture(113)
        proof = fixture.close_fixture()
        path = fixture.managed('long-proof')
        callbacks = []

        def remove(resource):
            bound = resource['cleanup_proof']
            self.assertGreaterEqual(len(str(fixture.runtime.directory / 'cleanup-proofs' / Path(bound['path']).name)), 260)
            self.assertEqual(len(Path(bound['path']).stem), 64)
            self.assertEqual(load_bound(bound)['resource_id'], 'long-proof')
            self.assertTrue(fixture.runtime.verify_cleanup_receipt(dict(resource, resource_id=resource['id']), bound)['verified'])
            callbacks.append(resource['id'])
            return fixture.remove(resource)

        result = fixture.runtime.cleanup_task('A', 'long-proof', proof, remove, fixture.actor, authorized=True)
        self.assertEqual(result['status'], 'cleanup_completed')
        self.assertFalse(path.exists())
        self.assertEqual(callbacks, ['long-proof'])
        again = fixture.runtime.cleanup_task('A', 'long-proof', proof,
            lambda resource: self.fail('duplicate effect'), fixture.actor, authorized=True)
        self.assertEqual(again, result)

    def test_long_release_receipt_is_readable_verified_and_idempotent(self):
        fixture = self.fixture(125)
        fixture.close_fixture()
        receipt = fixture.runtime.release_receipt('fixture-agent-close', fixture.actor)
        normal_path = fixture.runtime.directory / 'receipts' / Path(receipt['path']).name
        self.assertGreaterEqual(len(str(normal_path)), 260)
        self.assertEqual(len(normal_path.stem), 64)
        context = load_bound(receipt)
        self.assertTrue(fixture.runtime.verify_release_receipt(context, receipt)['verified'])
        before = fixture.runtime.path.read_bytes()
        self.assertEqual(fixture.runtime.release_receipt('fixture-agent-close', fixture.actor), receipt)
        self.assertEqual(before, fixture.runtime.path.read_bytes())

    def test_long_atomic_failure_keeps_pending_and_requires_effect_reconciliation(self):
        fixture = self.fixture(113)
        proof = fixture.close_fixture()
        path = fixture.managed('retain-on-write-failure')
        original = coordination_v1.os.replace

        def refused(source, destination):
            if 'cleanup-proofs' in str(destination):
                raise OSError('fixture atomic replace refused')
            return original(source, destination)

        with patch.object(coordination_v1.os, 'replace', refused):
            result = fixture.runtime.cleanup_task('A', 'failed-long-proof', proof,
                lambda resource: self.fail('proof not persisted'), fixture.actor, authorized=True)
        self.assertEqual(result['status'], 'pending_cleanup')
        self.assertTrue(path.exists())
        self.assertEqual(fixture.runtime._load()['tasks']['A']['phase'], 'completed')
        blocked = fixture.runtime.cleanup_task('A', 'cannot-blind-retry', proof,
            lambda resource: self.fail('uncertain proof write must be reconciled'), fixture.actor, authorized=True)
        self.assertEqual(blocked['status'], 'pending_cleanup')
        observation = fixture.json('retained-after-write-failure.json', dict(resource_id='retain-on-write-failure',
            status='retained', evidence=[fixture.file('physical-retained-observation')]))
        fixture.runtime.reconcile_cleanup('A', 'retain-on-write-failure', observation, fixture.actor)
        final = fixture.runtime.cleanup_task('A', 'after-real-observation', proof,
            fixture.remove, fixture.actor, authorized=True)
        self.assertEqual(final['status'], 'cleanup_completed')
        self.assertFalse(path.exists())

    def test_public_atomic_replace_load_ref_artifact_short_263_and_310(self):
        for length in (None, 263, 310):
            with self.subTest(chars=length):
                parent = self.base / str(length)
                if length:
                    parent = parent / ('x' * (length - len(str(parent)) - 71))
                Path('\\\\?\\' + str(parent)).mkdir(parents=True)
                target = parent / ('a' * 64 + '.json')
                if length:
                    self.assertEqual(len(str(target)), length)
                write_atomic(target, dict(revision=1))
                write_atomic(target, dict(revision=2))
                self.assertEqual(load(target), dict(revision=2))
                bound = ref(target)
                self.assertEqual(load_bound(bound), dict(revision=2))
                self.assertEqual(artifact(bound).read_bytes(), Path(bound['path']).read_bytes())

    def test_extended_io_keeps_absolute_and_hash_validation(self):
        with self.assertRaises(ValueError):
            artifact(dict(path='relative.json', sha256='0' * 64))
        with self.assertRaises(ValueError):
            load_bound(dict(path='relative.json', sha256='0' * 64))
        target = self.base / 'guard.json'
        write_atomic(target, dict(revision=1))
        bound = ref(target)
        with self.assertRaises(ValueError):
            artifact(dict(bound, sha256='0' * 64))
        with self.assertRaises(ValueError):
            load_bound(dict(bound, sha256='0' * 64))

