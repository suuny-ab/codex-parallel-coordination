"""Real removal only of registered temporary test files below CORE's temp root."""
from copy import deepcopy
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3

from test_coordination import LedgerFixture
import test_core_runtime as core_tests
from evidence import load, ref, write_atomic


class CleanupTests(core_tests.CoreFixture):
    def close_fixture(self,merged=True,close_agent=True):
        proof=core_tests.CoreRuntimeTests.close_fixture(self,merged)
        if merged and close_agent:
            adapter,_=self.adapter({'close_agent':{'closed':True}})
            self.runtime.lifecycle('close','worker-A','A','fixture-agent-close',1,adapter,preservation_ref=proof,authorized=True)
            self.runtime.release_receipt('fixture-agent-close',self.actor)
        return proof

    def managed(self,identity,kind='environment',**extra):
        root=Path(self.runtime._load()['runtime']['cleanup_root']);root.mkdir(parents=True,exist_ok=True)
        path=root/identity
        if kind=='database':
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE fixture(id INTEGER)');db.commit()
        elif kind!='agent':path.mkdir();(path/'fixture.txt').write_text('synthetic',encoding='utf-8')
        spec=dict(id=identity,kind=kind,path=str(path),**extra)
        if kind=='agent':spec.update(executor_id='worker-A',generation=1)
        self.runtime.register_resource('A',spec,self.file(identity+'-creation-proof'),self.actor)
        return path

    def remove(self,resource):
        path=Path(resource['path']).resolve();root=Path(self.runtime._load()['runtime']['cleanup_root']).resolve()
        self.assertNotEqual(path,root);self.assertTrue(path.is_relative_to(root))
        if path.is_dir():shutil.rmtree(path)
        elif path.exists():path.unlink()
        return dict(status='cleaned',evidence=self.file(resource['id']+'-removed.txt','Scoped temporary removal confirmed'))

    def test_merge_then_preserve_then_real_isolated_database_environment_cleanup(self):
        proof=self.close_fixture();env=self.managed('environment');db=self.managed('database','database')
        result=self.runtime.cleanup_task('A','cleanup-1',proof,self.remove,self.actor,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed');self.assertFalse(env.exists());self.assertFalse(db.exists())
        state=self.runtime._load();self.assertEqual(state['tasks']['A']['phase'],'completed')
        self.assertEqual(state['tasks']['A']['cleanup']['status'],'cleanup_completed')
        again=self.runtime.cleanup_task('A','cleanup-1',proof,lambda r:self.fail('duplicate side effect'),self.actor,authorized=True)
        self.assertEqual(again,result)

    def test_premerge_and_no_cleanup_authority_never_call_effect(self):
        proof=self.close_fixture(merged=False);path=self.managed('retain')
        with self.assertRaises(ValueError):self.runtime.cleanup_task('A','bad',proof,lambda r:self.fail('premature cleanup'),self.actor,authorized=True)
        self.assertTrue(path.exists())
        with self.assertRaises(ValueError):self.runtime.cleanup_task('A','bad2',proof,lambda r:self.fail('no authority'),self.actor)

    def test_unknown_partial_cleanup_preserves_task_and_requires_observation_before_retry(self):
        proof=self.close_fixture();first=self.managed('first');second=self.managed('second','database');calls=[]
        def partial(resource):
            calls.append(resource['id'])
            if resource['id']=='second':raise TimeoutError('unknown physical outcome')
            return self.remove(resource)
        result=self.runtime.cleanup_task('A','partial',proof,partial,self.actor,authorized=True)
        self.assertEqual(result['status'],'pending_cleanup');self.assertFalse(first.exists());self.assertTrue(second.exists())
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'completed')
        self.runtime.cleanup_task('A','another',proof,lambda r:self.fail('unknown must not retry'),self.actor,authorized=True)
        observed=self.json('retained-observation.json',dict(resource_id='second',status='retained',evidence=[self.file('exists-proof')]))
        self.runtime.reconcile_cleanup('A','second',observed,self.actor)
        final=self.runtime.cleanup_task('A','after-observation',proof,self.remove,self.actor,authorized=True)
        self.assertEqual(final['status'],'cleanup_completed');self.assertFalse(second.exists())
        self.assertEqual(calls,['first','second'])

    def test_known_failure_retries_only_new_operation_and_preserves_old_result(self):
        proof=self.close_fixture();path=self.managed('retry')
        first=self.runtime.cleanup_task('A','failed',proof,lambda r:dict(status='failed',reason='safe refusal'),self.actor,authorized=True)
        second=self.runtime.cleanup_task('A','retry-new',proof,self.remove,self.actor,authorized=True)
        self.assertEqual(first['status'],'pending_cleanup');self.assertEqual(second['status'],'cleanup_completed')
        self.assertEqual(self.runtime._load()['runtime']['operations']['failed']['status'],'pending_cleanup')
        self.assertFalse(path.exists())

    def test_shared_reference_and_protected_resources_are_not_removed(self):
        proof=self.close_fixture();protected=self.managed('protected',protected=True)
        self.reload();self.human('unrelated')
        # Register a different task which has not completed; ownership cannot erase its reference.
        self.dispatch('B');self.store()
        shared=self.managed('shared',references=['A','B'],shared=True)
        result=self.runtime.cleanup_task('A','protected',proof,lambda r:self.fail('protected or referenced'),self.actor,authorized=True)
        self.assertEqual(result['status'],'pending_cleanup');self.assertTrue(shared.exists());self.assertTrue(protected.exists())

    def test_shared_unused_proof_is_explicit_and_post_merge_only(self):
        proof=self.close_fixture();unused=self.json('unused.json',dict(resource_id='shared',active_references=[]))
        path=self.managed('shared',shared=True,unused_proof=unused)
        self.assertEqual(self.runtime.cleanup_task('A','shared-close',proof,self.remove,self.actor,authorized=True)['status'],'cleanup_completed')
        self.assertFalse(path.exists())

    def test_agent_completed_response_is_not_close_or_release(self):
        proof=self.close_fixture(close_agent=False);self.managed('agent','agent')
        result=self.runtime.cleanup_task('A','agent',proof,lambda r:dict(status='cleaned',evidence=self.file('agent-report')),self.actor,authorized=True)
        self.assertEqual(result['status'],'pending_cleanup')
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'open')
        adapter,_=self.adapter({'close_agent':{'closed':True}})
        self.runtime.lifecycle('close','worker-A','A','canonical-close',1,adapter,preservation_ref=proof,authorized=True)
        self.runtime.release_receipt('canonical-close',self.actor)
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'closed')
        final=self.runtime.cleanup_task('A','agent-after-observed',proof,lambda r:self.fail('already observed closed'),self.actor,authorized=True)
        self.assertEqual(final['status'],'cleanup_completed')

    def test_unclosed_registered_agent_keeps_cleanup_pending_even_with_no_files(self):
        proof=self.close_fixture(close_agent=False)
        result=self.runtime.cleanup_task('A','no-files',proof,lambda r:self.fail('no file resources'),self.actor,authorized=True)
        self.assertEqual(result['status'],'pending_cleanup')
        self.assertEqual(result['agent_context'],'not_confirmed_closed')

    def test_resource_verifier_reads_actual_canonical_intent_under_core_writer_lock(self):
        proof=self.close_fixture();self.managed('bridge')
        seen=[]
        def invoke(resource):
            before=self.runtime.path.read_bytes()
            context=dict(resource,resource_id=resource['id'])
            flags=self.runtime.verify_cleanup_receipt(context,resource['cleanup_proof'])
            self.assertTrue(flags['verified']);self.assertTrue(flags['cleanup_authorized'])
            self.assertFalse(self.runtime.verify_cleanup_receipt(dict(context,attempt_id='wrong'),resource['cleanup_proof'])['verified'])
            self.assertEqual(before,self.runtime.path.read_bytes())
            seen.append((context,resource['cleanup_proof']))
            return self.remove(resource)
        result=self.runtime.cleanup_task('A','actual-guard',proof,invoke,self.actor,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed');self.assertEqual(len(seen),1)
        self.assertFalse(self.runtime.verify_cleanup_receipt(*seen[0])['verified'])

    def test_registration_refuses_unmanaged_absolute_path_without_touching_it(self):
        self.close_fixture();outside=self.root/'not-managed';outside.mkdir()
        with self.assertRaises(ValueError):self.runtime.register_resource('A',dict(id='bad',kind='worktree',path=str(outside)),self.file('registration-authority.txt'),self.actor)
        self.assertTrue(outside.exists())

    def test_windows_open_database_is_pending_until_handle_closed_and_effect_reconciled(self):
        if os.name!='nt':self.skipTest('Windows open-file deletion semantics only')
        proof=self.close_fixture();path=self.managed('held','database')
        with closing(sqlite3.connect(path)) as db:
            db.execute('SELECT * FROM fixture').fetchall()
            first=self.runtime.cleanup_task('A','held-db',proof,self.remove,self.actor,authorized=True)
            self.assertEqual(first['status'],'pending_cleanup');self.assertTrue(path.exists())
        observed=self.json('held-retained.json',dict(resource_id='held',status='retained',evidence=[self.file('held-now-closed')]))
        self.runtime.reconcile_cleanup('A','held',observed,self.actor)
        result=self.runtime.cleanup_task('A','held-after-close',proof,self.remove,self.actor,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed');self.assertFalse(path.exists())

    def test_release_dependency_or_active_child_prevents_all_resource_effects(self):
        proof=self.close_fixture();path=self.managed('keep');baseline=self.runtime._load()
        for source,key,value in [('task','publication_dependencies',['frozen-release-needs-record']),('executor','active_children',['child'])]:
            state=deepcopy(baseline);target=state['tasks']['A'] if source=='task' else state['runtime']['executors']['worker-A']
            target[key]=value;write_atomic(self.runtime.path,state)
            with self.assertRaises(ValueError):self.runtime.cleanup_task('A',key,proof,lambda r:self.fail('dependency'),self.actor,authorized=True)
            self.assertTrue(path.exists())
