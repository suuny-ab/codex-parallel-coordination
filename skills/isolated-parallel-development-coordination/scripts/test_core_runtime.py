"""Portable synthetic trajectories only. No native spawn/close, real project or Git."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
from unittest.mock import patch

from core_runtime import Runtime
from host_adapter import HostAdapter, APP, NATIVE
from evidence import load, ref, write_atomic
from test_coordination import LedgerFixture
from test_delivery import DeliveryFixture


class CoreFixture(LedgerFixture):
    def setUp(self):
        super().setUp()
        project=self.root/'project';project.mkdir()
        self.runtime=Runtime(self.root/'authority',project)
        self.runtime.initialize(self.state['coordinator_id'],worker_capacity=2)
        self.state=self.runtime._load();self.actor=self.state['coordinator_id']

    def store(self): write_atomic(self.runtime.path,self.state)
    def reload(self): self.state=self.runtime._load()
    def launch_fixture(self, key, adapter, **options):
        from routing_fixture import launch
        return launch(self, key, adapter, **options)
    def prepare(self,key='A',owner=None):
        work=self.root/key;work.mkdir(exist_ok=True)
        self.runtime.prepare(dict(task_id=key,owner_agent_id=owner,worktree=str(work),branch='task/'+key,
            scope='Synthetic isolated feature',acceptance=['Correct observable result'],user_reference='explicit fixture scope'),self.actor)
        self.reload()
    def permit(self,task='A',attempt='attempt1',executor=None):
        executor=executor or self.runtime._load()['tasks'][task]['owner_agent_id'] or 'spawn:'+attempt
        return self.json(attempt+'-permit.json',dict(project_id=self.runtime.project_id,task_id=task,attempt_id=attempt,
            request_id='request-'+attempt,permit_id='permit-'+attempt,status='granted',launch_identity=executor))
    def adapter(self,replies,channel='native_agent',tools=None):
        calls=[]
        def invoke(tool,args):
            calls.append((tool,args));result=replies[tool]
            return result(args) if callable(result) else deepcopy(result)
        return HostAdapter(channel,tools if tools is not None else list(NATIVE.values()),invoke,self.actor),calls
    def observed(self,identity='worker-A',**changes):
        state=self.runtime._load()
        facts=dict(executor_id=identity,generation=state['runtime']['executors'][identity]['generation'],
            based_on_revision=state['revision'],execution='completed',active_children=[],pending_work=False)
        facts.update(changes)
        self.runtime.observe_executor(identity,facts,self.actor,self.json('observed-'+str(state['revision'])+'.json',facts))
        self.reload()
    def bind(self,identity='worker-A'):
        self.runtime.bind_executor(identity,{'native_agent':dict(target_id='native-A',host_id='test-host')},
            self.actor,self.file('binding-authority.txt'))
        self.observed(identity)


class CoreRuntimeTests(CoreFixture):
    def test_unique_project_entry_recovery_is_non_destructive_and_two_projects_isolated(self):
        same=Runtime(self.root/'authority',self.root/'project'/'.')
        self.assertEqual(same.path,self.runtime.path)
        self.runtime.apply(dict(op='enqueue',item=dict(id='one',kind='discussion',title='One',prompt='Choose one')),self.actor)
        before=self.runtime.path.read_bytes()
        self.assertEqual(same.recover()['revision'],self.runtime._load()['revision'])
        with self.assertRaises(ValueError):same.initialize(self.actor)
        self.assertEqual(before,self.runtime.path.read_bytes())
        project=self.root/'other';project.mkdir();other=Runtime(self.root/'authority',project);other.initialize('other-main')
        self.assertNotEqual(other.path,same.path);self.assertEqual(other._load()['items'],{})

    def test_corrupt_recovery_does_not_replace_or_reset(self):
        self.runtime.path.write_bytes(b'broken');before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.runtime.recover()
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_pending_unknown_executor_can_recover_without_faking_identity(self):
        self.prepare()
        result=self.runtime.recover()
        self.assertIsNone(self.runtime._load()['tasks']['A']['owner_agent_id'])
        self.assertEqual(result['revision'],self.runtime._load()['revision'])

    def test_prepare_accepts_omitted_unknown_owner_without_fabricated_agent(self):
        work=self.root/'unknown';work.mkdir()
        self.runtime.prepare(dict(task_id='unknown',worktree=str(work),branch='unknown',scope='s',acceptance=['a'],user_reference='actual fixture'),self.actor)
        self.assertIsNone(self.runtime._load()['tasks']['unknown']['owner_agent_id'])

    def test_same_route_cannot_register_two_identities_even_with_extra_metadata(self):
        self.bind()
        routes={'native_agent':dict(target_id='native-A',host_id='test-host',extra='not another identity')}
        with self.assertRaises(ValueError):self.runtime.bind_executor('different-canonical',routes,self.actor,self.file('alias-authority'))

    def test_init_recover_are_actual_cli_entrypoints(self):
        script=Path(__file__).with_name('core_runtime.py')
        result=subprocess.run([sys.executable,'-B',str(script),'--state-root',str(self.root/'authority'),
            '--project-root',str(self.root/'project'),'recover'],capture_output=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['ledger'],str(self.runtime.path))

    def test_spawn_three_failures_publish_queue_not_running_then_actual_reuse(self):
        # Redacted trajectory: three host slot refusals, then reuse an observed
        # completed registered worker. No actual sender IDs or transcript text.
        self.prepare()
        adapter,calls=self.adapter({'spawn_agent':{'error':{'code':'agent_capacity_exceeded'}}})
        trajectory=load(Path(__file__).resolve().parents[1]/'fixtures/redacted-trajectories.json')
        for number in range(len(trajectory['spawn_failures'])):
            attempt='capacity-'+str(number)
            result=self.launch_fixture('A',adapter,attempt_id=attempt,admission=self.permit(attempt=attempt),
                authorized=True,message='same confirmed task',spawn=True)
            self.assertEqual(result['status'],'capacity');self.assertIn('排队',result['public_progress'])
            self.assertEqual(self.runtime._load()['tasks']['A']['worker_run']['status'],'queued')
            proof=self.runtime.release_receipt(attempt,self.actor)
            self.assertTrue(self.runtime.verify_release_receipt(load(proof['path']),proof)['not_started'])
            self.assertFalse(self.runtime.verify_release_receipt(dict(load(proof['path']),launch_identity='different-executor'),proof)['verified'])
        self.assertEqual([tool for tool,_ in calls],['spawn_agent']*3)
        self.bind()
        adapter,calls=self.adapter({'send_input':{'submission_id':'test-submit'},'wait_agent':{'status':{'native-A':'running'}}})
        result=self.launch_fixture('A',adapter,attempt_id='reuse',admission=self.permit(attempt='reuse',executor='worker-A'),
            authorized=True,message='continue original scope',executor_id='worker-A')
        self.assertEqual(result['status'],'running')
        self.assertEqual(self.runtime._load()['tasks']['A']['owner_agent_id'],'worker-A')
        self.assertNotIn('close_agent',[tool for tool,_ in calls])

    def test_unavailable_and_unknown_attempt_do_not_start_or_repeat(self):
        self.prepare()
        adapter,calls=self.adapter({},tools=[])
        result=self.launch_fixture('A',adapter,attempt_id='unsupported',admission=self.permit(attempt='unsupported'),
            authorized=True,message='scope',spawn=True)
        self.assertEqual(result['status'],'unsupported');self.assertEqual(calls,[])
        def timeout(_):raise TimeoutError('unknown response')
        adapter,calls=self.adapter({'spawn_agent':timeout})
        result=self.launch_fixture('A',adapter,attempt_id='unknown',admission=self.permit(attempt='unknown'),
            authorized=True,message='scope',spawn=True)
        self.assertEqual(result['status'],'unknown')
        self.launch_fixture('A',adapter,attempt_id='unknown',admission=self.permit(attempt='unknown'),
            authorized=True,message='scope',spawn=True)
        self.assertEqual(len(calls),1)
        with self.assertRaises(ValueError):self.runtime.release_receipt('unknown',self.actor)
        self.assertEqual(self.runtime._load()['tasks']['A']['worker_run']['status'],'queued')
        with self.assertRaisesRegex(ValueError,'uncertain'):
            self.launch_fixture('A',adapter,attempt_id='new-id-not-a-retry',admission=self.permit(attempt='new-id-not-a-retry'),
                authorized=True,message='scope',spawn=True)
        facts=self.json('no-execution.json',dict(project_id=self.runtime.project_id,task_id='A',operation_id='unknown',
            status='not_started',evidence=[self.file('host-no-start.txt')]))
        self.runtime.reconcile_launch('unknown',facts,self.actor)
        receipt=self.runtime.release_receipt('unknown',self.actor)
        self.assertTrue(self.runtime.verify_release_receipt(load(receipt['path']),receipt)['not_started'])

    def test_send_accepted_is_not_running_or_final(self):
        self.prepare(owner='worker-A');self.bind()
        adapter,_=self.adapter({'send_input':{'submission_id':'one'},'wait_agent':{'status':{'native-A':{'completed':'not task evidence'}}}})
        result=self.launch_fixture('A',adapter,attempt_id='accepted',admission=self.permit(attempt='accepted'),
            authorized=True,message='scope',executor_id='worker-A')
        self.assertEqual(result['status'],'accepted_unconfirmed')
        state=self.runtime._load();self.assertEqual(state['tasks']['A']['implementation'],'queued')
        self.assertEqual(state['tasks']['A']['delivery_revision'],0)

    def test_verified_fast_final_is_consumed_without_inventing_running_observation(self):
        first=self.delivery();self.receive(first);event=self.delivery(revision=2);self.store();self.bind()
        # Author and full report are real fixture artifacts, not acceptance from a send.
        self.runtime.apply(dict(op='queue',task_id='A',reason='reuse original author',user_reference='same fixture scope'),self.actor)
        adapter,_=self.adapter({'send_input':{'submission_id':'fast'},'wait_agent':{'status':{'native-A':{'completed':'turn done'}}}})
        self.launch_fixture('A',adapter,attempt_id='fast',admission=self.permit(attempt='fast'),
            authorized=True,message='same scope',executor_id='worker-A')
        self.runtime.receive(event,self.actor);self.runtime.apply(dict(op='drain'),self.actor)
        task=self.runtime._load()['tasks']['A']
        self.assertEqual(task['delivery_revision'],2);self.assertEqual(task['worker_run']['status'],'reported')
        self.assertEqual(self.runtime._load()['runtime']['operations']['fast']['status'],'reported')

    def test_real_host_tool_response_shape_app_and_native_capabilities(self):
        adapter,calls=self.adapter({APP['send']:{'content':[{'type':'text','text':json.dumps({'threadId':'thread-A'})}]},
            APP['observe']:{'schemaVersion':1,'thread':{'id':'thread-A','status':{'type':'idle'}},'turns':[{'status':'completed'}]}},
            channel='app_thread',tools=list(APP.values()))
        self.assertFalse(adapter.capabilities()['spawn']);self.assertFalse(adapter.capabilities()['idle_wakeup'])
        self.assertEqual(adapter.call('send',target='thread-A',message='test',authorized=True)['status'],'accepted')
        self.assertEqual(adapter.call('observe',target='thread-A',authorized=True)['execution'],'completed')
        self.assertNotIn('model',calls[0][1]);self.assertNotIn('thinking',calls[0][1])

    def test_app_wrong_identity_and_idle_without_turn_are_not_running(self):
        adapter,_=self.adapter({APP['observe']:{'thread':{'id':'wrong'},'turns':[{'status':'inProgress'}]}},
            channel='app_thread',tools=list(APP.values()))
        with self.assertRaises(ValueError):adapter.call('observe',target='thread-A',authorized=True)
        adapter,_=self.adapter({APP['observe']:{'thread':{'id':'thread-A','status':{'type':'idle'}}}},
            channel='app_thread',tools=list(APP.values()))
        self.assertEqual(adapter.call('observe',target='thread-A',authorized=True)['execution'],'unknown')

    def test_developer_cannot_write_or_self_grant_rights(self):
        self.prepare(owner='worker-A')
        with self.assertRaises(ValueError):self.runtime.apply(dict(op='authorize',task_id='A',rights=['merge'],user_reference='fake'), 'worker-A')
        with self.assertRaises(ValueError):self.runtime.apply(dict(op='start',task_id='A',user_reference='fake'),self.actor)
        self.assertFalse(self.runtime._load()['tasks']['A']['authorization']['merge']['allowed'])

    def test_closeout_revisions_match_and_atomic_human_successor_does_not_interrupt(self):
        self.human('current');self.human('next');self.apply(op='next');self.store()
        obs=self.observation([]);result=self.runtime.cycle(self.json('closeout.json',obs),self.actor)
        self.assertEqual(result['revision'],result['summary']['revision'])
        self.assertEqual(result['user_handoff']['kind'],'focus')
        next_result=self.runtime.resolve_and_next(dict(item_id='current',outcome='resolved',user_reference='actual fixture answer'),self.actor)
        self.assertEqual(next_result['next']['id'],'next')
        self.assertEqual(next_result['revision'],next_result['summary']['revision'])
        self.assertFalse(next_result['delivered_to_user'])

    def test_old_ledger_new_blocked_report_not_consumed_prevents_fake_wait(self):
        event=self.delivery(revision=3)
        self.store()
        worker=dict(task_id='A',owner_agent_id='worker-A',status='completed',last_delivery_revision=3,
            evidence=self.file('terminal-fact.json'))
        observation=self.observation([worker])
        result=self.runtime.cycle(self.json('missing-receipt.json',observation),self.actor)
        self.assertFalse(result['allow_end']);self.assertEqual(result['action'],'reconcile_required')
        self.reload();observation=self.observation([dict(worker,receipt=event)])
        result=self.runtime.cycle(self.json('consumed-final.json',observation),self.actor)
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery_revision'],3)
        self.assertEqual(result['revision'],result['summary']['revision'])
        self.assertEqual(result['user_handoff']['kind'],'human_item')

    def close_fixture(self,merged=True):
        event=self.delivery();self.receive(event);self.store();self.bind()
        self.reload();task=self.state['tasks']['A']
        task['worker_run'].update(attempt_id='original-run')
        self.state['runtime']['operations']['original-run']=dict(operation='send',status='running',task_id='A',
            attempt_id='original-run',request_id='request-original-run',permit_id='permit-original-run')
        if merged:task.update(phase='completed',merge=dict(verified=True,head_sha='a'*40,target_sha='b'*40))
        self.store()
        backup=self.root/'external-copy.json';backup.write_bytes(Path(event['report']['path']).read_bytes())
        merged_proof=self.json('main-proof.json',dict(project_id=self.runtime.project_id,task_id='A',source_head='a'*40,
            main_head='c'*40,main_ref='main',contains_source=True,scope='fixture_only',evidence=[self.file('main-observation.txt')]))
        preservation=self.json('preservation.json',dict(executor_id='worker-A',generation=1,
            files=[dict(ref(backup),original=event['report'])],merge_verification=merged_proof,
            handoff=self.file('knowledge-handoff.txt'),unregistered_evidence_preserved=True))
        return preservation

    def test_premerge_close_refused_even_when_report_completed_and_idle(self):
        preservation=self.close_fixture(merged=False)
        adapter,calls=self.adapter({'close_agent':{'closed':True}})
        with self.assertRaisesRegex(ValueError,'merged'):
            self.runtime.lifecycle('close','worker-A','A','close-1',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(calls,[])

    def test_close_success_idempotent_and_completed_task_not_resumed_for_bugfix(self):
        preservation=self.close_fixture();adapter,calls=self.adapter({'close_agent':{'closed':True},'resume_agent':{'resumed':True}})
        result=self.runtime.lifecycle('close','worker-A','A','close-1',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(result['status'],'closed')
        self.runtime.lifecycle('close','worker-A','A','close-1',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(len(calls),1)
        receipt=self.runtime.release_receipt('close-1',self.actor);context=load(receipt['path'])
        self.assertTrue(self.runtime.verify_release_receipt(context,receipt)['task_completed_on_main'])
        self.assertFalse(self.runtime.verify_release_receipt(dict(context,permit_id='wrong'),receipt)['verified'])
        with self.assertRaisesRegex(ValueError,'new task'):
            self.runtime.lifecycle('resume','worker-A','A','resume-2',1,adapter,admission=self.permit(attempt='resume-2'),authorized=True)
        self.assertTrue(self.runtime.verify_release_receipt(context,receipt)['verified'])
        state=self.runtime._load();self.assertEqual(state['runtime']['executors']['worker-A']['generation'],1)
        self.assertEqual(state['tasks']['A']['phase'],'completed')

    def test_unfinished_external_close_resume_needs_new_admission_and_does_not_claim_execution(self):
        self.close_fixture(merged=False);self.reload()
        # Simulated host loss, not an early cleanup decision or actual close.
        self.state['runtime']['executors']['worker-A']['lease']='closed';self.store()
        adapter,calls=self.adapter({'resume_agent':{'resumed':True}})
        bad=self.permit(attempt='wrong')
        with self.assertRaises(ValueError):self.runtime.lifecycle('resume','worker-A','A','resume-2',1,adapter,admission=bad,authorized=True)
        result=self.runtime.lifecycle('resume','worker-A','A','resume-2',1,adapter,admission=self.permit(attempt='resume-2'),authorized=True)
        self.assertEqual(result['status'],'resumed');self.assertEqual(len(calls),1)
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['execution'],'unknown')
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'awaiting_acceptance')

    def test_completed_or_failed_close_result_does_not_release_and_is_not_retried(self):
        preservation=self.close_fixture();adapter,calls=self.adapter({'close_agent':{'status':{'completed':'turn complete'}}})
        result=self.runtime.lifecycle('close','worker-A','A','close-unknown',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(result['status'],'unknown');self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'unknown')
        with self.assertRaises(ValueError):self.runtime.release_receipt('close-unknown',self.actor)
        self.runtime.lifecycle('close','worker-A','A','close-unknown',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(len(calls),1)
        with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','new-close-id',1,adapter,preservation_ref=preservation,authorized=True)
        observed=self.json('actual-closed.json',dict(operation_id='close-unknown',executor_id='worker-A',generation=1,
            closed=True,evidence=[self.file('host-confirmed-closed.txt')]))
        self.runtime.reconcile_lifecycle('close-unknown',observed,self.actor)
        receipt=self.runtime.release_receipt('close-unknown',self.actor)
        self.assertTrue(self.runtime.verify_release_receipt(load(receipt['path']),receipt)['verified'])

    def test_running_children_shared_publish_dependency_and_missing_handoff_refuse_close(self):
        preservation=self.close_fixture();baseline=self.runtime._load()
        adapter,calls=self.adapter({'close_agent':{'closed':True}})
        variants=[('runtime','execution','running'),('runtime','active_children',['child']),('runtime','pending_work',True),
                  ('runtime','shared',True),('runtime','protected',True),('task','publication_dependencies',['release'])]
        for kind,key,value in variants:
            state=deepcopy(baseline)
            target=state['runtime']['executors']['worker-A'] if kind=='runtime' else state['tasks']['A']
            target[key]=value;write_atomic(self.runtime.path,state)
            with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','close-'+key,1,adapter,preservation_ref=preservation,authorized=True)
        write_atomic(self.runtime.path,baseline)
        Path(load(preservation['path'])['handoff']['path']).unlink()
        with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','close-handoff',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(calls,[])

    def test_guard_blocks_reassignment_and_fixture_cannot_close_real_host(self):
        preservation=self.close_fixture();adapter,calls=self.adapter({'close_agent':{'closed':True}})
        with self.runtime.lifecycle_guard('worker-A','A','close-1',1,self.actor,preservation):
            with self.assertRaisesRegex(ValueError,'lock'):
                self.runtime.apply(dict(op='describe',task_id='A',reference='x',title='New'),self.actor)
        adapter.scope='host_verified'
        with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','real-disallowed',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(calls,[])

    def test_missing_report_is_quarantined_not_discarded_and_can_recover_same_revision(self):
        event=self.delivery();self.store()
        path=Path(event['report']['path']);data=path.read_bytes();path.unlink()
        result=self.runtime.receive(event,self.actor)
        self.assertEqual(result['receipt'],'quarantined')
        self.assertEqual(len(self.runtime._load()['unverified_receipts']),1)
        path.write_bytes(data)
        self.runtime.receive(event,self.actor)
        self.runtime.apply(dict(op='drain'),self.actor)
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery_revision'],1)


class MixedChannelTests(DeliveryFixture):
    def setUp(self):
        super().setUp()
        project=self.root/'project';project.mkdir()
        self.runtime=Runtime(self.root/'authority',project);self.runtime.initialize(self.state['coordinator_id'])
        self.state['project'].update(id=self.runtime.project_id,root=str(project))
        self.state['runtime']=dict(idle_wakeup=False,version=1,executors={},operations={})
        authors={}
        for key in ('A','B'):
            owner='worker-'+key;channels={};pairs={}
            for channel in ('native_agent','app_thread'):
                target=channel+'-'+key
                channels[channel]=dict(target_id=target,host_id='fixture-host',evidence=self.file(key+'-'+channel+'-route.txt'))
                mode='native_verified' if channel=='native_agent' else 'app_thread_verified'
                pairs[channel]=dict(outbound=self.json(key+channel+'-send.json',dict(sender_id='delivery-worker',recipient_id=owner,
                    target_id=target,status='accepted',mode=mode,correlation_id=target)),
                    inbound=self.json(key+channel+'-ack.json',dict(sender_id=owner,recipient_id='delivery-worker',
                    status='acknowledged',mode=mode,correlation_id=target)))
            self.state['delivery']['routes'][owner]['channels']=channels
            self.state['runtime']['executors'][owner]=dict(id=owner,routes=channels)
            authors[owner]=dict(channels=pairs)
        self.apply(op='delivery-transport',transport=dict(mode='mixed_verified',send_tool='send_input + existing_App_send',
            receive_tool='wait_agent + existing_App_read',creates_conversation=False,authors=authors))
        self.store()

    def store(self):write_atomic(self.runtime.path,self.state)
    def final(self,event,channel):
        proof=self.json('final-'+channel+'.json',dict(sender_id='worker-A',recipient_id='delivery-worker',
            sender_target_id=channel+'-A',event_id=event['event_id'],kind='task_final'))
        adapter=HostAdapter(channel,[],lambda *a:None,'delivery-worker')
        return adapter.final_receipt(self.state,'A',dict(event,recipient_agent_id='delivery-worker'),
            channel+'-A','delivery-worker',proof)

    def test_same_executor_native_app_final_deduplicates_after_both_provenances_checked(self):
        event=self.delivery();self.store()
        native=self.final(event,'native_agent');app=self.final(event,'app_thread')
        self.runtime.receive(native,'delivery-worker')
        self.assertEqual(self.runtime.receive(app,'delivery-worker')['receipt'],'duplicate')
        state=self.runtime._load()
        self.assertEqual(state['receipts'][event['event_id']]['payload'],native)
        self.assertEqual(len(state['runtime']['alternate_receipts']),1)
        self.runtime.apply(dict(op='drain'),'delivery-worker')
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery_revision'],1)

    def test_wrong_final_sender_or_recipient_and_later_proof_tamper_cannot_promote(self):
        event=self.delivery();self.store();native=self.final(event,'native_agent')
        adapter=HostAdapter('app_thread',[],lambda *a:None,'delivery-worker')
        with self.assertRaises(ValueError):adapter.final_receipt(self.state,'A',event,'different-target','delivery-worker',native['transport_evidence'])
        with self.assertRaises(ValueError):adapter.final_receipt(self.state,'A',event,'app_thread-A','old-recipient',native['transport_evidence'])
        self.runtime.receive(native,'delivery-worker')
        Path(native['transport_evidence']['path']).write_text('{}',encoding='utf-8')
        self.runtime.apply(dict(op='drain'),'delivery-worker')
        self.assertEqual(self.runtime._load()['receipts'][event['event_id']]['status'],'held')

    def test_launch_cannot_bypass_current_delivery_scope(self):
        # A third queued task is outside the explicitly delegated A/B scope.
        self.dispatch('C');self.state['tasks']['C']['worker_run']['status']='queued';self.store()
        adapter=HostAdapter('native_agent',list(NATIVE.values()),lambda *a:self.fail('host should not be called'),'delivery-worker')
        with self.assertRaises(ValueError):self.runtime.launch('C',adapter,attempt_id='outside',
            admission=self.file('unused-admission'),authorized=True,message='do not run',spawn=True)


class MainInclusionTests(DeliveryFixture):
    def setUp(self):
        super().setUp()
        project=self.root/'project';project.mkdir()
        self.runtime=Runtime(self.root/'authority',project);self.runtime.initialize(self.state['coordinator_id'])
        self.state['project'].update(id=self.runtime.project_id,root=str(project))
        self.state['runtime']=self.runtime._load()['runtime']
        self.ready('A')

    def proof(self,**changes):
        value=dict(project_id=self.runtime.project_id,task_id='A',based_on_revision=self.state['revision'],
            provider=self.state['project']['provider'],source_head=self.heads['A'],previous_main=self.main,
            main_ref='main',main_head='d'*40,contains_source=True,scope='fixture_only',evidence=[self.file('merge-observation.txt')])
        value.update(changes);write_atomic(self.runtime.path,self.state)
        return self.json('actual-inclusion-fixture.json',value)

    def test_explicit_verified_main_result_finishes_task_but_does_not_clean_or_merge(self):
        proof=self.proof()
        with patch('merge_gate.git',side_effect=AssertionError('No Git operation allowed')):
            result=self.runtime.record_main_inclusion(proof,'delivery-worker')
        self.assertEqual(result['status'],'completed');self.assertFalse(result['cleanup_authorized'])
        self.assertTrue(result['cleanup_required']);self.assertEqual(result['next_action'],'preserve_and_reclaim')
        self.assertEqual(self.runtime._load()['tasks']['A']['cleanup'],
                         dict(status='pending_cleanup',owner='delivery-worker',next_action='preserve_and_reclaim'))
        self.assertEqual(self.runtime._load()['tasks']['A']['merge']['main_sha'],'d'*40)

    def test_wrong_target_or_missing_merge_right_cannot_finish_task(self):
        proof=self.proof(previous_main='f'*40)
        with self.assertRaises(ValueError):self.runtime.record_main_inclusion(proof,'delivery-worker')
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'awaiting_merge')
        self.state['tasks']['A']['authorization']['merge']['allowed']=False
        proof=self.proof()
        with self.assertRaises(ValueError):self.runtime.record_main_inclusion(proof,'delivery-worker')

    def test_github_provider_uses_same_bound_proof_not_gitlab_only(self):
        self.state['project']['provider']='github';self.state['tasks']['A']['mr']['provider']='github'
        proof=self.proof(provider='github')
        result=self.runtime.record_main_inclusion(proof,'delivery-worker')
        self.assertEqual(result['status'],'completed')

    def test_record_main_is_a_real_cli_entry_without_remote_git_actions(self):
        proof=self.proof()
        result=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('core_runtime.py')),
            '--state-root',str(self.root/'authority'),'--project-root',str(self.root/'project'),
            'record-main',proof['path'],'--actor','delivery-worker'],capture_output=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'completed')
