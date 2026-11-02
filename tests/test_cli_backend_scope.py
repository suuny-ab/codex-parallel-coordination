"""Backend grants at the real Bridge seam; hosts and task facts are disposable fixtures."""
from copy import deepcopy
import hashlib
import unittest

import test_real_use_closeout as cli_tests
import test_resource_flow as flow_tests
from evidence import write_atomic


class BackendScopeTests(unittest.TestCase):
    def fixture(self, changes=None, *, missing=(), plain=False):
        f=flow_tests.ResourceFlowTests('test_D04_failed_native_to_authorized_managed_CLI_keeps_original_operations_and_single_author')
        f.setUp();self.addCleanup(f.doCleanups)
        command,log=cli_tests.RealUseTests.cli(f)
        state=f.runtime._load();task=state['tasks']['C']
        state['runtime']['operations']['original-native']=dict(operation='spawn',status='capacity',executor_id=None,
            task_id='C',attempt_id='original-native',request_id='original-request',permit_id='original-permit')
        write_atomic(f.runtime.path,state)
        grant=dict(source='direct user message in original main conversation',main_conversation=f.actor,
            task_id='C',backend='codex_exec',worktree=task['worktree'],branch=task['branch'],quote='Only this original task may use CLI')
        grant.update(changes or {})
        for name in missing:grant.pop(name)
        if grant.get('previous_scope')=='other':grant['previous_scope']=f.json('other-scope.json',dict(task_id='D'))
        if grant.get('previous_scope')=='original':grant['previous_scope']=command['approval']
        backend=f.file('backend.txt','unscoped approval text') if plain else f.json('backend.json',grant)
        command.update(backend_authority=backend,prior_operation_id='original-native')
        admission=f.bridge.admit(f.json('cli-capacity.json',dict(request_id='cli-capacity',project_id=f.runtime.project_id,
            task_id='C',attempt_id='cli-attempt',launch_identity='codex_exec:cli-attempt',resource_class='agent_execution_slot')))
        command['launch']=f.json('managed-launch.json',dict(schema='observed-managed-codex-exec-launch/1',backend='codex_exec',
            origin_mode='observed_codex_exec',thread_id='actual-cli',task_id='C',attempt_id='cli-attempt',
            process_identity=dict(pid=12345,created=123456789),job_name='fixture-job',admission=admission['admission'],
            authority=backend,worktree=task['worktree'],branch=task['branch'],stdout_path=log['path']))
        origin=dict(role='user',conversation_id=f.actor,reference=backend)
        actual=dict(thread_id='actual-cli',pid=12345,worktree=task['worktree'],backend='codex_exec',
            job_name='fixture-job',origin='independent_codex_process')
        return f,command,origin,actual

    @staticmethod
    def files(f):
        return {p.relative_to(f.root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in f.root.rglob('*') if p.is_file()}

    def rejected(self, **options):
        f,command,origin,actual=self.fixture(**options);before=self.files(f);calls=[]
        def observe(value):calls.append(deepcopy(value));return actual
        with self.assertRaises(ValueError):f.bridge.bind_cli(command,'integration',user_origin=origin,observe_cli=observe)
        self.assertEqual(calls,[],'invalid grant must be rejected before the observer')
        self.assertEqual(self.files(f),before,'rejection must not change ledger/controller/receipt files')

    def test_other_task_grant_rejected_before_observation_or_write(self):
        self.rejected(changes=dict(task_id='D'))

    def test_wrong_backend_grant_rejected_before_observation_or_write(self):
        self.rejected(changes=dict(backend='native_agent'))

    def test_wrong_worktree_grant_rejected_before_observation_or_write(self):
        self.rejected(changes=dict(worktree='C:/wrong-task-tree'))

    def test_wrong_branch_grant_rejected_before_observation_or_write(self):
        self.rejected(changes=dict(branch='codex/other-task'))

    def test_wrong_original_main_or_user_source_rejected_before_observation_or_write(self):
        for changes in [dict(main_conversation='other-main'),dict(source='self-signed report'),dict(quote='')]:
            with self.subTest(changes=changes):self.rejected(changes=changes)

    def test_wrong_continuation_scope_rejected_before_observation_or_write(self):
        self.rejected(changes=dict(previous_scope='other'))

    def test_missing_scope_fields_or_plain_text_rejected_before_observation_or_write(self):
        for name in ['task_id','backend','worktree','branch','main_conversation','source','quote']:
            with self.subTest(missing=name):self.rejected(missing=[name])
        self.rejected(plain=True)

    def test_missing_backend_reference_or_wrong_actual_user_origin_rejected_before_observation(self):
        for field in ['backend_authority','conversation_id']:
            with self.subTest(field=field):
                f,command,origin,actual=self.fixture()
                if field=='backend_authority':command.pop(field)
                else:origin[field]='other-main'
                before=self.files(f);calls=[]
                with self.assertRaises(ValueError):
                    f.bridge.bind_cli(command,'integration',user_origin=origin,observe_cli=lambda _:calls.append('observe') or actual)
                self.assertEqual(calls,[]);self.assertEqual(self.files(f),before)

    def test_correct_original_scope_binds_once_and_reentry_does_not_observe(self):
        f,command,origin,actual=self.fixture(changes=dict(previous_scope='original'));calls=[]
        result=f.bridge.bind_cli(command,'integration',user_origin=origin,observe_cli=lambda _:calls.append('observe') or actual)
        self.assertEqual(result['status'],'bound');self.assertEqual(calls,['observe'])
        self.assertEqual(f.runtime._load()['tasks']['C']['owner_agent_id'],'actual-cli')
        before=self.files(f)
        self.assertTrue(f.bridge.bind_cli(command,'integration',user_origin=None,observe_cli=None)['duplicate'])
        self.assertEqual(self.files(f),before)
