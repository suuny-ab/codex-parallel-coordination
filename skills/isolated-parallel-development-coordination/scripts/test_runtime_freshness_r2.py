"""Independent-review findings: reject stale identity evidence before side effects."""
from pathlib import Path

import test_core_runtime as core_tests
from evidence import load


class RuntimeFreshnessTests(core_tests.CoreFixture):
    def test_empty_registry_first_route_must_be_complete_without_mutation(self):
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):
            self.runtime.bind_executor('executor',{'native_agent':{}},self.actor,self.file('authority.txt'))
        self.assertEqual(before,self.runtime.path.read_bytes())
        self.assertEqual(self.runtime._load()['runtime']['executors'],{})

    def setup_reuse(self):
        self.prepare(owner='worker-A');self.bind()
        state=self.runtime._load()
        executor=state['runtime']['executors']['worker-A']
        adapter,calls=self.adapter({'send_input':{'submission_id':'must-not-send'},'wait_agent':{'status':{'native-A':'running'}}})
        return executor,adapter,calls

    def attempt(self,adapter,attempt='new-attempt'):
        return self.runtime.launch('A',adapter,attempt_id=attempt,admission=self.permit(attempt=attempt),
            authorized=True,message='same fixture scope',executor_id='worker-A')

    def test_changed_bound_observation_rejects_before_send_and_running(self):
        executor,adapter,calls=self.setup_reuse()
        path=Path(executor['observation']['path'])
        facts=load(path);facts.update(execution='running',active_children=['child'])
        path.write_text(__import__('json').dumps(facts),encoding='utf-8')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.attempt(adapter)
        self.assertEqual(calls,[]);self.assertEqual(before,self.runtime.path.read_bytes())

    def test_missing_bound_observation_rejects_without_any_host_call(self):
        executor,adapter,calls=self.setup_reuse();Path(executor['observation']['path']).unlink()
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.attempt(adapter)
        self.assertEqual(calls,[]);self.assertEqual(before,self.runtime.path.read_bytes())

    def test_changed_binding_authority_rejects_without_any_host_call(self):
        executor,adapter,calls=self.setup_reuse();Path(executor['authority']['path']).write_text('revoked',encoding='utf-8')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.attempt(adapter)
        self.assertEqual(calls,[]);self.assertEqual(before,self.runtime.path.read_bytes())

    def test_first_route_rejects_nonstring_blank_and_missing_host_or_target(self):
        for route in ({'host_id':'h'}, {'target_id':'t'}, {'host_id':' ','target_id':'t'}, {'host_id':'h','target_id':4}):
            with self.subTest(route=route):
                before=self.runtime.path.read_bytes()
                with self.assertRaises(ValueError):self.runtime.bind_executor('bad',{'app_thread':route},self.actor,self.file('authority.txt'))
                self.assertEqual(before,self.runtime.path.read_bytes())

    def test_late_binding_cannot_take_executor_from_another_unfinished_task(self):
        self.prepare('A',owner='worker-A');self.prepare('B');self.bind()
        adapter,calls=self.adapter({'send_input':{'submission_id':'must-not-send'},'wait_agent':{'status':{'native-A':'running'}}})
        before=self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'unfinished'):
            self.runtime.launch('B',adapter,attempt_id='other-task',admission=self.permit(task='B',attempt='other-task',executor='worker-A'),
                authorized=True,message='different task',executor_id='worker-A')
        self.assertEqual(calls,[]);self.assertEqual(before,self.runtime.path.read_bytes())

    def test_late_binding_allows_an_executor_whose_other_task_is_completed(self):
        self.prepare('A',owner='worker-A');self.reload()
        self.state['tasks']['A'].update(phase='completed',merge=dict(verified=True,head_sha='a'*40));self.store()
        self.prepare('B');self.bind()
        adapter,calls=self.adapter({'send_input':{'submission_id':'new-task'},'wait_agent':{'status':{'native-A':'running'}}})
        result=self.runtime.launch('B',adapter,attempt_id='after-completed',admission=self.permit(task='B',attempt='after-completed',executor='worker-A'),
            authorized=True,message='separately authorized new task',executor_id='worker-A')
        self.assertEqual(result['status'],'running');self.assertEqual(len(calls),2)
