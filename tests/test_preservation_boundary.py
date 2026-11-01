"""Exact capture bytes on disposable tasks; native and main facts are fixtures."""
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch

import test_core_runtime as core_tests
import test_native_control as native_tests
from evidence import artifact, load_bound, ref, preserved_read, write_atomic


class PreservationBoundary(core_tests.CoreFixture):
    ready = native_tests.NativeControlTests.ready

    def setUp(self):
        super().setUp()
        proof, self.tree = self.ready()
        self.merge_proof = load_bound(proof)['merge_verification']
        # The ready() helper supplies old fixture preservation, not a canonical one.
        state = self.runtime._load(); state['tasks']['A'].pop('preservation', None)
        write_atomic(self.runtime.path, state)
        self.classification_fixture_setup()

    approve = core_tests.CoreFixture.classification_fixture

    def preserve(self, payload, name='durable'):
        handoff = self.json('handoff.json', payload)
        return self.runtime.preserve_task('A', self.root/name, handoff, self.actor,
            scope='fixture_only', merge_verification=self.merge_proof)

    def test_new_task_expired_test_files_are_captured_data(self):
        temporary = self.root/'test-temporary'
        temporary.mkdir()
        leaf = self.file('test-temporary/host-receipt.json', 'disposable simulated receipt')
        capture = self.json('test-result.json', dict(results=[dict(evidence=leaf)], status='passed'))
        self.approve([capture])
        Path(leaf['path']).unlink(); temporary.rmdir()
        handoff = self.json('handoff.json', dict(evidence=[capture]))
        preservation = self.runtime.preserve_task('A', self.root/'durable', handoff, self.actor,
            scope='fixture_only', merge_verification=self.merge_proof)
        row = next(row for row in load_bound(preservation)['files'] if row['original']==capture)
        self.assertEqual(artifact(row).read_bytes(), artifact(capture).read_bytes())

    def test_capture_contract_does_not_hide_adjacent_missing_required_evidence(self):
        capture = self.json('test-result.json', dict(evidence=dict(path=str(self.root/'expired'), sha256='0'*64)))
        self.approve([capture])
        for field in ('proof', 'report', 'authority', 'evidence'):
            with self.subTest(field=field):
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'artifact missing'):
                    self.preserve(dict(evidence=[capture], adjacent={field:dict(path=str(self.root/'missing'),sha256='0'*64)}), 'adjacent-'+field)
                self.assertEqual(before, self.runtime.path.read_bytes())

    def test_capture_bytes_missing_or_tampered_still_reject(self):
        capture = self.json('capture.json', dict(evidence=[]))
        self.approve([capture])
        original = Path(capture['path']).read_bytes()
        for mode in ('missing', 'tampered'):
            with self.subTest(mode=mode):
                Path(capture['path']).write_bytes(original)
                if mode=='missing': Path(capture['path']).unlink()
                else: Path(capture['path']).write_bytes(b'changed')
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'artifact missing|hash mismatch'):
                    self.preserve(dict(evidence=[capture]), mode)
                self.assertEqual(before, self.runtime.path.read_bytes())

    def test_classification_rejects_wrong_identity_revision_path_sha_and_contract(self):
        capture = self.json('capture.json', dict(evidence=[]))
        cases = [dict(project_id='foreign'), dict(task_id='B'), dict(delivery_revision=99),
                 dict(delivery=self.file('wrong-report.txt')), dict(schema='worker-override/1')]
        row = dict(original=capture, contract='frozen-test-result/1', reason='Fixture capture')
        for change in (dict(original=dict(capture, path='relative.json')),
                       dict(original=dict(capture, sha256='0'*64)),
                       dict(original=dict(capture, path=str(self.root/'missing'))),
                       dict(contract='*'), dict(reason='')):
            cases.append(dict(captures=[dict(row, **change)]))
        for change in cases:
            with self.subTest(change=change):
                # Prepare original authority outside the assertion so only the
                # rejected formal call is required to leave its ledger unchanged.
                self.classification_fixture_setup()
                before = self.runtime.path.read_bytes()
                with self.assertRaises(ValueError): self.approve([capture], **change)
                self.assertEqual(before, self.runtime.path.read_bytes())

    def test_worker_tags_and_unapproved_classification_have_no_authority(self):
        missing = dict(path=str(self.root/'missing'), sha256='0'*64)
        for label in (dict(kind='result'), dict(schema='dispatch-host-result/1'),
                      dict(classification='frozen-test-result/1')):
            with self.subTest(label=label):
                capture = self.json('self-labelled.json', dict(label, evidence=[missing]))
                with self.assertRaisesRegex(ValueError, 'artifact missing'):
                    self.preserve(dict(evidence=[capture]), str(len(label))+next(iter(label)))
        capture = self.json('capture.json', dict(evidence=[]))
        self.approve([capture])
        record = self.runtime._load()['tasks']['A']['preservation_classification']
        with self.assertRaises(ValueError):
            self.runtime.classify_preservation('A', record['declaration'], 'worker-A', authorized=True)
        with self.assertRaises(ValueError):
            self.runtime.classify_preservation('A', record['declaration'], self.actor)

    def test_required_slots_cannot_be_downgraded_even_with_approved_capture(self):
        capture = self.json('capture.json', dict(evidence=[]))
        self.approve([capture])
        for slot in ('proof', 'authority', 'report', 'review', 'independent_review', 'merge_verification', 'delivery', 'host_confirmation'):
            with self.subTest(slot=slot):
                with self.assertRaisesRegex(ValueError, 'cannot be classified'):
                    self.preserve(dict(evidence=[capture], **{slot:capture}), 'required-'+slot)

    def test_final_report_cannot_be_hidden_in_capture_evidence(self):
        delivery = self.runtime._load()['tasks']['A']['delivery']
        with self.assertRaisesRegex(ValueError, 'cannot be classified'):
            self.approve([delivery])
            self.preserve(dict(evidence=[delivery]))

    def test_canonical_review_evidence_is_required_even_in_an_evidence_slot(self):
        # Seed the canonical review packet in this disposable ledger; the ready
        # fixture has no review attachment. No real review or live state is created.
        state = self.runtime._load(); task = state['tasks']['A']
        task['review'] = dict(evidence=self.file('canonical-review.txt'))
        task['merge']['proof'] = self.merge_proof
        write_atomic(self.runtime.path, state)
        for reference in (task['review']['evidence'],task['merge']['proof'],task['resource_authority']):
            with self.subTest(reference=reference):
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'cannot be classified'):
                    self.approve([reference])
                self.assertEqual(before, self.runtime.path.read_bytes())

    def test_test_result_slot_accepts_only_the_test_capture_contract(self):
        capture = self.json('capture.json', dict(evidence=[dict(path=str(self.root/'expired'),sha256='0'*64)]))
        self.approve([capture])
        manifest = load_bound(self.preserve(dict(result=capture)))
        self.assertEqual(next(r for r in manifest['files'] if r['original']==capture)['capture']['contract'], 'frozen-test-result/1')

    def test_baseline_or_host_contract_cannot_hide_a_required_result(self):
        capture = self.json('capture.json', dict(evidence=[]))
        baseline = self.runtime.path.read_bytes()
        for contract in ('frozen-baseline/1', 'frozen-host-message/1'):
            with self.subTest(contract=contract):
                self.runtime.path.write_bytes(baseline)
                self.approve([capture], captures=[dict(original=capture, contract=contract, reason='fixture')])
                with self.assertRaisesRegex(ValueError, 'cannot be classified'):
                    self.preserve(dict(result=capture), contract.replace('/','-'))

    def test_forged_integrator_and_changed_original_approval_cannot_grant_classification(self):
        capture = self.json('capture.json', dict(evidence=[]))
        task = self.runtime._load()['tasks']['A']
        from preservation_contract import binding
        declaration = self.json('forged.json', dict(schema='preservation-classification/1',
            **binding(self.runtime._load(), 'A'), source='approved', user_quote='Pretend user authority',
            integration_agent='forged-integrator', captures=[dict(original=capture,
                contract='frozen-test-result/1', reason='test')]))
        for actor in ('worker-A', 'forged-integrator'):
            with self.subTest(actor=actor):
                before = self.runtime.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.runtime.classify_preservation('A', declaration, actor, authorized=True)
                self.assertEqual(before, self.runtime.path.read_bytes())
        authority = Path(task['resource_authority']['path']); authority.write_bytes(b'changed original approval')
        with self.assertRaises(ValueError):
            self.runtime.classify_preservation('A', declaration, self.actor, authorized=True)

    def test_existing_async_approval_uses_shared_identity_checks_without_cleanup_privilege(self):
        from real_use_closeout import approval
        state = self.runtime._load(); task = state['tasks']['A']
        value = load_bound(task['resource_authority'])
        value['source'] = 'explicit async user approval in original main conversation'
        original = self.json('async-original-approval.json',value)
        task['resource_authority'] = original; task['requirements']['user_reference'] = original['path']
        write_atomic(self.runtime.path,state)
        self.assertEqual(approval(state,'A',original,'delivery-worker'),value)
        capture = self.json('capture.json',dict(evidence=[]))
        record = self.approve([capture])
        self.assertEqual(record['authority'],original)
        self.assertEqual(load_bound(original),value)
        from resource_lifecycle import actor
        with self.assertRaisesRegex(ValueError,'explicit bounded'):
            actor(self.runtime._load(),'A','delivery-worker')

    def test_unknown_original_preservation_accepts_additive_classification_without_rewriting_marker(self):
        capture = self.json('capture.json', dict(evidence=[dict(path=str(self.root/'expired'), sha256='0'*64)]))
        handoff = self.json('handoff.json', dict(evidence=[capture]))
        operation_id = 'original-request:preservation'
        state = self.runtime._load()
        state['runtime']['operations'][operation_id] = dict(operation='continuation-effect',
            task_id='A', request=self.file('original-request.txt'), status='unknown', reason='Original missing fixture failure')
        write_atomic(self.runtime.path, state)
        def preserve():
            return self.runtime.preserve_task('A', self.root/'durable', handoff, self.actor,
                scope='fixture_only', merge_verification=self.merge_proof, operation_id=operation_id)
        with self.assertRaisesRegex(ValueError, 'artifact missing'): preserve()
        marker = self.root/'durable/preservation-intent.json'; original_marker = marker.read_bytes()
        partial = {p:p.read_bytes() for p in marker.parent.iterdir()}
        failed = deepcopy(self.runtime._load()['runtime']['operations'][operation_id])
        self.approve([capture])
        # An interruption after the amendment must resume the same bytes and identity.
        original_write = write_atomic
        def interrupted(path, value):
            if path.name=='preservation.json': raise OSError('fixture interrupted final manifest')
            return original_write(path, value)
        with patch('resource_lifecycle.write_atomic', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'fixture interrupted'): preserve()
        preservation = preserve()
        self.assertEqual(preserve(), preservation)
        self.assertEqual(marker.read_bytes(), original_marker)
        for path, data in partial.items(): self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.runtime._load()['runtime']['operations'][operation_id], failed)
        self.assertTrue(load_bound(preservation).get('classification_intent'))
        with preserved_read(self.runtime._load(), 'A'): self.assertEqual(load_bound(capture)['evidence'][0]['sha256'], '0'*64)
        amendment = Path(load_bound(preservation)['classification_intent']['path'])
        amendment_bytes = amendment.read_bytes(); amendment.write_bytes(b'changed additive intent')
        with self.assertRaises(ValueError):
            with preserved_read(self.runtime._load(), 'A'): pass
        amendment.write_bytes(amendment_bytes)
        marker.write_bytes(b'changed old marker')
        with self.assertRaises(ValueError):
            with preserved_read(self.runtime._load(), 'A'): pass

    def test_classification_and_saved_bytes_are_rechecked_by_read_and_eligibility(self):
        from cleanup import eligibility
        capture = self.json('capture.json', dict(evidence=[]))
        record = self.approve([capture])
        preservation = self.preserve(dict(evidence=[capture]))
        state = self.runtime._load()
        saved = next(r for r in load_bound(preservation)['files'] if r['original']==capture)
        original = Path(saved['path']).read_bytes()
        for mode in ('capture', 'original', 'approval', 'declaration', 'revision', 'classification'):
            with self.subTest(mode=mode):
                altered = deepcopy(state)
                if mode=='capture': Path(saved['path']).write_bytes(b'tampered')
                if mode=='original': Path(capture['path']).write_bytes(b'tampered original capture')
                if mode in ('approval','declaration'):
                    authority = Path(record['authority' if mode=='approval' else 'declaration']['path']); authority_bytes = authority.read_bytes()
                    authority.write_bytes(b'changed')
                if mode=='revision': altered['tasks']['A']['delivery_revision'] += 1
                if mode=='classification': altered['tasks']['A']['preservation_classification']['captures'][0]['contract']='frozen-baseline/1'
                with self.assertRaises(ValueError):
                    with preserved_read(altered, 'A'): pass
                with self.assertRaises(ValueError): eligibility(self.runtime, altered, 'A', preservation, 'fixture_only')
                Path(saved['path']).write_bytes(original)
                if mode=='original': Path(capture['path']).write_bytes(original)
                if mode in ('approval','declaration'): authority.write_bytes(authority_bytes)

    def test_capture_missing_or_changed_before_classification_is_rejected_without_ledger_write(self):
        capture = self.json('capture.json', dict(evidence=[]))
        original = Path(capture['path']).read_bytes()
        for mode in ('missing','tampered'):
            with self.subTest(mode=mode):
                Path(capture['path']).write_bytes(original)
                if mode=='missing': Path(capture['path']).unlink()
                else: Path(capture['path']).write_bytes(b'changed before classification')
                before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError,'artifact missing|hash mismatch'): self.approve([capture])
                self.assertEqual(before,self.runtime.path.read_bytes())
