"""Archive amendments and explicit capacity continuation; disposable resources only."""
from copy import deepcopy
from pathlib import Path
import json

import test_core_runtime as core_tests
import test_native_control as native_tests
from evidence import load_bound, ref, preserved_read, write_atomic
from preservation_contract import binding
from test_dispatch import DispatchTests
from host_adapter import HostCallError


class ClassificationRevisionTests(core_tests.CoreFixture):
    ready = native_tests.NativeControlTests.ready
    approve = core_tests.CoreFixture.classification_fixture

    def setUp(self):
        super().setUp()
        proof, self.tree = self.ready()
        self.merge_proof = load_bound(proof)['merge_verification']
        state = self.runtime._load()
        state['tasks']['A'].pop('preservation', None)
        write_atomic(self.runtime.path, state)
        self.classification_fixture_setup()

    def amendment(self, previous, originals, **changes):
        value = dict(schema='preservation-classification/1',
            **binding(self.runtime._load(), 'A'), previous=previous['declaration'],
            reason='Add omitted frozen test captures', captures=deepcopy(previous['captures']) + [
                dict(original=x, contract='frozen-test-result/1', reason='Omitted test run capture')
                for x in originals])
        value.update(changes)
        declaration = self.json('amendment-'+str(len(value['captures']))+'.json', value)
        return declaration

    def revise(self, previous, declaration):
        return self.runtime.classify_preservation('A', declaration, self.actor, authorized=True,
            previous_declaration=previous['declaration'])

    def test_add_omitted_capture_keeps_original_declaration_and_reads_archive(self):
        first = self.json('first.json', dict(evidence=[]))
        second = self.json('second.json', dict(evidence=[dict(path=str(self.root/'expired'), sha256='0'*64)]))
        original = self.approve([first]); original_bytes = Path(original['declaration']['path']).read_bytes()
        amendment = self.amendment(original, [second])
        revised = self.revise(original, amendment)
        self.assertEqual(revised['previous'], original)
        self.assertEqual(self.revise(original, amendment), revised)
        self.assertEqual(Path(original['declaration']['path']).read_bytes(), original_bytes)
        handoff = self.json('handoff.json', dict(evidence=[first, second]))
        archived = self.runtime.preserve_task('A', self.root/'archive', handoff, self.actor,
            scope='fixture_only', merge_verification=self.merge_proof)
        self.assertEqual(len([x for x in load_bound(archived)['files'] if x.get('capture')]), 2)
        with preserved_read(self.runtime._load(), 'A'):
            self.assertEqual(load_bound(second)['evidence'][0]['sha256'], '0'*64)

    def test_amendment_cannot_remove_capture_or_change_its_role(self):
        first = self.json('first.json', dict(evidence=[])); original = self.approve([first])
        second = self.json('second.json', dict(evidence=[]))
        for mode in ('remove', 'role', 'wrong_prior', 'worker'):
            with self.subTest(mode=mode):
                declaration = self.amendment(original, [second])
                value = load_bound(declaration)
                if mode=='remove': value['captures'] = value['captures'][1:]
                if mode=='role': value['captures'][0]['contract'] = 'frozen-baseline/1'
                if mode=='wrong_prior': value['previous'] = second
                write_atomic(Path(declaration['path']), value); declaration = ref(Path(declaration['path']))
                before = self.runtime.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.runtime.classify_preservation('A', declaration,
                        'worker-A' if mode=='worker' else self.actor, authorized=True,
                        previous_declaration=original['declaration'])
                self.assertEqual(self.runtime.path.read_bytes(), before)

    def test_retry_same_partial_archive_after_capture_revision(self):
        first = self.json('first.json', dict(evidence=[])); original = self.approve([first])
        omitted = self.json('omitted.json', dict(evidence=[dict(path=str(self.root/'expired'), sha256='0'*64)]))
        handoff = self.json('handoff.json', dict(evidence=[first, omitted]))
        op = 'original:preservation'; state = self.runtime._load()
        state['runtime']['operations'][op] = dict(operation='continuation-effect', task_id='A',
            status='unknown', request=self.file('request.txt'))
        write_atomic(self.runtime.path, state)
        def preserve():
            return self.runtime.preserve_task('A', self.root/'archive', handoff, self.actor,
                scope='fixture_only', merge_verification=self.merge_proof, operation_id=op)
        with self.assertRaisesRegex(ValueError, 'artifact missing'): preserve()
        marker = self.root/'archive/preservation-intent.json'; marker_bytes = marker.read_bytes()
        self.revise(original, self.amendment(original, [omitted]))
        archived = preserve()
        self.assertEqual(marker.read_bytes(), marker_bytes)
        self.assertEqual(preserve(), archived)
        with preserved_read(self.runtime._load(), 'A'): load_bound(omitted)

    def test_duplicate_classification_rechecks_capture_bytes(self):
        first = self.json('first.json', dict(evidence=[])); original = self.approve([first])
        Path(first['path']).write_bytes(b'tampered')
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.runtime.classify_preservation('A', original['declaration'], self.actor, authorized=True)
        self.assertEqual(self.runtime.path.read_bytes(), before)

    def test_archived_classification_cannot_be_amended(self):
        first = self.json('first.json', dict(evidence=[])); original = self.approve([first])
        handoff = self.json('handoff.json', dict(evidence=[first]))
        self.runtime.preserve_task('A', self.root/'archive', handoff, self.actor,
            scope='fixture_only', merge_verification=self.merge_proof)
        second = self.json('second.json', dict(evidence=[]))
        declaration = self.amendment(original, [second]); before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'before preservation'):
            self.revise(original, declaration)
        self.assertEqual(self.runtime.path.read_bytes(), before)


