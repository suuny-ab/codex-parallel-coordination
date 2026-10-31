"""B01-B12: host/main/PR facts are fixtures; owned temporary file removal is real."""
from copy import deepcopy
from pathlib import Path

import test_cleanup_runtime as cleanup_tests
import test_core_runtime as core_tests
from coordination import transition
from development_lifecycle import cancellation_fields
from evidence import load_bound, ref, write_atomic


class DevelopmentLifecycleTests(core_tests.CoreFixture):
    close_fixture = core_tests.CoreRuntimeTests.close_fixture
    managed = cleanup_tests.CleanupTests.managed
    remove = cleanup_tests.CleanupTests.remove

    def cancellation(self, *, pr=False):
        preservation = self.close_fixture(merged=False)
        state = self.runtime._load(); task = state['tasks']['A']
        if pr:
            task['mr'] = dict(provider='github', id='fixture-1', source_branch=task['branch'], target_branch='main',
                              head_sha='a'*40, target_sha='b'*40)
            write_atomic(self.runtime.path, state)
        proof = self.json('cancel.json', dict(cancellation_fields(task), project_id=self.runtime.project_id, task_id='A',
            based_on_revision=state['revision'], explicit_cancel=True, user_reference='explicit fixture cancellation',
            artifacts=[task['delivery']], scope='fixture_only', evidence=[self.file('cancel-instruction.txt')]))
        self.runtime.cancel_task(proof, self.actor)
        inventory = load_bound(preservation)
        inventory.pop('merge_verification')
        inventory['cancellation_verification'] = proof
        return self.json('cancel-preservation.json', inventory), proof

    def close(self, preservation, attempt='closed'):
        adapter, calls = self.adapter({'close_agent': {'closed': True}})
        result = self.runtime.lifecycle('close', 'worker-A', 'A', attempt, 1, adapter,
                                        preservation_ref=preservation, authorized=True)
        return result, calls

    def test_B01_actual_binding_has_one_spec_author_worktree_and_original_PR(self):
        self.close_fixture(merged=False)
        spec = self.file('spec.md')
        bound = self.runtime.bind_development('A', spec, self.actor)
        self.assertEqual(bound['owner_agent_id'], 'worker-A')
        self.assertIsNone(self.runtime._load()['tasks']['A']['mr'])
        self.runtime.apply(dict(op='authorize', task_id='A', rights=['mr'], user_reference='fixture PR permission'), self.actor)
        mr = dict(provider='github', id='fixture-1', source_branch='task/A', target_branch='main',
                  head_sha='a'*40, target_sha='b'*40)
        self.runtime.apply(dict(op='mr', task_id='A', mr=mr), self.actor)
        with self.assertRaisesRegex(ValueError, 'same MR'):
            self.runtime.apply(dict(op='mr', task_id='A', mr=dict(mr, id='second-PR')), self.actor)
        self.assertEqual(self.runtime.bind_development('A', spec, self.actor), bound)
        with self.assertRaises(ValueError): self.runtime.bind_development('A', self.file('different-spec'), self.actor)

    def test_B01_missing_identity_is_left_null_and_binding_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError): self.runtime.bind_development('A', self.file('spec'), self.actor)
        self.assertIsNone(self.runtime._load()['tasks']['A']['owner_agent_id'])

    def test_B02_new_read_only_prepare_refused_and_historical_task_cannot_bind_developer(self):
        work = self.root/'read-only'; work.mkdir()
        (work/'input.txt').write_text('frozen fixture input',encoding='utf-8')
        spec=dict(task_id='R', owner_agent_id='worker-A', worktree=str(work), branch='read-only',
            task_kind='read_only', acceptance_type='delivery_verified', scope='read sources', acceptance=['report'],
            user_reference='historical fixture scope')
        before=self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'stay with main'):
            self.runtime.prepare(spec,self.actor)
        self.assertEqual(self.runtime.path.read_bytes(),before)
        # Explicit historical ledger fixture only: never dispatch a new read-only developer.
        self.runtime.apply(dict(spec,op='register'),self.actor)
        self.runtime.apply(dict(op='confirm',task_id='R',**{k:spec[k] for k in ('scope','acceptance','user_reference')}),self.actor)
        self.runtime.apply(dict(op='queue',task_id='R',reason='Historical fixture',user_reference=spec['user_reference']),self.actor)
        self.bind()
        with self.assertRaisesRegex(ValueError, 'development task'):
            self.runtime.bind_development('R', self.file('spec'), self.actor)

    def test_B03_rework_keeps_author_worktree_spec_and_PR(self):
        self.close_fixture(merged=False); spec = self.file('spec')
        bound = self.runtime.bind_development('A', spec, self.actor)
        state = self.runtime._load()
        state, _ = transition(state, dict(op='rework', task_id='A', reason='original goal defect'))
        write_atomic(self.runtime.path, state)
        self.assertEqual(self.runtime.bind_development('A', spec, self.actor), bound)
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'], 'open')

    def test_B04_idle_or_completed_developer_cannot_own_another_requirement(self):
        self.close_fixture(merged=False)
        self.runtime.bind_development('A', self.file('spec'), self.actor)
        state=self.runtime._load(); state['tasks']['A'].update(phase='completed',merge=dict(verified=True,head_sha='a'*40))
        write_atomic(self.runtime.path,state)
        before=self.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'one requirement'):
            self.prepare('B',owner='worker-A')
        self.assertEqual(self.runtime.path.read_bytes(),before)

    def test_B05_premerge_every_success_claim_keeps_context_and_worktree(self):
        preservation = self.close_fixture(merged=False)
        state = self.runtime._load(); state['tasks']['A'].update(user_acceptance='accepted',self_test='passed')
        write_atomic(self.runtime.path, state)
        adapter, calls = self.adapter({'close_agent': {'closed': True}})
        with self.assertRaises(ValueError):
            self.runtime.lifecycle('close','worker-A','A','too-early',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(calls,[])
        self.assertTrue(Path(state['tasks']['A']['worktree']).is_dir())

    def test_B06_preserve_close_then_real_removal_without_deployment(self):
        preservation=self.close_fixture(); path=self.managed('environment')
        self.close(preservation); receipt=self.runtime.release_receipt('closed',self.actor)
        result=self.runtime.cleanup_task('A','clean',preservation,self.remove,self.actor,authorized=True)
        self.assertEqual(result['status'],'cleanup_completed'); self.assertFalse(path.exists())
        self.assertTrue(self.runtime.verify_release_receipt(load_bound(receipt),receipt)['verified'])

    def test_B07_cancellation_preserve_PR_close_before_agent_and_file_cleanup(self):
        preservation,proof=self.cancellation(pr=True); path=self.managed('environment')
        adapter,calls=self.adapter({'close_agent': {'closed':True}})
        with self.assertRaisesRegex(ValueError,'PR'):
            self.runtime.lifecycle('close','worker-A','A','premature',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(calls,[])
        pr_calls=[]
        def close_pr(operation):
            self.assertTrue(path.exists()); self.assertEqual(operation['mr']['id'],'fixture-1')
            pr_calls.append(operation['operation_id'])
            return dict(status='closed',evidence=self.file('PR-closed.txt'))
        self.runtime.close_cancelled_pr('A','pr-close',preservation,close_pr,self.actor,authorized=True)
        self.runtime.close_cancelled_pr('A','second-id',preservation,lambda _:self.fail('duplicate PR close'),self.actor,authorized=True)
        self.close(preservation); self.runtime.release_receipt('closed',self.actor)
        result=self.runtime.cleanup_task('A','cancel-clean',preservation,self.remove,self.actor,authorized=True)
        self.assertEqual(pr_calls,['pr-close']); self.assertFalse(path.exists()); self.assertEqual(result['status'],'cleanup_completed')
        task=self.runtime._load()['tasks']['A']; self.assertEqual(task['phase'],'cancelled'); self.assertIsNone(task['merge'])
        self.assertEqual(self.runtime.cancel_task(proof,self.actor)['pr_status'],'closed')

    def test_B08_no_PR_skips_close_other_task_unchanged(self):
        preservation,_=self.cancellation()
        self.prepare('B'); before=deepcopy(self.runtime._load()['tasks']['B'])
        result=self.runtime.close_cancelled_pr('A','no-pr',preservation,lambda _:self.fail('no PR to close'),self.actor,authorized=True)
        self.assertEqual(result['pr_status'],'not_required'); self.assertIsNone(self.runtime._load()['tasks']['A']['mr'])
        self.assertEqual(self.runtime._load()['tasks']['B'],before)

    def test_B09_shared_protected_active_children_or_dependencies_block_close(self):
        preservation,_=self.cancellation(); baseline=self.runtime._load()
        for source,key,value in [('executor','protected',True),('executor','shared',True),('executor','active_children',['child']),
                                 ('executor','pending_work',True),('task','publication_dependencies',['B'])]:
            state=deepcopy(baseline)
            target=state['runtime']['executors']['worker-A'] if source=='executor' else state['tasks']['A']
            target[key]=value; write_atomic(self.runtime.path,state)
            adapter,calls=self.adapter({'close_agent':{'closed':True}})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.runtime.lifecycle('close','worker-A','A',key,1,adapter,preservation_ref=preservation,authorized=True)
            self.assertEqual(calls,[])

    def test_B10_unknown_PR_requires_observation_never_second_effect(self):
        preservation,_=self.cancellation(pr=True); calls=[]
        def timeout(operation): calls.append(operation); raise TimeoutError('unknown close')
        result=self.runtime.close_cancelled_pr('A','pr-timeout',preservation,timeout,self.actor,authorized=True)
        self.assertEqual(result['status'],'unknown')
        self.runtime.close_cancelled_pr('A','pr-timeout',preservation,timeout,self.actor,authorized=True)
        with self.assertRaisesRegex(ValueError,'uncertain'):
            self.runtime.close_cancelled_pr('A','new-id',preservation,timeout,self.actor,authorized=True)
        self.assertEqual(len(calls),1)
        state=self.runtime._load()
        facts=self.json('PR-observed.json',dict(task_id='A',operation_id='pr-timeout',mr=state['tasks']['A']['mr'],
            status='closed',scope='fixture_only',evidence=[self.file('PR-observed.txt')]))
        self.runtime.reconcile_cancelled_pr('A',facts,self.actor)
        self.close(preservation)

    def test_B10_unknown_agent_and_partial_cleanup_remain_cancelled_not_completed(self):
        preservation,_=self.cancellation(); first=self.managed('first'); second=self.managed('second')
        adapter,calls=self.adapter({'close_agent':{'status':'completed'}})
        self.runtime.lifecycle('close','worker-A','A','uncertain',1,adapter,preservation_ref=preservation,authorized=True)
        with self.assertRaises(ValueError): self.runtime.release_receipt('uncertain',self.actor)
        facts=self.json('close-observed.json',dict(operation_id='uncertain',executor_id='worker-A',generation=1,closed=True,
            evidence=[self.file('actual-closed.txt')]))
        self.runtime.reconcile_lifecycle('uncertain',facts,self.actor); self.runtime.release_receipt('uncertain',self.actor)
        effects=[]
        def partial(resource):
            effects.append(resource['id'])
            return self.remove(resource) if resource['id']=='first' else dict(status='unknown')
        result=self.runtime.cleanup_task('A','partial',preservation,partial,self.actor,authorized=True)
        self.assertEqual(result['status'],'pending_cleanup'); self.assertFalse(first.exists()); self.assertTrue(second.exists())
        self.runtime.cleanup_task('A','new',preservation,lambda _:self.fail('unknown replay'),self.actor,authorized=True)
        self.assertEqual(effects,['first','second']); self.assertEqual(len(calls),1)
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'cancelled')

    def test_B11_stale_scope_generation_authority_and_replaced_path_do_not_release(self):
        preservation,_=self.cancellation(); adapter,calls=self.adapter({'close_agent':{'closed':True}})
        with self.assertRaises(ValueError):
            self.runtime.lifecycle('close','worker-A','A','wrong-generation',2,adapter,preservation_ref=preservation,authorized=True)
        adapter.scope='host_verified'
        with self.assertRaises(ValueError):
            self.runtime.lifecycle('close','worker-A','A','fixture-real',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(calls,[])
        path=self.managed('replace'); retained=path.with_name('retained-original'); path.rename(retained); path.mkdir()
        self.close(preservation); receipt=self.runtime.release_receipt('closed',self.actor)
        result=self.runtime.cleanup_task('A','changed-path',preservation,lambda _:self.fail('new object'),self.actor,authorized=True)
        self.assertEqual(result['status'],'pending_cleanup'); self.assertTrue(path.exists()); self.assertTrue(retained.exists())
        state=self.runtime._load(); state['runtime']['executors']['worker-A']['routes']['native_agent']['target_id']='new-identity'
        write_atomic(self.runtime.path,state)
        self.assertFalse(self.runtime.verify_release_receipt(load_bound(receipt),receipt)['verified'])
        with self.assertRaises(ValueError):
            self.runtime.cleanup_task('A','changed-actor',preservation,lambda _:self.fail('changed identity'),self.actor,authorized=True)

    def test_B12_new_request_links_old_and_never_resumes_cancelled_agent(self):
        preservation,_=self.cancellation(); self.close(preservation)
        adapter,calls=self.adapter({'resume_agent':{'resumed':True}})
        with self.assertRaises(ValueError):
            self.runtime.lifecycle('resume','worker-A','A','resume-old',1,adapter,admission=self.permit(),authorized=True)
        self.assertEqual(calls,[])
        self.prepare('B',owner='worker-B')
        self.runtime.bind_executor('worker-B',{'native_agent':dict(target_id='native-B',host_id='test-host')},self.actor,self.file('B-authority'))
        bound=self.runtime.bind_development('B',self.file('new-spec'),self.actor,predecessor_id='A')
        self.assertEqual(bound['predecessor_id'],'A'); self.assertEqual(bound['owner_agent_id'],'worker-B')

    def test_cancellation_does_not_accept_stale_or_non_user_instruction(self):
        _,proof=self.cancellation(); state=self.runtime._load(); before=self.runtime.path.read_bytes()
        changed=load_bound(proof); changed['explicit_cancel']=False
        with self.assertRaises(ValueError): self.runtime.cancel_task(self.json('not-cancel.json',changed),self.actor)
        with self.assertRaises(ValueError): self.runtime.cancel_task(proof,'worker-A')
        self.assertEqual(self.runtime.path.read_bytes(),before)

    def test_missing_capability_retains_context_and_reports_unsupported(self):
        preservation,_=self.cancellation(); adapter,calls=self.adapter({},tools=[])
        result=self.runtime.lifecycle('close','worker-A','A','unsupported',1,adapter,preservation_ref=preservation,authorized=True)
        self.assertEqual(result['status'],'unsupported'); self.assertEqual(calls,[])
        self.assertEqual(self.runtime._load()['runtime']['executors']['worker-A']['lease'],'open')

    def test_release_after_actual_original_report_cleanup_uses_preserved_copy(self):
        preservation,_=self.cancellation()
        path=self.managed('report-area')
        # Place the original report inside the registered resource before cancelling.
        state=self.runtime._load(); original=state['tasks']['A']['delivery']
        owned=path/'report.json'; owned.write_bytes(Path(original['path']).read_bytes())
        state['tasks']['A'].update(phase='awaiting_acceptance',cancellation=None,delivery=ref(owned))
        write_atomic(self.runtime.path,state)
        task=state['tasks']['A']; proof=self.json('owned-cancel.json',dict(cancellation_fields(task),
            project_id=self.runtime.project_id,task_id='A',based_on_revision=state['revision'],explicit_cancel=True,
            user_reference='fixture cancellation',scope='fixture_only',artifacts=[ref(owned)],evidence=[self.file('user-cancel.txt')]))
        self.runtime.cancel_task(proof,self.actor)
        backup=self.root/'saved-owned-report.json'; backup.write_bytes(owned.read_bytes())
        inventory=load_bound(preservation); inventory.update(files=[dict(ref(backup),original=ref(owned))],cancellation_verification=proof)
        preservation=self.json('owned-preservation.json',inventory)
        self.close(preservation); receipt=self.runtime.release_receipt('closed',self.actor)
        self.runtime.cleanup_task('A','remove-report',preservation,self.remove,self.actor,authorized=True)
        self.assertFalse(owned.exists())
        self.assertTrue(self.runtime.verify_release_receipt(load_bound(receipt),receipt)['verified'])
        self.runtime.cleanup_task('A','after-removal',preservation,lambda _:self.fail('duplicate cleanup'),self.actor,authorized=True)

    def test_preservation_inside_resource_or_missing_handoff_never_closes(self):
        preservation,_=self.cancellation(); path=self.managed('unsafe-evidence')
        inventory=load_bound(preservation); inventory['handoff']=self.file(str(path/'handoff.txt'))
        unsafe=self.json('unsafe-preservation.json',inventory)
        adapter,calls=self.adapter({'close_agent':{'closed':True}})
        with self.assertRaises(ValueError):
            self.runtime.lifecycle('close','worker-A','A','unsafe',1,adapter,preservation_ref=unsafe,authorized=True)
        self.assertEqual(calls,[])

    def test_cancellation_before_delivery_needs_no_fake_report_or_PR(self):
        self.prepare(owner='worker-A'); self.bind()
        state=self.runtime._load(); task=state['tasks']['A']; source=self.file('unfinished.py','pass')
        proof=self.json('early-cancel.json',dict(cancellation_fields(task),project_id=self.runtime.project_id,
            task_id='A',based_on_revision=state['revision'],explicit_cancel=True,user_reference='fixture cancellation',
            scope='fixture_only',artifacts=[source],evidence=[self.file('early-instruction.txt')]))
        self.runtime.cancel_task(proof,self.actor)
        backup=self.root/'early-backup.py'; backup.write_bytes(Path(source['path']).read_bytes())
        preservation=self.json('early-preservation.json',dict(executor_id='worker-A',generation=1,cancellation_verification=proof,
            files=[dict(ref(backup),original=source)],handoff=self.file('early-handoff.txt'),unregistered_evidence_preserved=True))
        self.close(preservation)
        task=self.runtime._load()['tasks']['A']
        self.assertIsNone(task['delivery']); self.assertIsNone(task['mr']); self.assertEqual(task['phase'],'cancelled')

    def test_late_report_cannot_reopen_cancelled_requirement(self):
        self.cancellation(); before=deepcopy(self.runtime._load()['tasks']['A'])
        state=self.runtime._load(); event=deepcopy(state['receipts']['A:1:completed']['payload'])
        report=dict(load_bound(before['delivery']),delivery_revision=2)
        event.update(delivery_revision=2,event_id='A:2:completed',report=self.json('late-report.json',report))
        self.runtime.receive(event,self.actor)
        self.runtime.apply(dict(op='drain'),self.actor)
        self.assertEqual(self.runtime._load()['tasks']['A']['phase'],'cancelled')
        self.assertEqual(self.runtime._load()['tasks']['A']['delivery'],before['delivery'])

    def test_bound_task_cannot_change_worktree_or_duplicate_another_spec(self):
        self.close_fixture(merged=False); spec=self.file('spec','first specification')
        self.runtime.bind_development('A',spec,self.actor)
        state=self.runtime._load(); changed=deepcopy(state); changed['tasks']['A']['worktree']=str(self.root/'moved')
        from coordination import validate
        with self.assertRaises(ValueError):validate(changed)
        self.prepare('B',owner='worker-B')
        self.runtime.bind_executor('worker-B',{'native_agent':dict(target_id='native-B',host_id='test-host')},self.actor,self.file('B-authority'))
        with self.assertRaisesRegex(ValueError,'spec already'):
            self.runtime.bind_development('B',self.file('copied-spec','first specification'),self.actor)

    def test_confirmed_close_updates_cancelled_running_assignment_without_freeing_early(self):
        preservation,_=self.cancellation(); state=self.runtime._load()
        state['tasks']['A']['worker_run']['status']='running'; write_atomic(self.runtime.path,state)
        self.close(preservation)
        self.assertEqual(self.runtime._load()['tasks']['A']['worker_run']['status'],'cancelled')

    def test_B08_registered_dependents_are_reported_to_integration_and_not_cancelled(self):
        self.close_fixture(merged=False); self.prepare('B')
        self.managed('shared','environment',shared=True,references=['A','B'])
        state=self.runtime._load(); task=state['tasks']['A']; other=deepcopy(state['tasks']['B'])
        proof=self.json('dependent-cancel.json',dict(cancellation_fields(task),project_id=self.runtime.project_id,
            task_id='A',based_on_revision=state['revision'],explicit_cancel=True,user_reference='fixture cancel A only',
            scope='fixture_only',artifacts=[task['delivery']],evidence=[self.file('dependent-instruction.txt')]))
        result=self.runtime.cancel_task(proof,self.actor)
        self.assertEqual(result['dependency_review'],dict(task_ids=['B'],owner=self.actor,status='pending'))
        self.assertEqual(self.runtime._load()['tasks']['B'],other)

    def test_B09_main_and_integration_actors_are_protected_without_shared_flag(self):
        preservation,_=self.cancellation(); baseline=self.runtime._load()
        for role in ('main','delivery'):
            state=deepcopy(baseline)
            if role=='main':state['coordinator_id']='worker-A'
            else:state['delivery']=dict(agent_id='worker-A',task_ids=['A'],merge_order=['A'])
            write_atomic(self.runtime.path,state)
            adapter,calls=self.adapter({'close_agent':{'closed':True}});adapter.actor_id='worker-A'
            with self.assertRaisesRegex(ValueError,'protected'):
                self.runtime.lifecycle('close','worker-A','A',role,1,adapter,preservation_ref=preservation,authorized=True)
            self.assertEqual(calls,[])
