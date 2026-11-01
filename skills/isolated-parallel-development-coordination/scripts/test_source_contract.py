"""Windows source-contract interoperability; disposable files and reviewer fixtures."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import unittest

from evidence import artifact, digest, fingerprint, io_path, ref, report_for, snapshot, verify_source
from test_coordination import LedgerFixture
import test_real_use_closeout


def legacy_snapshot(root, kind, extra=()):
    """Frozen dfb7bf8 public contract: ordinary-path rows plus supplied SHA refs."""
    root = Path(root).resolve()
    if kind == 'git':
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                 '--exclude-standard'], check=True, capture_output=True, timeout=20)
        paths = sorted(set(p.decode() for p in result.stdout.split(b'\0') if p))
    else:
        paths = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    value = dict(root=str(root), kind=kind,
        files=[dict(relative=p, sha256=digest(root/p) if (root/p).is_file() else None) for p in paths],
        extra=[ref(artifact(item)) for item in extra])
    return dict(value, digest=fingerprint(value))


@unittest.skipUnless(os.name == 'nt', 'Actual Windows ordinary/extended path contract')
class SourceContractTests(LedgerFixture):
    def source(self, kind):
        root = self.root/kind; root.mkdir()
        plain = root/'plain.txt'; plain.write_text('plain original', encoding='utf-8')
        hidden = root/('p'*80)/('q'*80)/'bound.txt'
        io_path(hidden.parent).mkdir(parents=True)
        io_path(hidden).write_text('long original', encoding='utf-8')
        self.assertFalse(hidden.is_file()); self.assertTrue(io_path(hidden).is_file())
        if kind == 'git':
            for args in (['init', '-q'], ['add', '.']):
                subprocess.run(['git', '-c', 'core.longpaths=true', *args], cwd=root,
                               check=True, capture_output=True, timeout=20)
        return root, plain, hidden

    def full(self, root, kind, plain, hidden):
        value = dict(root=str(root.resolve()), kind=kind, extra=[], files=sorted([
            dict(relative=p.relative_to(root).as_posix(), sha256=digest(p)) for p in (plain, hidden)],
            key=lambda row: row['relative']))
        return dict(value, digest=fingerprint(value))

    def test_new_source_remains_readable_by_frozen_legacy_contract(self):
        for kind in ('git', 'directory'):
            with self.subTest(kind=kind):
                root, plain, hidden = self.source(kind)
                current = snapshot(root)
                self.assertEqual(current, legacy_snapshot(root, kind, current['extra']))
                self.assertIn(ref(hidden), current['extra'])
                verify_source(current)

    def test_old_supplemented_and_complete_sources_verify_without_mutation(self):
        for kind in ('git', 'directory'):
            root, plain, hidden = self.source(kind)
            for value in (legacy_snapshot(root, kind, [ref(hidden)]), self.full(root, kind, plain, hidden)):
                before = deepcopy(value)
                verify_source(value)
                self.assertEqual(value, before)

    def test_blind_null_or_omission_without_actual_sha_is_not_accepted(self):
        for kind in ('git', 'directory'):
            root, _, _ = self.source(kind)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                verify_source(legacy_snapshot(root, kind))

    def test_real_changes_and_ordinary_or_long_additions_deletions_are_rejected(self):
        for kind in ('git', 'directory'):
            root, plain, hidden = self.source(kind)
            values = [legacy_snapshot(root, kind, [ref(hidden)]), self.full(root, kind, plain, hidden)]
            for target in (plain, hidden):
                original = io_path(target).read_bytes()
                for action in ('modify', 'delete'):
                    if action == 'modify': io_path(target).write_bytes(b'changed')
                    else: io_path(target).unlink()
                    for value in values:
                        with self.subTest(kind=kind, target=target.name, action=action), self.assertRaises(ValueError):
                            verify_source(value)
                    io_path(target).write_bytes(original)
                verify_source(values[0]); verify_source(values[1])
            for target in (root/'added.txt', hidden.with_name('added.txt')):
                io_path(target).write_bytes(b'added')
                for value in values:
                    with self.subTest(kind=kind, added=str(target)), self.assertRaises(ValueError):
                        verify_source(value)
                io_path(target).unlink()

    def test_wrong_actual_sha_and_changed_source_digest_are_rejected(self):
        root, _, hidden = self.source('git')
        value = legacy_snapshot(root, 'git', [ref(hidden)])
        bad_digest = dict(value, digest='0'*64)
        bad_extra = deepcopy(value); bad_extra['extra'][0]['sha256'] = '0'*64
        bad_extra['digest'] = fingerprint({k:v for k,v in bad_extra.items() if k != 'digest'})
        for bad in (bad_digest, bad_extra):
            with self.assertRaises(ValueError): verify_source(bad)

    def test_long_untracked_file_is_bound_and_keeps_legacy_reader_compatibility(self):
        root, _, hidden = self.source('git')
        added = hidden.with_name('untracked.txt'); io_path(added).write_bytes(b'untracked source')
        current = snapshot(root)
        self.assertIn(ref(added), current['extra'])
        self.assertEqual(current, legacy_snapshot(root, 'git', current['extra']))
        verify_source(current)


@unittest.skipUnless(os.name == 'nt', 'Actual Windows long-path quality gate')
class SourceQualityTests(unittest.TestCase):
    def test_new_packed_report_reaches_independent_quality_with_long_source(self):
        f = test_real_use_closeout.RealUseTests('test_quality_attaches_without_mutating_author_report_and_duplicates_do_not_write')
        f.setUp(); self.addCleanup(f.doCleanups)
        hidden = f.root/'A'/('p'*80)/('q'*80)/'bound.txt'
        io_path(hidden.parent).mkdir(parents=True)
        io_path(hidden).write_bytes(b'actual owned long file')
        packet = f.deliver(); original = Path(packet['report']['path']).read_bytes()
        f.call('quality', review=f.review())
        report = report_for(f.runtime._load(), 'A')
        self.assertIn(ref(hidden), report['source']['extra'])
        self.assertEqual(Path(packet['report']['path']).read_bytes(), original)
        io_path(hidden).write_bytes(b'changed after review')
        with self.assertRaises(ValueError): report_for(f.runtime._load(), 'A')
