"""Public task preparation/completion; native effects are explicit local fixtures."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import unittest

import test_core_runtime as fixtures
from evidence import load_bound, pack, ref
from host_adapter import HostAdapter


class ReadOnlyFixture(fixtures.CoreFixture):
    reviewer = 'delivery-worker'

    def delivered(self, mode='delivery_verified', kind='read_only', managed_input=False):
        work = self.runtime.directory/'resources/audited-input' if managed_input else self.root/'A'
        work.mkdir(parents=True);(work/'feature.py').write_text('original source\n',encoding='utf-8')
        # Historical task fixture predates demand intake. New prepare no longer
        # creates read-only developers; its completion protocol remains intact.
        spec=dict(task_id='A',owner_agent_id='worker-A',worktree=str(work),branch='audit/A',
            task_kind=kind,acceptance_type=mode,scope='Read existing source without changes',
            acceptance=['Verified findings'],user_reference='explicit historical scope and acceptance mode')
        self.runtime.apply(dict(spec,op='register'),self.actor)
        self.runtime.apply(dict(op='confirm',task_id='A',**{k:spec[k] for k in ('scope','acceptance','user_reference')}),self.actor)
        self.runtime.apply(dict(op='queue',task_id='A',reason='Historical controlled worker',user_reference=spec['user_reference']),self.actor)
        self.bind()
        self.runtime.apply(dict(op='delegate',agent_id=self.reviewer,task_ids=['A'],merge_order=['A'],
            required_ci_jobs=[] if kind=='read_only' else ['code-only-job'],routes={'worker-A':dict(target_id='native-A',existing_work_agent=True,
                evidence=self.file('route-authority'))},authority_evidence=self.file('delivery-authority'),
            user_reference='sole independent delivery reviewer'),self.actor)
        pair=dict(outbound=self.json('sent.json',dict(sender_id=self.reviewer,recipient_id='worker-A',target_id='native-A',
            status='accepted',mode='fixture',correlation_id='one')),
            inbound=self.json('ack.json',dict(sender_id='worker-A',recipient_id=self.reviewer,status='acknowledged',mode='fixture',correlation_id='one')))
        self.runtime.apply(dict(op='delivery-transport',transport=dict(mode='fixture',send_tool='fixture-send',receive_tool='fixture-read',
            creates_conversation=False,authors={'worker-A':pair})),self.reviewer)
        adapter=HostAdapter('native_agent',['send_input','wait_agent'],
            lambda tool,args: {'id':'native-A'} if tool=='send_input' else {'status':{'native-A':'running'}},self.reviewer)
        self.launch_fixture('A',adapter,attempt_id='audit-run',admission=self.permit(attempt='audit-run',executor='worker-A'),
            authorized=True,message='Read-only fixture',executor_id='worker-A')
        report=pack(dict(project_id=self.runtime.project_id,task_id='A',delivery_revision=1,summary='Read-only findings',
            task_kind=kind,acceptance_type=mode,
            source_root=str(work),evidence=[self.file('findings.log')],self_test=dict(status='passed',command='bounded source inspection'),
            quality_review=dict(status='passed'),entry=dict(kind='file',target=self.file('findings.html')['path']),
            steps=['Read findings'],expected='Source unchanged and findings verified'))
        self.report=self.json('current-report.json',report)
        self.runtime.receive(dict(project_id=self.runtime.project_id,task_id='A',owner_agent_id='worker-A',recipient_agent_id=self.reviewer,
            channel='native_agent',kind='completed',delivery_revision=1,event_id='A:1:completed',report=self.report),self.reviewer)
        self.runtime.apply(dict(op='drain'),self.reviewer)
        self.input=work

    def proof(self, **changes):
        state=self.runtime._load();task=state['tasks']['A'];report=load_bound(self.report)
        value=dict(project_id=self.runtime.project_id,task_id='A',based_on_revision=state['revision'],task_kind='read_only',
            acceptance_type=task.get('acceptance_type','delivery_verified'),owner_agent_id='worker-A',reviewer_id=self.reviewer,
            delivery_revision=task['delivery_revision'],report=self.report,source_digest=report['source']['digest'],
            source_baseline_digest=report['source']['digest'],requirements_reference=task['requirements']['user_reference'],
            authority=state['delivery']['authority_evidence'],status='passed',code_changes=False,scope='fixture_only',
            evidence=[self.file('independent-verification')])
        value.update(changes);return self.json('verification-'+str(state['revision'])+'-'+str(len(changes))+'.json',value)

    def accept(self):
        task=self.runtime._load()['tasks']['A']
        self.runtime.apply(dict(op='record-acceptance',task_id='A',scope='confirmed_task_scope',acceptance_type='user_required',
            delivery_revision=task['delivery_revision'],report=self.report,user_reference='actual fixture human accepted this revision',
            evidence=self.file('user-acceptance')),self.actor)

    def preserve(self, proof):
        state=self.runtime._load();facts=dict(executor_id='worker-A',generation=1,based_on_revision=state['revision'],
            execution='completed',active_children=[],pending_work=False)
        self.runtime.observe_executor('worker-A',facts,self.reviewer,self.json('current-observation.json',facts))
        backup=self.root/'preserved-report.json';backup.write_bytes(Path(self.report['path']).read_bytes())
        return self.json('preservation.json',dict(executor_id='worker-A',generation=1,completion_verification=proof,
            files=[dict(ref(backup),original=self.report)],handoff=self.file('handoff'),unregistered_evidence_preserved=True))


class ReadOnlyCompletionTests(ReadOnlyFixture):
    def test_delivery_verified_completion_is_public_idempotent_and_needs_no_mr_ci(self):
        self.delivered();before=self.runtime._load()
        self.assertFalse(any(i['kind']=='acceptance' for i in before['items'].values()))
        proof=self.proof();result=self.runtime.complete_read_only(proof,self.reviewer)
        state=self.runtime._load();task=state['tasks']['A']
        self.assertEqual(task['phase'],'completed');self.assertIsNone(task['merge']);self.assertIsNone(task['mr'])
        self.assertFalse(result['cleanup_completed']);self.assertFalse(result['cleanup_authorized'])
        self.assertTrue(all(not x['allowed'] for x in task['authorization'].values()))
        saved=self.runtime.path.read_bytes()
        self.assertEqual(self.runtime.complete_read_only(proof,self.reviewer),result)
        self.assertEqual(saved,self.runtime.path.read_bytes())

    def test_user_required_waits_for_current_actual_acceptance(self):
        self.delivered('user_required')
        with self.assertRaises(ValueError):self.runtime.complete_read_only(self.proof(),self.reviewer)
        self.accept();self.runtime.complete_read_only(self.proof(),self.reviewer)
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'completed')

    def test_source_changes_fail_before_completion(self):
        self.delivered();proof=self.proof();(self.input/'feature.py').write_text('changed',encoding='utf-8')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.runtime.complete_read_only(proof,self.reviewer)
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_wrong_kind_mode_revision_owner_and_actor_are_rejected(self):
        self.delivered()
        for field,value in [('task_kind','code'),('acceptance_type','user_required'),('delivery_revision',0),
                            ('owner_agent_id','other'),('code_changes',True),('based_on_revision',-1)]:
            with self.subTest(field=field):
                before=self.runtime.path.read_bytes()
                with self.assertRaises(ValueError):self.runtime.complete_read_only(self.proof(**{field:value}),self.reviewer)
                self.assertEqual(before,self.runtime.path.read_bytes())
        with self.assertRaises(ValueError):self.runtime.complete_read_only(self.proof(),'worker-A')

    def test_code_and_legacy_cannot_use_read_only_completion(self):
        self.delivered('user_required','code')
        with self.assertRaises(ValueError):self.runtime.complete_read_only(self.proof(),self.reviewer)

    def test_completion_evidence_and_current_authority_are_rechecked(self):
        self.delivered();proof=self.proof();self.runtime.complete_read_only(proof,self.reviewer)
        authority=self.runtime._load()['delivery']['authority_evidence']
        Path(authority['path']).write_text('revoked',encoding='utf-8')
        with self.assertRaises(ValueError):self.runtime.complete_read_only(proof,self.reviewer)

    def test_complete_preserve_canonical_close_and_real_owned_file_cleanup(self):
        self.delivered();proof=self.proof();self.runtime.complete_read_only(proof,self.reviewer);preserved=self.preserve(proof)
        calls=[];adapter=HostAdapter('native_agent',['close_agent'],lambda tool,args:calls.append(tool) or {'closed':True},self.reviewer)
        self.runtime.lifecycle('close','worker-A','A','close-audit',1,adapter,preservation_ref=preserved,authorized=True)
        receipt=self.runtime.release_receipt('close-audit',self.reviewer)
        flags=self.runtime.verify_release_receipt(load_bound(receipt),receipt)
        self.assertTrue(flags['task_completion_verified']);self.assertTrue(flags['read_only_completion_verified'])
        self.assertFalse(flags['task_completed_on_main']);self.assertEqual(flags['task_kind'],'read_only')
        path=Path(self.runtime._load()['runtime']['cleanup_root'])/'environment';path.mkdir(parents=True)
        self.runtime.register_resource('A',dict(id='env',kind='environment',path=str(path)),self.file('environment-authority'),self.reviewer)
        def remove(resource):
            self.assertTrue(self.runtime.verify_cleanup_receipt(dict(resource,resource_id=resource['id']),resource['cleanup_proof'])['read_only_completion_verified'])
            shutil.rmtree(Path(resource['path']));return dict(status='cleaned',evidence=self.file('removed'))
        result=self.runtime.cleanup_task('A','cleanup-audit',preserved,remove,self.reviewer,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed');self.assertFalse(path.exists());self.assertEqual(calls,['close_agent'])

    def test_current_cli_entry_finishes_without_direct_ledger_edit(self):
        self.delivered();proof=self.proof()
        command=[sys.executable,'-B',str(Path(__file__).with_name('core_runtime.py')),
            '--state-root',str(self.root/'authority'),'--project-root',str(self.root/'project'),
            'complete-read-only',proof['path'],'--actor',self.reviewer]
        result=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertTrue(json.loads(result.stdout)['task_completed'])

    def test_missing_kind_defaults_to_code_and_read_only_requires_explicit_mode(self):
        work=self.root/'legacy';work.mkdir();(work/'source.txt').write_text('unchanged')
        spec=dict(task_id='legacy',owner_agent_id=None,worktree=str(work),branch='legacy',scope='Audit',acceptance=['Finding'],user_reference='fixture scope')
        self.runtime.prepare(spec,self.actor)
        self.assertEqual(self.runtime._load()['tasks']['legacy']['task_kind'],'code')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.runtime.prepare(dict(spec,task_id='new',branch='new',task_kind='read_only',worktree=str(self.root/'new')),self.actor)
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_definition_tamper_and_direct_completed_without_record_are_rejected(self):
        self.delivered();baseline=self.runtime._load()
        from copy import deepcopy
        from coordination import validate
        for changes in (dict(task_kind='code'),dict(acceptance_type='user_required'),dict(phase='completed')):
            state=deepcopy(baseline);state['tasks']['A'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate(state)

    def test_changed_report_or_verification_evidence_cannot_complete_or_close(self):
        self.delivered();proof=self.proof()
        report_path=Path(self.report['path']);original=report_path.read_bytes();report_path.write_bytes(original+b' ')
        with self.assertRaises(ValueError):self.runtime.complete_read_only(proof,self.reviewer)
        report_path.write_bytes(original)
        self.runtime.complete_read_only(proof,self.reviewer);preserved=self.preserve(proof)
        checked=load_bound(proof);Path(checked['evidence'][0]['path']).write_text('changed',encoding='utf-8')
        calls=[];adapter=HostAdapter('native_agent',['close_agent'],lambda *args:calls.append(args) or {'closed':True},self.reviewer)
        with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','no-close',1,adapter,preservation_ref=preserved,authorized=True)
        self.assertEqual(calls,[])

    def test_actual_user_evidence_is_revision_bound_and_cannot_be_delivery_authored(self):
        self.delivered('user_required');task=self.runtime._load()['tasks']['A']
        operation=dict(op='record-acceptance',task_id='A',scope='confirmed_task_scope',acceptance_type='user_required',
            delivery_revision=1,report=self.report,user_reference='fixture current human',evidence=self.file('human-proof'))
        with self.assertRaises(ValueError):self.runtime.apply(operation,self.reviewer)
        with self.assertRaises(ValueError):self.runtime.apply(dict(operation,delivery_revision=0),self.actor)
        self.accept();self.runtime.complete_read_only(self.proof(),self.reviewer)

    def test_source_removal_is_only_trusted_after_confirmed_owned_cleanup_and_partial_retry(self):
        self.delivered(managed_input=True);proof=self.proof();self.runtime.complete_read_only(proof,self.reviewer);preserved=self.preserve(proof)
        adapter=HostAdapter('native_agent',['close_agent'],lambda *args:{'closed':True},self.reviewer)
        self.runtime.lifecycle('close','worker-A','A','close-for-cleanup',1,adapter,preservation_ref=preserved,authorized=True)
        self.runtime.release_receipt('close-for-cleanup',self.reviewer)
        for rid,path in [('input',self.input),('other',self.input.parent/'other')]:
            path.mkdir(exist_ok=True)
            self.runtime.register_resource('A',dict(id=rid,kind='worktree' if rid=='input' else 'environment',path=str(path)),self.file(rid+'-authority'),self.reviewer)
        def partial(resource):
            if resource['id']=='other':return dict(status='failed',reason='safe physical refusal')
            shutil.rmtree(resource['path']);return dict(status='cleaned',evidence=self.file('input-confirmed-removed'))
        first=self.runtime.cleanup_task('A','partial-readonly',preserved,partial,self.reviewer,authorized=True)
        self.assertEqual(first['status'],'pending_cleanup');self.assertFalse(self.input.exists())
        def remaining(resource):
            self.assertEqual(resource['id'],'other');shutil.rmtree(resource['path'])
            return dict(status='cleaned',evidence=self.file('other-confirmed-removed'))
        final=self.runtime.cleanup_task('A','partial-retry',preserved,remaining,self.reviewer,authorized=True)
        self.assertEqual(final['status'],'cleanup_completed')

    def test_shared_or_active_child_still_blocks_read_only_close(self):
        self.delivered();proof=self.proof();self.runtime.complete_read_only(proof,self.reviewer);preserved=self.preserve(proof)
        state=self.runtime._load();facts=dict(executor_id='worker-A',generation=1,based_on_revision=state['revision'],
            execution='completed',active_children=['child'],pending_work=False)
        self.runtime.observe_executor('worker-A',facts,self.reviewer,self.json('actual-child-observation',facts))
        calls=[];adapter=HostAdapter('native_agent',['close_agent'],lambda *args:calls.append(args) or {'closed':True},self.reviewer)
        with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','protected-close',1,adapter,preservation_ref=preserved,authorized=True)
        self.assertEqual(calls,[])

    def test_read_only_cannot_use_mr_or_main_entry_and_main_code_gate_is_not_relaxed(self):
        self.delivered();before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.runtime.apply(dict(op='mr',task_id='A',mr={}),self.reviewer)
        with self.assertRaises(ValueError):self.runtime.record_main_inclusion(self.proof(),self.reviewer)
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_failed_proof_scope_and_missing_verification_cannot_authorize_host_close(self):
        self.delivered()
        for changes in (dict(status='failed'),dict(scope='unknown'),dict(evidence=[]),dict(authority=self.file('not-current-authority'))):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.runtime.complete_read_only(self.proof(**changes),self.reviewer)
        proof=self.proof();self.runtime.complete_read_only(proof,self.reviewer);preserved=self.preserve(proof)
        calls=[];adapter=HostAdapter('native_agent',['close_agent'],lambda *args:calls.append(args) or {'closed':True},self.reviewer,scope='host_verified')
        with self.assertRaises(ValueError):self.runtime.lifecycle('close','worker-A','A','scope-escalation',1,adapter,preservation_ref=preserved,authorized=True)
        self.assertEqual(calls,[])

    def test_reconfirmation_cannot_rebaseline_source_changes(self):
        self.delivered()
        self.runtime.apply(dict(op='queue',task_id='A',reason='verify same input again',user_reference='existing scope retry'),self.reviewer)
        (self.input/'feature.py').write_text('changed source',encoding='utf-8')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):
            self.runtime.apply(dict(op='confirm',task_id='A',scope='Read existing source without changes',acceptance=['Verified findings'],
                user_reference='existing scope retry'),self.actor)
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_user_required_atomic_human_close_preserves_real_acceptance_path(self):
        self.delivered('user_required')
        self.runtime.apply(dict(op='next'),self.actor)
        self.runtime.resolve_and_next(dict(item_id='A:1:completed',outcome='accepted',user_reference='current fixture user passed',
            evidence=self.file('actual-human-outcome')),self.actor)
        self.runtime.complete_read_only(self.proof(),self.reviewer)
        self.assertEqual(self.runtime._load()['tasks']['A']['acceptance_record']['accepted_by'],self.actor)

    def test_unknown_read_only_close_does_not_release_until_canonical_reconciliation(self):
        self.delivered();proof=self.proof();self.runtime.complete_read_only(proof,self.reviewer);preserved=self.preserve(proof)
        calls=[];adapter=HostAdapter('native_agent',['close_agent'],lambda *args:calls.append(args) or {'status':'completed'},self.reviewer)
        result=self.runtime.lifecycle('close','worker-A','A','unknown-audit-close',1,adapter,preservation_ref=preserved,authorized=True)
        self.assertEqual(result['status'],'unknown')
        with self.assertRaises(ValueError):self.runtime.release_receipt('unknown-audit-close',self.reviewer)
        self.runtime.lifecycle('close','worker-A','A','unknown-audit-close',1,adapter,preservation_ref=preserved,authorized=True)
        self.assertEqual(len(calls),1)
        observed=self.json('actual-close-reconciliation',dict(operation_id='unknown-audit-close',executor_id='worker-A',
            generation=1,closed=True,evidence=[self.file('actual-close-proof')]))
        self.runtime.reconcile_lifecycle('unknown-audit-close',observed,self.reviewer)
        receipt=self.runtime.release_receipt('unknown-audit-close',self.reviewer)
        self.assertTrue(self.runtime.verify_release_receipt(load_bound(receipt),receipt)['read_only_completion_verified'])
