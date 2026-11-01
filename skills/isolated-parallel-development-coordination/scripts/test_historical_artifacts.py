"""Disposable Git R1/R2 history through the public preservation API; no live host."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from evidence import artifact, fingerprint, load_bound, pack, ref, write_atomic, preserved_read, verify_historical_copy
from test_core_runtime import CoreFixture


class HistoricalArtifacts(CoreFixture):
    def git(self, root, *args):
        result = subprocess.run(['git', '-c', 'core.longpaths=true', '-c', 'core.autocrlf=false',
            '-c', 'core.hooksPath='+os.devnull, '-c', 'commit.gpgsign=false',
            '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-C', str(root), *args],
            cwd=root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        return result.stdout.decode('utf-8').strip()

    def setUp(self):
        super().setUp()
        self.main = self.runtime.project_root
        self.git(self.main, 'init', '--initial-branch=main')
        (self.main/'seed.txt').write_bytes(b'base\n')
        self.git(self.main, 'add', '.')
        self.git(self.main, 'commit', '-m', 'fixture base')
        self.base = self.git(self.main, 'rev-parse', 'HEAD')
        self.tree = Path(self.state['runtime']['cleanup_root'])/'author'
        self.tree.parent.mkdir(parents=True)
        self.git(self.main, 'worktree', 'add', '-b', 'task/A', str(self.tree))
        self.dispatch(worktree=str(self.tree))
        self.state['project']['local_main_repo'] = str(self.main)
        self.store(); self.bind(); self.reload()
        self.runtime.register_resource('A', dict(id='A:worktree', kind='worktree', path=str(self.tree)),
            self.file('tree-authority.txt'), self.actor)
        self.runtime.register_resource('A', dict(id='A:agent', kind='agent', executor_id='worker-A', generation=1),
            self.file('agent-authority.txt'), self.actor)
        self.reload()
        self.state['tasks']['A']['resource_contract'] = 'new-task/1'
        self.entry = self.tree/'entry.md'
        self.old_bytes = b'R1 exact bytes\r\n\x00\xff'
        self.entry.write_bytes(self.old_bytes)
        self.git(self.tree, 'add', '.')
        self.git(self.tree, 'commit', '-m', 'fixture R1')
        self.old_head = self.git(self.tree, 'rev-parse', 'HEAD')
        self.old_ref = ref(self.entry)
        self.r1 = self.deliver(1)
        self.apply(op='rework', task_id='A', reason='same author normal review repair')
        self.entry.write_bytes(b'R2 repaired bytes\r\n')
        self.git(self.tree, 'add', '.')
        self.git(self.tree, 'commit', '-m', 'fixture R2')
        self.head = self.git(self.tree, 'rev-parse', 'HEAD')
        self.r2 = self.deliver(2)
        self.git(self.main, 'merge', '--ff-only', 'task/A')
        self.proof = self.json('main-proof.json', dict(project_id=self.runtime.project_id, task_id='A',
            source_head=self.head, main_head=self.head, main_ref='main', contains_source=True,
            scope='fixture_only', evidence=[self.file('main-observation.txt')]))
        self.state['active_human_item'] = None
        self.state['tasks']['A'].update(phase='completed', merge=dict(verified=True,
            head_sha=self.head, target_sha=self.base, proof=self.proof))
        self.store()
        self.handoff = self.file('handoff.txt')

    def deliver(self, revision):
        report = pack(dict(project_id=self.runtime.project_id, task_id='A', owner_agent_id='worker-A',
            delivery_revision=revision, worktree=str(self.tree), branch='task/A', summary='Fixture revision',
            source_root=str(self.tree), evidence=[self.file('test-'+str(revision)+'.txt')],
            self_test=dict(status='passed', command='fixture exact bytes assertion'),
            quality_review=dict(status='passed'), entry=dict(kind='file', target=str(self.entry)),
            steps=['Inspect exact bytes'], expected='Revision bytes',
            git=dict(head_sha=self.git(self.tree, 'rev-parse', 'HEAD'), target_sha=self.base)))
        reference = self.json('report-'+str(revision)+'.json', report)
        self.apply(op='bind-report', task_id='A', path=reference['path'], reference='immutable fixture revision')
        self.receive(dict(project_id=self.runtime.project_id, task_id='A', owner_agent_id='worker-A',
            channel='native_agent', kind='completed', delivery_revision=revision,
            event_id='A:'+str(revision)+':completed', report=reference))
        return reference

    def preserve(self, **options):
        return self.runtime.preserve_task('A', self.root/'preserved', self.handoff, self.actor,
            scope='fixture_only', **options)

    def test_first_preservation_keeps_old_and_new_bytes_and_original_records(self):
        before = {p:Path(p).read_bytes() for p in (self.entry, self.r1['path'], self.r2['path'])}
        receipts = deepcopy(self.runtime._load()['receipts'])
        self.assertFalse(self.runtime._load()['tasks']['A'].get('preservation'))
        manifest = load_bound(self.preserve())
        old = next(row for row in manifest['files'] if row['original']==self.old_ref)
        self.assertEqual(artifact(old).read_bytes(), self.old_bytes)
        self.assertEqual(old['git_source']['commit'], self.old_head)
        self.assertEqual(old['git_source']['relative'], 'entry.md')
        self.assertEqual(old['git_source']['report'], self.r1)
        self.assertEqual(old['git_source']['receipt_id'], 'A:1:completed')
        self.assertEqual(Path(old['git_source']['repository']), self.main)
        self.assertTrue(any(row['original']==ref(self.entry) for row in manifest['files']))
        self.assertEqual({p:Path(p).read_bytes() for p in before}, before)
        self.assertEqual(self.runtime._load()['receipts'], receipts)
        with preserved_read(self.runtime._load(), 'A'):
            self.assertEqual(artifact(self.old_ref).read_bytes(), self.old_bytes)
        self.assertFalse(any(row['original']['path'].endswith('seed.txt') for row in manifest['files']))

    def operation(self, identity='keep-original'):
        state = self.runtime._load()
        state['runtime']['operations'][identity] = dict(operation='continuation-effect',
            task_id='A', status='unknown')
        write_atomic(self.runtime.path, state)

    def test_same_operation_partial_resume_and_repeat_verify_exact_copies(self):
        self.operation()
        original_write = write_atomic
        def fail_manifest(path, value):
            if path.name=='preservation.json': raise OSError('fixture interrupted after exact copies')
            return original_write(path, value)
        with patch('resource_lifecycle.write_atomic', side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, 'fixture interrupted'):
                self.preserve(operation_id='keep-original')
        before = {p.name:p.read_bytes() for p in (self.root/'preserved').iterdir()}
        self.assertFalse(self.runtime._load()['tasks']['A'].get('preservation'))
        first = self.preserve(operation_id='keep-original')
        again = self.preserve(operation_id='keep-original')
        self.assertEqual(first, again)
        for name, data in before.items(): self.assertEqual((self.root/'preserved'/name).read_bytes(), data)
        self.assertEqual(self.runtime._load()['runtime']['operations']['keep-original']['status'], 'unknown')
        self.operation('different-operation')
        with self.assertRaisesRegex(ValueError, 'operation|resume binding'):
            self.preserve(operation_id='different-operation')
        old = next(row for row in load_bound(first)['files'] if row['original']==self.old_ref)
        Path(old['path']).write_bytes(b'tampered partial copy')
        with self.assertRaises(ValueError): self.preserve(operation_id='keep-original')

    def alter_old_report(self, mutate):
        report = load_bound(self.r1); mutate(report)
        reference = self.json('invalid-old-report.json', report)
        state = self.runtime._load(); receipt = state['receipts']['A:1:completed']
        receipt['payload']['report'] = reference
        receipt['fingerprint'] = fingerprint(receipt['payload'])
        write_atomic(self.runtime.path, state)

    def test_wrong_report_task_project_author_root_commit_path_and_sha_are_rejected(self):
        baseline = self.runtime.path.read_bytes()
        mutations = [lambda r:r.update(task_id='B'), lambda r:r.update(project_id='other'),
            lambda r:r.update(owner_agent_id='other'), lambda r:r['source'].update(root=str(self.main)),
            lambda r:r['git'].update(head_sha='f'*40), lambda r:r['git'].update(head_sha=self.base),
            lambda r:r['entry']['artifact'].update(path=str(self.tree/'seed.txt')),
            lambda r:r['entry']['artifact'].update(sha256='0'*64)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.runtime.path.write_bytes(baseline)
                self.alter_old_report(mutate)
                before = self.runtime.path.read_bytes()
                with self.assertRaises(ValueError): self.preserve()
                self.assertEqual(self.runtime.path.read_bytes(), before)
                self.assertFalse(self.runtime._load()['tasks']['A'].get('preservation'))
                # Failed destinations are owned test data, never a production resume.
                import shutil
                if (self.root/'preserved').exists(): shutil.rmtree(self.root/'preserved')

    def test_arbitrary_handoff_cannot_authorize_git_history(self):
        self.handoff = self.json('untrusted-handoff.json', dict(evidence=[self.old_ref],
            source=load_bound(self.r1)['source'], git=dict(head_sha=self.old_head)))
        with self.assertRaises(ValueError): self.preserve()

    def test_non_git_evidence_missing_still_rejected(self):
        evidence = load_bound(self.r1)['evidence'][0]
        Path(evidence['path']).unlink()
        with self.assertRaisesRegex(ValueError, 'artifact missing'): self.preserve()

    def test_untracked_file_is_not_recovered_from_source_inventory(self):
        missing = dict(path=str(self.tree/'not-tracked.txt'), sha256=self.old_ref['sha256'])
        def untracked_inventory(report):
            report['entry']['artifact'] = missing
            report['source']['files'].append(dict(relative='not-tracked.txt', sha256=missing['sha256']))
            report['source']['digest'] = fingerprint({k:v for k,v in report['source'].items() if k != 'digest'})
            report['self_test']['source_digest'] = report['source']['digest']
        self.alter_old_report(untracked_inventory)
        with self.assertRaisesRegex(ValueError, 'not a tracked regular blob'): self.preserve()

    def test_foreign_repository_and_unmerged_history_rejected(self):
        self.git(self.main, 'update-ref', 'refs/heads/main', self.base)
        with self.assertRaises(ValueError): self.preserve()

    def test_foreign_repository_with_identical_objects_is_rejected(self):
        foreign = self.root/'foreign.git'
        self.git(self.main, 'clone', '--bare', str(self.main), str(foreign))
        state = self.runtime._load(); state['project']['local_main_repo'] = str(foreign)
        write_atomic(self.runtime.path, state)
        with self.assertRaisesRegex(ValueError, 'repository differs'): self.preserve()

    def test_git_source_metadata_and_saved_bytes_are_not_authority(self):
        manifest = load_bound(self.preserve())
        old = next(row for row in manifest['files'] if row['original']==self.old_ref)
        state = self.runtime._load()
        for key, value in (('task_id','B'), ('commit',self.head), ('relative','seed.txt'),
                           ('receipt_id','B:1:completed'), ('repository',str(self.tree)), ('blob','0'*40)):
            with self.subTest(key=key):
                bad = deepcopy(old); bad['git_source'][key] = value
                with self.assertRaises(ValueError): verify_historical_copy(state, 'A', bad)
        Path(old['path']).write_bytes(b'changed saved bytes')
        with self.assertRaises(ValueError): verify_historical_copy(state, 'A', old)

    def test_historical_read_after_confirmed_owned_worktree_cleanup(self):
        manifest = load_bound(self.preserve())
        self.git(self.main, 'worktree', 'remove', str(self.tree))
        state = self.runtime._load(); resource = state['runtime']['resources']['A:worktree']
        resource.update(cleanup_status='cleaned', cleanup_result=dict(evidence=self.file('removed.txt')))
        write_atomic(self.runtime.path, state)
        with preserved_read(state, 'A'):
            self.assertEqual(artifact(self.old_ref).read_bytes(), self.old_bytes)
        old = next(row for row in manifest['files'] if row['original']==self.old_ref)
        verify_historical_copy(state, 'A', old)
        Path(resource['cleanup_result']['evidence']['path']).write_bytes(b'tampered cleanup proof')
        with preserved_read(state, 'A'):
            with self.assertRaisesRegex(ValueError, 'hash mismatch'): artifact(self.old_ref)

    def test_matching_old_bytes_use_normal_copy_and_current_source_changes_still_fail(self):
        self.alter_old_report(lambda r:r['entry'].update(artifact=ref(self.tree/'seed.txt')))
        manifest = load_bound(self.preserve())
        seed = next(row for row in manifest['files'] if row['original']==ref(self.tree/'seed.txt'))
        self.assertNotIn('git_source', seed)
        self.entry.write_bytes(self.old_bytes)
        with self.assertRaisesRegex(ValueError, 'source inventory changed'):
            self.preserve(operation_id='keep-original')

    def test_active_shared_and_protected_executor_still_rejected(self):
        baseline = self.runtime.path.read_bytes()
        for change in (dict(pending_work=True), dict(shared=True), dict(protected=True)):
            with self.subTest(change=change):
                self.runtime.path.write_bytes(baseline)
                state = self.runtime._load(); state['runtime']['executors']['worker-A'].update(change)
                write_atomic(self.runtime.path, state)
                with self.assertRaises(ValueError): self.preserve()
                self.assertFalse(self.runtime._load()['tasks']['A'].get('preservation'))
                import shutil
                if (self.root/'preserved').exists(): shutil.rmtree(self.root/'preserved')

    def legacy_source(self, report, extras=()):
        source = report['source']
        source['extra'] = [dict(path=str(self.tree/row['relative']), sha256=row['sha256'])
            for row in source['files']] + list(extras)
        for row in source['files']: row['sha256'] = None
        source['digest'] = fingerprint({k:v for k,v in source.items() if k != 'digest'})
        report['self_test']['source_digest'] = source['digest']

    def test_null_supplements_keep_git_identity_without_hiding_explicit_entry(self):
        def source_first(report):
            self.legacy_source(report)
            source = report.pop('source'); rest = dict(report)
            report.clear(); report.update(source=source, **rest)
        self.alter_old_report(source_first)
        manifest = load_bound(self.preserve())
        seed = ref(self.tree/'seed.txt')
        self.assertFalse(any(row['original']==seed for row in manifest['files']))
        for original in (seed, self.old_ref):
            identities = [row for row in manifest['git_sources'] if row['original']==original]
            self.assertEqual(len(identities), 1)
            self.assertEqual(identities[0]['git_source']['commit'], self.old_head)
        old = next(row for row in manifest['files'] if row['original']==self.old_ref)
        self.assertEqual(artifact(old).read_bytes(), self.old_bytes)

    def test_null_supplements_with_wrong_sha_or_missing_path_still_fail(self):
        baseline = self.runtime.path.read_bytes()
        for extra in (dict(path=str(self.tree/'seed.txt'), sha256='0'*64),
                      dict(path=str(self.tree/'not-tracked.txt'), sha256=self.old_ref['sha256'])):
            with self.subTest(extra=extra):
                self.runtime.path.write_bytes(baseline)
                self.alter_old_report(lambda r:self.legacy_source(r, [extra]))
                with self.assertRaises(ValueError): self.preserve()
                import shutil
                if (self.root/'preserved').exists(): shutil.rmtree(self.root/'preserved')

    def test_untracked_supplement_requires_live_exact_bytes_and_is_copied(self):
        extra = self.file('necessary-ignored.txt')
        self.alter_old_report(lambda r:self.legacy_source(r, [extra]))
        manifest = load_bound(self.preserve())
        self.assertTrue(any(row['original']==extra for row in manifest['files']))
        self.assertFalse(any(row['original']==extra for row in manifest['git_sources']))
