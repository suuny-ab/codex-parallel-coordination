"""Public preservation on disposable tasks; host/main observations are fixtures."""
from pathlib import Path
from unittest.mock import patch

import test_core_runtime as core_tests
import test_native_control as native_tests
from coordination import new_state
from evidence import artifact, load_bound, ref
from resource_lifecycle import REFERENCE_FIELDS


class PreservationRefs(core_tests.CoreFixture):
    ready = native_tests.NativeControlTests.ready

    def setUp(self):
        super().setUp()
        proof, self.tree = self.ready()
        self.merge_proof = load_bound(proof)['merge_verification']

    def preserve(self, payload, destination=None):
        handoff = self.json('handoff-data.json', payload)
        return self.runtime.preserve_task('A', destination or self.root/'preserved', handoff,
            self.actor, scope='fixture_only', merge_verification=self.merge_proof)

    def test_relative_inventory_and_absolute_text_are_data(self):
        payload = dict(file_inventory=[dict(path='README.md', sha256='0'*64),
            [dict(path='src/feature.py', sha256='1'*64)]],
            captured_failure=[dict(path=str(self.root/'expired-data.txt'),sha256='0'*64)],
            ordinary_absolute_text=str(self.root/'not-an-evidence-file.txt'))
        manifest = load_bound(self.preserve(payload))
        handoff = next(row for row in manifest['files'] if row['original']['path'].endswith('handoff-data.json'))
        self.assertEqual(load_bound(handoff), payload)
        self.assertFalse(any(row['original']['path']=='README.md' for row in manifest['files']))

    def test_mixed_nested_and_duplicate_references_keep_all_exact_bytes(self):
        leaf = self.file('nested.txt', 'Required non-Git evidence')
        nested = self.json('nested.json', dict(evidence=[leaf, leaf],
            inventory=[dict(path='README.md', sha256='0'*64, evidence=[leaf])]))
        # References may have metadata containing additional genuine references.
        metadata = self.file('metadata.txt')
        manifest = load_bound(self.preserve(dict(evidence=[dict(nested, evidence=[metadata]), nested])))
        for original in (leaf, nested, metadata):
            rows = [row for row in manifest['files'] if row['original']==original]
            self.assertEqual(len(rows), 1)
            self.assertEqual(artifact(rows[0]).read_bytes(), artifact(original).read_bytes())

    def test_missing_or_tampered_nested_evidence_blocks_without_deletion(self):
        for mode in ('missing', 'tampered'):
            with self.subTest(mode=mode):
                leaf = self.file(mode+'.txt', 'Original exact bytes')
                nested = self.json(mode+'.json', dict(evidence=[leaf]))
                if mode=='missing':
                    Path(leaf['path']).unlink()
                else:
                    Path(leaf['path']).write_text('changed', encoding='utf-8')
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'artifact missing|hash mismatch'):
                    self.preserve(dict(inventory=[dict(path='README.md', sha256='0'*64)],
                        nested=[dict(evidence=[nested])]), self.root/mode)
                self.assertEqual(self.runtime.path.read_bytes(), before)
                self.assertTrue(self.tree.exists())
                self.assertFalse(self.runtime._load()['tasks']['A'].get('preservation'))
                self.assertFalse(any(v.get('operation')=='close' for v in
                    self.runtime._load()['runtime']['operations'].values()))

    def test_relative_reference_in_declared_evidence_slot_is_still_invalid(self):
        for field in ('evidence', 'report', 'proof', 'authority', 'probe', 'raw_journals'):
            with self.subTest(field=field):
                invalid = dict(path='missing.txt', sha256='0'*64)
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'artifact missing or not absolute'):
                    self.preserve({field: [invalid]}, self.root/('invalid-'+field))
                self.assertEqual(self.runtime.path.read_bytes(), before)

    def test_reference_inside_ordinary_inventory_is_not_skipped(self):
        missing = dict(path=str(self.root/'missing.txt'), sha256='0'*64)
        with self.assertRaisesRegex(ValueError, 'artifact missing'):
            self.preserve(dict(inventory=[dict(path='README.md',sha256='0'*64,
                nested=[dict(evidence=[missing])])]))

    def test_destination_inside_resource_or_outside_evidence_copy_is_rejected(self):
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'outlive resources|inside resource'):
            self.preserve(dict(note='Fixture'), self.tree/'unsafe-preservation')
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertFalse((self.tree/'unsafe-preservation').exists())

    def test_active_executor_does_not_gain_cleanup_permission(self):
        self.observed(execution='running', pending_work=True)
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'executor|active|running'):
            self.preserve(dict(inventory=[dict(path='README.md',sha256='0'*64)]))
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertTrue(self.tree.exists())

    def test_repeated_reference_reads_are_bounded(self):
        leaf = self.file('repeated.txt')
        with patch('resource_lifecycle.artifact', wraps=artifact) as read:
            manifest = load_bound(self.preserve(dict(evidence=[leaf]*20)))
        self.assertEqual(sum(call.args[0]==leaf for call in read.call_args_list), 1)
        self.assertEqual(sum(row['original']==leaf for row in manifest['files']), 1)

    def test_captured_state_requires_trusted_external_classification(self):
        captured = new_state('fixture-main', 'fixture-project')
        captured['tasks']['A'] = dict(integration=dict(report=dict(
            path=str(self.root/'expired-fixture-integration.json'),sha256='0'*64)))
        missing_context = dict(path=str(self.root/'expired-request.json'),sha256='0'*64)
        request_data = dict(request_id='captured',project_id='foreign-fixture-project',task_id='A',
            requirement={},approval=missing_context,worktree='captured',branch='fixture')
        captured_request = self.json('captured-request.json',request_data)
        trace = self.json('trace.json', dict(captured=captured,
            retained=dict(kind='result',result=dict(authority=missing_context)),
            request=captured_request,
            source=dict(root=str(self.tree),kind='git',files=[missing_context],extra=[],digest='captured'),
            stored=dict(schema='dispatch-host-result/1',pending_packet=dict(request_ref=missing_context))))
        leaf = self.file('actual-evidence.txt')
        with self.assertRaisesRegex(ValueError, 'artifact missing'):
            self.preserve(dict(evidence=[trace, leaf]), self.root/'unclassified')
        self.classification_fixture([trace, captured_request])
        manifest = load_bound(self.preserve(dict(evidence=[trace, captured_request, leaf])))
        self.assertTrue(any(row['original']==trace for row in manifest['files']))
        self.assertTrue(any(row['original']==leaf for row in manifest['files']))
        captured_copy = next(row for row in manifest['files'] if row['original']==captured_request)
        self.assertEqual(load_bound(captured_copy),request_data)
        with self.assertRaisesRegex(ValueError,'artifact missing'):
            self.preserve(dict(request=request_data),self.root/'inline-request')
        missing = dict(path=str(self.root/'missing-actual-evidence.txt'),sha256='0'*64)
        with self.assertRaisesRegex(ValueError, 'artifact missing'):
            self.preserve(dict(captured=captured,evidence=[missing]),self.root/'missing-actual')

    def test_packet_metadata_cannot_hide_an_explicit_missing_reference(self):
        for index, metadata in enumerate((dict(kind='result'), dict(schema='parallel-coordination/2'),
                                         dict(project_id='foreign',task_id='A'))):
            with self.subTest(metadata=metadata):
                missing = dict(path=str(self.root/'missing.txt'),sha256='0'*64,**metadata)
                with self.assertRaisesRegex(ValueError, 'artifact missing'):
                    self.preserve(dict(evidence=[missing]),self.root/('metadata-'+str(index)))

    def test_preserved_copy_original_identity_is_data(self):
        backup = self.file('previous-copy.txt', 'Preserved exact bytes')
        previous = dict(backup,original=dict(path=str(self.root/'already-cleaned.txt'),sha256=backup['sha256']))
        manifest = load_bound(self.preserve(dict(evidence=[previous])))
        self.assertTrue(any(row['original']==backup for row in manifest['files']))

    def test_source_identities_are_data_but_non_git_extras_and_explicit_evidence_are_not(self):
        code = dict(path=str(self.root/'old-source'/'feature.py'),sha256='0'*64)
        extra = self.file('necessary-ignored.txt')
        inventory = dict(root=str(self.root/'old-source'),kind='git',
            files=[dict(relative='feature.py',sha256=code['sha256'])],extra=[code, extra],digest='captured')
        manifest = load_bound(self.preserve(dict(source=inventory,contract=code)))
        self.assertFalse(any(row['original']==code for row in manifest['files']))
        self.assertTrue(any(row['original']==extra for row in manifest['files']))
        with self.assertRaisesRegex(ValueError, 'artifact missing'):
            self.preserve(dict(source=inventory,evidence=[code]),self.root/'explicit-code-evidence')

    def test_relative_extra_cannot_inherit_a_tracked_code_identity(self):
        invalid = dict(path='feature.py',sha256='0'*64)
        inventory = dict(root=str(Path.cwd()),kind='git',files=[dict(relative='feature.py',sha256='0'*64)],
                         extra=[invalid],digest='captured')
        with self.assertRaisesRegex(ValueError, 'artifact missing or not absolute'):
            self.preserve(dict(source=inventory))

    def test_source_inventory_shape_cannot_hide_adjacent_required_evidence(self):
        missing = dict(path=str(self.root/'missing'),sha256='0'*64)
        inventory = dict(root=str(self.root), kind='git', files=[], extra=[], digest='self-labelled', evidence=[missing])
        with self.assertRaisesRegex(ValueError, 'artifact missing'):
            self.preserve(dict(source=inventory))

    def test_root_reference_manifest_and_root_relative_inventory_are_distinct(self):
        leaf = self.file('manifest-leaf.txt')
        references = self.json('reference-list.json', [leaf, leaf])
        data = self.json('inventory-list.json', [dict(path='README.md',sha256='0'*64)])
        manifest = load_bound(self.preserve(dict(evidence=[references, data])))
        self.assertEqual(sum(row['original']==leaf for row in manifest['files']), 1)
        self.assertTrue(any(row['original']==data for row in manifest['files']))

    def test_nested_preservation_keeps_copies_without_dereferencing_originals(self):
        backup = self.file('durable-previous.txt')
        previous = self.json('previous-manifest.json',dict(executor_id='previous',generation=1,
            files=[dict(backup,original=dict(path=str(self.root/'old-removed.txt'),sha256=backup['sha256']))],
            handoff=self.file('previous-handoff.txt'),unregistered_evidence_preserved=True))
        manifest = load_bound(self.preserve(dict(preservation=previous)))
        self.assertTrue(any(row['original']==backup for row in manifest['files']))

    def test_declared_evidence_wrappers_reject_missing_and_tampered_dependencies(self):
        for label, metadata in (('result',dict(kind='result')),
                                ('host',dict(schema='dispatch-host-result/1'))):
            for mode in ('missing','tampered'):
                for field, grouped in ((field,grouped) for field in ('evidence','proof','report','authority')
                                       for grouped in (False,True)):
                    with self.subTest(field=field,label=label,mode=mode,grouped=grouped):
                        name = field+'-'+label+'-'+mode+'-'+str(grouped)
                        leaf = self.file(name+'.txt','Original required bytes')
                        if mode=='missing': Path(leaf['path']).unlink()
                        else: Path(leaf['path']).write_text('changed',encoding='utf-8')
                        wrapper = dict(metadata,evidence=[leaf])
                        if grouped: wrapper = dict(metadata,groups=[[wrapper]])
                        before = self.runtime.path.read_bytes()
                        destination = self.root/name
                        with self.assertRaisesRegex(ValueError,'artifact missing|hash mismatch'):
                            self.preserve({field:[wrapper] if field=='evidence' else wrapper},destination)
                        self.assertEqual(self.runtime.path.read_bytes(),before)
                        self.assertFalse((destination/'preservation.json').exists())
                        self.assertFalse(self.runtime._load()['tasks']['A'].get('preservation'))
                        self.assertTrue(self.tree.exists())
                        self.assertFalse(any(v.get('operation') in ('close','cleanup') for v in
                            self.runtime._load()['runtime']['operations'].values()))

    def test_declared_wrapper_groups_preserve_refs_without_reading_inventory_data(self):
        leaf = self.file('wrapper-evidence.txt')
        data = dict(path=str(self.root/'ordinary-inventory.txt'),sha256='0'*64)
        wrapper = dict(kind='result',groups=[[dict(schema='dispatch-host-result/1',
            inventory=[data],evidence=[leaf,leaf])]])
        for field in ('evidence','proof','report','authority'):
            with self.subTest(field=field):
                manifest = load_bound(self.preserve({field:[wrapper] if field=='evidence' else wrapper},
                    self.root/('valid-'+field)))
                self.assertEqual(sum(row['original']==leaf for row in manifest['files']),1)
                self.assertFalse(any(row['original']==data for row in manifest['files']))

    def test_all_existing_reference_slots_reject_missing_inline_evidence(self):
        for field in sorted(REFERENCE_FIELDS):
            with self.subTest(field=field):
                missing = dict(path=str(self.root/'missing-inline.txt'),sha256='0'*64)
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError,'artifact missing'):
                    self.preserve({field:dict(kind='result',evidence=[missing])},self.root/('slot-'+field))
                self.assertEqual(self.runtime.path.read_bytes(),before)
                self.assertFalse((self.root/('slot-'+field)/'preservation.json').exists())
                self.assertTrue(self.tree.exists())
