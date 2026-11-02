"""STATE S01-S08/F01-F03: isolated ledgers; host roles/merge/display are fixtures.
The Git tests execute real disposable local repositories, never remote operations.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core_runtime import Runtime
from evidence import io_path, ref, write_atomic
from task_state import TaskState, capture_producer_result, record_producer_result


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='state-tool-',dir=('\\\\?\\' if os.name=='nt' else '')+tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name.removeprefix('\\\\?\\')); project=self.root/'project'; project.mkdir()
        self.rt=Runtime(self.root/'state', project); self.rt.initialize('main', provider='github')
        self.version=dict(head_sha='a'*40,target_sha='b'*40,source_digest='c'*64)
        self.fact=self.root/'fact.txt';self.fact.write_text('fixture result',encoding='utf-8')
        self.proof=ref(self.fact)
        self.main=self.client('main');self.author=self.client('author-A');self.integrator=self.client('integrator')
        self.number=0

    def client(self, actor, **options):
        return TaskState(self.rt,actor,observe_source=lambda task: deepcopy(self.version),**options)

    def command(self, action, key='A', **fields):
        self.number+=1
        value=dict(action=action,task_id=key,event_id='event-'+str(self.number),expected_revision=self.rt._load()['revision'],**fields)
        from task_state import VERSIONED
        if action in VERSIONED:value['version']=deepcopy(self.version)
        return value

    def apply(self, client, action, key='A', **fields):
        c=self.command(action,key,**fields)
        origin=dict(role='user',conversation_id='main',reference=c['user_reference']) if 'user_reference' in c else None
        result=client.record(c,user_origin=origin)
        self.assertEqual(result['status'],'recorded',result)
        return result

    def register(self,key='A'):
        return self.apply(self.main,'register',key,spec=self.proof,scope='fixture feature',acceptance=['observable result'],
            acceptance_type='delivery_verified',integrator_id='integrator',rights=dict(commit=True,push=True,mr=True,merge=False),user_reference='fixture approval')

    def bind(self,key='A'):
        self.register(key)
        # Disposable producer setup only; does not claim actual native dispatch.
        state=self.rt._load();task=state['tasks'][key];work=self.root/key;work.mkdir()
        task.update(owner_agent_id='author-'+key,worktree=str(work),branch='task/'+key)
        task['worker_run']['owner_agent_id']='author-'+key
        write_atomic(self.rt.path,state)

    def deliver(self):
        self.apply(self.author,'self-test',status='passed',command='fixture test',evidence=[self.proof])
        return self.apply(self.author,'deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/project/pull/1'),summary='ready',evidence=[self.proof])

    def ready(self):
        self.bind();self.deliver()
        self.apply(self.integrator,'review',status='passed',summary='fixture independent review',evidence=[self.proof])
        self.apply(self.integrator,'technical-acceptance',status='passed',summary='fixture acceptance',evidence=[self.proof])

    def test_S01_registration_no_fake_resources_and_real_origin(self):
        c=self.command('register',spec=self.proof,scope='s',acceptance=['a'],acceptance_type='delivery_verified',integrator_id='integrator',rights={},user_reference='approval')
        with self.assertRaises(ValueError):self.main.record(c)
        self.register();task=self.rt._load()['tasks']['A']
        self.assertIsNone(task['owner_agent_id']);self.assertIsNone(task['worktree']);self.assertIsNone(task['branch'])
        self.assertEqual(task['phase'],'backlog');self.assertEqual(task['resources'],[])
        self.assertFalse(task['authorization']['merge']['allowed'])
        with self.assertRaises(ValueError):self.register()

    def test_S02_author_delivery_is_technical_not_human(self):
        self.bind();r=self.deliver()
        pending=self.integrator.pending()
        self.assertEqual(pending['technical'][0]['id'],r['notification_id'])
        self.assertEqual(self.rt._load()['items'],{})
        self.assertEqual(self.rt._load()['tasks']['A']['phase'],'awaiting_delivery_review')
        self.assertFalse(self.rt._load()['tasks']['A'].get('merge'))

    def test_S03_author_cannot_review_accept_merge_cancel_or_write_phase(self):
        self.bind();self.deliver()
        for action in ('review','technical-acceptance','user-acceptance','merge-verified','authorize','cancel','register'):
            before=self.rt.path.read_bytes()
            with self.assertRaises((ValueError,KeyError)):
                self.author.record(self.command(action,user_reference='not authority'))
            self.assertEqual(before,self.rt.path.read_bytes(),action)
        for field in ('phase','reviewer_id','role','actor_id','producer','state_root','project_id','user_origin','archive','cleanup'):
            with self.assertRaises(ValueError):self.author.record(self.command('progress',summary='s',**{field:'forged'}))
        with self.assertRaises(ValueError):self.rt.apply(dict(op='authorize',task_id='A',right='merge',user_reference='forged'),'author-A')

    def test_S04_wrong_caller_cross_task_and_no_duplicate_bypass(self):
        self.bind();self.bind('B');c=self.command('progress',summary='valid')
        self.author.record(c)
        for actor in ('wrong','author-B','main-as-author'):
            with self.assertRaises(ValueError):self.client(actor).record(c)
        with self.assertRaises(ValueError):self.author.record(self.command('progress','B',summary='cross task'))
        with self.assertRaises(ValueError):self.author.query('B')
        self.assertEqual(set(self.author.query()['tasks']),{'A'})

    def test_S05_concurrent_revision_cannot_overwrite(self):
        self.bind();commands=[self.command('progress',summary='first'),self.command('progress',summary='second')]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(self.author.record,commands))
        self.assertCountEqual([r['status'] for r in results],['recorded','conflict'])
        conflict=next(r for r in results if r['status']=='conflict')
        self.assertIn('same event',conflict['next_action'])

    def test_duplicate_event_conflicting_content_and_restart(self):
        self.bind();c=self.command('progress',summary='one');r=self.author.record(c)
        before=self.rt.path.read_bytes();again=self.author.record(c)
        self.assertTrue(again['duplicate']);self.assertEqual(again['revision'],r['revision']);self.assertEqual(before,self.rt.path.read_bytes())
        with self.assertRaises(ValueError):self.author.record(dict(c,summary='changed'))
        restart=Runtime(self.root/'state',self.rt.project_root).task_state('author-A')
        self.assertEqual(restart.query()['tasks']['A']['progress']['last_registered']['summary'],'one')
        self.assertFalse(restart.query()['host_verified_now'])
        self.assertEqual(before,self.rt.path.read_bytes())

    def test_S06_new_process_same_fixed_ledger_and_no_cwd_derivation(self):
        self.bind();self.apply(self.author,'progress',summary='persisted')
        script=Path(__file__).resolve().parent
        code="import sys,json;sys.path.insert(0,sys.argv[1]);from core_runtime import Runtime;print(json.dumps(Runtime(sys.argv[2],sys.argv[3]).task_state('author-A').query()))"
        proc=subprocess.run([sys.executable,'-B','-c',code,str(script),str(self.root/'state'),str(self.rt.project_root)],cwd=self.root/'A',capture_output=True,text=True,check=True)
        result=json.loads(proc.stdout);self.assertEqual(result['ledger'],str(self.rt.path));self.assertEqual(set(result['tasks']),{'A'})
        with self.assertRaises((ValueError,OSError)):
            Runtime(self.root/'state',self.root/'A').task_state('author-A')

    def test_S07_source_change_blocks_old_review_then_same_pr_redelivered(self):
        self.ready();old=deepcopy(self.rt._load()['tasks']['A']['mr']);self.version['head_sha']='d'*40
        with self.assertRaisesRegex(ValueError,'source/main changed'):
            self.apply(self.integrator,'review',status='passed',summary='stale',evidence=[self.proof])
        self.deliver();task=self.rt._load()['tasks']['A']
        self.assertIsNone(task['review']);self.assertEqual(task['mr']['url'],old['url']);self.assertEqual(task['owner_agent_id'],'author-A')
        self.assertEqual(task['delivery_revision'],2);self.assertTrue(task['state_result_history'])
        self.version['target_sha']='e'*40
        with self.assertRaises(ValueError):self.apply(self.integrator,'technical-acceptance',status='passed',summary='old main',evidence=[self.proof])

    def test_F01_rework_and_review_keep_author_task_pr_and_no_user_item(self):
        self.ready();self.apply(self.integrator,'rework',summary='fix ordinary issue')
        task=self.rt._load()['tasks']['A'];self.assertEqual(task['self_test'],'pending')
        self.assertEqual(len(self.author.pending()['technical']),1)
        self.deliver();self.assertEqual(self.rt._load()['tasks']['A']['delivery_revision'],2)
        self.assertEqual(self.rt._load()['items'],{})
        self.apply(self.main,'user-question',title='business choice',prompt='Which rule?',options=['one','two'])
        self.assertEqual(len(self.main.pending()['human_items']),1)

    def test_review_failure_cannot_be_replaced_without_author_rework(self):
        self.bind();self.deliver()
        self.apply(self.integrator,'review',status='failed',summary='broken',evidence=[self.proof])
        with self.assertRaises(ValueError):
            self.apply(self.integrator,'review',status='passed',summary='no author fix',evidence=[self.proof])

    def test_problem_routes_to_integrator_without_human_escalation(self):
        self.bind();self.apply(self.author,'problem',summary='test environment issue')
        self.assertEqual(len(self.integrator.pending()['technical']),1);self.assertEqual(self.main.pending()['human_items'],[])

    def test_merge_requires_rights_and_trusted_actual_observation(self):
        self.ready()
        with self.assertRaises(ValueError):self.apply(self.integrator,'merge-verified',operation_id='original-merge')
        self.apply(self.main,'authorize',rights=dict(merge=True),user_reference='fixture merge approval')
        with self.assertRaises(ValueError):self.apply(self.integrator,'merge-verified',operation_id='original-merge')
        client=self.client('integrator',observe_merge=lambda t,op:dict(project_id=self.rt.project_id,task_id='B',operation_id=op,contains_source=True,source_head=self.version['head_sha'],previous_main=self.version['target_sha'],main_head='f'*40,scope='fixture_only'))
        with self.assertRaises(ValueError):self.apply(client,'merge-verified',operation_id='original-merge')

    def merge_observation(self, task, operation):
        return dict(project_id=self.rt.project_id,task_id='A',operation_id=operation,contains_source=True,
            source_head=self.version['head_sha'],previous_main=self.version['target_sha'],main_head='f'*40,scope='fixture_only')

    def test_S08_F02_completed_distinct_from_archive_resources_and_failed_notice(self):
        self.ready();self.apply(self.main,'authorize',rights=dict(merge=True),user_reference='fixture merge approval')
        integrated=self.client('integrator',observe_merge=self.merge_observation)
        result=self.apply(integrated,'merge-verified',operation_id='original-merge')
        self.assertTrue(result['task_completed']);self.assertFalse(result['cleanup_completed'])
        self.assertEqual(self.main.query('A')['tasks']['A']['archive']['status'],'not_registered')
        event=result['notification_id'];self.apply(integrated,'send-intent',notification_id=event,attempt_id='send-one')
        self.apply(integrated,'send-result',notification_id=event,attempt_id='send-one',status='failed',evidence=[self.proof])
        self.assertEqual(self.rt._load()['tasks']['A']['phase'],'completed')
        self.assertFalse(self.apply(integrated,'send-intent',notification_id=event,attempt_id='send-one')['send_allowed'])
        with self.assertRaises(ValueError):self.apply(integrated,'send-intent',notification_id=event,attempt_id='blind-new-id')

    def test_F03_accepted_received_displayed_and_dedup_distinct(self):
        self.ready();event=self.main.pending()['notifications'][0]['id']
        self.apply(self.integrator,'send-intent',notification_id=event,attempt_id='notice')
        self.apply(self.integrator,'send-result',notification_id=event,attempt_id='notice',status='accepted',evidence=[self.proof])
        self.assertTrue(self.main.pending()['notifications'])
        self.apply(self.main,'notification-received',notification_id=event)
        self.assertIn(event,[e['id'] for e in self.main.pending()['notifications']])
        with self.assertRaises(ValueError):self.apply(self.main,'notification-displayed',notification_id=event)
        display=self.client('main',verify_display=lambda e:dict(visible=True,message_id='actual-fixture-message',notification_id=e['id'],recipient_id='main'))
        c=self.command('notification-displayed',notification_id=event)
        self.assertTrue(display.record(c)['delivered_to_user']);self.assertTrue(display.record(c)['duplicate'])
        self.assertNotIn(event,[e['id'] for e in display.pending()['notifications']])
        before=len(display.pending()['notifications']);self.apply(self.integrator,'rework',summary='new issue')
        self.assertEqual(len(display.pending()['notifications']),before)

    def test_technical_feedback_cannot_be_displayed_by_main_or_wrong_task(self):
        self.bind();result=self.deliver();event=result['notification_id']
        with self.assertRaises(ValueError):self.apply(self.main,'notification-received',notification_id=event)
        self.apply(self.integrator,'notification-received',notification_id=event)
        self.assertEqual(self.integrator.pending()['technical'],[])
        with self.assertRaises(ValueError):self.rt.feedback(dict(op='prepare-send',notification_ids=[event],attempt_id='legacy-bypass'),'integrator')

    def test_no_report_rescan_or_callbacks_under_lock(self):
        self.bind()
        def observe(task):
            with writer_lock(self.rt.path):pass
            return deepcopy(self.version)
        from coordination import writer_lock
        client=TaskState(self.rt,'author-A',observe_source=observe)
        with patch('evidence.report_for',side_effect=AssertionError('recursive report gate')):
            self.apply(client,'self-test',status='passed',command='real fixture command',evidence=[self.proof])
        self.author.query();self.author.pending()

    def test_callback_concurrent_change_preserves_actual_result_and_reconcile_info(self):
        self.ready();self.apply(self.main,'authorize',rights=dict(merge=True),user_reference='fixture merge approval')
        def observe(task,op):
            self.apply(self.main,'user-question',title='other',prompt='Choose',options=['a','b'])
            return self.merge_observation(task,op)
        c=self.command('merge-verified',operation_id='original')
        r=self.client('integrator',observe_merge=observe).record(c)
        self.assertEqual(r['status'],'conflict');self.assertTrue(r['actual_result']['contains_source'])
        self.assertNotEqual(self.rt._load()['tasks']['A']['phase'],'completed')

    def test_successful_observation_registration_failure_retains_result(self):
        self.ready();self.apply(self.main,'authorize',rights=dict(merge=True),user_reference='fixture merge approval')
        client=self.client('integrator',observe_merge=self.merge_observation);c=self.command('merge-verified',operation_id='original')
        with patch('core_runtime.write_atomic',side_effect=OSError('fixture disk failure')):
            r=client.record(c)
        self.assertEqual(r['status'],'registration_pending');self.assertTrue(r['actual_result']['contains_source'])
        restart=self.client('integrator',observe_merge=lambda *args: self.fail('must reuse saved actual result'))
        self.assertEqual(restart.record(c)['status'],'recorded')

    def test_cancel_is_user_request_not_actual_stop_or_cleanup(self):
        self.bind();r=self.apply(self.main,'cancel',reason='explicit user cancel',user_reference='fixture cancel')
        self.assertEqual(r['actual_stop'],'not_observed');self.assertNotEqual(self.rt._load()['tasks']['A']['phase'],'cancelled')
        with self.assertRaises(ValueError):self.apply(self.author,'progress',summary='continue anyway')

    def test_producer_capability_not_file_claim_and_no_archive_fabrication(self):
        self.bind();state=self.rt._load()
        with self.assertRaises(ValueError):record_producer_result(state,'resource','A','fake',dict(status='cleaned'))
        capture_producer_result(state,dict(op='bind-executor',task_id='A'))
        row=state['tasks']['A']['producer_results']['dispatch:bind-executor']
        self.assertEqual(row['result']['owner_agent_id'],'author-A');self.assertNotIn('archive',state['tasks']['A'])


    def test_callback_result_survives_revision_conflict_and_restarts_without_reobservation(self):
        self.ready();self.apply(self.main,'authorize',rights=dict(merge=True),user_reference='fixture merge approval')
        def observed(task, op):
            self.apply(self.main,'user-question',title='concurrent',prompt='choose',options=['a','b'])
            return self.merge_observation(task,op)
        command=self.command('merge-verified',operation_id='merge-one')
        result=self.client('integrator',observe_merge=observed).record(command)
        self.assertEqual(result['status'],'conflict');self.assertTrue(result['actual_result_ref'])
        command['expected_revision']=self.rt._load()['revision']
        restart=self.client('integrator',observe_merge=lambda *args:self.fail('repeat observation'))
        self.assertEqual(restart.record(command)['status'],'recorded')
        self.assertTrue(restart.record(command)['duplicate'])

    def test_user_acceptance_rejection_cannot_keep_merge_ready(self):
        self.ready()
        state=self.rt._load();t=state['tasks']['A'];t['acceptance_type']='user_required';t['requirements']['acceptance_type']='user_required';write_atomic(self.rt.path,state)
        self.apply(self.main,'user-acceptance',status='accepted',user_reference='fixture accepted')
        self.assertEqual(self.rt._load()['tasks']['A']['phase'],'awaiting_merge')
        self.apply(self.main,'user-acceptance',status='rejected',user_reference='fixture rejected')
        self.assertEqual(self.rt._load()['tasks']['A']['phase'],'awaiting_acceptance')

    def test_main_feedback_query_does_not_read_old_test_artifacts(self):
        self.ready();self.fact.unlink()
        with patch('stage_feedback.report_for',side_effect=AssertionError('no history rescan')):
            self.assertTrue(self.main.pending()['notifications'])
            from stage_feedback import pending
            self.assertTrue(pending(self.rt._load())['items'])
        self.assertIn('source_observed_at',self.main.query('A')['tasks']['A'])

    def test_real_git_source_and_main_forward_require_original_author_retest(self):
        def git(root,*args):
            return subprocess.run(['git','-c','core.longpaths=true','-c','core.hooksPath=','-C',str(root),*args],capture_output=True,text=True,check=True).stdout.strip()
        project=self.rt.project_root
        git(project,'init','-b','main');git(project,'config','user.email','fixture@example.invalid');git(project,'config','user.name','Disposable fixture')
        (project/'code.txt').write_text('base',encoding='utf-8');git(project,'add','code.txt');git(project,'-c','commit.gpgsign=false','commit','-m','fixture base')
        self.register();work=self.root/'git-work';git(project,'worktree','add','-b','task/A',str(work))
        state=self.rt._load();state['tasks']['A'].update(owner_agent_id='author-A',worktree=str(work),branch='task/A');write_atomic(self.rt.path,state)
        (work/'code.txt').write_text('feature',encoding='utf-8');git(work,'add','code.txt');git(work,'-c','commit.gpgsign=false','commit','-m','fixture feature')
        author=TaskState(self.rt,'author-A');integrator=TaskState(self.rt,'integrator')
        self.version=author.observe('A')['version']
        self.apply(author,'self-test',status='passed',command='real Git fixture check',evidence=[self.proof])
        self.apply(author,'deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/project/pull/1'),summary='Git source ready',evidence=[self.proof])
        self.apply(integrator,'review',status='passed',summary='bound real source',evidence=[self.proof])
        (work/'code.txt').write_text('dirty change',encoding='utf-8')
        with self.assertRaises(ValueError):self.apply(integrator,'technical-acceptance',status='passed',summary='old bytes',evidence=[self.proof])
        git(work,'restore','code.txt')
        (project/'main.txt').write_text('new main',encoding='utf-8');git(project,'add','main.txt');git(project,'-c','commit.gpgsign=false','commit','-m','fixture main forward')
        with self.assertRaisesRegex(ValueError,'synchronize actual main'):
            self.apply(integrator,'review',status='passed',summary='old main',evidence=[self.proof])
        git(work,'-c','commit.gpgsign=false','merge','main','--no-edit')
        self.version=author.observe('A')['version']
        self.apply(author,'self-test',status='passed',command='regression on actual main',evidence=[self.proof])
        self.apply(author,'deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/project/pull/1'),summary='same PR new source',evidence=[self.proof])
        self.assertEqual(self.rt._load()['tasks']['A']['mr']['id'],1)
        self.assertIsNone(self.rt._load()['tasks']['A']['review'])


    def committed_git_fixture(self):
        def git(root,*args):
            result=subprocess.run(['git','-c','core.longpaths=true','-c','core.hooksPath=','-C',str(root),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout.strip()
        project=self.rt.project_root
        git(project,'init','-b','main');git(project,'config','user.email','fixture@example.invalid');git(project,'config','user.name','Disposable fixture')
        (project/'code.txt').write_text('base',encoding='utf-8')
        (project/'.gitignore').write_text('*.log\n',encoding='utf-8')
        git(project,'add','.');git(project,'-c','commit.gpgsign=false','commit','-m','fixture base')
        self.register();work=self.root/'git-work';git(project,'worktree','add','-b','task/A',str(work))
        state=self.rt._load();state['tasks']['A'].update(owner_agent_id='author-A',worktree=str(work),branch='task/A');write_atomic(self.rt.path,state)
        (work/'code.txt').write_text('committed-A',encoding='utf-8');git(work,'add','code.txt');git(work,'-c','commit.gpgsign=false','commit','-m','fixture feature')
        return project,work,git,TaskState(self.rt,'author-A')

    def assert_dirty_delivery_rejected(self,kind):
        project,work,git,author=self.committed_git_fixture()
        if kind=='untracked':
            (work/'extra.py').write_text('tested-uncommitted-B',encoding='utf-8')
        else:
            (work/'code.txt').write_text('tested-uncommitted-B',encoding='utf-8')
            if kind=='staged':git(work,'add','code.txt')
        self.version=author.observe('A')['version']
        self.apply(author,'progress',summary='ordinary dirty development is allowed')
        self.apply(author,'self-test',status='passed',command='check actual dirty fixture bytes',evidence=[self.proof])
        before=self.rt.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'committed source'):
            self.apply(author,'deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/project/pull/1'),summary='dirty source must not be promoted',evidence=[self.proof])
        self.assertEqual(before,self.rt.path.read_bytes())
        self.assertIsNone(self.rt._load()['tasks']['A']['mr'])

    def test_dirty_unstaged_bytes_cannot_be_delivered_as_committed_head(self):
        self.assert_dirty_delivery_rejected('unstaged')

    def test_dirty_staged_bytes_cannot_be_delivered_as_committed_head(self):
        self.assert_dirty_delivery_rejected('staged')

    def test_untracked_source_cannot_be_delivered_as_committed_head(self):
        self.assert_dirty_delivery_rejected('untracked')

    def test_clean_committed_bytes_retested_after_commit_can_complete_actual_local_main(self):
        project,work,git,author=self.committed_git_fixture()
        (work/'code.txt').write_text('tested-committed-B',encoding='utf-8')
        self.version=author.observe('A')['version']
        self.apply(author,'self-test',status='passed',command='check working B before commit',evidence=[self.proof])
        git(work,'add','code.txt');git(work,'-c','commit.gpgsign=false','commit','-m','fixture commit tested B')
        with self.assertRaisesRegex(ValueError,'current actual source version required'):
            self.apply(author,'deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/project/pull/1'),summary='old test predates committed HEAD',evidence=[self.proof])
        self.version=author.observe('A')['version']
        self.apply(author,'self-test',status='passed',command='retest committed B bytes',evidence=[self.proof])
        (work/'ignored.log').write_text('test runtime output, excluded from source',encoding='utf-8')
        self.apply(author,'deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/project/pull/1'),summary='clean committed B',evidence=[self.proof])
        def merge_observation(task,operation):
            head=git(work,'rev-parse','HEAD');main=git(project,'rev-parse','main')
            return dict(project_id=self.rt.project_id,task_id='A',operation_id=operation,source_head=head,
                previous_main=self.version['target_sha'],main_head=main,contains_source=git(project,'merge-base',head,main)==head,scope='fixture_only')
        integrator=TaskState(self.rt,'integrator',observe_merge=merge_observation)
        self.apply(integrator,'review',status='passed',summary='actual committed B checked',evidence=[self.proof])
        self.apply(integrator,'technical-acceptance',status='passed',summary='committed source accepted',evidence=[self.proof])
        self.apply(self.main,'authorize',rights=dict(merge=True),user_reference='fixture local merge approval')
        git(project,'merge','--ff-only','task/A')
        self.apply(integrator,'merge-verified',operation_id='actual-local-merge')
        self.assertEqual(self.rt._load()['tasks']['A']['phase'],'completed')
        self.assertEqual((project/'code.txt').read_bytes(),(work/'code.txt').read_bytes())
        self.assertEqual(git(project,'show','main:code.txt'),'tested-committed-B')


from test_core_runtime import CoreFixture


class CurrentProducerTests(CoreFixture):
    def test_existing_launch_registers_only_actual_canonical_result(self):
        self.prepare()
        adapter,calls=self.adapter({'spawn_agent':{'error':{'code':'agent_capacity_exceeded'}}})
        result=self.launch_fixture('A',adapter,attempt_id='state-producer',admission=self.permit(attempt='state-producer'),
            authorized=True,message='fixture scope',spawn=True)
        self.assertEqual(result['status'],'capacity')
        row=self.runtime._load()['tasks']['A']['producer_results']['dispatch:state-producer']
        self.assertEqual(row['result']['worker_run']['status'],'queued')
        self.assertIsNone(row['result']['owner_agent_id']);self.assertEqual(len(calls),1)
        self.assertNotIn('archive',self.runtime._load()['tasks']['A'])


class DirectLaunchHandoffTests(CoreFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        from routing_fixture import enroll
        from demand_routing import intake, plan_item
        from evidence import fingerprint, load_bound
        enroll(self,'A')
        state=self.runtime._load();_,item=plan_item(state,'A')
        raw=load_bound(intake(state)['approvals'][fingerprint(item)]['reference'])
        approval=self.json('direct-approval.json',dict(raw,integration_agent='integrator',tasks=[dict(id='A',worktree=state['tasks']['A']['worktree'],branch=state['tasks']['A']['branch'])]))
        result=self.runtime.routing(dict(op='approve',event_id='direct-approve-r2',plan_id='A',reference=approval),self.actor,
            user_origin=dict(role='user',conversation_id=self.actor,reference=approval))
        self.assertEqual(result['status'],'recorded')
        adapter,self.calls=self.adapter({'spawn_agent':dict(agent_id='direct-native'), 'wait_agent':dict(status={'direct-native':'unknown'})})
        self.launch_fixture('A',adapter,attempt_id='direct-launch',admission=self.permit(attempt='direct-launch'),authorized=True,message='same task scope',spawn=True)

    def test_direct_launch_plan_and_actual_running_observation_reuse_original_identity(self):
        from task_handoff import APP
        plan=self.runtime.handoff('A','integrator',available_tools=[APP])
        self.assertEqual(plan['binding']['core_launch'],'direct-launch');self.assertNotIn('request_id',plan['binding'])
        self.assertEqual(plan['binding']['owner_agent_id'],'native_agent:direct-native')
        before=self.runtime._load();self.assertEqual(before['tasks']['A']['worker_run']['status'],'queued')
        capture=self.json('actual-running-fixture.json',dict(schema='task-handoff-observation/1',binding=plan['binding'],tool=APP,
            arguments=plan['host_call']['arguments'],result=dict(thread=dict(id='direct-native'),turns=[dict(status='inProgress')])))
        result=self.runtime.handoff('A','integrator',observation_ref=capture)
        self.assertEqual(result['status'],'running');state=self.runtime._load()
        self.assertEqual(state['tasks']['A']['phase'],'developing');self.assertEqual(len(self.calls),2)
        self.assertFalse(any(op.get('operation')=='standard-dispatch' for op in state['runtime']['operations'].values()))
        self.assertEqual(state['runtime'].get('observed_dispatches',{}),{})
        self.assertEqual(self.runtime.task_state('integrator').query()['tasks']['A']['owner_agent_id'],'native_agent:direct-native')

    def test_direct_launch_wrong_generation_admission_and_capture_reject_without_mutating(self):
        from task_handoff import APP, authority
        original=self.runtime._load()
        for field,value in [('generation',2),('executor_id','other'),('admission',self.file('forged-admission','{}'))]:
            state=deepcopy(original);state['runtime']['operations']['direct-launch'][field]=value
            with self.assertRaises((ValueError,KeyError)):authority(state,'A')
        state=deepcopy(original);state['runtime']['operations']['direct-launch']['host_results']=[]
        with self.assertRaises(ValueError):authority(state,'A')
        before=self.runtime.path.read_bytes()
        with self.assertRaises(ValueError):self.runtime.handoff('A','another-recipient',available_tools=[APP])
        self.assertEqual(before,self.runtime.path.read_bytes())

    def test_direct_terminal_without_report_stays_needs_author_not_completed(self):
        from task_handoff import APP
        plan=self.runtime.handoff('A','integrator',available_tools=[APP])
        capture=self.json('terminal-fixture.json',dict(schema='task-handoff-observation/1',binding=plan['binding'],tool=APP,
            arguments=plan['host_call']['arguments'],result=dict(thread=dict(id='direct-native'),turns=[dict(status='completed',items=[])])))
        result=self.runtime.handoff('A','integrator',observation_ref=capture)
        self.assertEqual(result['status'],'needs_author');self.assertEqual(self.runtime._load()['receipts'],{})


@unittest.skipUnless(os.environ.get('STATE_TOOL_LAUNCH_CAPTURE'), 'Optional frozen task-only actual launch capture not supplied')
class CapturedLaunchTests(unittest.TestCase):
    def test_frozen_actual_launch_shape_old_rejects_new_authority_accepts_without_ledger_io(self):
        from coordination import new_state
        from evidence import fingerprint, load_bound, ref
        from task_handoff import authority
        capture=Path(os.environ['STATE_TOOL_LAUNCH_CAPTURE']);before=capture.read_bytes()
        data=load_bound(ref(capture));key=data['task_id'];routing=data['routing'];item=routing['approved_item']
        state=new_state(data['coordinator_id'],data['project']['id']);state['project']=deepcopy(data['project'])
        state['tasks']={key:deepcopy(data['task'])}
        presentation=load_bound(routing['approval']['presentation'])
        plan=dict(id=presentation['plan_id'],version=presentation['version'],items=presentation['items'],presentation=routing['approval']['presentation'])
        launch=data['launch_operation'];attempt=launch['attempt_id'];executor=data['executor']
        state['runtime']=dict(executors={executor['id']:deepcopy(executor)},operations={attempt:deepcopy(launch)},
            intake=dict(plans={plan['id']:plan},requirements={item['requirement_id']:routing['requirement']},
                approvals={fingerprint(item):routing['approval']},routes={launch['route_id']:routing['route']}))
        # No source_ledger path is opened; this projection is not initialized as a Runtime.
        root=Path(__file__).resolve().parents[3]
        original=subprocess.run(['git','-c','core.longpaths=true','-C',str(root),'show',
            '4097bd8bb9deed4ab96bdf216b5a8a84ee75e627:skills/isolated-parallel-development-coordination/scripts/task_handoff.py'],check=True,capture_output=True).stdout
        namespace={'__name__':'frozen_baseline_handoff'};exec(compile(original,'frozen-baseline-handoff.py','exec'),namespace)
        with self.assertRaisesRegex(ValueError,'one bound standard dispatch required'):
            namespace['authority'](deepcopy(state),key)
        bound=authority(deepcopy(state),key)
        self.assertEqual(bound['owner_agent_id'],data['task']['owner_agent_id']);self.assertEqual(bound['core_launch'],attempt)
        self.assertEqual(bound['recipient_agent_id'],load_bound(routing['approval']['reference'])['integration_agent'])
        self.assertEqual(before,capture.read_bytes())


if __name__=='__main__':unittest.main()
