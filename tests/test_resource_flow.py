"""RESOURCE-FLOW: disposable Git/files/processes; native and completion are fixtures."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import test_core_runtime as core_tests
import test_resource_closed_loop as resource_tests
import test_combination as combined
from evidence import load_bound, write_atomic
from coordination import writer_lock


class ResourceFlowTests(core_tests.CoreFixture):
    close_fixture = core_tests.CoreRuntimeTests.close_fixture
    repository = resource_tests.ResourceClosedLoop.repository
    new_task = resource_tests.ResourceClosedLoop.new_task
    set_up_resources = combined.Combination.set_up_resources

    def setUp(self):
        super().setUp()
        self.set_up_resources()

    def finished(self):
        self.close_fixture()
        state=self.runtime._load();state['tasks']['A']['merge']['scope']='fixture_only'
        write_atomic(self.runtime.path,state)
        self.runtime.register_resource('A', dict(id='A:agent', kind='agent', executor_id='worker-A', generation=1),
                                       self.file('agent-authority.txt'), self.actor)
        return self.runtime._load()

    def actual_archive(self, extras=()):
        """Actual file mover; task identity/merge/host authority remain fixtures."""
        from evidence import ref
        materials=self.root/'materials';materials.mkdir(exist_ok=True)
        source=materials/'A.md';source.write_text('Final confirmed fixture spec',encoding='utf-8')
        state=self.runtime._load();task=state['tasks']['A']
        task['state_contract']=dict(integrator_id=self.actor,spec=ref(source),deliverables=[ref(p) for p in extras])
        if task['phase']=='completed':
            merge=task['merge'];merge['main_sha']='c'*40
            task['source_version']=dict(head_sha=merge['head_sha'],target_sha=merge['target_sha'])
            merge['observation']=dict(project_id=self.runtime.project_id,task_id='A',source_head=merge['head_sha'],
                previous_main=merge['target_sha'],main_head=merge['main_sha'],contains_source=True,scope='fixture_only')
        write_atomic(self.runtime.path,state)
        tool=self.runtime.archive_tool(self.actor,archive_root=self.root/'archive',spec_root=materials,
            authorize=lambda *args:True,observe_stop=lambda task:dict(project_id=self.runtime.project_id,task_id='A',
                owner_agent_id=task['owner_agent_id'],stopped=True,merge_status='not_merged',scope='fixture_only'))
        return tool,source

    def test_A04_R01_R06_actual_archive_partial_then_resume_same_cleanup_without_report_graph(self):
        from archive_tool import move_original
        self.finished();auth=self.file('authority.txt')
        directory=self.managed/'deliverables';directory.mkdir();extra=directory/'result.bin';extra.write_bytes(b'actual original')
        unrelated=self.managed/'unrelated';unrelated.write_text('temporary')
        for key,path,kind in [('deliverables',directory,'environment'),('unrelated',unrelated,'artifact')]:
            self.runtime.register_resource('A',dict(id=key,kind=kind,path=str(path)),auth,self.actor)
        tool,source=self.actual_archive([extra])
        def fail_extra(a,b):
            if Path(a)==extra:raise PermissionError('explicit archive mover fault')
            move_original(a,b)
        with patch('archive_tool.move_original',side_effect=fail_extra):
            partial=tool.execute('A','archive-A',self.runtime._load()['revision'])
        self.assertEqual(partial['status'],'partial');self.assertFalse(source.exists());self.assertTrue(extra.exists())
        host,calls=self.adapter({'close_agent':{'closed':True}})
        self.runtime.lifecycle('close','worker-A','A','close',1,host,authorized=True)
        removed=[]
        def remove(resource):
            path=Path(resource['path']);path.rmdir() if path.is_dir() else path.unlink();removed.append(resource['id'])
            return dict(status='cleaned',evidence=self.file(resource['id']+'-removed.txt'))
        first=self.runtime.reclaim_resources('A','cleanup',remove,self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(first['status'],'pending_cleanup');self.assertEqual(removed,['unrelated']);self.assertTrue(directory.exists())
        finished=tool.execute('A','archive-A',self.runtime._load()['revision'])
        self.assertEqual(finished['status'],'completed');self.assertFalse(extra.exists())
        with patch('archive_tool.move_original',side_effect=AssertionError('resource must only consume archive fact')):
            result=self.runtime.reclaim_resources('A','cleanup',remove,self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'cleanup_completed',result);self.assertEqual(removed,['unrelated','deliverables'])
        self.assertEqual(len(calls),1);self.assertTrue(Path(finished['files'][1]['current']['path']).exists())

    def test_C01_actual_archive_without_author_or_resources_moves_only_final_spec(self):
        self.prepare();tool,source=self.actual_archive();self.cancel_decision('A')
        self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        result=tool.execute('A','archive-A',self.runtime._load()['revision'])
        self.assertEqual(result['status'],'completed');self.assertFalse(source.exists());self.assertEqual(len(result['files']),1)
        result=self.runtime.reclaim_resources('A','cleanup',lambda _:self.fail('no resource acquired'),self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'cleanup_completed');task=self.runtime._load()['tasks']['A']
        self.assertIsNone(task['mr']);self.assertIsNone(task['owner_agent_id'])

    def test_R01_archive_failure_does_not_block_independent_close_or_quota_release(self):
        self.finished()
        state = self.runtime._load()
        state['tasks']['A']['archive'] = dict(status='failed', reason='PermissionError')
        write_atomic(self.runtime.path, state)
        adapter, calls = self.adapter({'close_agent': {'closed': True}})
        result = self.runtime.lifecycle('close','worker-A','A','independent-close',1,adapter,
                                        preservation_ref=None,authorized=True)
        self.assertEqual(result['status'], 'closed')
        receipt = self.runtime.release_receipt('independent-close', self.actor)
        self.assertTrue(self.runtime.verify_release_receipt(load_bound(receipt), receipt)['verified'])
        self.assertEqual(self.runtime._load()['tasks']['A']['archive']['status'], 'failed')
        self.assertEqual(len(calls), 1)

    def test_R02_accepted_close_is_unknown_and_cannot_release(self):
        self.finished()
        adapter, calls = self.adapter({'close_agent': {'accepted': True}})
        result = self.runtime.lifecycle('close','worker-A','A','uncertain-close',1,adapter,
                                        preservation_ref=None,authorized=True)
        self.assertEqual(result['status'],'unknown')
        with self.assertRaises(ValueError): self.runtime.release_receipt('uncertain-close',self.actor)
        again = self.runtime.lifecycle('close','worker-A','A','uncertain-close',1,adapter,
                                       preservation_ref=None,authorized=True)
        self.assertEqual(again,result); self.assertEqual(len(calls),1)

    def test_R05_shared_active_or_changed_author_is_protected(self):
        original = self.finished()
        for key,value in [('shared',True),('protected',True),('active_children',['child']),('pending_work',True)]:
            state = deepcopy(original);state['runtime']['executors']['worker-A'][key] = value
            write_atomic(self.runtime.path,state)
            adapter,calls = self.adapter({'close_agent': {'closed':True}})
            with self.assertRaises(ValueError):
                self.runtime.lifecycle('close','worker-A','A','bad-'+key,1,adapter,authorized=True)
            self.assertEqual(calls,[])

    def test_D01_creation_runs_without_ledger_lock_and_is_registered_once(self):
        self.repository(); tree = self.new_task()
        from resource_lifecycle import create
        calls=[]
        def create_file(resource):
            with writer_lock(self.runtime.path): calls.append(resource['id'])
            Path(resource['path']).mkdir()
            return dict(attempt_id='create')
        spec=dict(id='A:env',kind='environment',path=str(self.managed/'env'))
        authority=self.file('authority.txt')
        first=create(self.runtime,'A','create',spec,authority,self.actor,create_file,authorized=True)
        second=create(self.runtime,'A','create',spec,authority,self.actor,create_file,authorized=True)
        self.assertEqual(first,second);self.assertEqual(first['status'],'registered');self.assertEqual(calls,['A:env'])

    def test_D06_actual_worktree_and_temporary_file_creation(self):
        self.repository();tree=self.new_task();authority=self.file('authority.txt')
        self.assertEqual(self.bridge.create_worktree('A','create-tree',authority,self.actor,authorized=True)['status'],'registered')
        self.assertTrue((tree/'.git').exists())
        target=self.managed/'test.sqlite'
        self.assertEqual(self.bridge.create_temporary('A','create-db','A:db','database',target,authority,self.actor,
                                                    authorized=True)['status'],'registered')
        self.assertTrue(target.exists())
        state=self.runtime._load()
        producer=state['tasks']['A']['producer_results']['resource:create-db']
        self.assertEqual(producer['ledger_revision'],state['revision'])
        self.assertTrue(any(e.get('kind')=='resource' and e.get('ledger_revision')==state['revision']
            for e in state['runtime']['stage_feedback']['events'].values()))

    def test_R07_developer_temporary_grant_does_not_grant_cleanup_or_other_tasks(self):
        self.repository()
        authority=self.json('approved-task.json',dict(schema='approved-dispatch-plan/1',approval='approved',
            source='direct user message in original main conversation',main_conversation=self.actor,
            integration_agent='delivery-worker',user_quote='Fixture task temporary grant',
            authorized=['bounded_new_task_resource_cleanup'],tasks=[dict(id='A',branch='codex/new-task',
                worktree=str(self.managed/'author'),temporary_resources=['environment'])]))
        self.new_task(authority)
        self.bridge.create_worktree('A','create-tree',authority,self.actor,authorized=True)
        self.bind()
        state=self.runtime._load();state['tasks']['A']['owner_agent_id']='worker-A'
        state['tasks']['A']['worker_run']['owner_agent_id']='worker-A'
        write_atomic(self.runtime.path,state)
        result=self.bridge.create_temporary('A','create-env','A:env','environment',self.managed/'env',authority,'worker-A',authorized=True)
        self.assertEqual(result['status'],'registered')
        before=self.runtime.path.read_bytes()
        with patch.object(self.artifacts,'observe',side_effect=AssertionError('unauthorized observation')):
            with self.assertRaises(ValueError):self.bridge.reconcile_cleanup('A','A:env','worker-A')
        self.assertEqual(self.runtime.path.read_bytes(),before)
        with self.assertRaises(ValueError):
            self.bridge.create_temporary('A','create-db','A:db','database',self.managed/'db',authority,'worker-A',authorized=True)
        with self.assertRaises(ValueError):
            self.bridge.create_worktree('A','another-tree',authority,'worker-A',authorized=True)
        with self.assertRaises(ValueError):
            self.runtime.cleanup_task('A','bad',None,lambda _:None,'worker-A',authorized=True)


    def test_D04_failed_native_to_authorized_managed_CLI_keeps_original_operations_and_single_author(self):
        import test_real_use_closeout as cli_tests
        command,log=cli_tests.RealUseTests.cli(self)
        state=self.runtime._load();key='C';task=state['tasks'][key]
        original=dict(operation='spawn',status='capacity',executor_id=None,task_id=key,
            attempt_id='original-native',request_id='original-request',permit_id='original-permit')
        state['runtime']['operations']['original-native']=deepcopy(original)
        write_atomic(self.runtime.path,state)
        backend=self.json('backend-grant.json',dict(source='direct user message in original main conversation',
            main_conversation=self.actor,task_id=key,backend='codex_exec',worktree=task['worktree'],branch=task['branch'],
            quote='Additional user approval for this original task CLI backend'))
        command.update(backend_authority=backend,prior_operation_id='original-native')
        cli_admission=self.bridge.admit(self.json('cli-capacity.json',dict(request_id='cli-capacity',project_id=self.runtime.project_id,
            task_id=key,attempt_id='cli-attempt',launch_identity='codex_exec:cli-attempt',resource_class='agent_execution_slot')))
        command['launch']=self.json('managed-launch.json',dict(schema='observed-managed-codex-exec-launch/1',
            backend='codex_exec',origin_mode='observed_codex_exec',thread_id='actual-cli',task_id=key,attempt_id='cli-attempt',
            process_identity=dict(pid=12345,created=123456789),job_name='fixture-job',admission=cli_admission['admission'],
            authority=backend,worktree=task['worktree'],branch=task['branch'],stdout_path=log['path']))
        with self.assertRaises(ValueError):self.runtime.real_use(command,'integration')
        actual=dict(thread_id='actual-cli',pid=12345,worktree=task['worktree'],backend='codex_exec',
                    job_name='fixture-job',origin='independent_codex_process')
        def observe(command):
            with writer_lock(self.runtime.path): pass
            return actual
        result=self.bridge.bind_cli(command,'integration',user_origin=dict(role='user',conversation_id=self.actor,reference=backend),
                                   observe_cli=observe)
        self.assertEqual(result['status'],'bound')
        after=self.runtime._load()
        self.assertEqual(after['runtime']['operations']['original-native'],original)
        self.assertEqual(after['tasks'][key]['owner_agent_id'],'actual-cli')
        self.assertEqual(after['runtime']['executors']['actual-cli']['routes'],{'codex_exec':dict(host_id='local',target_id='actual-cli')})
        self.assertTrue(self.runtime.real_use(command,'integration')['duplicate'])
        with self.assertRaises(ValueError):self.runtime.real_use(dict(command,thread_id='another-author'),'integration')

    def test_D04_unknown_native_cannot_switch_to_CLI_even_with_backend_grant(self):
        import test_real_use_closeout as cli_tests
        command,_=cli_tests.RealUseTests.cli(self)
        state=self.runtime._load();state['runtime']['operations']['original-native']=dict(operation='spawn',task_id='C',status='unknown')
        write_atomic(self.runtime.path,state);before=self.runtime.path.read_bytes()
        task=state['tasks']['C']
        backend=self.json('backend-grant.json',dict(source='direct user message in original main conversation',
            main_conversation=self.actor,task_id='C',backend='codex_exec',worktree=task['worktree'],branch=task['branch'],
            quote='Additional user approval for this original task CLI backend'))
        command.update(backend_authority=backend,prior_operation_id='original-native')
        with self.assertRaisesRegex(ValueError,'unknown'):
            self.runtime.real_use(command,'integration',user_origin=dict(role='user',conversation_id=self.actor,reference=backend),
                observe_cli=lambda _:dict(thread_id='actual-cli'))
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_R03_R06_partial_cleanup_does_not_repeat_confirmed_close_or_success(self):
        self.finished()
        adapter,calls=self.adapter({'close_agent':{'closed':True}})
        self.runtime.lifecycle('close','worker-A','A','closed',1,adapter,authorized=True)
        authority=self.file('resource-authority.txt');good=self.managed/'good';good.write_text('temporary')
        bad=self.managed/'bad';bad.write_text('temporary')
        self.runtime.register_resource('A',dict(id='good',kind='artifact',path=str(good)),authority,self.actor)
        self.runtime.register_resource('A',dict(id='bad',kind='artifact',path=str(bad)),authority,self.actor)
        removed=[]
        def remove(resource):
            if resource['id']=='bad':return dict(status='failed',reason='fixture unlink permission failure')
            Path(resource['path']).unlink();removed.append(resource['id'])
            return dict(status='cleaned',evidence=self.file('removed.txt'))
        with patch('resource_flow.archive_protection',return_value=dict(verified=True,protected_paths=[])):
            first=self.runtime.reclaim_resources('A','files',remove,self.actor,authorized=True,scope='fixture_only')
            again=self.runtime.reclaim_resources('A','files',remove,self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(first['status'],'pending_cleanup');self.assertFalse(good.exists());self.assertTrue(bad.exists())
        self.assertEqual(removed,['good']);self.assertEqual(len(calls),1)
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'closed')

    def test_R05_archive_pending_protects_only_affected_directory(self):
        self.finished();adapter,_=self.adapter({'close_agent':{'closed':True}})
        self.runtime.lifecycle('close','worker-A','A','closed',1,adapter,authorized=True)
        auth=self.file('authority.txt');pending=self.managed/'pending';pending.mkdir();(pending/'spec.md').write_text('spec')
        other=self.managed/'other';other.write_text('temporary')
        for key,path,kind in [('pending',pending,'environment'),('other',other,'artifact')]:
            self.runtime.register_resource('A',dict(id=key,kind=kind,path=str(path)),auth,self.actor)
        def remove(resource):
            self.assertEqual(resource['id'],'other');other.unlink()
            return dict(status='cleaned',evidence=self.file('removed'))
        with patch('resource_flow.archive_protection',return_value=dict(verified=True,protected_paths=[str(pending/'spec.md')])):
            result=self.runtime.reclaim_resources('A','files',remove,self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['results']['pending']['status'],'retained');self.assertTrue(pending.exists());self.assertFalse(other.exists())

    def cancel_decision(self, key):
        state=self.runtime._load();state['tasks'][key]['cancellation_request']=dict(user_reference='explicit fixture user decision',reason='cancel')
        write_atomic(self.runtime.path,state)

    def test_C01_cancel_before_resources_does_not_invent_author_or_PR(self):
        self.prepare();self.cancel_decision('A')
        result=self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'cancelled')
        task=self.runtime._load()['tasks']['A'];self.assertIsNone(task['owner_agent_id']);self.assertIsNone(task['mr'])
        result=self.runtime.close_stopped_pr('A','pr',lambda _:self.fail('no PR exists'),self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['pr_status'],'not_required')
        result=self.runtime.reclaim_resources('A','files',lambda _:self.fail('no resource exists'),self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'cleanup_completed')

    def test_C04_merge_reservation_and_actual_merge_cannot_be_cancelled_as_unmerged(self):
        self.finished();self.cancel_decision('A')
        result=self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'already_merged');self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'completed')

    def test_C02_C03_real_dirty_worktree_local_remote_branch_and_extra_discard_without_backup(self):
        import subprocess
        from codex_parallel_coordination.worktrees import GitWorktrees
        self.repository();tree=self.new_task();auth=self.file('create-authority.txt')
        self.bridge.create_worktree('A','create-tree',auth,self.actor,authorized=True)
        adapter=GitWorktrees(self.runtime.project_root,self.managed)
        remote=self.root/'remote.git'
        subprocess.run(['git','-c','core.longpaths=true','init','--bare',str(remote)],check=True,capture_output=True)
        adapter._git('remote','add','origin',str(remote))
        adapter._git('push','origin','main','codex/new-task')
        self.bind();state=self.runtime._load();task=state['tasks']['A'];task['owner_agent_id']='worker-A'
        task['worker_run'].update(owner_agent_id='worker-A',status='running')
        task['mr']=dict(provider='github',id='fixture-PR',url='https://example.invalid/fixture/pull/1',
            source_branch=task['branch'],target_branch='main')
        write_atomic(self.runtime.path,state)
        self.runtime.register_resource('A',dict(id='A:agent',kind='agent',executor_id='worker-A',generation=1),auth,self.actor)
        (tree/'feature.py').write_text('discard uncommitted change')
        (tree/'new-output.txt').write_text('discard extra task output')
        extra=self.managed/'extra.txt'
        self.bridge.create_temporary('A','create-extra','A:extra','artifact',extra,auth,self.actor,authorized=True)
        self.cancel_decision('A')
        self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only',
            observe_stop=lambda task:dict(executor_id='worker-A',generation=1,execution='idle',pending_work=False,active_children=[],scope='fixture_only'),
            observe_main=lambda task:dict(task_id='A',mr=task['mr'],scope='fixture_only',merged=False))
        pr_calls=[]
        def close_pr(operation):
            with writer_lock(self.runtime.path):pr_calls.append(operation['mr']['id'])
            return dict(status='closed',evidence=self.file('pr-closed.txt'))
        self.runtime.close_stopped_pr('A','pr-close',close_pr,self.actor,authorized=True,scope='fixture_only')
        host,calls=self.adapter({'close_agent':{'closed':True}})
        self.bridge.close_developer('A','close',host,authorized=True)
        archive,source=self.actual_archive()
        archived=archive.execute('A','archive-A',self.runtime._load()['revision'])
        self.assertEqual(archived['status'],'completed');self.assertFalse(source.exists())
        result=self.bridge.reclaim_resources('A','cleanup',self.actor,authorized=True,scope='fixture_only',cleanup_remote_branch=True)
        self.assertEqual(result['status'],'cleanup_completed',result)
        self.assertFalse(tree.exists());self.assertFalse(extra.exists())
        self.assertIsNone(adapter.branch_head('codex/new-task'));self.assertIsNone(adapter.branch_head('codex/new-task',remote=True))
        self.assertEqual(adapter._git('for-each-ref','--format=%(refname)').stdout,b'refs/heads/main\nrefs/remotes/origin/main\n')
        self.assertEqual(pr_calls,['fixture-PR']);self.assertEqual(len(calls),1)

    def test_D01_STATE_registered_task_uses_same_dispatch_and_resource_entry(self):
        import test_dispatch
        fixture=test_dispatch.DispatchTests('test_actual_worktree_resource_binding_and_one_handoff_are_idempotent')
        fixture.setUp()
        try:
            request=fixture.request;runtime=fixture.runtime;state=runtime._load()
            command=dict(action='register',task_id='A',event_id='state-register',expected_revision=state['revision'],
                spec=request['requirement']['spec'],scope=request['requirement']['scope'],acceptance=request['requirement']['acceptance'],
                acceptance_type='delivery_verified',integrator_id=request['integration_agent'],rights=request['approval'] and
                    dict(commit=True,push=True,mr=True,merge=False),user_reference=request['approval']['path'])
            runtime.task_state(fixture.actor).record(command,user_origin=dict(role='user',conversation_id=fixture.actor,reference=request['approval']['path']))
            result=fixture.run_dispatch()
            self.assertEqual(result['status'],'dispatched')
            self.assertEqual(set(runtime._load()['tasks']),{'A'})
            self.assertEqual(runtime._load()['tasks']['A']['owner_agent_id'],'native_agent:native-fixture')
        finally:fixture.doCleanups()

    def test_D07_confirmed_no_author_releases_local_permit_without_native_close(self):
        self.prepare();state=self.runtime._load()
        source=self.json('capacity.json',dict(request_id='capacity',project_id=self.runtime.project_id,task_id='A',
            attempt_id='native-attempt',launch_identity='spawn:native-attempt',resource_class='agent_execution_slot'))
        admission=self.bridge.admit(source);permit=load_bound(admission['admission'])
        state['runtime']['operations']['native-attempt']=dict(operation='spawn',status='capacity',executor_id=None,task_id='A',
            attempt_id='native-attempt',request_id=permit['request_id'],permit_id=permit['permit_id'],launch_identity=permit['launch_identity'])
        write_atomic(self.runtime.path,state);self.cancel_decision('A')
        self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        adapter,calls=self.adapter({})
        result=self.bridge.close_developer('A','close',adapter,authorized=True)
        self.assertEqual(result['status'],'not_acquired');self.assertFalse(result['native_agent_closed']);self.assertEqual(calls,[])
        self.assertEqual(self.controller.read()[0]['capacity_requests']['capacity']['status'],'released')
        self.assertFalse(any(o.get('operation')=='close' for o in self.runtime._load()['runtime']['operations'].values()))

    def test_R01_ROOT_native_request_has_no_archive_dependency(self):
        self.finished()
        state=self.runtime._load();state['tasks']['A']['archive']=dict(status='failed')
        write_atomic(self.runtime.path,state)
        record=self.runtime.prepare_native_close('A','root-close',None,self.actor,authorized=True,scope='fixture_only')
        claim=self.runtime.claim_native_close('root-close',self.actor,authorized=True)
        self.assertTrue(claim['dispatch_allowed'])
        raw_close=self.json('close-result.json',dict(previous_status={'completed':'fixture result'}))
        raw_wait=self.json('wait-result.json',dict(status={'native-A':'not_found'},timed_out=False))
        facts=self.json('root-observation.json',dict(operation_id='root-close',executor_id='worker-A',generation=1,
            controller_id=self.actor,request=record['native_request'],
            close=dict(tool='multi_agent_v1__close_agent',arguments={'target':'native-A'},result=raw_close),
            wait=dict(tool='multi_agent_v1__wait_agent',arguments={'targets':['native-A'],'timeout_ms':10000},result=raw_wait)))
        self.runtime.reconcile_lifecycle('root-close',facts,self.actor)
        confirmation=self.runtime._load()['runtime']['operations']['root-close']['close_confirmation']
        self.assertTrue(self.runtime.verify_close_confirmation(confirmation)['verified'])
        self.assertEqual(self.runtime._load()['tasks']['A']['archive']['status'],'failed')

    def test_R04_actual_Job_stop_is_confirmed_before_owned_files_are_removed(self):
        import threading
        import sys
        from codex_parallel_coordination.resources import Controller,ArtifactCleanup
        from codex_parallel_coordination.bridge import ResourceBridge
        from codex_parallel_coordination.resources.windows_resources import Job,reclaim_idle_job
        self.repository();self.new_task();auth=self.file('process-authority.txt')
        policy=self.controller.read()[1]
        policy['authority']=self.json('process-policy-authority.json',dict(scope='explicit_temporary_managed_resources',
            launch=True,cleanup_artifacts=True,cleanup_task_processes=True))
        self.controller=Controller(self.root/'process-control/resources.json')
        self.controller.initialize(self.json('process-policy.json',policy)['path'])
        self.artifacts=ArtifactCleanup(self.controller)
        self.bridge=ResourceBridge(self.runtime,self.controller,self.artifacts,self.root/'process-receipts')
        target=self.managed/'database.sqlite'
        self.bridge.create_temporary('A','db','A:db','database',target,auth,self.actor,authorized=True)
        source=self.json('owned-process.json',dict(request_id='A:service',project_id=self.runtime.project_id,task_id='A',
            attempt_id='service',launch_identity='service',phase='self_test',purpose='Owned bounded fixture service',
            cwd=str(self.runtime.project_root),argv=[sys.executable,'-B','-c','import time;time.sleep(30)'],
            peak_bytes=4*1024*1024,exclusive_keys=['fixture'],resource_kind='regression',protection='running',
            reclaim_allowed=False,task_cleanup_allowed=True))
        started=threading.Event();real_start=Job.start;failures=[]
        def start(job):real_start(job);started.set()
        def launch():
            try:self.bridge.run_process('A',source,auth,self.actor,authorized=True)
            except Exception as error:failures.append(error)
        with patch.object(Job,'start',start):
            thread=threading.Thread(target=launch);thread.start()
            try:
                self.assertTrue(started.wait(10),'owned Job did not start')
                self.bridge.observe_process('A','A:service',self.actor)
                self.cancel_decision('A')
                self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
                with patch('resource_flow.archive_protection',return_value=dict(verified=True,protected_paths=[])):
                    first=self.bridge.reclaim_resources('A','cleanup',self.actor,authorized=True,scope='fixture_only')
                self.assertEqual(first['status'],'pending_cleanup');self.assertTrue(target.exists())
                thread.join(10);self.assertFalse(thread.is_alive());self.assertEqual(failures,[])
                observed=self.bridge.reconcile_cleanup('A','A:service',self.actor)
                self.assertEqual(observed['cleanup_status'],'cleaned')
                with patch('resource_flow.archive_protection',return_value=dict(verified=True,protected_paths=[])):
                    result=self.bridge.reclaim_resources('A','cleanup',self.actor,authorized=True,scope='fixture_only')
                self.assertEqual(result['status'],'cleanup_completed',result);self.assertFalse(target.exists())
            finally:
                if thread.is_alive():
                    request=self.controller.read()[0]['requests'].get('A:service',{})
                    if request.get('process'):reclaim_idle_job(request['job_name'],request['process'])
                    thread.join(10)

    def test_C04_same_cancel_operation_observes_unknown_main_then_unmerged_without_reissue(self):
        self.prepare(owner='worker-A');self.bind()
        state=self.runtime._load();state['tasks']['A']['mr']=dict(provider='github',id='fixture-PR')
        state['merge_reservation']=dict(task_id='A',operation_id='issued-merge')
        write_atomic(self.runtime.path,state);self.cancel_decision('A')
        result=self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'pending_merge_reconciliation')
        state=self.runtime._load();state['merge_reservation']=None;write_atomic(self.runtime.path,state)
        values=[None,False];calls=[]
        def main(task):calls.append('observe');return dict(task_id='A',mr=task['mr'],scope='fixture_only',merged=values.pop(0))
        options=dict(authorized=True,scope='fixture_only',observe_main=main,
            observe_stop=lambda _:dict(executor_id='worker-A',generation=1,execution='idle',pending_work=False,active_children=[],scope='fixture_only'))
        first=self.runtime.confirm_cancellation('A','cancel',self.actor,**options)
        self.assertEqual(first['status'],'unknown');self.assertEqual(first['reason'],'merge_unknown')
        self.assertNotEqual(self.runtime._load()['tasks']['A']['phase'],'cancelled')
        second=self.runtime.confirm_cancellation('A','cancel',self.actor,**options)
        self.assertEqual(second['status'],'cancelled');self.assertEqual(calls,['observe','observe'])
        self.assertEqual(len(second['observations']),2)

    def test_C01_cancel_queued_process_removes_original_queue_entry_without_creating_job(self):
        import sys
        from codex_parallel_coordination.resources import Controller,ArtifactCleanup
        from codex_parallel_coordination.bridge import ResourceBridge
        self.repository();self.new_task();auth=self.file('authority.txt')
        policy=self.controller.read()[1];policy['allow_test_memory_injection']=True
        self.controller=Controller(self.root/'queued-controller/state.json',memory=lambda:dict(source='fixture shortage',injected=True,
            physical_available=0,commit_available=0))
        self.controller.initialize(self.json('queued-policy.json',policy)['path'])
        self.artifacts=ArtifactCleanup(self.controller);self.bridge=ResourceBridge(self.runtime,self.controller,self.artifacts,self.root/'queued-receipts')
        source=self.json('queued.json',dict(request_id='A:queued',project_id=self.runtime.project_id,task_id='A',attempt_id='queued',
            launch_identity='queued',phase='self_test',purpose='Owned queued fixture',cwd=str(self.runtime.project_root),
            argv=[sys.executable,'-B','-c','raise AssertionError("cancelled work must never start")'],peak_bytes=4096,
            exclusive_keys=['fixture'],resource_kind='regression',protection='running',reclaim_allowed=False))
        self.assertEqual(self.bridge.run_process('A',source,auth,self.actor,authorized=True)['status'],'queued')
        self.cancel_decision('A');self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        result=self.bridge.reclaim_resources('A','cleanup',self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'cleanup_completed',result)
        request=self.controller.read()[0]['requests']['A:queued'];self.assertEqual(request['status'],'cancelled');self.assertNotIn('process',request)
        self.assertEqual(self.controller.pump(),[])
        self.assertEqual(self.bridge.observe_process('A','A:queued',self.actor)['status'],'cancelled_not_started')

    def test_resource_producer_revision_matches_canonical_commit(self):
        self.finished();adapter,_=self.adapter({'close_agent':{'closed':True}})
        self.runtime.lifecycle('close','worker-A','A','closed',1,adapter,authorized=True)
        state=self.runtime._load();row=state['tasks']['A']['producer_results']['resource:closed']
        self.assertEqual(row['ledger_revision'],state['revision'])

    def test_R06_file_delete_registration_failure_replays_result_without_second_delete(self):
        self.finished();adapter,_=self.adapter({'close_agent':{'closed':True}})
        self.runtime.lifecycle('close','worker-A','A','closed',1,adapter,authorized=True)
        path=self.managed/'temporary.txt';path.write_text('temporary');auth=self.file('authority.txt')
        self.runtime.register_resource('A',dict(id='owned',kind='artifact',path=str(path)),auth,self.actor)
        real=self.runtime._save;calls=[]
        def fail_save(state,operation):
            if state['runtime']['resources']['owned']['cleanup_status']=='cleaned':raise OSError('fixture registration failure after delete')
            return real(state,operation)
        def remove(resource):
            path.unlink();calls.append('delete');return dict(status='cleaned',evidence=self.file('deleted.txt'))
        with patch('resource_flow.archive_protection',return_value=dict(verified=True,protected_paths=[])):
            with patch.object(self.runtime,'_save',fail_save):
                first=self.runtime.reclaim_resources('A','cleanup',remove,self.actor,authorized=True,scope='fixture_only')
            self.assertEqual(first['status'],'registration_pending');self.assertFalse(path.exists())
            second=self.runtime.reclaim_resources('A','cleanup',remove,self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(second['status'],'cleanup_completed');self.assertEqual(calls,['delete'])

    def test_D06_creation_registration_failure_is_observed_without_recreation(self):
        self.repository();self.new_task();auth=self.file('authority.txt');path=self.managed/'env'
        real=self.runtime._save;calls=[]
        def save(state,operation):
            if operation['op']=='create-resource-outcome':raise OSError('fixture failed registration')
            return real(state,operation)
        from resource_lifecycle import create
        def invoke(resource):path.mkdir();calls.append('create');return dict(attempt_id='create')
        with patch.object(self.runtime,'_save',save):
            first=create(self.runtime,'A','create',dict(id='A:env',kind='environment',path=str(path)),auth,self.actor,invoke,authorized=True)
        self.assertEqual(first['status'],'registration_pending')
        result=self.runtime.observe_resource_creation('A','create',self.actor)
        self.assertEqual(result['status'],'registered');self.assertEqual(calls,['create'])

    def test_R02_managed_CLI_exit_adapter_never_calls_native_tools(self):
        from host_adapter import ManagedCLIExitAdapter
        self.finished();created=self.json('created.json',dict(identity=dict(pid=123,created=456)))
        log=self.file('terminal.jsonl','{"type":"thread.started","thread_id":"worker-A"}\n{"type":"turn.completed"}\n')
        launch=self.json('cli.json',dict(thread_id='worker-A',backend='codex_exec',job_name='fixture-job',created_receipt_path=created['path']))
        state=self.runtime._load();state['tasks']['A']['cli_dispatch']=dict(command=dict(launch=launch))
        state['runtime']['executors']['worker-A']['routes']={'codex_exec':dict(host_id='local',target_id='worker-A')}
        write_atomic(self.runtime.path,state)
        raw=self.json('exit.json',dict(tree_empty=True,completion_event='JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO',members=[dict(pid=123,created=456)]))
        calls=[]
        def observe(launch):calls.append(launch['job_name']);return dict(thread_id='worker-A',job_name='fixture-job',
            process_identity=dict(pid=123,created=456),exit_evidence=raw,thread_log=log)
        adapter=ManagedCLIExitAdapter(self.runtime,'A',self.actor,observe,scope='fixture_only')
        result=adapter.call('close',target='worker-A',authorized=True)
        self.assertEqual(result['status'],'closed');self.assertFalse(result['native_operation_executed']);self.assertEqual(calls,['fixture-job'])
        self.assertFalse(adapter.capabilities()['spawn'])

    def test_C01_queued_agent_admission_is_cancelled_without_acquiring_or_closing_author(self):
        self.prepare()
        occupied=self.json('occupied.json',dict(request_id='other',project_id=self.runtime.project_id,task_id='other',
            attempt_id='other',launch_identity='other',resource_class='agent_execution_slot'))
        self.controller.capacity_acquire(occupied['path'])
        source=self.json('queued-agent.json',dict(request_id='queued-A',project_id=self.runtime.project_id,task_id='A',
            attempt_id='not-started',launch_identity='spawn:not-started',resource_class='agent_execution_slot'))
        self.assertEqual(self.bridge.admit(source)['status'],'queued')
        self.cancel_decision('A');self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        result=self.bridge.reclaim_resources('A','cleanup',self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'cleanup_completed')
        requests=self.controller.read()[0]['capacity_requests']
        self.assertEqual(requests['queued-A']['status'],'cancelled');self.assertIsNone(requests['queued-A'].get('permit_id'))
        self.assertEqual(requests['other']['status'],'granted')

    def test_unknown_granted_backend_protects_files_and_cannot_be_assumed_uncreated(self):
        self.prepare();source=self.json('unbound.json',dict(request_id='unbound',project_id=self.runtime.project_id,task_id='A',
            attempt_id='external-backend',launch_identity='codex_exec:external-backend',resource_class='agent_execution_slot'))
        self.bridge.admit(source)
        path=self.managed/'keep.txt';path.write_text('must not delete')
        self.runtime.register_resource('A',dict(id='owned',kind='artifact',path=str(path)),self.file('authority.txt'),self.actor)
        self.cancel_decision('A');self.runtime.confirm_cancellation('A','cancel',self.actor,authorized=True,scope='fixture_only')
        result=self.bridge.reclaim_resources('A','cleanup',self.actor,authorized=True,scope='fixture_only')
        self.assertEqual(result['status'],'pending_cleanup');self.assertTrue(path.exists())
        adapter,calls=self.adapter({});closed=self.bridge.close_developer('A','close',adapter,authorized=True)
        self.assertEqual(closed['status'],'pending_close');self.assertEqual(calls,[])
        self.assertEqual(self.controller.read()[0]['capacity_requests']['unbound']['status'],'granted')

    def test_R07_task_process_entry_refuses_shared_host_tools_before_any_creation(self):
        import sys
        self.repository();self.new_task()
        source=self.json('shared-process.json',dict(request_id='forbidden-shared',project_id=self.runtime.project_id,task_id='A',
            attempt_id='bad',launch_identity='bad',phase='self_test',purpose='Must be refused',cwd=str(self.runtime.project_root),
            argv=[sys.executable,'-B','-c','print("must not launch")'],peak_bytes=4096,exclusive_keys=['fixture'],
            resource_kind='shared_tool',protection='running',reclaim_allowed=False))
        before=self.runtime.path.read_bytes(),self.controller.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'shared'):
            self.bridge.run_process('A',source,self.file('authority.txt'),self.actor,authorized=True)
        self.assertEqual(before,(self.runtime.path.read_bytes(),self.controller.path.read_bytes()))

    def test_R01_close_and_license_fact_do_not_read_deleted_report_history(self):
        state=self.finished()
        source=self.json('real-slot.json',dict(request_id='request-original-run',project_id=self.runtime.project_id,task_id='A',
            attempt_id='original-run',launch_identity='worker-A',resource_class='agent_execution_slot'))
        grant=self.controller.capacity_acquire(source['path'])
        state=self.runtime._load();state['runtime']['operations']['original-run'].update(permit_id=grant['permit_id'],
            launch_identity='worker-A',executor_id='worker-A')
        report=Path(state['tasks']['A']['delivery']['path']);report.unlink();write_atomic(self.runtime.path,state)
        adapter,calls=self.adapter({'close_agent':{'closed':True}})
        with patch('stage_feedback.capture',side_effect=AssertionError('new resource facts must not traverse report history')):
            result=self.bridge.close_developer('A','close',adapter,authorized=True)
        self.assertEqual(result['status'],'closed');self.assertEqual(len(calls),1)
        self.assertEqual(self.controller.read()[0]['capacity_requests']['request-original-run']['status'],'released')
        self.assertFalse(report.exists())
