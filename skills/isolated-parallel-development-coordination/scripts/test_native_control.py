"""ROOT tools are explicit fixtures; only registered temporary directories are removed."""
from copy import deepcopy
from pathlib import Path
import shutil

import test_core_runtime as core_tests
import test_cleanup_runtime as cleanup_tests
from evidence import fingerprint, load_bound, pack, ref, write_atomic


class NativeControlTests(core_tests.CoreFixture):
    def remove(self, resource):
        return cleanup_tests.CleanupTests.remove(self,dict(resource,id=resource['id'].replace(':','-')))

    def ready(self, *, merged=True, permit=False):
        proof = core_tests.CoreRuntimeTests.close_fixture(self, merged)
        state = self.runtime._load(); task = state['tasks']['A']
        tree = Path(state['runtime']['cleanup_root'])/'author'; tree.parent.mkdir(parents=True)
        shutil.copytree(task['worktree'], tree)
        report = load_bound(task['delivery']); report.pop('source'); report['source_root'] = str(tree)
        report = pack(report); reference = self.json('current-delivery.json', report)
        old = task['delivery']; task.update(worktree=str(tree), delivery=reference)
        for receipt in state['receipts'].values():
            if receipt.get('payload',{}).get('report') == old:
                receipt['payload']['report'] = reference; receipt['fingerprint'] = fingerprint(receipt['payload'])
        if not permit:
            state['runtime']['operations'].pop('original-run')
            task['worker_run'].pop('attempt_id')
        write_atomic(self.runtime.path, state)
        backup = self.root/'current-copy.json'; backup.write_bytes(Path(reference['path']).read_bytes())
        preservation = load_bound(proof); preservation['files'] = [dict(ref(backup), original=reference)]
        proof = self.json('current-preservation.json', preservation)
        self.runtime.register_resource('A',dict(id='A:worktree',kind='worktree',path=str(tree)),self.file('tree-authority'),self.actor)
        self.runtime.register_resource('A',dict(id='A:agent',kind='agent',executor_id='worker-A',generation=1),
            self.file('agent-authority'),self.actor)
        return proof, tree

    def prepare(self, proof, operation='root-close'):
        return self.runtime.prepare_native_close('A', operation, proof, self.actor,
            authorized=True, scope='fixture_only')

    def observation(self, request, *, previous=None, after='not_found', **changes):
        previous = {'completed':'fixture final'} if previous is None else previous
        close = self.json('root-close-result.json',dict(previous_status=previous))
        wait = self.json('root-wait-result.json',dict(status={'native-A':after},timed_out=False))
        value = dict(operation_id='root-close',executor_id='worker-A',generation=1,request=request,
            controller_id=self.actor,
            close=dict(tool='multi_agent_v1__close_agent',arguments={'target':'native-A'},result=close),
            wait=dict(tool='multi_agent_v1__wait_agent',arguments={'targets':['native-A'],'timeout_ms':10000},result=wait))
        value.update(changes)
        return self.json('root-observation.json',value)

    def test_prepare_then_root_claim_is_one_shot_and_uses_actual_tool_contract(self):
        proof,_ = self.ready()
        prepared = self.prepare(proof)
        self.assertEqual(prepared['status'],'intent')
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'closing')
        request = load_bound(prepared['native_request'])
        self.assertEqual(request['close'],dict(tool='multi_agent_v1__close_agent',arguments={'target':'native-A'}))
        self.assertEqual(request['wait'],dict(tool='multi_agent_v1__wait_agent',arguments={'targets':['native-A'],'timeout_ms':10000}))
        with self.assertRaises(ValueError):self.runtime.claim_native_close('root-close','other-controller',authorized=True)
        claimed = self.runtime.claim_native_close('root-close',self.actor,authorized=True)
        self.assertTrue(claimed['dispatch_allowed'])
        self.assertFalse(self.runtime.claim_native_close('root-close',self.actor,authorized=True)['dispatch_allowed'])
        self.assertEqual(self.prepare(proof)['native_request'],prepared['native_request'])
        with self.assertRaises(ValueError):self.prepare(proof,'another-close')

    def test_premerge_or_unregistered_legacy_task_cannot_create_close_intent(self):
        proof,_ = self.ready(merged=False)
        before = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.prepare(proof)
        self.assertEqual(before,self.runtime.path.read_bytes())
        state=self.runtime._load();state['tasks']['A'].update(phase='completed',merge=dict(verified=True,head_sha='a'*40))
        state['runtime']['resources'].clear();write_atomic(self.runtime.path,state)
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.prepare(proof)
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_close_previous_status_or_wait_not_found_alone_is_not_confirmation(self):
        proof,_=self.ready();prepared=self.prepare(proof);self.runtime.claim_native_close('root-close',self.actor,authorized=True)
        fake=self.json('old-boolean.json',dict(operation_id='root-close',executor_id='worker-A',generation=1,
            closed=True,evidence=[self.file('unbound-not-found.txt')]))
        with self.assertRaises(ValueError):self.runtime.reconcile_lifecycle('root-close',fake,self.actor)
        unknown=self.observation(prepared['native_request'],previous='not_found')
        result=self.runtime.reconcile_lifecycle('root-close',unknown,self.actor)
        self.assertEqual(result['status'],'unknown');self.assertEqual(result['next_technical_owner'],self.actor)
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'closing')
        with self.assertRaises(ValueError):self.runtime.release_receipt('root-close',self.actor)

    def test_formal_close_and_wait_confirm_context_without_manufacturing_local_permit(self):
        proof,tree=self.ready();prepared=self.prepare(proof)
        self.runtime.claim_native_close('root-close',self.actor,authorized=True)
        observed=self.observation(prepared['native_request'])
        closed=self.runtime.reconcile_lifecycle('root-close',observed,self.actor)
        self.assertEqual(closed['status'],'closed')
        confirmation=closed['close_confirmation']
        self.assertTrue(self.runtime.verify_close_confirmation(confirmation)['verified'])
        before=self.runtime.path.read_bytes()
        self.assertEqual(self.runtime.reconcile_lifecycle('root-close',observed,self.actor),closed)
        self.assertEqual(before,self.runtime.path.read_bytes())
        with self.assertRaises(ValueError):self.runtime.release_receipt('root-close',self.actor)
        adapter,calls=self.adapter({})
        result=self.runtime.reclaim_development('A','reclaim',proof,adapter,self.remove,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed',result);self.assertFalse(tree.exists());self.assertEqual(calls,[])
        self.assertIsNone(result['release_receipt'])
        self.assertEqual(result['close_confirmation'],confirmation)
        self.assertEqual(result['plugin_quota'],'not_reserved')
        self.assertFalse(load_bound(confirmation)['platform_capacity_count_known'])

    def test_timeout_can_reconcile_original_close_later_without_another_close_claim(self):
        proof,_=self.ready();prepared=self.prepare(proof);self.runtime.claim_native_close('root-close',self.actor,authorized=True)
        observed=self.observation(prepared['native_request'],after='running')
        result=self.runtime.reconcile_lifecycle('root-close',observed,self.actor)
        self.assertEqual(result['status'],'unknown')
        self.assertFalse(self.runtime.claim_native_close('root-close',self.actor,authorized=True)['dispatch_allowed'])
        later=self.observation(prepared['native_request'])
        self.assertEqual(self.runtime.reconcile_lifecycle('root-close',later,self.actor)['status'],'closed')

    def test_stale_target_or_new_active_work_blocks_root_claim_before_host_call(self):
        proof,_=self.ready();self.prepare(proof);state=self.runtime._load()
        for change in ('route','generation','active_children','protected'):
            bad=deepcopy(state);executor=bad['runtime']['executors']['worker-A']
            if change=='route':executor['routes']['native_agent']['target_id']='other-agent'
            elif change=='generation':executor['generation']=2
            elif change=='active_children':executor['active_children']=['active-child']
            else:bad['runtime']['resources']['A:agent']['protected']=True
            write_atomic(self.runtime.path,bad);before=self.runtime.path.read_bytes()
            with self.assertRaises(ValueError):self.runtime.claim_native_close('root-close',self.actor,authorized=True)
            self.assertEqual(before,self.runtime.path.read_bytes())
        write_atomic(self.runtime.path,state)

    def test_wrong_controller_target_or_tampered_raw_result_cannot_close_lease(self):
        proof,_=self.ready();prepared=self.prepare(proof);self.runtime.claim_native_close('root-close',self.actor,authorized=True)
        observed=self.observation(prepared['native_request'],controller_id='not-root')
        with self.assertRaises(ValueError):self.runtime.reconcile_lifecycle('root-close',observed,self.actor)
        observed=self.observation(prepared['native_request'])
        raw=load_bound(observed);raw['wait']['arguments']['targets']=['other-agent']
        wrong=self.json('wrong-target.json',raw)
        with self.assertRaises(ValueError):self.runtime.reconcile_lifecycle('root-close',wrong,self.actor)
        Path(raw['close']['result']['path']).write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):self.runtime.reconcile_lifecycle('root-close',observed,self.actor)
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'closing')
