"""Real disposable ledger/files; native, installation and independent judgements are fixtures."""
from copy import deepcopy
import os
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
if os.name == 'nt' and not str(ROOT).startswith('\\\\?\\'): ROOT = Path('\\\\?\\'+str(ROOT))
sys.path.insert(0,str(ROOT/'skills/isolated-parallel-development-coordination/scripts'))
sys.path.insert(0,str(ROOT/'src'))
from evidence import load_bound, ref, write_atomic
import test_native_control as native_tests


class DeliveryContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f = native_tests.NativeControlTests('test_prepare_then_root_claim_is_one_shot_and_uses_actual_tool_contract')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.proof,self.tree = self.f.ready()
        self.runtime,self.actor = self.f.runtime,self.f.actor
        self.approval = self.f.json('dispatch-approval.json',dict(schema='approved-dispatch-plan/1',approval='approved',
            source='direct user message in original main conversation',main_conversation=self.actor,
            integration_agent='integration',user_quote='Approve bounded task delivery',
            authorized=['bounded_new_task_resource_cleanup','update_local_plugin_after_review'],
            tasks=[dict(id='A',worktree=str(self.tree),branch=self.runtime._load()['tasks']['A']['branch'])]))
        state=self.runtime._load();state['tasks']['A']['requirements']['user_reference']=self.approval['path']
        write_atomic(self.runtime.path,state)
        self.request = self.f.json('continuation.json',dict(schema='delivery-continuation/1',project_id=self.runtime.project_id,
            request_id='finish-A',task_id='A',approval=self.approval))
        self.calls=[]
        def cleanup(resource):
            self.calls.append(resource['id'])
            return self.f.remove(resource)
        self.bridge=SimpleNamespace(cleanup=cleanup,reconcile_cleanup=lambda *args: None)

    def run_flow(self, **options):
        return self.runtime.continue_delivery(self.request,self.actor,bridge=self.bridge,
            reclaim_authorized=True,scope='fixture_only',inputs=dict(preservation=self.proof),**options)

    def close(self):
        result=self.run_flow();self.assertEqual(result['step'],'close',result)
        package=result['host_package'];key=package['operation_id']
        self.assertTrue(self.runtime.claim_native_close(key,self.actor,authorized=True)['dispatch_allowed'])
        observed=self.f.observation(package['native_request'],operation_id=key)
        return self.runtime.continue_delivery(self.request,self.actor,bridge=self.bridge,reclaim_authorized=True,
            scope='fixture_only',inputs=dict(preservation=self.proof,native_observation=observed))

    def test_repeated_calls_and_one_native_claim_close_and_clean_once(self):
        first=self.run_flow();again=self.run_flow()
        self.assertEqual(first['host_package'],again['host_package'])
        self.assertEqual(self.calls,[])
        final=self.close();self.assertEqual(final['status'],'completed',final)
        self.assertFalse(self.tree.exists())
        calls=list(self.calls)
        self.assertEqual(self.run_flow()['status'],'completed')
        self.assertEqual(self.calls,calls)
        self.assertEqual(len([op for op in self.runtime._load()['runtime']['operations'].values()
                              if op.get('operation')=='close']),1)

    def test_no_authority_and_wrong_actor_never_close(self):
        result=self.runtime.continue_delivery(self.request,self.actor,scope='fixture_only')
        self.assertEqual(result['status'],'authorization_required')
        with self.assertRaises(ValueError):
            self.runtime.continue_delivery(self.request,'worker-A',scope='fixture_only')
        self.assertFalse(any(op.get('operation')=='close' for op in self.runtime._load()['runtime']['operations'].values()))

    def test_old_integrator_authority_yields_root_package_without_changing_approval(self):
        raw=load_bound(self.approval);raw['authorized']=[]
        self.approval=self.f.json('old-approval.json',raw)
        state=self.runtime._load();state['tasks']['A']['requirements']['user_reference']=self.approval['path']
        write_atomic(self.runtime.path,state)
        request=load_bound(self.request);request['approval']=self.approval
        self.request=self.f.json('old-request.json',request)
        result=self.runtime.continue_delivery(self.request,'integration',bridge=self.bridge,reclaim_authorized=True,
            scope='fixture_only',inputs=dict(preservation=self.proof))
        self.assertEqual(result['status'],'host_required',result)
        self.assertEqual(result['next_owner'],self.actor)
        self.assertEqual(result['host_package']['action'],'continue')
        self.assertEqual(load_bound(self.approval)['authorized'],[])

    def test_unknown_close_retains_original_claim_and_accepts_late_confirmation(self):
        result=self.run_flow();package=result['host_package'];key=package['operation_id']
        self.runtime.claim_native_close(key,self.actor,authorized=True)
        unknown=self.f.observation(package['native_request'],operation_id=key,after='running')
        self.runtime.reconcile_lifecycle(key,unknown,self.actor)
        self.assertFalse(self.runtime.claim_native_close(key,self.actor,authorized=True)['dispatch_allowed'])
        self.assertEqual(self.run_flow()['host_package']['operation_id'],key)
        late=self.f.observation(package['native_request'],operation_id=key)
        self.runtime.reconcile_lifecycle(key,late,self.actor)
        self.assertEqual(self.run_flow()['status'],'completed')

    def test_shared_descendants_incomplete_preservation_and_changed_report_block(self):
        original=self.runtime.path.read_bytes()
        for field,value in (('active_children',['child']),('shared',True),('pending_work',True)):
            state=self.runtime._load();state['runtime']['executors']['worker-A'][field]=value
            write_atomic(self.runtime.path,state)
            result=self.run_flow();self.assertEqual(result['status'],'blocked',result)
            self.assertFalse(any(o.get('operation')=='close' for o in self.runtime._load()['runtime']['operations'].values()))
            self.runtime.path.write_bytes(original)
        bad=load_bound(self.proof);bad['files']=[]
        proof=self.f.json('bad-preservation.json',bad)
        result=self.runtime.continue_delivery(self.request,self.actor,reclaim_authorized=True,scope='fixture_only',
            inputs=dict(preservation=proof))
        self.assertEqual(result['status'],'blocked')
        state=self.runtime._load();state['tasks']['A']['delivery_revision']+=1;write_atomic(self.runtime.path,state)
        result=self.run_flow();self.assertIn('source/report changed',result['reason'])

    def test_install_interrupted_after_effect_is_observe_only(self):
        request=load_bound(self.request);request['installation_required']=True
        self.request=self.f.json('install-request.json',request)
        calls=[]
        def interrupted(_):
            calls.append('installed');raise SystemExit('fixture death after external effect')
        with self.assertRaises(SystemExit):
            self.run_flow(install_authorized=True,install=interrupted)
        result=self.run_flow(install_authorized=True,install=lambda _: self.fail('must not reinstall'))
        self.assertEqual(result['status'],'needs_reconcile',result)
        self.assertEqual(result['installation']['status'],'unknown')
        self.assertEqual(calls,['installed'])
        self.assertEqual(result['task_completed'],True)
        self.assertEqual(result['session_loaded'],'unknown')

    def test_original_request_conflicts_and_json_cannot_supply_authority(self):
        self.run_flow()
        changed=load_bound(self.request);changed['request_id']='another'
        with self.assertRaisesRegex(ValueError,'original request'):
            self.runtime.continue_delivery(self.f.json('replacement.json',changed),self.actor,scope='fixture_only')
        changed=load_bound(self.request);changed['actor_id']=self.actor
        with self.assertRaisesRegex(ValueError,'bounded continuation'):
            self.runtime.continue_delivery(self.f.json('spoofed.json',changed),self.actor,scope='fixture_only')

    def test_cleanup_callback_can_write_ledger_without_lost_concurrent_change(self):
        original=self.bridge.cleanup
        def concurrent(resource):
            with __import__('coordination').writer_lock(self.runtime.path):
                state=self.runtime._load();state['unrelated_fixture_fact']='retained'
                self.runtime._save(state,dict(op='fixture-concurrent-update'))
            return original(resource)
        self.bridge.cleanup=concurrent
        final=self.close()
        self.assertEqual(final['status'],'completed',final)
        self.assertEqual(self.runtime._load()['unrelated_fixture_fact'],'retained')

    def test_cleanup_rechecks_shared_and_preservation_changes_after_callback(self):
        original=self.bridge.cleanup
        def concurrent(resource):
            actual=original(resource)
            state=self.runtime._load();state['runtime']['resources'][resource['id']]['shared']=True
            self.runtime._save(state,dict(op='fixture-shared-during-cleanup'))
            return actual
        self.bridge.cleanup=concurrent
        final=self.close()
        self.assertEqual(final['status'],'pending_cleanup',final)
        state=self.runtime._load();operation=state['runtime']['operations']['finish-A:files']
        self.assertTrue(state['runtime']['resources']['A:worktree']['shared'])
        self.assertEqual(operation['external_results']['A:worktree']['status'],'cleaned')
        self.assertIn('A:worktree',operation['conflicts'])
        self.assertEqual(state['runtime']['resources']['A:worktree']['cleanup_status'],'intent')

    def test_same_cleanup_round_recovers_results_after_death_without_redelete(self):
        original=self.bridge.cleanup
        def interrupted(resource):
            result=original(resource)
            self.retained=(resource,result)
            raise SystemExit('fixture process died after delete')
        self.bridge.cleanup=interrupted
        with self.assertRaises(SystemExit):self.close()
        resource,result=self.retained
        self.runtime.reconcile_cleanup('A',resource['id'],self.f.json('delete-observed.json',dict(
            resource_id=resource['id'],status='cleaned',evidence=[result['evidence']])),self.actor)
        self.bridge.cleanup=lambda _:self.fail('duplicate delete')
        final=self.run_flow();self.assertEqual(final['status'],'completed',final)

    def resources(self, *, successor=False):
        from codex_parallel_coordination.resources import Controller, ArtifactCleanup
        from codex_parallel_coordination.bridge import ResourceBridge
        from codex_parallel_coordination.worktrees import GitWorktrees
        from codex_parallel_coordination.dispatch import StandardDispatch
        from host_adapter import HostAdapter,NATIVE
        f=self.f;project=self.runtime.project_root;managed=Path(self.runtime._load()['runtime']['cleanup_root'])
        git=GitWorktrees(project,managed)
        subprocess.run(['git','-c','core.longpaths=true','clone','--shared','--bare','--branch','main',
                        str(ROOT).removeprefix('\\\\?\\'),str(project/'.git')],check=True,capture_output=True)
        git._git('config','core.bare','false')
        base=git._git('rev-parse','HEAD').stdout.decode().strip()
        controller=Controller(f.root/'resource-control/resources.json')
        controller.initialize(f.json('policy.json',dict(authority=f.json('authority.json',dict(
            scope='explicit_temporary_managed_resources',launch=True,cleanup_artifacts=True)),max_parallel=1,max_agent_slots=1,
            physical_reserve=1024,commit_reserve=1024,projects={self.runtime.project_id:[str(project),str(managed)]},
            cleanup_roots={self.runtime.project_id:[str(managed)]},executables=[sys.executable],exclusive_keys=['fixture'],
            phase_resources={'self_test':['fixture']}))['path'])
        artifacts=ArtifactCleanup(controller,{'git_worktree':git})
        self.bridge=ResourceBridge(self.runtime,controller,artifacts,f.root/'host-evidence')
        artifacts.register(dict(resource_id='A:worktree',project_id=self.runtime.project_id,task_id='A',
            attempt_id=self.runtime._load()['runtime']['resources']['A:worktree']['attempt_id'],
            path=str(self.tree),kind='temporary_directory',references=[],protected=False))
        # Real local quota; native effects remain explicit fixture replies.
        quota=controller.capacity_acquire(f.json('parent-quota.json',dict(request_id='request-original-run',
            project_id=self.runtime.project_id,task_id='A',attempt_id='original-run',launch_identity='worker-A',
            resource_class='agent_execution_slot'))['path'])
        state=self.runtime._load();task=state['tasks']['A'];task['worker_run']['attempt_id']='original-run'
        state['runtime']['operations']['original-run']=dict(operation='send',status='running',task_id='A',
            attempt_id='original-run',request_id='request-original-run',permit_id=quota['permit_id'],launch_identity='worker-A',
            executor_id='worker-A',generation=1)
        write_atomic(self.runtime.path,state)
        if not successor:return
        from demand_routing import scope_digest
        discussion=f.json('discussion.json',dict(confirmed=True));spec=f.file('spec.md','Original approved successor')
        check=f.json('spec-check.json',dict(coordinator_id=self.actor,requirement_id='REQ-B',discussion=discussion,spec=spec,result='consistent'))
        record=dict(name='B',scope='Original approved successor',acceptance=['Fixture result'],discussion=discussion,spec=spec,check=check)
        item=dict(task_id='B',requirement_id='REQ-B',name='B',scope=record['scope'],acceptance=record['acceptance'],
            scope_digest=scope_digest(record),split_reason='One goal',assignment='new',executor_id=None,assignment_reason='Original queued author')
        presentation=f.json('presented-B.json',dict(conversation_id=self.actor,plan_id='PLAN-B',version=1,items=[item]))
        approval=f.json('approval-B.json',dict(plan_id='PLAN-B',presentation=presentation,task_ids=['B'],decision='approved',
            text='Approve original B',quote='Approve original B',task_id='B',rights=dict(commit=True,push=True,mr=True,merge=False),
            base_main=base,specs=[spec],integration_agent='integration-B',tasks=[dict(id='B',branch='codex/B',worktree=str(managed/'B'))]))
        self.successor=dict(request_id='dispatch-B',project_id=self.runtime.project_id,task_id='B',base_main=base,
            requirement=dict(record,start_reference=approval),approval=approval,presentation=presentation,worktree=str(managed/'B'),
            branch='codex/B',integration_agent='integration-B',acceptance_type='delivery_verified',report_path=str(f.root/'B-report.json'))
        self.origin=dict(role='user',conversation_id=self.actor,reference=approval)
        calls=[]
        def invoke(tool,args):
            calls.append(tool)
            return {'spawn_agent':dict(agent_id='fixture-B'),'wait_agent':dict(status={'fixture-B':'running'}),
                    'send_input':dict(submission_id='fixture-accepted')}[tool]
        adapter=HostAdapter('native_agent',list(NATIVE.values()),invoke,self.actor)
        self.dispatch=StandardDispatch(self.runtime,self.bridge,git,adapter,adapter,self.origin,'integration-B')
        self.assertEqual(self.dispatch.run(self.successor)['status'],'queued');self.assertEqual(calls,[])
        self.successor_ref=f.json('original-B-request.json',self.successor)
        request=load_bound(self.request);request['successor']=self.successor_ref
        self.request=f.json('parent-with-successor.json',request)
        self.successor_calls=calls

    def test_real_quota_release_continues_original_queued_request_once(self):
        self.resources(successor=True)
        # Close was already prepared/claimed by the same public entry; late result resumes it.
        first=self.run_flow();package=first['host_package'];key=package['operation_id']
        self.runtime.claim_native_close(key,self.actor,authorized=True)
        observed=self.f.observation(package['native_request'],operation_id=key)
        final=self.runtime.continue_delivery(self.request,self.actor,bridge=self.bridge,dispatch=self.dispatch,
            reclaim_authorized=True,successor_authorized=True,successor_origin=self.origin,scope='fixture_only',
            inputs=dict(preservation=self.proof,native_observation=observed))
        self.assertEqual(final['status'],'completed',final)
        self.assertEqual(self.successor_calls,['spawn_agent','wait_agent','send_input'])
        self.assertEqual(self.bridge.controller.read()[0]['capacity_requests']['request-original-run']['status'],'released')
        self.assertEqual(self.bridge.controller.read()[0]['capacity_requests']['dispatch-B:capacity']['status'],'granted')
        self.assertEqual(self.runtime.continue_delivery(self.request,self.actor,bridge=self.bridge,scope='fixture_only',
            reclaim_authorized=True,inputs=dict(preservation=self.proof))['status'],'completed')
        self.assertEqual(self.successor_calls,['spawn_agent','wait_agent','send_input'])
        self.assertEqual(load_bound(self.successor_ref),self.successor)

    def test_release_died_after_actual_effect_recovers_without_releasing_twice(self):
        self.resources();original=self.bridge.release_agent_quota;calls=[]
        def interrupted(*args):
            calls.append(args);original(*args);raise SystemExit('fixture death after actual quota release')
        self.bridge.release_agent_quota=interrupted
        with self.assertRaises(SystemExit):self.close()
        self.bridge.release_agent_quota=lambda *args:self.fail('duplicate quota release')
        self.assertEqual(self.run_flow()['status'],'completed')
        self.assertEqual(len(calls),1)

    def test_already_released_fact_is_adopted_without_another_release_call(self):
        self.resources()
        first=self.run_flow();package=first['host_package'];key=package['operation_id']
        self.runtime.claim_native_close(key,self.actor,authorized=True)
        self.runtime.reconcile_lifecycle(key,self.f.observation(package['native_request'],operation_id=key),self.actor)
        self.bridge.release_agent_quota(key,self.actor)
        self.bridge.release_agent_quota=lambda *args:self.fail('actual release already confirmed')
        final=self.run_flow();self.assertEqual(final['status'],'completed',final)

    def test_known_granted_permit_without_legal_actor_mapping_never_becomes_not_reserved(self):
        self.resources()
        state=self.runtime._load();state['tasks']['A']['worker_run'].pop('attempt_id')
        state['runtime']['operations']['original-run'].update(status='not_started',executor_id=None)
        write_atomic(self.runtime.path,state)
        original=deepcopy(self.runtime._load()['runtime']['operations']['original-run'])
        first=self.run_flow();again=self.run_flow()
        self.assertEqual(first['status'],'needs_reconcile',first);self.assertEqual(first['step'],'close')
        self.assertEqual(again['status'],'needs_reconcile')
        self.assertEqual(self.runtime._load()['runtime']['operations']['original-run'],original)
        self.assertFalse(any(o.get('operation')=='close' for o in self.runtime._load()['runtime']['operations'].values()))
        self.assertEqual(self.bridge.controller.read()[0]['capacity_requests']['request-original-run']['status'],'granted')

    def test_partial_preservation_from_dead_call_resumes_exact_original_bytes(self):
        self.resources()
        state=self.runtime._load();state['tasks']['A']['merge']['proof']=load_bound(self.proof)['merge_verification']
        write_atomic(self.runtime.path,state)
        request=load_bound(self.request);request.update(preservation_destination=str(self.f.root/'saved-delivery'),
            handoff=self.f.file('handoff.txt','Fixture handoff'))
        self.request=self.f.json('preserving-request.json',request)
        cfg=self.f.json('preserve-host.json',dict(protocol='delivery-continuation-host/1',action='run',request=self.request,
            profile=dict(project_root=str(self.runtime.project_root),state_root=str(self.f.root/'authority'),
                resource_state=str(self.bridge.controller.path),evidence_root=str(self.bridge.evidence)),
            host=dict(actor_id=self.actor,scope='fixture_only',available_tools=[],reclaim_authorized=True)))
        code="""
import sys,json,os
sys.path.insert(0,SCRIPTS);sys.path.insert(0,SOURCE)
import resource_lifecycle,continuation_endpoint
original=resource_lifecycle.write_atomic
def die(path,value):
    if path.name=='preservation.json': os._exit(72)
    return original(path,value)
resource_lifecycle.write_atomic=die
continuation_endpoint.main()
""".replace('SCRIPTS',repr(str(ROOT/'skills/isolated-parallel-development-coordination/scripts'))).replace('SOURCE',repr(str(ROOT/'src')))
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',code],input=json.dumps(load_bound(cfg))+'\n',
            encoding='utf-8',capture_output=True,timeout=20)
        self.assertEqual(result.returncode,72,result.stderr)
        destination=Path(request['preservation_destination']);before={p.name:p.read_bytes() for p in destination.iterdir()}
        lock=self.runtime.path.with_suffix('.json.lock')
        self.runtime.recover_call_locks([ref(lock)],str(self.f.root/'preservation-call-backup'),self.actor,request_ref=self.request)
        resumed=self.runtime.continue_delivery(self.request,self.actor,bridge=self.bridge,reclaim_authorized=True,scope='fixture_only')
        self.assertEqual(resumed['step'],'close',resumed)
        for name,raw in before.items():self.assertEqual((destination/name).read_bytes(),raw)
        self.assertEqual(self.runtime._load()['runtime']['operations']['finish-A:preservation']['status'],'confirmed')

    def test_fixed_node_driver_uses_only_close_wait_and_original_results(self):
        self.resources()
        module=(ROOT/'skills/isolated-parallel-development-coordination/scripts/continuation_host.mjs')
        # Node handles the ordinary absolute module URL; the endpoint handles deep Python import paths.
        module_uri=Path(str(module).removeprefix('\\\\?\\')).as_uri()
        config=self.f.json('host-config.json',dict(python=sys.executable,profile=dict(project_root=str(self.runtime.project_root),
            state_root=str(self.f.root/'authority'),resource_state=str(self.bridge.controller.path),evidence_root=str(self.bridge.evidence)),
            requestMeta=dict(threadId=self.actor,sessionId='not-actor'),availableTools=['multi_agent_v1__close_agent','multi_agent_v1__wait_agent'],
            scope='fixture_only',permissions=dict(reclaim=True)))
        program="""
import fs from 'node:fs';
const {ContinuationHost}=await import(MODULE);
const cfg=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const request=JSON.parse(process.argv[2]),proof=JSON.parse(process.argv[3]);
const host=new ContinuationHost(cfg),calls=[];
const invoke=async(tool,args)=>{calls.push({tool,args});return tool.endsWith('close_agent') ?
  {previous_status:{completed:'fixture'}}:{status:{'native-A':'not_found'},timed_out:false};};
const first=await host.run(request,{inputs:{preservation:proof},invoke});
const again=await host.run(request,{inputs:{preservation:proof},invoke});
console.log(JSON.stringify({first,again,calls}));
""".replace('MODULE',json.dumps(module_uri))
        result=subprocess.run(['node','--input-type=module','-e',program,config['path'],json.dumps(self.request),json.dumps(self.proof)],
                              capture_output=True,encoding='utf-8',timeout=60,cwd=str(self.runtime.project_root))
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['first']['status'],'completed',value)
        self.assertEqual(value['again']['status'],'completed',value)
        self.assertEqual([call['tool'] for call in value['calls']],['multi_agent_v1__close_agent','multi_agent_v1__wait_agent'])


