"""ARCHIVE A01-A05 with real disposable Git/files and the STATE transaction seam.

Identity, review/merge grants and host stop observations are fixture context.
No live ledger, author close, quota release, network merge or cleanup is exercised.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive_tool import ArchiveTool, move_original
from coordination import writer_lock
from core_runtime import Runtime
from evidence import git, io_path, ref, write_atomic
from task_state import TaskState


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='arc-')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.project=self.root/'project';self.project.mkdir()
        self.git(self.project,'init','-b','main')
        self.git(self.project,'config','user.name','Archive fixture')
        self.git(self.project,'config','user.email','fixture@example.test')
        (self.project/'code.txt').write_text('base\n',encoding='utf-8')
        self.git(self.project,'add','.');self.git(self.project,'commit','-m','fixture base')
        self.rt=Runtime(self.root/'state',self.project);self.rt.initialize('main',provider='github')
        self.materials=self.root/'materials';self.materials.mkdir()
        self.spec=self.materials/'A.md';self.spec.write_text('# Confirmed A\nNo extra deliverables.\n',encoding='utf-8')
        self.original=ref(self.spec);self.archive=self.root/'archive';self.number=0
        self.tool=self.client()

    @staticmethod
    def git(root,*args):
        return git(root,'-c','core.longpaths=true',*args)

    def client(self, actor='integrator', **kw):
        return self.rt.archive_tool(actor,archive_root=kw.pop('archive_root',self.archive),
            spec_root=kw.pop('spec_root',self.materials),authorize=kw.pop('authorize',lambda task,actor,plan:True),**kw)

    def state_client(self,actor,**kw):
        return self.rt.task_state(actor,**kw)

    def record(self,actor,action,**fields):
        self.number+=1;client=self.state_client(actor,**fields.pop('callbacks',{}))
        value=dict(action=action,task_id='A',event_id='event-'+str(self.number),expected_revision=self.revision(),**fields)
        if action in ('self-test','deliver-pr','review','technical-acceptance','merge-verified'):
            value['version']=client.observe('A')['version']
        origin=dict(role='user',conversation_id='main',reference=value['user_reference']) if 'user_reference' in value else None
        result=client.record(value,user_origin=origin)
        self.assertEqual(result['status'],'recorded',result)
        return result

    def revision(self):return self.rt._load()['revision']

    def register(self,extras=None):
        if getattr(self, 'use_routing', False):
            return self.route_prepare(extras)
        fields=dict(spec=self.original,scope='fixture task',acceptance=['actual original move'],
            acceptance_type='delivery_verified',integrator_id='integrator',rights=dict(commit=True,push=True,mr=True,merge=True),
            user_reference='fixture actual-host user authorization')
        if extras is not None: fields['deliverables']=extras
        self.record('main','register',**fields)

    def route_prepare(self, extras=None):
        from demand_routing import scope_digest
        def saved(name,value):
            p=self.materials/name;p.write_text(json.dumps(value),encoding='utf-8');return ref(p)
        def route(op,**fields):
            result=self.rt.routing(dict(op=op,event_id='route-'+op,**fields),'main')
            self.assertEqual(result['status'],'recorded',result)
        discussion=saved('discussion.json',dict(confirmed=True))
        check=saved('check.json',dict(coordinator_id='main',requirement_id='REQ-A',spec=self.original,discussion=discussion,result='consistent'))
        route('spec',requirement_id='REQ-A',name='A',scope='fixture task',acceptance=['actual original move'],
            discussion=discussion,spec=self.original,check=check,start_reference=discussion,deliverables=extras or [])
        record=self.rt._load()['runtime']['intake']['requirements']['REQ-A']
        item=dict(task_id='A',requirement_id='REQ-A',name='A',scope=record['scope'],acceptance=record['acceptance'],
            scope_digest=scope_digest(record),split_reason='one fixture demand',assignment='new',executor_id=None,assignment_reason='one new author')
        presentation=saved('presentation.json',dict(conversation_id='main',plan_id='plan-A',version=1,items=[item]))
        route('present',plan_id='plan-A',version=1,items=[item],presentation=presentation)
        approval=saved('approval.json',dict(plan_id='plan-A',presentation=presentation,task_ids=['A'],decision='approved',quote='Approve fixture task and dispatch summary',integration_agent='integrator'))
        result=self.rt.routing(dict(op='approve',event_id='route-approve',plan_id='plan-A',reference=approval),'main',
            user_origin=dict(role='user',conversation_id='main',reference=approval))
        self.assertEqual(result['status'],'recorded',result)
        self.rt.prepare(dict(task_id='A',worktree=str(Path(self.rt._load()['runtime']['cleanup_root'])/'A'),branch='task/A',
            scope=record['scope'],acceptance=record['acceptance'],acceptance_type='delivery_verified',user_reference=approval['path']),'main')
        # Same public policy/rights entry; no re-register or fabricated state_contract.
        self.record('main','authorize',rights=dict(commit=True,push=True,mr=True,merge=True),user_reference=approval['path'])

    def bind(self):
        self.work=Path(self.rt._load()['runtime']['cleanup_root'])/'A'
        self.git(self.project,'worktree','add','-b','task/A',str(self.work),'main')
        (self.work/'code.txt').write_text('task result\n',encoding='utf-8')
        self.git(self.work,'add','.');self.git(self.work,'commit','-m','fixture task')
        # Disposable producer fixture only. Production ArchiveTool never writes ledger JSON.
        state=self.rt._load();task=state['tasks']['A']
        task.update(owner_agent_id='author',worktree=str(self.work),branch='task/A')
        task['worker_run']['owner_agent_id']='author'
        write_atomic(self.rt.path,state)

    def ready(self,extra=False):
        # Register extras only when explicitly declared; make a genuine untracked task file.
        self.register();self.bind()
        if extra:
            self.extra=self.work/'handoff.bin';self.extra.write_bytes(b'exact external deliverable\x00\xff')
            state=self.rt._load();state['tasks']['A']['state_contract']['deliverables']=[ref(self.extra)]
            write_atomic(self.rt.path,state)
            self.git(self.work,'config','core.excludesFile',str(self.root/'excludes'))
            (self.root/'excludes').write_text('handoff.bin\n',encoding='utf-8')
        self.record('author','self-test',status='passed',command='fixture code validation',evidence=[self.original])
        self.record('author','deliver-pr',pr=dict(provider='github',id=1,url='https://example.test/repo/pull/1'),summary='fixture delivery',evidence=[self.original])
        self.record('integrator','review',status='passed',summary='fixture independent review',evidence=[self.original])
        self.record('integrator','technical-acceptance',status='passed',summary='fixture acceptance',evidence=[self.original])
        before=self.git(self.project,'rev-parse','main');head=self.git(self.work,'rev-parse','HEAD')
        self.git(self.project,'merge','--no-ff','task/A','-m','fixture actual local merge')
        after=self.git(self.project,'rev-parse','main')
        # The STATE default observes actual source against the original tested main for merge.
        merge=dict(project_id=self.rt.project_id,task_id='A',operation_id='merge-A',source_head=head,
            previous_main=before,main_head=after,contains_source=self.git(self.project,'merge-base',head,after)==head,scope='fixture_only')
        client=self.state_client('integrator',observe_merge=lambda t,op:deepcopy(merge))
        version=self.rt._load()['tasks']['A']['source_version']
        result=client.record(dict(action='merge-verified',task_id='A',event_id='merge-local',operation_id='merge-A',
            expected_revision=self.revision(),version=version))
        self.assertEqual(result['status'],'recorded',result)

    def execute(self,tool=None):return (tool or self.tool).execute('A','archive-A',self.revision())
    def observe(self,tool=None):return (tool or self.tool).observe('A','archive-A',self.revision())

    def test_A01_original_move_current_spec_history_and_tree_removal(self):
        self.ready();history=deepcopy(self.rt._load()['history']);review=deepcopy(self.rt._load()['tasks']['A']['review'])
        result=self.execute();self.assertEqual(result['status'],'completed');self.assertEqual(result['state_registration']['status'],'recorded')
        producer=self.rt._load()['tasks']['A']['producer_results']['archive:archive-A']
        self.assertEqual(producer['ledger_revision'],result['state_registration']['revision'])
        task=self.rt.task_state('main').query('A')['tasks']['A'];current=task['state_contract']['spec']
        self.assertFalse(self.spec.exists());self.assertEqual(result['files'][0]['current'],current);self.assertEqual(result['admission']['basis'],'merged_main_verified');self.assertEqual(ref(current['path']),current);self.assertEqual(current['sha256'],self.original['sha256'])
        self.assertEqual(task['review'],review);self.assertEqual(self.rt._load()['history'][:len(history)],history)
        self.assertEqual(task['state_contract']['location_history'][0]['original'],self.original)
        self.assertEqual(result['protected_directories'],[]);self.assertEqual(len(result['files']),1)
        self.git(self.project,'worktree','remove',str(self.work))
        self.assertEqual(ref(current['path']),current)
        self.assertEqual(self.tool.query('A')['archive']['status'],'completed')
        self.assertEqual(task['cleanup']['status'],'pending_cleanup')

    def test_A02_explicit_untracked_deliverable_moves_exact_original(self):
        self.ready(extra=True);expected=ref(self.extra);result=self.execute()
        self.assertEqual(result['status'],'completed');self.assertEqual(len(result['files']),2)
        self.assertFalse(self.extra.exists());self.assertFalse(self.spec.exists())
        actual=self.rt._load()['tasks']['A']['state_contract']['deliverables'][0]
        self.assertEqual(actual['sha256'],expected['sha256']);self.assertEqual(ref(actual['path']),actual)

    def test_A03_report_paths_do_not_expand_or_block_manifest(self):
        self.ready();other=self.root/'other-task.txt';other.write_text('leave me',encoding='utf-8')
        report=self.root/'unused-report.json';report.write_text(json.dumps(dict(evidence=[str(self.root/'deleted.log'),str(other)])),encoding='utf-8')
        state=self.rt._load();state['tasks']['A']['delivery']=ref(report);write_atomic(self.rt.path,state)
        with patch('resource_lifecycle.preserve',side_effect=AssertionError('legacy graph invoked')):
            result=self.execute()
        self.assertEqual(result['status'],'completed');self.assertEqual(len(result['files']),1)
        self.assertTrue(other.exists());self.assertTrue(report.exists())

    def test_A04_partial_move_restart_only_moves_remaining(self):
        self.ready(extra=True);calls=[]
        def interrupted(a,b):
            calls.append((a,b))
            if len(calls)==2:raise PermissionError('fixture second file locked')
            move_original(a,b)
        with patch('archive_tool.move_original',side_effect=interrupted):result=self.execute()
        self.assertEqual(result['status'],'partial');self.assertFalse(self.spec.exists());self.assertTrue(self.extra.exists())
        self.assertIn(str(self.work),result['protected_directories'])
        new=self.client()
        with patch('archive_tool.move_original',wraps=move_original) as mover:result=new.execute('A','archive-A',self.revision())
        self.assertEqual(mover.call_count,1);self.assertEqual(result['status'],'completed')

    def test_A04_move_succeeded_result_save_failed_then_observe_only(self):
        self.ready();real=self.rt._save
        def save(state,op):
            if state['runtime']['operations'].get('archive-A',{}).get('status')!='intent':raise OSError('fixture result disk failure')
            return real(state,op)
        with patch.object(self.rt,'_save',side_effect=save):result=self.execute()
        self.assertEqual(result['status'],'completed');self.assertEqual(result['state_registration']['status'],'registration_pending')
        self.assertFalse(self.spec.exists());self.assertEqual(self.rt._load()['tasks']['A']['state_contract']['spec'],self.original)
        with patch('archive_tool.move_original',side_effect=AssertionError('must only observe')):result=self.observe(self.client())
        self.assertEqual(result['status'],'completed');self.assertEqual(result['state_registration']['status'],'recorded')
        self.assertFalse(self.spec.exists())

    def test_A04_actual_process_interrupt_after_move_recovers_original_intent(self):
        self.ready();prepared=self.tool.prepare('A','archive-A',self.revision());plan=prepared['plan']
        code="import os,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from archive_tool import move_original;p=Path(sys.argv[3]);p.parent.mkdir(parents=True,exist_ok=True);move_original(sys.argv[2],sys.argv[3]);os._exit(9)"
        item=plan['files'][0]
        proc=subprocess.run([sys.executable,'-X','utf8','-B','-c',code,str(Path(__file__).resolve().parent),item['original']['path'],item['destination']['path']],cwd=self.project)
        self.assertEqual(proc.returncode,9);self.assertFalse(self.spec.exists())
        with patch('archive_tool.move_original',side_effect=AssertionError('repeated original move')):result=self.execute(self.client())
        self.assertEqual(result['status'],'completed')

    def test_A05_manual_file_tool_takeover_and_observe_does_not_invoke_broken_mover(self):
        self.ready()
        with patch('archive_tool.move_original',side_effect=PermissionError('fixture tool failure')):failed=self.execute()
        self.assertEqual(failed['status'],'pending');self.assertIn(str(self.materials),failed['protected_directories'])
        plan=self.tool.query('A')['archive']['plan'];item=plan['files'][0]
        # Independent normal file operation, using only the persisted same-scope intent.
        Path(item['destination']['path']).parent.mkdir(parents=True,exist_ok=True)
        shutil.move(item['original']['path'],item['destination']['path'])
        with patch('archive_tool.move_original',side_effect=AssertionError('broken mover must not run')):result=self.observe()
        self.assertEqual(result['status'],'completed');self.assertEqual(result['state_registration']['status'],'recorded')

    def test_cancel_without_resources_archives_spec_only_without_pr_or_merge(self):
        self.register();self.record('main','cancel',reason='fixture cancel',user_reference='fixture actual user cancellation')
        def stop(task):
            with writer_lock(self.rt.path):pass
            return dict(project_id=self.rt.project_id,task_id='A',owner_agent_id=None,stopped=True,merge_status='not_merged',scope='fixture_only')
        result=self.execute(self.client(observe_stop=stop))
        self.assertEqual(result['mode'],'cancelled');self.assertEqual(result['status'],'completed')
        task=self.rt._load()['tasks']['A'];self.assertIsNone(task['owner_agent_id']);self.assertIsNone(task['mr']);self.assertIsNone(task['merge'])

    def test_cancel_does_not_require_missing_abandoned_deliverables(self):
        self.register();self.bind();lost=self.work/'abandoned.bin';lost.write_bytes(b'abandoned')
        state=self.rt._load();state['tasks']['A']['state_contract']['deliverables']=[ref(lost)];write_atomic(self.rt.path,state);lost.unlink()
        self.record('main','cancel',reason='fixture cancellation',user_reference='fixture actual cancel')
        stop=lambda t:dict(project_id=self.rt.project_id,task_id='A',owner_agent_id='author',stopped=True,merge_status='not_merged',scope='fixture_only')
        result=self.execute(self.client(observe_stop=stop))
        self.assertEqual(len(result['files']),1);self.assertEqual(result['status'],'completed');self.assertFalse(lost.exists());self.assertTrue(self.work.exists())

    def test_cancel_unknown_stop_wrong_identity_or_merge_result_rejected(self):
        self.register();self.record('main','cancel',reason='fixture',user_reference='fixture cancel')
        good=dict(project_id=self.rt.project_id,task_id='A',owner_agent_id=None,stopped=True,merge_status='not_merged',scope='fixture_only')
        for change in ({'stopped':False},{'project_id':'other'},{'task_id':'B'},{'owner_agent_id':'invented'},{'merge_status':'unknown'},{'merge_status':'merged'}):
            before=self.rt.path.read_bytes()
            with self.assertRaises(ValueError):self.execute(self.client(observe_stop=lambda t:dict(good,**change)))
            self.assertEqual(self.rt.path.read_bytes(),before);self.assertTrue(self.spec.exists())
        with self.assertRaises(ValueError):self.execute()

    def test_normal_premerge_rejected_and_cancel_requested_is_not_stop(self):
        self.register()
        with self.assertRaises(ValueError):self.execute()
        self.record('main','cancel',reason='fixture',user_reference='fixture cancel')
        with self.assertRaises(ValueError):self.execute()
        self.assertTrue(self.spec.exists())

    def test_cancel_merge_reservation_requires_reconcile(self):
        self.register();self.record('main','cancel',reason='fixture',user_reference='fixture cancel')
        state=self.rt._load();state['merge_reservation']=dict(task_id='A');write_atomic(self.rt.path,state)
        with self.assertRaises(ValueError):self.execute(self.client(observe_stop=lambda t:self.fail('must reconcile first')))

    def test_author_wrong_actor_and_self_signed_authority_rejected(self):
        self.ready();before=self.rt.path.read_bytes()
        for actor in ('author','main','other'):
            with self.assertRaises(ValueError):self.execute(self.client(actor))
        with self.assertRaises(ValueError):self.execute(self.client(authorize=lambda *a:dict(allowed=True,actor='integrator')))
        with self.assertRaises(ValueError):self.execute(self.client(authorize=lambda *a:False))
        with self.assertRaises(TypeError):self.tool.execute('A','archive-A',self.revision(),role='integrator')
        self.assertEqual(self.rt.path.read_bytes(),before);self.assertTrue(self.spec.exists())

    def test_stale_revision_and_changed_task_facts_never_overwrite(self):
        self.ready();stale=self.revision()-1;before=self.rt.path.read_bytes()
        result=self.tool.execute('A','archive-A',stale);self.assertEqual(result['status'],'conflict')
        self.assertEqual(before,self.rt.path.read_bytes());self.assertTrue(self.spec.exists())
        self.tool.prepare('A','archive-A',self.revision())
        state=self.rt._load();state['tasks']['A']['source_version']['head_sha']='changed';write_atomic(self.rt.path,state)
        with self.assertRaises(ValueError):self.execute()
        self.assertTrue(self.spec.exists())

    def test_intent_write_failure_never_moves_files(self):
        self.ready()
        with patch.object(self.rt,'_save',side_effect=OSError('intent not durable')):result=self.execute()
        self.assertEqual(result['status'],'not_started');self.assertTrue(self.spec.exists());self.assertFalse(self.archive.exists())

    def test_new_operation_id_or_conflicting_task_cannot_bypass_unknown(self):
        self.ready();self.tool.prepare('A','archive-A',self.revision())
        before=self.rt.path.read_bytes()
        with self.assertRaises(ValueError):self.tool.execute('A','new-operation',self.revision())
        self.assertEqual(before,self.rt.path.read_bytes())
        with self.assertRaises(KeyError):self.tool.execute('OTHER','archive-A',self.revision())

    def test_existing_destination_even_identical_never_adopted_without_intent(self):
        self.ready();target=self.archive/'task-A/spec/A.md';target.parent.mkdir(parents=True);target.write_bytes(self.spec.read_bytes())
        with self.assertRaises(ValueError):self.execute()
        self.assertTrue(self.spec.exists());self.assertEqual(ref(target)['sha256'],self.original['sha256'])

    def test_changed_missing_both_or_duplicate_files_require_manual_reconcile(self):
        self.ready();plan=self.tool.prepare('A','archive-A',self.revision())['plan'];target=Path(plan['files'][0]['destination']['path'])
        target.parent.mkdir(parents=True);target.write_bytes(self.spec.read_bytes())
        result=self.execute();self.assertEqual(result['status'],'needs_reconcile');self.assertTrue(self.spec.exists())
        target.write_text('different task bytes',encoding='utf-8');result=self.observe();self.assertEqual(result['status'],'needs_reconcile')
        self.assertEqual(target.read_text(encoding='utf-8'),'different task bytes')
        target.unlink();self.spec.unlink();result=self.execute();self.assertEqual(result['status'],'needs_reconcile')
        self.assertIn(str(self.materials),result['protected_directories'])

    def test_source_hash_change_is_rejected_before_intent_or_move(self):
        self.ready();self.spec.write_text('unexpected new content',encoding='utf-8');before=self.rt.path.read_bytes()
        with self.assertRaises(ValueError):self.execute()
        self.assertEqual(before,self.rt.path.read_bytes());self.assertFalse(self.archive.exists())

    def test_fixed_archive_policy_and_cleaning_roots(self):
        self.ready()
        for root in (self.project,self.work,self.rt.directory,Path(self.rt._load()['runtime']['cleanup_root'])/'nested',self.materials/'archive'):
            with self.assertRaises(ValueError):self.execute(self.client(archive_root=root))
        self.tool.prepare('A','archive-A',self.revision())
        with self.assertRaises(ValueError):self.execute(self.client(archive_root=self.root/'changed-archive'))

    def test_git_tracked_and_unowned_extra_files_rejected(self):
        self.ready();contract=self.rt._load()['tasks']['A']['state_contract']
        for path in (self.work/'code.txt',self.project/'code.txt',self.materials/'other.bin'):
            if not path.exists():path.write_bytes(b'not task owned')
            state=self.rt._load();state['tasks']['A']['state_contract']['deliverables']=[ref(path)];write_atomic(self.rt.path,state)
            with self.assertRaises(ValueError):self.execute()
            self.assertTrue(path.exists());self.assertTrue(self.spec.exists())

    def test_shared_protected_customer_files_and_cross_task_refs_rejected(self):
        self.ready(extra=True)
        for changes in (dict(protected=True),dict(shared=True),dict(customer_in_use=True),dict(in_use=True),dict(references=['A','B']),dict(task_id='B')):
            state=self.rt._load();state['runtime'].setdefault('resources',{})['extra']=dict(id='extra',kind='artifact',task_id='A',path=str(self.extra),**{k:v for k,v in changes.items() if k!='task_id'})
            if 'task_id' in changes:state['runtime']['resources']['extra']['task_id']=changes['task_id']
            write_atomic(self.rt.path,state)
            with self.assertRaises(ValueError):self.execute()
            self.assertTrue(self.extra.exists());self.assertTrue(self.spec.exists())
        state=self.rt._load();state['runtime']['resources']={};state['project']['protected']=[ref(self.spec)];write_atomic(self.rt.path,state)
        with self.assertRaises(ValueError):self.execute()
        state=self.rt._load();state['project']['protected']=[];state['tasks']['B']=deepcopy(state['tasks']['A']);write_atomic(self.rt.path,state)
        with self.assertRaises(ValueError):self.execute()

    def test_hardlink_and_junction_sources_rejected(self):
        self.ready();alias=self.materials/'alias.md';os.link(self.spec,alias)
        with self.assertRaises(ValueError):self.execute()
        alias.unlink()
        if os.name=='nt':
            junction=self.root/'junction'
            proc=subprocess.run(['cmd','/d','/c','mklink','/J',str(junction),str(self.materials)],cwd=self.root,capture_output=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            self.addCleanup(lambda:junction.rmdir() if junction.exists() else None)
            with self.assertRaises(ValueError):self.client(spec_root=junction)
        self.assertTrue(self.spec.exists())

    def test_move_and_host_callbacks_never_hold_project_writer_lock(self):
        self.ready()
        def authorized(*a):
            with writer_lock(self.rt.path):pass
            return True
        def mover(*a):
            with writer_lock(self.rt.path):pass
            move_original(*a)
        with patch('archive_tool.move_original',side_effect=mover):result=self.execute(self.client(authorize=authorized))
        self.assertEqual(result['status'],'completed')

    def test_concurrent_revision_after_move_preserves_actual_files(self):
        self.ready()
        def mover(*a):
            move_original(*a)
            self.rt.apply(dict(op='enqueue',item=dict(id='concurrent',kind='decision',task_id=None,
                title='other task choice',prompt='fixture business choice',options=['a','b'])),'main')
        with patch('archive_tool.move_original',side_effect=mover):result=self.execute()
        self.assertEqual(result['status'],'completed');self.assertEqual(result['state_registration']['status'],'conflict')
        self.assertFalse(self.spec.exists());self.assertEqual(self.rt._load()['tasks']['A']['state_contract']['spec'],self.original)
        self.assertEqual(self.observe()['state_registration']['status'],'recorded')

    def test_query_does_not_probe_reports_files_or_rerun_tests(self):
        self.ready();self.execute();before=self.rt.path.read_bytes()
        with patch('archive_tool.file_fact',side_effect=AssertionError('query probe')),patch('evidence.report_for',side_effect=AssertionError('report graph')):
            result=self.tool.query('A');task=self.rt.task_state('main').query('A')
        self.assertFalse(result['host_verified_now']);self.assertEqual(before,self.rt.path.read_bytes())
        self.assertEqual(task['tasks']['A']['archive']['status'],'completed')

    def test_repeated_execute_observes_completed_does_not_move_again(self):
        self.ready();first=self.execute();history=deepcopy(self.rt._load()['tasks']['A']['state_contract']['location_history'])
        with patch('archive_tool.move_original',side_effect=AssertionError('duplicate move')):second=self.execute()
        self.assertEqual(first['files'],second['files']);self.assertEqual(self.rt._load()['tasks']['A']['state_contract']['location_history'],history)

    def test_public_registration_preserves_explicit_default_empty_deliverables(self):
        self.register()
        self.assertEqual(self.rt._load()['tasks']['A']['state_contract']['deliverables'],[])

    def test_public_registration_accepts_only_exact_explicit_file_references(self):
        extra=self.materials/'declared.bin';extra.write_bytes(b'file promised by confirmed spec')
        self.register([ref(extra)])
        self.assertEqual(self.rt._load()['tasks']['A']['state_contract']['deliverables'],[ref(extra)])

    def test_busy_project_lock_keeps_intent_and_files_unmoved(self):
        self.ready()
        with writer_lock(self.rt.path):result=self.execute()
        self.assertEqual(result['status'],'not_started')
        self.assertEqual(result['state_registration']['status'],'conflict');self.assertTrue(self.spec.exists())

    def test_busy_original_operation_lock_cannot_start_second_move(self):
        self.ready()
        from evidence import fingerprint
        lock=self.rt.directory/'archive-operations'/fingerprint(['A','archive-A'])
        with writer_lock(lock):
            with self.assertRaisesRegex(ValueError,'writer lock exists'):self.execute()
        self.assertTrue(self.spec.exists())

    def test_canonical_routing_prepare_normal_move_current_location_and_frozen_check(self):
        self.use_routing=True;self.ready()
        before=deepcopy(self.rt._load()['runtime']['intake']['requirements']['REQ-A'])
        result=self.execute();self.assertEqual(result['status'],'completed')
        task=self.rt._load()['tasks']['A'];record=self.rt._load()['runtime']['intake']['requirements']['REQ-A']
        self.assertIsNone(task.get('state_contract'));self.assertEqual(record['spec'],before['spec']);self.assertEqual(record['check'],before['check'])
        self.assertEqual(record['current_spec'],task['spec_location']);self.assertEqual(ref(task['spec_location']['path']),task['spec_location'])
        from demand_routing import current_requirement
        self.assertEqual(current_requirement(self.rt._load(),'REQ-A')['spec'],before['spec'])
        self.assertFalse(self.spec.exists());self.assertEqual(self.observe()['status'],'completed')

    def test_canonical_cancel_without_reregister_and_missing_extra_not_required(self):
        self.use_routing=True;extra=self.materials/'abandoned';extra.write_bytes(b'no longer delivered')
        self.register([ref(extra)]);extra.unlink()
        self.record('main','cancel',reason='fixture cancel',user_reference='fixture user cancellation')
        stop=lambda t:dict(project_id=self.rt.project_id,task_id='A',owner_agent_id=None,stopped=True,merge_status='not_merged',scope='fixture_only')
        result=self.execute(self.client(observe_stop=stop))
        self.assertEqual(result['status'],'completed');self.assertEqual(len(result['files']),1)
        self.assertIsNone(self.rt._load()['tasks']['A'].get('state_contract'))

    def test_canonical_unapproved_or_ambiguous_binding_is_rejected(self):
        self.use_routing=True;self.ready();state=self.rt._load()
        state['runtime']['intake']['approvals']={};write_atomic(self.rt.path,state)
        with self.assertRaises(ValueError):self.execute()
        self.assertTrue(self.spec.exists())

    def test_registered_main_inclusion_proof_uses_minimal_current_merge_fact(self):
        self.ready();state=self.rt._load();task=state['tasks']['A'];merge=task['merge'];observation=merge.pop('observation')
        proof=self.root/'merge-proof.json';proof.write_text(json.dumps(observation),encoding='utf-8');merge['proof']=ref(proof)
        task.pop('source_version');write_atomic(self.rt.path,state)
        self.assertEqual(self.execute()['status'],'completed')

    def test_git_administrative_file_is_never_a_deliverable(self):
        self.ready();state=self.rt._load();state['tasks']['A']['state_contract']['deliverables']=[ref(self.work/'.git')];write_atomic(self.rt.path,state)
        with self.assertRaises(ValueError):self.execute()
        self.assertTrue((self.work/'.git').exists())

    def test_destination_created_after_intent_never_gets_overwritten(self):
        self.ready()
        def raced(source,target):
            Path(target).write_bytes(b'concurrent different file')
            move_original(source,target)
        with patch('archive_tool.move_original',side_effect=raced):result=self.execute()
        self.assertEqual(result['status'],'needs_reconcile');self.assertTrue(self.spec.exists())
        self.assertEqual(Path(result['files'][0]['destination']['path']).read_bytes(),b'concurrent different file')

    def test_authorization_observer_revision_change_before_move_stops_execution(self):
        self.ready();changed=[]
        def grant(*a):
            if not changed and 'archive-A' in self.rt._load()['runtime']['operations']:
                changed.append(True)
                self.rt.apply(dict(op='enqueue',item=dict(id='new-revision',kind='decision',title='other task',prompt='choose',options=['a','b'])),'main')
            return True
        result=self.execute(self.client(authorize=grant))
        self.assertEqual(result['state_registration']['status'],'conflict');self.assertTrue(self.spec.exists())

    def test_canonical_and_state_current_locators_update_in_same_transaction(self):
        self.use_routing=True;self.ready();state=self.rt._load()
        state['tasks']['A']['state_contract']=dict(schema='task-state/1',integrator_id='integrator',spec=deepcopy(self.original),deliverables=[])
        write_atomic(self.rt.path,state)
        self.assertEqual(self.execute()['status'],'completed')
        task=self.rt._load()['tasks']['A'];record=self.rt._load()['runtime']['intake']['requirements']['REQ-A']
        self.assertEqual(task['spec_location'],task['state_contract']['spec']);self.assertEqual(task['spec_location'],record['current_spec'])
        self.assertEqual(record['spec'],self.original)

    def test_long_destination_path_preserves_original_content(self):
        self.ready();deep=self.root/('archive-'+('x'*170))
        result=self.execute(self.client(archive_root=deep))
        self.assertEqual(result['status'],'completed');self.assertFalse(self.spec.exists())
        self.assertEqual(ref(result['files'][0]['current']['path'])['sha256'],self.original['sha256'])

    def test_archive_contract_links_and_entry_are_in_portable_payload(self):
        import package
        import check_guidance
        source=Path(__file__).resolve().parents[3]
        files=package.payload(source)
        names=('skills/isolated-parallel-development-coordination/references/archive-tool.md',
               'skills/isolated-parallel-development-coordination/references/task-state.md',
               'docs/design/plugin-module-architecture.md','docs/design/tool-interface-contracts.md',
               'docs/acceptance/tool-workflow-checklist.md')
        with patch.object(check_guidance,'CURRENT',names):
            self.assertEqual(check_guidance.check(files)['errors'],[])
        self.assertIn('skills/isolated-parallel-development-coordination/scripts/archive_tool.py',files)

    def revise_confirmed_requirement(self, **changes):
        previous=self.rt._load()['runtime']['intake']['requirements']['REQ-A']
        fields={key:deepcopy(previous[key]) for key in
                ('name','scope','acceptance','discussion','spec','check','start_reference')}
        fields['deliverables']=deepcopy(previous.get('deliverables', []))
        fields.update(changes)
        return self.rt.routing(dict(op='spec',event_id='same-scope-spec-update',requirement_id='REQ-A',**fields),'main')

    def test_completed_canonical_manifest_cannot_add_undeclared_deliverable(self):
        self.use_routing=True;self.ready()
        previous=deepcopy(self.rt._load()['runtime']['intake']['requirements']['REQ-A'])
        extra=self.work/'undeclared-after-merge.bin';extra.write_bytes(b'not in confirmed empty manifest')
        update=self.revise_confirmed_requirement(deliverables=[ref(extra)])
        result=self.execute()
        self.assertTrue(extra.exists(),'same-scope update moved an undeclared extra')
        self.assertEqual(len(result['files']),1)
        self.assertEqual(update['status'],'rejected',update)
        record=self.rt._load()['runtime']['intake']['requirements']['REQ-A']
        self.assertEqual(record['spec'],previous['spec']);self.assertEqual(record['deliverables'],[])

    def test_completed_canonical_manifest_cannot_replace_spec_under_same_scope(self):
        self.use_routing=True;self.ready()
        previous=deepcopy(self.rt._load()['runtime']['intake']['requirements']['REQ-A'])
        changed=self.materials/'unapproved-replacement.md';changed.write_text('different final spec bytes',encoding='utf-8')
        check=self.materials/'unapproved-self-check.json'
        check.write_text(json.dumps(dict(coordinator_id='main',requirement_id='REQ-A',spec=ref(changed),
            discussion=previous['discussion'],result='consistent')),encoding='utf-8')
        update=self.revise_confirmed_requirement(spec=ref(changed),check=ref(check))
        result=self.execute()
        self.assertTrue(changed.exists(),'same-scope update moved replacement spec')
        self.assertEqual(update['status'],'rejected',update)
        self.assertEqual(result['files'][0]['original'],self.original)

    def test_identical_ended_spec_update_preserves_archived_locator_and_history(self):
        self.use_routing=True;self.ready();self.assertEqual(self.execute()['status'],'completed')
        before=deepcopy(self.rt._load()['runtime']['intake']['requirements']['REQ-A'])
        update=self.revise_confirmed_requirement()
        self.assertEqual(update['status'],'recorded',update)
        after=self.rt._load()['runtime']['intake']['requirements']['REQ-A']
        self.assertEqual(after,before)
        self.assertEqual(ref(after['current_spec']['path']),after['current_spec'])
        self.assertEqual(self.observe()['status'],'completed')

    def test_low_level_state_event_cannot_forge_archive(self):
        self.ready();before=self.rt.path.read_bytes()
        with self.assertRaises(ValueError):self.rt.task_state('author').record(dict(action='archive',task_id='A',event_id='forged',expected_revision=self.revision(),status='completed'))
        self.assertEqual(self.rt.path.read_bytes(),before)


if __name__=='__main__':unittest.main()
