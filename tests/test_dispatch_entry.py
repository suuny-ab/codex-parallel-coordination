"""Ordinary isolated file copies only; no host plugin installation or native actor."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import package


@unittest.skipUnless(shutil.which('node'), 'Node required for reviewed entry loader')
class InstalledEntry(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dispatch-entry-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.candidate, self.installed = self.root / 'candidate', self.root / 'installed-copy'
        source = Path('\\\\?\\' + str(ROOT)) if os.name == 'nt' else ROOT
        files = package.payload(source)
        for name, data in files.items():
            target = self.candidate / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        shutil.copytree(self.candidate, self.installed)
        self.binding = dict(source_root=str(self.installed), source_head='reviewed-fixture-only',
                            files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
        # Removing the candidate proves loading does not depend on its lifetime.
        assert self.candidate != self.root and self.candidate.is_relative_to(self.root)
        shutil.rmtree(self.candidate)

    def load_entry(self):
        entry = self.installed / 'skills/isolated-parallel-development-coordination/scripts/dispatch_entry.mjs'
        program = """
const {loadInstalledDispatch} = await import(ENTRY);
try {
  const module = await loadInstalledDispatch(BINDING);
  console.log(JSON.stringify({status:'loaded',name:module.DispatchSession.name}));
} catch (error) { console.log(JSON.stringify({status:'rejected',reason:error.message})); }
""".replace('ENTRY', json.dumps(entry.as_uri())).replace('BINDING', json.dumps(self.binding))
        result = subprocess.run(['node', '--input-type=module', '-e', program], capture_output=True,
                                encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_installed_entry_survives_candidate_removal(self):
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.load_entry(), dict(status='loaded', name='DispatchSession'))

    def test_installed_bytes_must_match_reviewed_fingerprints(self):
        target = self.installed / 'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs'
        target.write_bytes(target.read_bytes() + b'\n// changed after review\n')
        result = self.load_entry()
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('fingerprint mismatch', result['reason'])

    def test_loader_cannot_fall_back_to_another_source_root(self):
        self.binding['source_root'] = str(self.root)
        self.assertIn('differs from bound installation', self.load_entry()['reason'])


if __name__ == '__main__':
    unittest.main()