class MergeContinuationTests(unittest.TestCase):
    def test_approved_fixture_update_is_once_source_bound_and_session_loading_stays_unknown(self):
        import test_real_use_closeout as real_tests
        f=real_tests.RealUseTests('test_actual_git_required_then_completion_survives_later_main_and_duplicate')
        f.setUp();self.addCleanup(f.doCleanups)
        approval=load_bound(f.approval);approval['authorized']=['update_local_plugin_after_review']
        f.approval=f.json('approved-update.json',approval)
        state=f.runtime._load();state['tasks']['A']['requirements']['user_reference']=f.approval['path'];write_atomic(f.runtime.path,state)
        git=f.git_setup();f.accepted(git);proof=f.merge_proof()
        f.git('update-ref','refs/heads/main',git['head_sha'],git['target_sha']);f.call('record-merge',**proof)
        f.runtime.apply(dict(op='delegate',agent_id='integration',task_ids=['A'],merge_order=['A'],required_ci_jobs=['test'],
            routes={'worker-A':dict(target_id='native-A',existing_work_agent=True,evidence=f.file('fixture-route.txt'))},
            authority_evidence=f.file('fixture-delegation.txt'),user_reference='Explicit fixture integration/update responsibility'),f.actor)
        request=f.json('update-request.json',dict(schema='delivery-continuation/1',project_id=f.runtime.project_id,
            task_id='A',request_id='approved-update-A',approval=f.approval,installation_required=True))
        task=f.runtime._load()['tasks']['A'];evidence=f.file('update-evidence.txt')
        verified=f.json('installation-proof.json',dict(schema='stage-installation-verification/1',project_id=f.runtime.project_id,
            task_id='A',delivery_revision=task['delivery_revision'],source_head=git['head_sha'],installed_sha=task['merge']['main_sha'],
            reviewer_id='integration',status='passed',scope='fixture_only',environment='disposable fixture files only',
            backup_evidence=[evidence],installation_evidence=[evidence],real_task_evidence=[evidence]))
        calls=[]
        def install(context):calls.append(context);return verified
        result=f.runtime.continue_delivery(request,'integration',scope='fixture_only',install_authorized=True,install=install)
        self.assertTrue(result['task_completed']);self.assertEqual(result['installation']['status'],'verified',result)
        self.assertEqual(result['session_loaded'],'unknown');self.assertEqual(result['status'],'authorization_required')
        f.runtime.continue_delivery(request,'integration',scope='fixture_only',install_authorized=True,install=lambda _:self.fail('reinstall'))
        self.assertEqual(len(calls),1)
        Path(verified['path']).write_text('{}',encoding='utf-8')
        result=f.runtime.continue_delivery(request,'integration',scope='fixture_only',install_authorized=True)
        self.assertEqual(result['status'],'blocked');self.assertTrue(result['task_completed'])

    def test_real_merge_clean_gate_supports_deep_tracked_source_and_still_rejects_dirty(self):
        import test_real_use_closeout as real_tests
        from evidence import io_path
        f=real_tests.RealUseTests('test_actual_git_required_then_completion_survives_later_main_and_duplicate')
        f.setUp();self.addCleanup(f.doCleanups)
        git=f.git_setup()
        deep=io_path(f.root/'A'/('nested-'+'x'*140)/('y'*85)/'feature.txt')
        deep.parent.mkdir(parents=True);deep.write_text('Tracked deep fixture\n',encoding='utf-8')
        f.git('-c','core.longpaths=true','add','.')
        f.git('-c','core.longpaths=true','commit','-m','Deep tracked fixture')
        git['head_sha']=f.git('rev-parse','HEAD')
        f.accepted(git);proof=f.merge_proof()
        f.git('update-ref','refs/heads/main',git['head_sha'],git['target_sha'])
        deep.write_text('Actual dirty change\n',encoding='utf-8')
        with self.assertRaises(ValueError):f.call('record-merge',**proof)
        deep.write_text('Tracked deep fixture\n',encoding='utf-8')
        self.assertEqual(f.call('record-merge',**proof)['status'],'completed')

    def test_existing_real_git_merge_registers_after_review_and_acceptance_without_merging(self):
        import test_real_use_closeout as real_tests
        f=real_tests.RealUseTests('test_actual_git_required_then_completion_survives_later_main_and_duplicate')
        f.setUp();self.addCleanup(f.doCleanups)
        git=f.git_setup();f.deliver(git)
        review=f.review();acceptance=f.acceptance_authority();proof=f.merge_proof()
        f.git('update-ref','refs/heads/main',git['head_sha'],git['target_sha'])
        request=f.json('continue.json',dict(schema='delivery-continuation/1',project_id=f.runtime.project_id,
            task_id='A',request_id='merge-existing-A',approval=f.approval))
        command=f.json('actual-merge-command.json',dict(proof,action='record-merge',task_id='A',approval=f.approval))
        inputs=dict(review=review,acceptance_authority=acceptance,merge=command)
        result=f.runtime.continue_delivery(request,'integration',scope='fixture_only',inputs=inputs)
        self.assertEqual(result['status'],'authorization_required',result)
        self.assertTrue(result['task_completed']);self.assertIn('merge',result['confirmed'])
        before=f.runtime._load()['tasks']['A']['real_use_completion']
        again=f.runtime.continue_delivery(request,'integration',scope='fixture_only',inputs=inputs)
        self.assertTrue(again['task_completed'])
        self.assertEqual(f.runtime._load()['tasks']['A']['real_use_completion'],before)


