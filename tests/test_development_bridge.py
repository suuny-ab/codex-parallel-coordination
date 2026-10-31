"""Real isolated RESOURCE quota/files; cancellation and native close are fixtures."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'skills/isolated-parallel-development-coordination/scripts'))
import test_combination as combined
import test_core_runtime as core_tests
import test_development_lifecycle as lifecycle_tests
from codex_parallel_coordination.resources.support import cleanup_verified
from evidence import load_bound, write_atomic


class CancelledCombination(core_tests.CoreFixture):
    set_up_resources=combined.Combination.set_up_resources
    register=combined.Combination.register
    close_fixture=core_tests.CoreRuntimeTests.close_fixture
    cancellation=lifecycle_tests.DevelopmentLifecycleTests.cancellation

    def setUp(self):
        super().setUp()
        self.set_up_resources()

    def test_cancelled_task_reclaims_real_quota_and_owned_directory_once(self):
        preservation,_=self.cancellation()
        quota=self.controller.capacity_acquire(self.json('quota.json',dict(request_id='request-original-run',
            project_id=self.runtime.project_id,task_id='A',attempt_id='original-run',launch_identity='worker-A',
            resource_class='agent_execution_slot'))['path'])
        state=self.runtime._load()
        state['runtime']['operations']['original-run'].update(permit_id=quota['permit_id'],launch_identity='worker-A')
        write_atomic(self.runtime.path,state)
        path=self.managed/'environment'; path.mkdir(); (path/'temp.txt').write_text('fixture',encoding='utf-8')
        self.register('environment',path)
        adapter,calls=self.adapter({'close_agent':{'closed':True}})
        result=self.runtime.reclaim_development('A','reclaim',preservation,adapter,self.bridge.cleanup,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed'); self.assertFalse(path.exists()); self.assertEqual(len(calls),1)
        flags=self.runtime.verify_release_receipt(load_bound(result['release_receipt']),result['release_receipt'])
        self.assertTrue(flags['task_cancelled_verified']); self.assertFalse(flags['task_completed_on_main'])
        cleanup_verified(flags)
        released=self.bridge.release_agent_quota('reclaim:agent',self.actor)
        self.assertTrue(released['released'])
        again=self.runtime.reclaim_development('A','another-round',preservation,adapter,
            lambda _:self.fail('cleaned resource repeated'),authorized=True)
        self.assertEqual(again['status'],'cleanup_completed'); self.assertEqual(len(calls),1)
        self.assertTrue(self.bridge.release_agent_quota('reclaim:agent',self.actor)['duplicate'])

    def test_cancellation_flags_cannot_masquerade_as_main_completion(self):
        flags=dict(verified=True,preservation_valid=True,no_active_work=True,no_active_descendants=True,
                   no_release_dependencies=True,exclusive_task_resource=True,cleanup_authorized=True,protected=False,
                   task_kind='code',task_cancelled_verified=True,task_completion_verified=False,
                   task_completed_on_main=False,read_only_completion_verified=False)
        cleanup_verified(flags)
        for key in ('task_completion_verified','task_completed_on_main','read_only_completion_verified'):
            with self.subTest(key=key),self.assertRaises(ValueError): cleanup_verified(dict(flags,**{key:True}))
        for key in ('task_cancelled_verified','preservation_valid','cleanup_authorized'):
            with self.subTest(key=key),self.assertRaises(ValueError): cleanup_verified(dict(flags,**{key:False}))


if __name__=='__main__':unittest.main()