class CapacityContinuationTests(DispatchTests):
    def test_capacity_error_with_actual_identity_never_creates_second_author(self):
        self.replies['spawn_agent'] = dict(agent_id='native-fixture',
            error=dict(code='agent_capacity_exceeded', message='agent thread limit reached'))
        self.assertEqual(self.run_dispatch()['status'], 'dispatched')
        self.assertEqual(self.runtime._load()['tasks']['A']['owner_agent_id'], 'native_agent:native-fixture')
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual([x[0] for x in self.calls].count('spawn_agent'), 1)

    def test_capacity_exception_with_actual_identity_is_retained_not_retried(self):
        self.replies['spawn_agent'] = HostCallError(dict(agent_id='native-fixture',
            code='agent_capacity_exceeded', message='agent thread limit reached'))
        self.assertEqual(self.run_dispatch()['status'], 'dispatched')
        self.assertEqual(self.runtime._load()['tasks']['A']['owner_agent_id'], 'native_agent:native-fixture')
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual([x[0] for x in self.calls].count('spawn_agent'), 1)

    def test_error_envelope_with_actual_identity_is_not_a_capacity_refusal(self):
        self.replies['spawn_agent'] = dict(isError=True, content=[dict(type='text', text=json.dumps(
            dict(agent_id='native-fixture', error=dict(code='agent_capacity_exceeded',
                message='agent thread limit reached'))))])
        self.assertEqual(self.run_dispatch()['status'], 'dispatched')
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual([x[0] for x in self.calls].count('spawn_agent'), 1)

    def test_conflicting_creation_identities_remain_unknown_and_cannot_retry(self):
        self.replies['spawn_agent'] = dict(agent_id='native-fixture', id='another-native',
            error=dict(code='agent_capacity_exceeded'))
        self.assertEqual(self.run_dispatch()['status'], 'unknown')
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual(len(self.calls), 1)

    def test_capacity_retry_rechecks_current_main_before_host_effect(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        self.run_dispatch()
        self.git._git('update-ref', 'refs/heads/main', 'HEAD^')
        before = self.runtime.path.read_bytes(), self.controller.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'stale local main'):
            self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual((self.runtime.path.read_bytes(), self.controller.path.read_bytes()), before)
        self.assertEqual(len(self.calls), 1)

    def test_capacity_retry_rejects_modified_worktree(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        self.run_dispatch()
        (Path(self.request['worktree'])/'unexpected.txt').write_text('not original', encoding='utf-8')
        before = self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'worktree is not clean'):
            self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertEqual(len(self.calls), 1)

    def test_capacity_retry_rechecks_full_live_permit_binding(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        self.run_dispatch()
        state, _ = self.controller.read()
        state['capacity_requests']['dispatch-A:capacity']['spec']['task_id'] = 'another-task'
        write_atomic(self.controller.path, state)
        before = self.runtime.path.read_bytes(), self.controller.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'RESOURCE permit binding'):
            self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual((self.runtime.path.read_bytes(), self.controller.path.read_bytes()), before)
        self.assertEqual(len(self.calls), 1)

    def test_capacity_retry_refuses_unreconciled_retained_results_or_handoff(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        self.run_dispatch(); original = deepcopy(self.runtime._load())
        for field in ('retained_host_results', 'handoff_intent'):
            with self.subTest(field=field):
                state = deepcopy(original)
                state['runtime']['operations']['dispatch-A'][field] = True
                write_atomic(self.runtime.path, state); before = self.runtime.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'unreconciled'):
                    self.dispatch.retry_capacity(self.request, authorized=True)
                self.assertEqual(self.runtime.path.read_bytes(), before)
                self.assertEqual(len(self.calls), 1)

    def test_explicit_capacity_retry_reuses_task_attempt_tree_and_permit(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        first = self.run_dispatch(); self.assertEqual(first['status'], 'capacity')
        before = deepcopy(self.runtime._load()['runtime']['operations']['dispatch-A:launch'])
        resource = deepcopy(self.controller.read()[0]['capacity_requests'])
        self.assertEqual(self.run_dispatch()['status'], 'capacity')
        self.assertEqual(len(self.calls), 1)
        self.replies['spawn_agent'] = dict(agent_id='native-fixture')
        final = self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual(final['status'], 'dispatched')
        current = self.runtime._load()['runtime']['operations']['dispatch-A:launch']
        self.assertEqual(current['capacity_retries'][0], before)
        self.assertEqual(current['attempt_id'], before['attempt_id'])
        self.assertEqual(self.controller.read()[0]['capacity_requests'], resource)
        self.assertEqual(self.dispatch.retry_capacity(self.request, authorized=True)['status'], 'dispatched')
        self.assertEqual([x[0] for x in self.calls].count('spawn_agent'), 2)

    def test_repeated_capacity_failures_preserve_each_callback_and_single_permit(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        self.assertEqual(self.run_dispatch()['status'], 'capacity')
        initial = deepcopy(self.runtime._load()['runtime']['operations']['dispatch-A:launch'])
        self.assertEqual(self.dispatch.retry_capacity(self.request, authorized=True)['status'], 'capacity')
        self.replies['spawn_agent'] = dict(agent_id='native-fixture')
        self.assertEqual(self.dispatch.retry_capacity(self.request, authorized=True)['status'], 'dispatched')
        current = self.runtime._load()['runtime']['operations']['dispatch-A:launch']
        self.assertEqual(len(current['capacity_retries']), 2)
        self.assertEqual(current['capacity_retries'][0], initial)
        refs = [x['host_results'][0]['capture'] for x in current['capacity_retries']]
        self.assertEqual(len({x['path'] for x in refs}), 2)
        for item in refs: self.assertEqual(load_bound(item)['result']['status'], 'capacity')
        self.assertEqual(len(self.controller.read()[0]['capacity_requests']), 1)

    def test_capacity_retry_rejects_missing_authority_and_altered_callback(self):
        self.replies['spawn_agent'] = RuntimeError('agent thread limit reached')
        self.assertEqual(self.run_dispatch()['status'], 'capacity')
        before = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request)
        self.assertEqual(self.runtime.path.read_bytes(), before)
        state = self.runtime._load(); record = state['runtime']['operations']['dispatch-A:launch']
        original = Path(record['host_results'][0]['capture']['path'])
        original.write_bytes(b'changed callback')
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_spawn_is_never_a_capacity_retry(self):
        self.replies['spawn_agent'] = TimeoutError('response lost')
        self.assertEqual(self.run_dispatch()['status'], 'unknown')
        before = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError): self.dispatch.retry_capacity(self.request, authorized=True)
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertEqual(len(self.calls), 1)