class DispatchStartupRecoveryTests(unittest.TestCase):
    def test_handoff_reentry_sends_once_without_core_or_driver_lock(self):
        import test_dispatch
        f=test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        f.setUp();self.addCleanup(f.doCleanups)
        original=f.invoke;nested=[]
        def invoke(tool,args):
            self.assertFalse(f.runtime.path.with_suffix('.json.lock').exists())
            self.assertFalse((f.runtime.directory/'standard-dispatch-driver.lock').exists())
            nested.append(f.run_dispatch()['status'])
            return original(tool,args)
        f.integration.invoke=invoke
        self.assertEqual(f.run_dispatch()['status'],'dispatched')
        self.assertEqual(nested,['handoff_unknown'])
        self.assertEqual([tool for tool,_ in f.calls],['spawn_agent','wait_agent','send_input'])

    def test_same_attempt_reentry_and_changed_task_keep_actual_spawn_id_unknown(self):
        import test_dispatch
        f=test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        f.setUp();self.addCleanup(f.doCleanups)
        original=f.invoke;reentries=[]
        def concurrent(tool,args):
            state=f.runtime._load();launch=state['runtime']['operations']['dispatch-A:launch']
            reentries.append(f.runtime.launch('A',f.developer,attempt_id='dispatch-A:launch',admission=launch['admission'],
                authorized=True,message='irrelevant reentry cannot invoke',spawn=True,route_id=launch['route_id'])['status'])
            state=f.runtime._load();state['tasks']['A']['requirements']['scope']='Concurrent changed task scope'
            f.runtime._save(state,dict(op='fixture-same-task-changed',task_id='A'))
            return original(tool,args)
        f.developer.invoke=concurrent
        result=f.run_dispatch();self.assertEqual(result['status'],'unknown')
        launch=f.runtime._load()['runtime']['operations']['dispatch-A:launch']
        self.assertEqual(reentries,['intent'])
        self.assertEqual(launch['host_results'][0]['result']['target_id'],'native-fixture')
        self.assertEqual(launch['host_results'][0]['result']['status'],'accepted')
        self.assertEqual(f.runtime._load()['tasks']['A']['requirements']['scope'],'Concurrent changed task scope')
        self.assertIsNone(f.runtime._load()['tasks']['A']['owner_agent_id'])
        self.assertEqual(f.controller.read()[0]['capacity_requests']['dispatch-A:capacity']['status'],'granted')
        self.assertEqual(f.run_dispatch()['status'],'unknown');self.assertEqual(len(f.calls),1)

    def test_native_callback_does_not_hold_core_lock_or_overwrite_concurrent_fact(self):
        import test_dispatch
        f=test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        f.setUp();self.addCleanup(f.doCleanups)
        original=f.invoke
        def concurrent(tool,args):
            from coordination import writer_lock
            self.assertFalse((f.runtime.directory/'standard-dispatch-driver.lock').exists())
            with writer_lock(f.runtime.path):
                state=f.runtime._load();state['concurrent_fixture_fact']=tool
                f.runtime._save(state,dict(op='fixture-concurrent-native',task_id='A'))
            return original(tool,args)
        f.developer.invoke=concurrent
        self.assertEqual(f.run_dispatch()['status'],'dispatched')
        self.assertEqual(f.runtime._load()['concurrent_fixture_fact'],'wait_agent')

    @unittest.skipUnless(os.name == 'nt', 'Scoped dispatch recovery requires Windows process creation identity')
    def test_tree_and_admission_before_native_dead_locks_resume_same_request(self):
        import test_dispatch
        f=test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        f.setUp();self.addCleanup(f.doCleanups)
        request_ref=f.json('original-request.json',f.request)
        bootstrap=dict(protocol='standard-dispatch-host/1',request=request_ref,profile=dict(
            project_root=str(f.runtime.project_root),state_root=str(f.root/'authority'),resource_state=str(f.controller.path),
            evidence_root=str(f.bridge.evidence),integration_channel='native_agent',integration_target='integration-fixture'),
            host=dict(actor_id=f.actor,user_origin=f.origin,available_tools=['multi_agent_v1__spawn_agent','multi_agent_v1__wait_agent','send_input'],scope='fixture_only'))
        code="""
import sys,os
sys.path.insert(0,SCRIPTS);sys.path.insert(0,SOURCE)
import standard_dispatch
from codex_parallel_coordination.bridge import ResourceBridge
original=ResourceBridge.observe_worktree_creation
def die(self,*args,**kwargs):
    original(self,*args,**kwargs);os._exit(72)
ResourceBridge.observe_worktree_creation=die
standard_dispatch.main()
""".replace('SCRIPTS',repr(str(ROOT/'skills/isolated-parallel-development-coordination/scripts'))).replace('SOURCE',repr(str(ROOT/'src')))
        child=subprocess.run([sys.executable,'-X','utf8','-B','-c',code],input=json.dumps(bootstrap)+'\n',
                             capture_output=True,encoding='utf-8',timeout=30)
        self.assertEqual(child.returncode,72,child.stderr)
        self.assertEqual(f.calls,[]);self.assertTrue(Path(f.request['worktree']).exists())
        request=deepcopy(f.request)
        references=[]
        for target in (f.runtime.path,f.runtime.directory/'standard-dispatch-driver'):
            lock=target.with_suffix(target.suffix+'.lock')
            if lock.exists():
                references.append(ref(lock))
        self.assertTrue(references)  # Actual scoped locks, never fabricated PID-only replacements.
        f.runtime.recover_call_locks(references,str(f.bridge.evidence),f.actor,request_ref=request_ref,
                                    dispatch_evidence_root=str(f.bridge.evidence))
        self.assertEqual(f.run_dispatch()['status'],'dispatched')
        self.assertEqual(f.run_dispatch()['status'],'dispatched')
        self.assertEqual(f.request,request);self.assertEqual([tool for tool,_ in f.calls],['spawn_agent','wait_agent','send_input'])


