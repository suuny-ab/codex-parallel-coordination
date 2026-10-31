"""Scoped real-use transitions; all identities, Git and host observations are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from evidence import fingerprint, load_bound, ref
from test_core_runtime import CoreFixture


class RealUseTests(CoreFixture):
    def setUp(self):
        super().setUp()
        self.prepare(owner='worker-A')
        (self.root/'A/feature.py').write_text('feature\n', encoding='utf-8')
        self.bind()
        adapter,_=self.adapter({'send_input':{'submission_id':'fixture'},'wait_agent':{'status':{'native-A':'running'}}})
        self.launch_fixture('A',adapter,attempt_id='fixture-start',admission=self.permit(attempt='fixture-start'),
            authorized=True,message='fixture approved scope',executor_id='worker-A')
        self.reload()
        self.serial = 0
        self.approval = self.json('approval.json', dict(schema='approved-dispatch-plan/1', approval='approved',
            source='direct user message in original main conversation', main_conversation=self.actor,
            integration_agent='integration', user_quote='Approve this task and independent reviewer',
            tasks=[dict(id='A', worktree=str(self.root/'A'), branch='task/A')]))
        # The fixture's original approved document is external to its source.
        self.reload();self.state['tasks']['A']['requirements']['user_reference']=self.approval['path'];self.store()

    def call(self, action, actor='integration', **fields):
        return self.runtime.real_use(dict(action=action, task_id='A', approval=self.approval, **fields), actor)

    def deliver(self, git=None, revision=1):
        self.reload()
        packet = self.delivery(quality='pending', revision=revision)
        if git:
            path = Path(packet['report']['path']); value=load_bound(packet['report']); value['git']=git
            path.write_text(json.dumps(value), encoding='utf-8'); packet['report']=ref(path)
        self.receive(packet); self.store()
        return packet

    def review(self):
        task=self.runtime._load()['tasks']['A']; report=load_bound(task['delivery'])
        return self.json('review.json',dict(schema='independent-quality-review/1', project_id=self.runtime.project_id,
            task_id='A', delivery_revision=task['delivery_revision'], report=task['delivery'],
            source_digest=report['source']['digest'], reviewer_id='integration', owner_agent_id='worker-A',
            head_sha=report.get('git',{}).get('head_sha'), target_sha=report.get('git',{}).get('target_sha'),
            status='passed', command='independent fixture checks', evidence=[self.file('independent.log')],
            ci=self.ci(report.get('git',{}).get('head_sha'))))

    def ci(self,head):
        return dict(policy='not_configured',head_sha=head,
            statuses=self.json('statuses.json',dict(sha=head,statuses=[],total_count=0)),
            runs=self.json('runs.json',dict(workflow_runs=[],total_count=0)))

    def acceptance_authority(self, mode='delivery_verified'):
        task=self.runtime._load()['tasks']['A']
        return self.json('acceptance-authority.json',dict(schema='technical-acceptance-authorization/1',
            project_id=self.runtime.project_id, task_id='A', main_conversation=self.actor,
            requirements_digest=fingerprint(task['requirements']), acceptance_type=mode,
            legacy_encoding_only=True, source='direct user message in original main conversation',
            user_quote='Technical verification is automatic; the legacy user_required encoding was a tool limitation',
            evidence=[self.file('user-instruction.txt')]))

    def reviewed(self, git=None):
        self.deliver(git); self.call('quality', review=self.review())

    def accepted(self, git=None):
        self.reviewed(git)
        self.call('technical-acceptance', authority=self.acceptance_authority())

    def test_quality_attaches_without_mutating_author_report_and_duplicates_do_not_write(self):
        packet=self.deliver(); before=Path(packet['report']['path']).read_bytes(); review=self.review()
        self.call('quality',review=review)
        state=self.runtime._load(); self.assertEqual(state['tasks']['A']['quality_review'],'passed')
        self.assertEqual(before,Path(packet['report']['path']).read_bytes())
        from evidence import report_for
        report_for(state,'A')
        ledger=self.runtime.path.read_bytes(); self.assertTrue(self.call('quality',review=review)['duplicate'])
        self.assertEqual(ledger,self.runtime.path.read_bytes())

    def test_self_review_wrong_task_revision_source_and_actor_rejected_atomically(self):
        self.deliver(); original=load_bound(self.review())
        for changes in ({'reviewer_id':'worker-A'},{'task_id':'other'},{'delivery_revision':2},
                        {'source_digest':'wrong'},{'report':self.file('other-report')}):
            self.serial+=1; before=self.runtime.path.read_bytes()
            with self.assertRaises((ValueError,KeyError)):
                self.call('quality',review=self.json(str(self.serial)+'.json',dict(original,**changes)))
            self.assertEqual(before,self.runtime.path.read_bytes())
        with self.assertRaises(ValueError): self.call('quality',actor='worker-A',review=self.review())

    def test_automatic_technical_acceptance_keeps_user_pending_and_confirmed_scope(self):
        self.reviewed(); prior=deepcopy(self.runtime._load()['tasks']['A']['requirements'])
        result=self.call('technical-acceptance',authority=self.acceptance_authority())
        task=self.runtime._load()['tasks']['A']
        self.assertEqual(task['user_acceptance'],'pending'); self.assertEqual(task['requirements'],prior)
        self.assertEqual(task['phase'],'awaiting_integration'); self.assertTrue(result['technical_accepted'])
        self.assertEqual(self.runtime._load()['items']['A:1:completed']['outcome'],'technical_verified')

    def test_human_required_and_active_acceptance_cannot_be_bypassed(self):
        self.reviewed(); before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.call('technical-acceptance',authority=self.acceptance_authority('user_required'))
        self.assertEqual(before,self.runtime.path.read_bytes())
        self.runtime.apply(dict(op='next'),self.actor)
        with self.assertRaises(ValueError):self.call('technical-acceptance',authority=self.acceptance_authority())

    def test_quality_and_automatic_acceptance_revalidate_source_and_original_authority(self):
        self.accepted()
        from real_use_closeout import code_acceptance
        code_acceptance(self.runtime._load(),'A')
        (self.root/'A/feature.py').write_text('changed\n',encoding='utf-8')
        with self.assertRaises(ValueError):code_acceptance(self.runtime._load(),'A')

    def cli(self, final='report', terminal='turn.completed'):
        # Rebuild this *fixture* as an unowned pending task, through public preparation.
        self.prepare('C')
        state=self.runtime._load(); task=state['tasks']['C']
        approval=self.json('cli-approval.json',dict(schema='approved-dispatch-plan/1',approval='approved',
            source='direct user message in original main conversation',main_conversation=self.actor,
            integration_agent='integration',user_quote='Approve C',tasks=[dict(id='C',worktree=task['worktree'],branch=task['branch'])]))
        state['tasks']['C']['requirements']['user_reference']=approval['path'];self.state=state;self.store()
        log=self.file('worker.jsonl','\n'.join(json.dumps(x) for x in [dict(type='thread.started',thread_id='actual-cli'),
            dict(type='turn.started'),dict(type='item.completed',item=dict(type='agent_message',text=final)),dict(type=terminal)]))
        launch=self.json('launch.json',dict(task_id='C',transport='codex_exec_child_process',pid=12345,
            worktree=task['worktree'],branch=task['branch'],authority=approval['path'],stdout=log['path'],native_agent_id=None))
        command=dict(action='bind-cli',task_id='C',approval=approval,launch=launch,log=log,thread_id='actual-cli')
        return command,log

    def test_cli_binding_is_truthful_idempotent_and_does_not_create_native_operations(self):
        command,log=self.cli()
        operations=deepcopy(self.runtime._load()['runtime']['operations'])
        self.runtime.real_use(command,'integration'); before=self.runtime.path.read_bytes()
        self.assertTrue(self.runtime.real_use(command,'integration')['duplicate'])
        self.assertEqual(before,self.runtime.path.read_bytes()); state=self.runtime._load()
        self.assertEqual(state['tasks']['C']['owner_agent_id'],'actual-cli')
        self.assertEqual(set(state['runtime']['executors']['actual-cli']['routes']),{'codex_exec'})
        self.assertEqual(state['runtime']['operations'],operations)
        self.assertEqual(state['tasks']['C']['worker_run']['status'],'awaiting_receipt')
        self.assertEqual(state['tasks']['C']['delivery_revision'],0)

    def test_cli_exit_without_terminal_or_missing_report_cannot_finish_task(self):
        command,log=self.cli(final='',terminal='process.exited')
        self.runtime.real_use(command,'integration')
        self.assertEqual(self.runtime._load()['tasks']['C']['worker_run']['status'],'unknown')
        with self.assertRaises((ValueError,KeyError)):
            self.runtime.real_use(dict(action='receive-cli',task_id='C',approval=command['approval'],
                observation=log,observation_kind='codex_exec',report=self.file('missing-report')), 'integration')
        self.assertNotEqual(self.runtime._load()['tasks']['C']['phase'],'completed')

    def cli_report(self, command, log_status='turn.completed'):
        from evidence import pack
        work=self.root/'C';(work/'feature.py').write_text('CLI feature\n',encoding='utf-8')
        report=self.json('cli-report.json',pack(dict(project_id=self.runtime.project_id,task_id='C',delivery_revision=1,
            summary='CLI delivery',source_root=str(work),evidence=[self.file('cli-tests.log')],
            self_test=dict(status='passed',command='fixture CLI checks'),quality_review=dict(status='pending'),
            entry=dict(kind='file',target=self.file('cli-entry.html')['path']),steps=['Inspect result'],expected='Approved behavior')))
        observation=self.file('cli-final.jsonl','\n'.join(json.dumps(e) for e in [
            dict(type='thread.started',thread_id='actual-cli'),dict(type='turn.started'),
            dict(type='item.completed',item=dict(type='agent_message',text=report['path'])),dict(type=log_status)]))
        return dict(action='receive-cli',task_id='C',approval=command['approval'],report=report,
            observation=observation,observation_kind='codex_exec')

    def test_cli_receipt_reobserved_dedup_keeps_original_origin_and_no_other_receipts_consumed(self):
        command,log=self.cli();self.runtime.real_use(command,'integration')
        receive=self.cli_report(command);before=deepcopy(self.runtime._load()['tasks']['A'])
        self.runtime.real_use(receive,'integration');state=self.runtime._load()
        self.assertEqual(state['tasks']['C']['implementation'],'completed')
        self.assertNotEqual(state['tasks']['C']['phase'],'completed')
        self.assertEqual(state['tasks']['C']['quality_review'],'pending')
        self.assertEqual(state['tasks']['A'],before)
        prior=deepcopy(state['receipts']['C:1:completed'])
        receive['observation']=self.file('cli-final-again.jsonl',Path(receive['observation']['path']).read_text())
        ledger=self.runtime.path.read_bytes();result=self.runtime.real_use(receive,'integration')
        self.assertTrue(result['duplicate']);self.assertEqual(ledger,self.runtime.path.read_bytes())
        self.assertEqual(prior,self.runtime._load()['receipts']['C:1:completed'])
        from coordination import receipt_context
        with self.assertRaises(ValueError):receipt_context(state,dict(prior['payload'],channel='native_agent',origin_mode='observed_terminal'))

    def test_cli_failed_outcome_wrong_thread_unrelated_final_and_report_loss_are_rejected(self):
        command,log=self.cli();self.runtime.real_use(command,'integration');receive=self.cli_report(command,'turn.failed')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.runtime.real_use(receive,'integration')
        self.assertEqual(before,self.runtime.path.read_bytes())
        receive['observation']=self.file('unrelated-final.jsonl','\n'.join(json.dumps(e) for e in [
            dict(type='thread.started',thread_id='actual-cli'),dict(type='turn.started'),
            dict(type='item.completed',item=dict(type='agent_message',text='unrelated work')),dict(type='turn.completed')]))
        with self.assertRaises(ValueError):self.runtime.real_use(receive,'integration')
        receive['observation']=self.file('wrong-thread.jsonl',Path(receive['observation']['path']).read_text().replace('actual-cli','someone-else'))
        with self.assertRaises(ValueError):self.runtime.real_use(receive,'integration')
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_historical_cli_adapter_preserves_raw_report_and_rechecks_its_actual_test_conclusions(self):
        from evidence import snapshot
        from real_use_closeout import imported_report,verify_imported_report
        command,_=self.cli();self.runtime.real_use(command,'integration')
        (self.root/'C/feature.py').write_text('actual fixture source\n',encoding='utf-8')
        manifest=self.json('author-source.json',{r['relative']:r['sha256'] for r in snapshot(self.root/'C')['files']})
        raw=dict(schema='development-delivery/2',task_id='C',revision=2,
            status='same_PR_synchronized_selftested_awaiting_independent_review',worktree=str(self.root/'C'),branch='task/C',
            head_sha='a'*40,base_sha='b'*40,pr=dict(pr_url='https://example.invalid/pull/2'),
            source_binding=dict(manifest=Path(manifest['path']).name),developer=dict(persistent_thread='actual-cli'),
            tests=dict(commands=[dict(command=['fixture checks'],exit_code=0)],core=dict(failures=[],errors=[]),root=dict(failures=0,errors=0)))
        original=self.json('raw-author.json',raw);before=Path(original['path']).read_bytes()
        wrapped=imported_report(self.runtime._load(),'C',original)
        verify_imported_report(wrapped,'actual-cli');self.assertEqual(before,Path(original['path']).read_bytes())
        changed=deepcopy(wrapped);changed['self_test']['command']='invented successful checks'
        with self.assertRaises(ValueError):verify_imported_report(changed,'actual-cli')
        raw['tests']['commands'][0]['exit_code']=1
        bad=self.json('failed-raw-author.json',raw)
        with self.assertRaises(ValueError):imported_report(self.runtime._load(),'C',bad)
    def test_explicit_human_policy_cannot_be_claimed_as_legacy_encoding(self):
        self.reviewed();self.reload()
        # Fixture models a new task with explicit user_required business acceptance.
        self.state['tasks']['A']['acceptance_policy_explicit']=True;self.store()
        with self.assertRaises(ValueError):self.call('technical-acceptance',authority=self.acceptance_authority())
        self.assertEqual(self.runtime._load()['tasks']['A']['user_acceptance'],'pending')

    def test_quality_revalidates_approval_and_new_delivery_invalidates_old_review(self):
        self.reviewed();old=self.review();self.deliver(revision=2)
        self.assertIsNone(self.runtime._load()['tasks']['A']['independent_quality'])
        with self.assertRaises(ValueError):self.call('quality',review=old)
        Path(self.approval['path']).write_text('revoked',encoding='utf-8')
        with self.assertRaises(ValueError):self.call('quality',review=self.review())

    def test_new_code_can_explicitly_prepare_delivery_verified_without_changing_existing_tasks(self):
        work=self.root/'new';work.mkdir()
        self.runtime.prepare(dict(task_id='new',worktree=str(work),branch='task/new',scope='technical',
            acceptance=['checks pass'],acceptance_type='delivery_verified',user_reference='explicit approved technical scope'),self.actor)
        self.assertEqual(self.runtime._load()['tasks']['new']['acceptance_type'],'delivery_verified')

    def git_setup(self):
        work=self.root/'A'
        def g(*args):
            result=subprocess.run(['git','-C',str(work),'-c','core.hooksPath='+str(self.root/'no-hooks'),
                '-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);return result.stdout.strip()
        g('init','-b','main');g('add','.');g('commit','-m','fixture base');base=g('rev-parse','HEAD')
        g('checkout','-b','task/A');(work/'feature.py').write_text('second\n',encoding='utf-8')
        g('add','.');g('commit','-m','fixture feature');head=g('rev-parse','HEAD')
        self.git=g
        return dict(head_sha=head,target_sha=base,branch='task/A',pr_url='https://example.invalid/pull/1')

    def merge_proof(self):
        task=self.runtime._load()['tasks']['A'];report=load_bound(task['delivery']);git=report['git']
        authority=self.json('merge-authority.json',dict(schema='code-merge-authorization/1',project_id=self.runtime.project_id,
            task_id='A',main_conversation=self.actor,head_sha=git['head_sha'],target_sha=git['target_sha'],
            merge_allowed=True,source='direct user message in original main conversation',user_quote='Merge A after independent checks',
            evidence=[self.file('merge-instruction.txt')]))
        integration=self.review()
        return dict(merge_authority=authority,integration=integration,source_head=git['head_sha'],previous_main=git['target_sha'],
            main_ref='main',main_head=git['head_sha'],git_ref='refs/heads/main',merge_commit=git['head_sha'],git_repository='task',
            ci=self.ci(git['head_sha']),
            scope='fixture_only',evidence=[self.file('merge-result.txt')],pr_url=git['pr_url'],
            mr=dict(provider='github',id=1,url=git['pr_url'],source_branch='task/A',target_branch='main',
                head_sha=git['head_sha'],target_sha=git['target_sha']))

    def test_actual_git_required_then_completion_survives_later_main_and_duplicate(self):
        git=self.git_setup();self.accepted(git);proof=self.merge_proof()
        with self.assertRaises(ValueError):self.call('record-merge',**proof)
        self.git('update-ref','refs/heads/main',git['head_sha'],git['target_sha'])
        result=self.call('record-merge',**proof);self.assertEqual(result['status'],'completed')
        before=self.runtime.path.read_bytes();self.assertTrue(self.call('record-merge',**proof)['duplicate'])
        self.assertEqual(before,self.runtime.path.read_bytes())
        self.git('checkout','main');(self.root/'A/later.txt').write_text('unrelated\n',encoding='utf-8')
        self.git('add','.');self.git('commit','-m','fixture unrelated later main')
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'completed')
        self.assertEqual(self.runtime.recover()['revision'],result['revision'])

    def test_merge_permission_is_separate_from_automatic_acceptance_and_ci_is_not_faked(self):
        git=self.git_setup();self.accepted(git);proof=self.merge_proof()
        self.git('update-ref','refs/heads/main',git['head_sha'],git['target_sha'])
        raw=load_bound(proof['merge_authority']);raw['merge_allowed']=False
        proof['merge_authority']=self.json('denied.json',raw)
        with self.assertRaises(ValueError):self.call('record-merge',**proof)
        self.assertNotEqual(self.runtime._load()['tasks']['A']['phase'],'completed')

    def test_pending_ci_wrong_source_main_pr_and_integration_are_rejected(self):
        git=self.git_setup();self.accepted(git);proof=self.merge_proof()
        self.git('update-ref','refs/heads/main',git['head_sha'],git['target_sha'])
        for changes in (dict(source_head=git['target_sha']),dict(main_head=git['target_sha']),
            dict(pr_url='https://example.invalid/pull/2'),dict(integration=self.file('unrelated-checks.json','{}')),
            dict(ci=dict(proof['ci'],policy='unknown'))):
            before=self.runtime.path.read_bytes()
            with self.assertRaises((ValueError,KeyError)):self.call('record-merge',**dict(proof,**changes))
            self.assertEqual(before,self.runtime.path.read_bytes())
        Path(proof['ci']['statuses']['path']).write_text(json.dumps(dict(sha=git['head_sha'],statuses=[dict(state='pending')])))
        with self.assertRaises(ValueError):self.call('record-merge',**proof)

    def test_public_cli_entrypoint_cannot_use_author_identity(self):
        self.deliver();command=self.json('command.json',dict(action='quality',task_id='A',approval=self.approval,review=self.review()))
        script=Path(__file__).with_name('core_runtime.py')
        run=subprocess.run([sys.executable,'-X','utf8','-B',str(script),'--state-root',str(self.root/'authority'),
            '--project-root',str(self.root/'project'),'real-use',command['path'],'--actor','worker-A'],capture_output=True,text=True)
        self.assertEqual(run.returncode,1);self.assertIn('independent integration reviewer',run.stdout)
