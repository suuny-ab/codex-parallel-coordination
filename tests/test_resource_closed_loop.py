"""Normal new-task resources; real disposable Git/files, fixture host/main facts."""
from pathlib import Path
import shutil
import subprocess
import sys

import test_combination as combined
import test_core_runtime as core_tests
from evidence import load_bound, pack, ref, report_for, write_atomic


class ResourceClosedLoop(core_tests.CoreFixture):
    set_up_resources = combined.Combination.set_up_resources
    register = combined.Combination.register
    completed = combined.Combination.completed
    cleanup = combined.Combination.cleanup
    close_fixture = core_tests.CoreRuntimeTests.close_fixture

    def setUp(self):
        super().setUp()
        self.set_up_resources()
    def new_task(self, authority_ref=None):
        tree = self.managed / 'author'
        spec = dict(task_id='A', worktree=str(tree), branch='codex/new-task',
                    scope='New isolated feature', acceptance=['Verified feature'],
                    user_reference=authority_ref['path'] if authority_ref else 'Explicit fixture approval')
        self.bridge.prepare_development(spec, self.actor,authority_ref=authority_ref)
        return tree

    def repository(self):
        repo = self.runtime.project_root
        def git(*args):
            return subprocess.run(['git', '-c', 'core.longpaths=true', '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
                '-C', str(repo), *args], capture_output=True, check=True)
        git('init', '--initial-branch=main')
        (repo / 'feature.py').write_text('feature = 1\n', encoding='utf-8')
        git('add', 'feature.py'); git('commit', '-m', 'Disposable fixture baseline')
        return repo

    def test_new_worktree_creation_registers_actual_identity_once(self):
        self.repository(); tree = self.new_task()
        authority = self.file('create-authority.txt')
        result = self.bridge.create_worktree('A', 'create-A', authority, self.actor, authorized=True)
        self.assertEqual(result['status'], 'registered')
        resource = self.runtime._load()['runtime']['resources']['A:worktree']
        self.assertEqual(resource['path'], str(tree)); self.assertIn('path_identity', resource)
        self.assertEqual(self.artifacts.controller.read()[0]['cleanup_resources']['A:worktree']['spec']['task_id'], 'A')
        self.assertEqual(self.bridge.create_worktree('A', 'create-A', authority, self.actor, authorized=True), result)

    def test_actual_agent_registered_in_same_launch_transaction(self):
        self.repository(); self.new_task()
        self.bridge.create_worktree('A','create-A',self.file('authority.txt'),self.actor,authorized=True)
        adapter, calls = self.adapter({'spawn_agent': {'agent_id':'new-native'},
            'wait_agent': {'status': {'new-native':'running'}}})
        result = self.launch_fixture('A',adapter,attempt_id='run',admission=self.permit(attempt='run'),
            authorized=True,message='new task',spawn=True)
        self.assertEqual(result['status'],'running')
        item = self.runtime._load()['runtime']['resources']['A:agent']
        self.assertEqual(item['executor_id'],'native_agent:new-native')
        self.assertEqual(item['generation'],1)
        self.assertEqual(item['references'],['A']); self.assertEqual(len(calls),2)

    def test_launch_before_worktree_registration_has_no_host_effect(self):
        self.repository(); self.new_task()
        adapter, calls = self.adapter({})
        with self.assertRaisesRegex(ValueError,'registered'):
            self.runtime.launch('A',adapter,attempt_id='run',admission=self.permit(attempt='run'),
                authorized=True,message='new task',spawn=True)
        self.assertEqual(calls,[])

    def test_optional_environment_database_are_created_and_registered(self):
        self.repository(); self.new_task(); authority = self.file('authority.txt')
        for kind in ('environment','database','artifact'):
            target = self.managed/kind
            result = self.bridge.create_temporary('A','create-'+kind,'A:'+kind,kind,target,authority,self.actor,authorized=True)
            self.assertEqual(result['status'],'registered')
            self.assertTrue(target.exists())
            self.assertEqual(self.runtime._load()['runtime']['resources']['A:'+kind]['path'],str(target))
        self.assertNotIn('A:agent',self.runtime._load()['runtime']['resources'])

    def test_real_managed_process_records_job_pid_and_whole_tree_exit(self):
        self.repository(); self.new_task()
        source = self.json('process.json',dict(request_id='A:test-process',project_id=self.runtime.project_id,task_id='A',
            attempt_id='selftest',launch_identity='new-task-test-process',phase='self_test',purpose='Owned finite regression',
            cwd=str(self.runtime.project_root),argv=[sys.executable,'-B','-c',"print('owned process')"],
            peak_bytes=4*1024*1024,exclusive_keys=['fixture'],resource_kind='regression',
            protection='running',reclaim_allowed=False))
        auth = self.file('process-authority.txt')
        result = self.bridge.run_process('A',source,auth,self.actor,authorized=True)
        self.assertEqual(result['status'],'registered')
        resource = self.runtime._load()['runtime']['resources']['A:test-process']
        self.assertGreater(resource['process_identity']['process']['pid'],0)
        self.assertEqual(self.bridge._process_result(resource)['status'],'cleaned')
        self.assertEqual(self.bridge.run_process('A',source,auth,self.actor,authorized=True),result)

    def test_queued_process_has_no_invented_pid_and_explicit_observation_binds_original_job(self):
        self.repository(); self.new_task()
        from codex_parallel_coordination.resources import Controller, ArtifactCleanup
        from codex_parallel_coordination.bridge import ResourceBridge
        policy = self.controller.read()[1]; policy['allow_test_memory_injection'] = True
        actual_memory = self.controller.memory
        self.controller = Controller(self.root/'queued-control/resources.json', memory=lambda:dict(source='fixture shortage',
            injected=True,physical_available=0,commit_available=0))
        self.controller.initialize(self.json('queued-policy.json',policy)['path'])
        self.artifacts = ArtifactCleanup(self.controller)
        self.bridge = ResourceBridge(self.runtime,self.controller,self.artifacts,self.root/'queued-receipts')
        source = self.json('queued-process.json',dict(request_id='A:queued-process',project_id=self.runtime.project_id,task_id='A',
            attempt_id='queued-test',launch_identity='finite-test',phase='self_test',purpose='Finite queued test',
            cwd=str(self.runtime.project_root),argv=[sys.executable,'-B','-c',"print('queued process')"],
            peak_bytes=4*1024*1024,exclusive_keys=['fixture'],resource_kind='regression',
            protection='running',reclaim_allowed=False))
        auth = self.file('process-authority.txt')
        waiting = self.bridge.run_process('A',source,auth,self.actor,authorized=True)
        self.assertEqual(waiting['status'],'queued')
        self.assertNotIn('A:queued-process',self.runtime._load()['runtime'].get('resources',{}))
        self.controller.memory = actual_memory
        self.controller.pump()
        bound = self.bridge.observe_process('A','A:queued-process',self.actor)
        self.assertEqual(bound['status'],'registered')
        request = self.controller.read()[0]['requests']['A:queued-process']
        item = self.runtime._load()['runtime']['resources']['A:queued-process']
        self.assertEqual(item['process_identity']['process'],request['process'])
        self.assertEqual(item['process_identity']['nonce'],request['nonce'])
        self.assertEqual(self.bridge.observe_process('A','A:queued-process',self.actor),bound)

    def test_normal_new_task_creation_to_durable_delivery_and_actual_worktree_removal(self):
        self.normal_cycle()

    def test_task_scoped_cleanup_without_global_delegation_keeps_global_roles_unchanged(self):
        self.normal_cycle(scoped=True)

    def test_native_root_handoff_preserves_real_local_permit_release(self):
        self.normal_cycle(scoped=True,native=True)

    def test_remote_bound_completion_does_not_depend_on_reused_old_source_path(self):
        self.normal_cycle(scoped=True,native=True,durable=True)

    def test_normal_json_inventory_preserves_nested_evidence_before_resource_cleanup(self):
        self.normal_cycle(scoped=True,native=True,durable=True,json_data=True)

    def test_approved_capture_survives_expired_fixture_then_worktree_cleanup_and_close_verification(self):
        self.normal_cycle(scoped=True,native=True,durable=True,capture_data=True)

    def test_ignored_worktree_capture_uses_sealed_bytes_before_and_after_cleanup(self):
        self.normal_cycle(scoped=True,native=True,durable=True,capture_data=True,capture_in_worktree=True)

    def test_directory_failure_does_not_retain_completed_handed_off_native_agent(self):
        self.normal_cycle(scoped=True,native=True,durable=True,directory_failure=True)

    def normal_cycle(self, scoped=False, native=False, durable=False, directory_failure=False, json_data=False, capture_data=False, capture_in_worktree=False):
        repo = self.repository()
        authority = self.json('approved-task.json',dict(schema='approved-dispatch-plan/1',approval='approved',
            source='direct user message in original main conversation',main_conversation=self.actor,
            integration_agent='delivery-worker',user_quote='Fixture explicit bounded new-task cleanup',
            authorized=['bounded_new_task_resource_cleanup'],
            tasks=[dict(id='A',worktree=str(self.managed/'author'),branch='codex/new-task')]))
        tree = self.new_task(authority)
        self.bridge.create_worktree('A','create-tree',authority,self.actor,authorized=True)
        self.bind()
        admission = self.bridge.admit(self.json('admission.json',dict(request_id='new-run',project_id=self.runtime.project_id,
            task_id='A',attempt_id='run',launch_identity='worker-A',resource_class='agent_execution_slot')))
        host, _ = self.adapter({'send_input':{'submission_id':'fixture-only'},'wait_agent':{'status':{'native-A':'running'}}})
        self.launch_fixture('A',host,attempt_id='run',admission=admission['admission'],authorized=True,
            message='Fixture approved feature',executor_id='worker-A')
        environment = self.managed/'acceptance'
        self.bridge.create_temporary('A','create-env','A:environment','environment',environment,authority,self.actor,authorized=True)
        self.bridge.create_temporary('A','create-db','A:database','database',self.managed/'temporary.sqlite',authority,self.actor,authorized=True)
        (environment/'test.log').write_text('fixture selftest passed',encoding='utf-8')
        (environment/'review.txt').write_text('fixture independent review',encoding='utf-8')
        capture = None
        if capture_data:
            expired = environment/'test-temporary.json'
            expired.write_bytes(b'simulated host receipt')
            capture = self.json('captured-test-result.json', dict(results=[dict(evidence=ref(expired))]))
            # The retained capture itself also lives in a task-owned environment.
            (environment/'captured-test-result.json').write_bytes(Path(capture['path']).read_bytes())
            capture = ref(environment/'captured-test-result.json')
            expired.unlink()
            capture_bytes = Path(capture['path']).read_bytes()
            if capture_in_worktree:
                (tree/'.gitignore').write_text('/test-capture.json\n',encoding='utf-8')
                (tree/'test-capture.json').write_bytes(capture_bytes)
                capture = ref(tree/'test-capture.json')
        base = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD']).decode().strip()
        # A normal developer advances HEAD after creation. This must remain reclaimable.
        (tree/'feature.py').write_text('feature = 2\n',encoding='utf-8')
        subprocess.run(['git','-c','core.longpaths=true','-C',str(tree),'add','feature.py']+
            (['.gitignore'] if capture_in_worktree else []),check=True,capture_output=True)
        subprocess.run(['git','-c','core.longpaths=true','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','-C',str(tree),'commit','-m','Disposable feature'],check=True,capture_output=True)
        head = subprocess.check_output(['git','-C',str(tree),'rev-parse','HEAD']).decode().strip()
        task = self.runtime._load()['tasks']['A']
        self.runtime.apply(dict(op='authorize',task_id='A',rights=['commit','push','mr','merge'],user_reference='Fixture Git authority'),self.actor)
        route = dict(target_id='native-A',existing_work_agent=True,evidence=self.file('route.txt'))
        self.runtime.apply(dict(op='delegate',agent_id='delivery-worker',task_ids=['A'],merge_order=['A'],
            required_ci_jobs=['test'],routes={'worker-A':route},authority_evidence=self.file('delegate.txt'),
            user_reference='Fixture independent integrator only'),self.actor)
        outbound = self.json('outbound.json',dict(sender_id='delivery-worker',recipient_id='worker-A',target_id='native-A',
            status='accepted',mode='fixture',correlation_id='contact-A'))
        inbound = self.json('inbound.json',dict(sender_id='worker-A',recipient_id='delivery-worker',
            status='acknowledged',mode='fixture',correlation_id='contact-A'))
        self.runtime.apply(dict(op='delivery-transport',transport=dict(mode='fixture',send_tool='fixture',receive_tool='fixture',
            creates_conversation=False,authors={'worker-A':dict(outbound=outbound,inbound=inbound)})),'delivery-worker')
        report = pack(dict(project_id=self.runtime.project_id,task_id='A',delivery_revision=1,summary='Fixture feature',
            source_root=str(tree),evidence=[ref(environment/'test.log')]+([capture] if capture else []),self_test=dict(status='passed',command='fixture test'),
            quality_review=dict(status='passed'),entry=dict(kind='file',target=self.file('acceptance.txt')['path']),
            steps=['Inspect fixture'],expected='Feature works',git=dict(head_sha=head,target_sha=base)))
        write_atomic(environment/'delivery.json',report)
        delivery = ref(environment/'delivery.json')
        event = dict(project_id=self.runtime.project_id,task_id='A',owner_agent_id='worker-A',recipient_agent_id='delivery-worker',
            channel='native_agent',kind='completed',delivery_revision=1,event_id='A:1:completed',report=delivery)
        self.runtime.receive(event,'delivery-worker');self.runtime.apply(dict(op='drain'),'delivery-worker')
        self.runtime.apply(dict(op='activate',item_id=event['event_id'],user_reference='Fixture acceptance'),self.actor)
        self.runtime.apply(dict(op='close',item_id=event['event_id'],outcome='accepted',user_reference='Fixture accepted'),self.actor)
        self.runtime.apply(dict(op='mr',task_id='A',mr=dict(provider='github',id='fixture://PR/A',url='https://example.invalid/fixture/PR/A',source_branch=task['branch'],
            target_branch='main',head_sha=head,target_sha=base)),'delivery-worker')
        # Independent integration/CI/host facts are explicitly fixtures. Product gates still run.
        from coordination import transition
        state,_ = transition(self.runtime._load(),dict(op='integration-start',task_id='A',actor_id='delivery-worker'))
        write_atomic(self.runtime.path,state)
        integration = self.json('integration.json',dict(project_id=self.runtime.project_id,task_id='A',delivery_revision=1,
            source_digest=report['source']['digest'],head_sha=head,target_sha=base,status='passed',
            command='fixture independent integration',evidence=[self.file('independent.log')]))
        self.runtime.apply(dict(op='integration-result',task_id='A',report=integration),'delivery-worker')
        self.runtime.apply(dict(op='review',task_id='A',evidence=ref(environment/'review.txt')),'delivery-worker')
        state = self.runtime._load()
        check = self.json('check.json',dict(project_id=self.runtime.project_id,task_id='A',delivery_revision=1,
            based_on_revision=state['revision'],source_digest=report['source']['digest'],head_sha=head,main_sha=base,
            ci=dict(head_sha=head,pipeline_id='fixture-ci',status='passed',jobs={'test':'passed'},evidence=[self.file('ci.log')])))
        self.runtime.apply(dict(op='delivery-check',task_id='A',observation=check),'delivery-worker')
        subprocess.run(['git','-c','core.longpaths=true','-C',str(repo),'merge','--ff-only',task['branch']],check=True,capture_output=True)
        proof = self.json('main-proof.json',dict(project_id=self.runtime.project_id,task_id='A',provider='github',
            based_on_revision=self.runtime._load()['revision'],source_head=head,previous_main=base,main_ref='main',
            main_head=head,contains_source=True,scope='fixture_only',evidence=[self.file('main-observation.txt')]))
        self.runtime.record_main_inclusion(proof,'delivery-worker')
        remote=None
        if durable:
            bare=self.root/'cloud-fixture.git'
            subprocess.run(['git','-c','core.longpaths=true','init','--bare',str(bare)],check=True,capture_output=True)
            subprocess.run(['git','-c','core.longpaths=true','-C',str(repo),'remote','add','origin',str(bare)],check=True,capture_output=True)
            with self.assertRaises(subprocess.CalledProcessError):
                self.bridge.observe_remote_completion('A','delivery-worker',scope='fixture_only')
            subprocess.run(['git','-c','core.longpaths=true','-C',str(repo),'push','origin','main'],check=True,capture_output=True)
            with self.assertRaises(ValueError):
                self.bridge.observe_remote_completion('A','delivery-worker',scope='host_verified')
            remote=self.bridge.observe_remote_completion('A','delivery-worker',scope='fixture_only')
        if scoped:
            self.runtime.apply(dict(op='revoke-delivery',user_reference='End fixture global delegation',
                evidence=self.file('revoke.txt')),self.actor)
            self.assertIsNone(self.runtime._load().get('delivery'))
            before = self.runtime.path.read_bytes()
            with self.assertRaises(ValueError):
                self.runtime.apply(dict(op='describe',task_id='A',title='not a global writer'),'delivery-worker')
            self.assertEqual(before,self.runtime.path.read_bytes())
        observed_state = self.runtime._load()
        facts = dict(executor_id='worker-A',generation=1,based_on_revision=observed_state['revision'],
            execution='completed',active_children=[],pending_work=False)
        self.runtime.observe_executor('worker-A',facts,'delivery-worker',self.json('post-merge-observation.json',facts),
            task_id='A' if scoped else None)
        handoff = (self.json('handoff.json', dict(file_inventory=[dict(path='README.md',sha256='0'*64)],
            nested=[dict(evidence=[ref(environment/'test.log')])])) if json_data else self.file('handoff.txt'))
        if capture_data:
            classification = self.classification_fixture([capture], actor='delivery-worker')['declaration']
        preservation = self.runtime.preserve_task('A',self.root/'external-preservation',handoff,
            'delivery-worker',scope='fixture_only',remote_verification=remote)
        if capture_in_worktree:
            from cleanup import eligibility
            from evidence import artifact, preserved_read
            row = next(r for r in load_bound(preservation)['files'] if r['original']==capture)
            # The cloud-bound completed task reads the sealed package, even while
            # its old ignored work path is still present and later disappears.
            Path(capture['path']).write_bytes(b'changed old work path after sealing')
            with preserved_read(self.runtime._load(),'A'):
                self.assertEqual(artifact(capture).read_bytes(),capture_bytes)
            eligibility(self.runtime,self.runtime._load(),'A',preservation,'fixture_only')
            saved = Path(row['path']); saved.write_bytes(b'tampered sealed package before cleanup')
            with self.assertRaises(ValueError):
                with preserved_read(self.runtime._load(),'A'): pass
            with self.assertRaises(ValueError):
                eligibility(self.runtime,self.runtime._load(),'A',preservation,'fixture_only')
            saved.write_bytes(capture_bytes); Path(capture['path']).unlink()
            eligibility(self.runtime,self.runtime._load(),'A',preservation,'fixture_only')
        if directory_failure:
            (tree/'disposable-test-output.txt').write_text('Untracked temporary output; directory remains protected',encoding='utf-8')
            self.assertEqual(report_for(self.runtime._load(),'A')['source'],report['source'])
        if native:
            intent=self.runtime.prepare_native_close('A','native-close',preservation,'delivery-worker',
                authorized=True,scope='fixture_only')
            claimed=self.runtime.claim_native_close('native-close',self.actor,authorized=True)
            self.assertTrue(claimed['dispatch_allowed'])
            raw_close=self.json('formal-close.json',dict(previous_status={'completed':'fixture final'}))
            raw_wait=self.json('formal-wait.json',dict(status={'native-A':'not_found'},timed_out=False))
            observation=self.json('formal-native-result.json',dict(operation_id='native-close',executor_id='worker-A',generation=1,
                request=intent['native_request'],controller_id=self.actor,
                close=dict(tool='multi_agent_v1__close_agent',arguments={'target':'native-A'},result=raw_close),
                wait=dict(tool='multi_agent_v1__wait_agent',arguments={'targets':['native-A'],'timeout_ms':10000},result=raw_wait)))
            self.runtime.reconcile_lifecycle('native-close',observation,'delivery-worker')
        adapter,_ = self.adapter({'close_agent':{'closed':True}});adapter.actor_id='delivery-worker'
        result = self.runtime.reclaim_development('A','reclaim',preservation,adapter,self.bridge.cleanup,authorized=True)
        if directory_failure:
            self.assertEqual(result['status'],'pending_cleanup');self.assertTrue(tree.exists())
            self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'closed')
            self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'completed')
            self.assertEqual(report_for(self.runtime._load(),'A')['source'],report['source'])
            self.assertTrue(self.bridge.release_agent_quota('native-close','delivery-worker')['released'])
            return
        self.assertEqual(result['status'],'cleanup_completed');self.assertFalse(tree.exists())
        self.assertFalse(environment.exists()); self.assertFalse(Path(delivery['path']).exists())
        self.assertEqual(report_for(self.runtime._load(),'A')['source'],report['source'])
        self.assertEqual(self.runtime._load()['tasks']['A']['merge']['head_sha'],head)
        self.assertTrue(self.bridge.release_agent_quota('native-close' if native else 'reclaim:agent','delivery-worker')['released'])
        again = self.runtime.reclaim_development('A','again',preservation,adapter,
            lambda _:self.fail('confirmed resource removed twice'),authorized=True)
        self.assertEqual(again['status'],'cleanup_completed')
        # All current review evidence stays usable, with original reference identities.
        from evidence import artifact, preserved_read
        with preserved_read(self.runtime._load(),'A'):
            self.assertTrue(artifact(self.runtime._load()['tasks']['A']['review']['evidence']).exists())
            if capture_data:
                self.assertEqual(artifact(capture).read_bytes(), capture_bytes)
        if capture_data:
            confirmation = self.runtime._load()['runtime']['operations']['native-close']['close_confirmation']
            self.assertTrue(self.runtime.verify_close_confirmation(confirmation)['verified'])
            row = next(r for r in load_bound(preservation)['files'] if r['original']==capture)
            self.assertEqual(Path(row['path']).read_bytes(), capture_bytes)
            approval_path = Path(classification['path']); approval_bytes = approval_path.read_bytes()
            approval_path.write_bytes(b'changed approval')
            self.assertFalse(self.runtime.verify_close_confirmation(confirmation)['verified'])
            approval_path.write_bytes(approval_bytes)
            saved_capture = Path(row['path']); saved_capture.write_bytes(b'tampered frozen capture')
            self.assertFalse(self.runtime.verify_close_confirmation(confirmation)['verified'])
            with self.assertRaises(ValueError): report_for(self.runtime._load(),'A')
            saved_capture.write_bytes(capture_bytes)
        archive = load_bound(preservation)
        copied = next(row for row in archive['files'] if row.get('original')==delivery)
        backup = Path(copied['path']); saved = backup.read_bytes(); backup.write_bytes(b'changed')
        with self.assertRaises(ValueError): report_for(self.runtime._load(),'A')
        backup.write_bytes(saved)
        # A replacement source is checked as a live source; it cannot inherit old completion.
        tree.mkdir(); (tree/'wrong.txt').write_text('new unrelated object',encoding='utf-8')
        if durable:
            self.assertEqual(report_for(self.runtime._load(),'A')['source'],report['source'])
        else:
            with self.assertRaises(ValueError): report_for(self.runtime._load(),'A')

    def test_task_resource_authority_rejects_author_other_task_and_changed_approval(self):
        self.repository()
        approval = self.json('approved-task.json',dict(schema='approved-dispatch-plan/1',approval='approved',
            source='direct user message in original main conversation',main_conversation=self.actor,
            integration_agent='delivery-worker',user_quote='Fixture scope',authorized=['bounded_new_task_resource_cleanup'],
            tasks=[dict(id='A',worktree=str(self.managed/'author'),branch='codex/new-task')]))
        self.new_task(approval)
        for identity in ('worker-A','unrelated-integrator'):
            with self.assertRaises(ValueError):
                self.bridge.create_temporary('A','forbidden-'+identity,'bad:'+identity,'environment',
                    self.managed/identity,approval,identity,authorized=True)
        self.bridge.prepare_development(dict(task_id='B',worktree=str(self.managed/'other-task'),branch='codex/other-task',
            scope='Other fixture task',acceptance=['Other task'],user_reference=approval['path']),
            self.actor,authority_ref=approval)
        with self.assertRaises(ValueError):
            self.bridge.create_temporary('B','outside-task-scope','B:environment','environment',
                self.managed/'forbidden-B',approval,'delivery-worker',authorized=True)
        before = self.runtime.path.read_bytes()
        Path(approval['path']).write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):
            self.bridge.create_worktree('A','create',approval,'delivery-worker',authorized=True)
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_unknown_creation_is_retained_without_second_callback(self):
        self.repository(); self.new_task()
        from resource_lifecycle import create
        target = self.managed/'uncertain'; calls = []
        def lost(resource):
            calls.append(resource['id']); target.mkdir(); raise TimeoutError('after actual creation')
        spec = dict(id='A:env',kind='environment',path=str(target))
        auth = self.file('authority.txt')
        first = create(self.runtime,'A','create-env',spec,auth,self.actor,lost,authorized=True)
        second = create(self.runtime,'A','create-env',spec,auth,self.actor,lost,authorized=True)
        self.assertEqual(first,second); self.assertEqual(first['status'],'unknown')
        self.assertEqual(calls,['A:env']); self.assertTrue(target.exists())

    def test_normal_creation_refuses_outside_scope_before_git(self):
        self.repository()
        before = self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):
            self.bridge.prepare_development(dict(task_id='A', worktree=str(self.root/'outside'), branch='codex/outside',
                scope='s', acceptance=['a'], user_reference='approval'), self.actor)
        self.assertEqual(before, self.runtime.path.read_bytes())
        self.assertFalse((self.root/'outside').exists())

    def test_unregistered_new_task_cannot_claim_cleanup_complete(self):
        self.completed()
        state = self.runtime._load(); state['tasks']['A']['resource_contract'] = 'new-task/1'
        write_atomic(self.runtime.path, state)
        with self.assertRaisesRegex(ValueError, 'registered'):
            self.cleanup('empty-inventory')

    def test_external_preservation_survives_confirmed_author_source_cleanup(self):
        self.completed()
        state = self.runtime._load(); task = state['tasks']['A']
        old = Path(task['worktree']); tree = self.managed/'author'; shutil.copytree(old, tree)
        task['worktree'] = str(tree)
        report = load_bound(task['delivery']); report.pop('source')
        report['source_root'] = str(tree)
        report['evidence'] = [self.file('source-test.log')]
        (tree/'test.log').write_bytes(Path(report['evidence'][0]['path']).read_bytes())
        report['evidence'] = [ref(tree/'test.log')]
        packed = pack(report); path = tree/'delivery.json'; write_atomic(path, packed)
        # Report is external in this first regression; a second case covers internal reports.
        external = self.root/'final-report.json'; write_atomic(external, packed)
        task['delivery'] = ref(external)
        receipt = next(r for r in state['receipts'].values() if r['status']=='processed')
        receipt['payload']['report'] = task['delivery']
        from evidence import fingerprint
        receipt['fingerprint'] = fingerprint(receipt['payload'])
        # The report must not add a new file to its own frozen source inventory.
        path.unlink()
        write_atomic(self.runtime.path, state)
        self.register('author', tree, 'worktree')
        preservation = self.runtime.preserve_task('A', self.root/'durable', self.file('handoff.txt'), self.actor,
            merge_verification=load_bound(self.preservation)['merge_verification'], scope='fixture_only')
        self.preservation = preservation
        self.assertEqual(report_for(self.runtime._load(), 'A')['source'], packed['source'])
        result = self.cleanup('remove-author')
        self.assertEqual(result['status'], 'cleanup_completed'); self.assertFalse(tree.exists())
        self.assertEqual(report_for(self.runtime._load(), 'A')['source'], packed['source'])
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'], 'completed')