class CallRecoveryTests(unittest.TestCase):
    setUp = DeliveryContinuationTests.setUp
    run_flow = DeliveryContinuationTests.run_flow
    def test_dead_locks_without_original_dispatch_are_retained(self):
        child=subprocess.Popen([sys.executable,'-B','-c','pass']);child.wait(timeout=10)
        targets=[self.runtime.path,self.runtime.directory/'standard-dispatch-driver']
        references=[]
        for target in targets:
            lock=target.with_suffix(target.suffix+'.lock')
            write_atomic(lock,dict(pid=child.pid,state=str(target)));references.append(ref(lock))
        with self.assertRaisesRegex(ValueError,'original StandardDispatch'):
            self.runtime.recover_call_locks(references,str(self.f.root/'dead-call-backup'),self.actor)
        for reference in references:self.assertEqual(ref(reference['path']),reference)

    def test_live_foreign_changed_locks_cannot_be_removed(self):
        lock=self.runtime.path.with_suffix('.json.lock');write_atomic(lock,dict(pid=os.getpid(),state=str(self.runtime.path)))
        before=lock.read_bytes()
        with self.assertRaisesRegex(ValueError,'alive/unknown'):
            self.runtime.recover_call_locks([ref(lock)],str(self.f.root/'live-backup'),self.actor)
        self.assertEqual(lock.read_bytes(),before)
        with self.assertRaises(ValueError):
            self.runtime.recover_call_locks([ref(lock)],str(self.f.root/'live-backup'),'integration')
        foreign=self.f.json('foreign.lock',dict(pid=99999999,state='foreign'))
        with self.assertRaisesRegex(ValueError,'only this runtime'):
            self.runtime.recover_call_locks([foreign],str(self.f.root/'live-backup'),self.actor)
        lock.unlink()


if __name__ == '__main__':unittest.main()
