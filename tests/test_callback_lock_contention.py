"""Owned temporary ledgers; real competing lock contexts, synthetic native replies."""
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch

import test_delivery_continuation as continuation
import test_dispatch
from coordination import writer_lock
from evidence import load_bound, ref


class CleanupContention(unittest.TestCase):
    def test_same_operation_reconciler_can_finish_before_producer_relocks(self):
        case=continuation.DeliveryContinuationTests('test_repeated_calls_and_one_native_claim_close_and_clean_once')
        case.setUp();self.addCleanup(case.doCleanups)
        import cleanup
        capture=cleanup.callback_result;reconciled=[]
        def returned(directory,binding,**options):
            reference=capture(directory,binding,**options)
            if options.get('capture'):
                reconciled.append(case.runtime.cleanup_task('A','finish-A:files',case.proof,
                    lambda _:self.fail('original effect must not repeat'),case.actor,authorized=True,scope='fixture_only')['status'])
            return reference
        with patch.object(cleanup,'callback_result',side_effect=returned):result=case.close()
        self.assertEqual(reconciled,['cleanup_completed'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(case.calls),1)
        self.assertEqual(case.runtime._load()['runtime']['operations']['finish-A:files']['status'],'cleanup_completed')

    def exercise(self, unknown=False):
        case=continuation.DeliveryContinuationTests('test_repeated_calls_and_one_native_claim_close_and_clean_once')
        case.setUp();self.addCleanup(case.doCleanups)
        runtime=case.runtime;original=case.bridge.cleanup;save=runtime._save
        locks=ExitStack();self.addCleanup(locks.close)
        active=False;writes=[];actual={};lock_bytes=None
        def watched(state,operation):
            if active:writes.append(operation['op'])
            return save(state,operation)
        runtime._save=watched
        def collision(resource):
            nonlocal active,lock_bytes
            result=original(resource);actual.update(resource=resource,result=result)
            locks.enter_context(writer_lock(runtime.path));active=True
            state=runtime._load();state['unrelated_fixture_fact']='must_survive_held_writer'
            runtime._save(state,dict(op='fixture-other-writer-still-holds-lock'))
            lock_bytes=runtime.path.with_suffix('.json.lock').read_bytes()
            return dict(status='unknown',reason='fixture outcome uncertain',evidence=result['evidence']) if unknown else result
        case.bridge.cleanup=collision
        with self.assertRaisesRegex(ValueError,'writer lock exists'):case.close()
        self.assertEqual(runtime._load().get('unrelated_fixture_fact'),'must_survive_held_writer')
        self.assertEqual(writes,['fixture-other-writer-still-holds-lock'],'cleanup saved without owning the lock')
        self.assertEqual(runtime.path.with_suffix('.json.lock').read_bytes(),lock_bytes)
        self.assertEqual(len(case.calls),1);self.assertFalse(case.tree.exists())
        state=runtime._load();operation=state['runtime']['operations']['finish-A:files']
        self.assertEqual(operation['status'],'intent')
        self.assertIn(state['runtime']['resources']['A:worktree']['cleanup_status'],('intent','unknown'))
        binding=operation['callback_captures']['A:worktree']
        from evidence import callback_result
        captured=callback_result(runtime.directory,binding)
        self.assertIsNotNone(captured)
        self.assertEqual(load_bound(captured)['result']['status'],'unknown' if unknown else 'cleaned')
        active=False;locks.close()
        case.bridge.cleanup=lambda _:self.fail('successful or uncertain delete must not repeat')
        resumed=case.run_flow()
        if unknown:
            self.assertNotEqual(resumed['status'],'completed')
            self.assertNotEqual(case.run_flow()['status'],'completed')
            self.assertNotEqual(runtime._load()['runtime']['resources']['A:worktree']['cleanup_status'],'cleaned')
            runtime.reconcile_cleanup('A','A:worktree',case.f.json('original-delete-observed.json',dict(
                resource_id='A:worktree',status='cleaned',evidence=[actual['result']['evidence']])),case.actor)
            resumed=case.run_flow()
        self.assertEqual(resumed['status'],'completed',resumed)
        self.assertEqual(runtime._load().get('unrelated_fixture_fact'),'must_survive_held_writer')
        self.assertEqual(case.run_flow()['status'],'completed');self.assertEqual(len(case.calls),1)

    def test_held_writer_result_survives_and_original_round_recovers_without_redelete(self):self.exercise()

    def test_held_writer_unknown_result_waits_for_original_observation_without_redelete(self):self.exercise(unknown=True)


class LaunchContention(unittest.TestCase):
    def exercise(self, stage='wait_agent', load_failure=False):
        f=test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        f.setUp();self.addCleanup(f.doCleanups)
        runtime=f.runtime;original=f.invoke;load=runtime._load;save=runtime._save
        locks=ExitStack();self.addCleanup(locks.close)
        active=False;fail_load=False;writes=[];snapshots=[]
        def watched(state,operation):
            if active:writes.append(operation['op'])
            snapshots.append((operation['op'],state.get('unrelated_fixture_fact')))
            return save(state,operation)
        runtime._save=watched
        def loading():
            nonlocal fail_load
            if fail_load:
                fail_load=False;raise ValueError('fixture fresh load failed after callback')
            return load()
        runtime._load=loading
        def collision(tool,args):
            nonlocal active,fail_load
            result=original(tool,args)
            if tool==stage:
                locks.enter_context(writer_lock(runtime.path));active=True
                state=load();state['unrelated_fixture_fact']='must_survive_native_return'
                runtime._save(state,dict(op='fixture-native-other-writer'))
                if load_failure:
                    active=False;locks.close();fail_load=True
            return result
        f.developer.invoke=collision
        with self.assertRaisesRegex(ValueError,'fresh load failed' if load_failure else 'writer lock exists'):f.run_dispatch()
        self.assertEqual(load().get('unrelated_fixture_fact'),'must_survive_native_return')
        self.assertEqual(writes,['fixture-native-other-writer'])
        if not load_failure:self.assertTrue(runtime.path.with_suffix('.json.lock').exists())
        active=False;locks.close()
        state=load();operation=state['runtime']['operations']['dispatch-A:launch']
        self.assertIn(operation['status'],('intent','accepted_unconfirmed'))
        self.assertNotEqual(state['tasks']['A']['worker_run']['status'],'running')
        binding=operation['callback_captures']['observe' if stage=='wait_agent' else 'spawn']
        from evidence import callback_result
        captured=callback_result(runtime.directory,binding)
        self.assertIsNotNone(captured);self.assertEqual(load_bound(captured)['result']['status'],'accepted')
        before=len(f.calls)
        result=f.run_dispatch();self.assertIn(result['status'],('intent','accepted_unconfirmed'))
        self.assertEqual(len(f.calls),before)
        if stage=='wait_agent':
            plan=runtime.handoff('A','integration-fixture',available_tools=['multi_agent_v1__wait_agent'])
            observation=f.json('actual-original-wait.json',dict(schema='task-handoff-observation/1',binding=plan['binding'],
                tool=plan['host_call']['tool'],arguments=plan['host_call']['arguments'],result=f.replies['wait_agent']))
            self.assertEqual(runtime.handoff('A','integration-fixture',observation_ref=observation)['status'],'running')
            self.assertEqual(f.run_dispatch()['status'],'dispatched')
            self.assertEqual([tool for tool,_ in f.calls],['spawn_agent','wait_agent','send_input'])
        self.assertEqual(load().get('unrelated_fixture_fact'),'must_survive_native_return')

    def test_observation_return_while_other_writer_holds_lock_never_saves_old_state(self):self.exercise()

    def test_spawn_return_while_other_writer_holds_lock_retains_result_without_resend(self):self.exercise(stage='spawn_agent')

    def test_observation_fresh_load_failure_never_saves_old_state(self):self.exercise(load_failure=True)


if __name__=='__main__':unittest.main()
