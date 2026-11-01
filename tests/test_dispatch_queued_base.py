"""Real disposable Git/RESOURCE queue recovery; native callbacks are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch
from evidence import load_bound, ref


class QueuedBaselineTests(unittest.TestCase):
    def setUp(self):
        self.f = test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.controller.capacity_acquire(f.json('occupied.json', dict(request_id='occupied', project_id=f.runtime.project_id,
            task_id='occupied', attempt_id='occupied', launch_identity='occupied', resource_class='agent_execution_slot'))['path'])
        self.assertEqual(f.run_dispatch()['status'], 'queued')
        self.original_request = deepcopy(f.request)
        self.original_refs = {r['path']:Path(r['path']).read_bytes() for r in
            (f.request['approval'], f.request['requirement']['spec'], f.request['requirement']['check'])}
        remote = f.root/'private-remote.git'
        f.git._git('clone','--bare',str(f.git.repository),str(remote))
        f.git._git('remote','set-url','origin',str(remote))

    def advance(self, *, parent=True, push=True):
        f = self.f
        previous = f.git._git('rev-parse','refs/heads/main').stdout.decode().strip()
        tree = f.git._git('rev-parse',previous+'^{tree}').stdout.decode().strip()
        args = ['-c','user.name=Fixture','-c','user.email=fixture@local.invalid','commit-tree',tree,'-m','Disposable main advance']
        if parent:args += ['-p',previous]
        current = f.git._git(*args).stdout.decode().strip()
        f.git._git('update-ref','refs/heads/main',current,previous)
        if push:
            self.assertEqual(Path(f.git._git('remote','get-url','origin').stdout.decode().strip()).resolve(),
                             (f.root/'private-remote.git').resolve())
            f.git._git('push',*(['--force-with-lease=refs/heads/main:'+previous] if not parent else []),'origin','main')
        return current

    def assert_frozen(self):
        self.assertEqual(self.f.request,self.original_request)
        for path,raw in self.original_refs.items():self.assertEqual(Path(path).read_bytes(),raw)

    def release_fixture_slot(self):
        f=self.f
        permit=f.controller.read()[0]['capacity_requests']['occupied']['permit_id']
        f.controller.capacity_release('occupied',permit,f.json('occupied-not-started.json',dict(request_id='occupied',
            project_id=f.runtime.project_id,task_id='occupied',attempt_id='occupied',permit_id=permit,outcome='confirmed_not_started')),
            verifier=lambda *_:dict(verified=True,not_started=True))

    def test_same_queued_request_rebases_without_effect_then_creates_from_effective_main(self):
        f = self.f; current = self.advance()
        with self.assertRaisesRegex(ValueError,'stale'):f.run_dispatch()
        resource_before = f.controller.path.read_bytes()
        result = f.dispatch.rebase_queued(f.request,authorized=True)
        self.assertEqual(result['status'],'queued')
        self.assertEqual(result['effective_base_main'],current)
        self.assertFalse(Path(f.request['worktree']).exists())
        self.assertEqual(f.calls,[])
        self.assertEqual(resource_before,f.controller.path.read_bytes())
        self.assert_frozen()
        before = f.runtime.path.read_bytes()
        self.assertTrue(f.dispatch.rebase_queued(f.request,authorized=True)['duplicate'])
        self.assertEqual(before,f.runtime.path.read_bytes())
        # Confirm the occupied synthetic permit was never used; no real release.
        self.release_fixture_slot()
        resumed = f.run_dispatch()
        self.assertEqual(resumed['status'],'dispatched')
        self.assertEqual(f.git._git('-C',f.request['worktree'],'rev-parse','HEAD').stdout.decode().strip(),current)
        self.assertIn('Base main: '+current,f.calls[0][1]['message'])
        state=f.runtime._load()
        self.assertEqual(state['runtime']['operations']['dispatch-A:launch']['attempt_id'],'dispatch-A:launch')
        self.assertEqual(state['runtime']['operations']['dispatch-A']['request_digest'],result['record']['request_digest'])
        self.assertEqual(len(f.calls),3)
        self.assert_frozen()

    def test_explicit_authorization_digest_and_original_scope_required(self):
        f=self.f;self.advance()
        before=f.runtime.path.read_bytes(),f.controller.path.read_bytes()
        for request,auth in ((f.request,False),(dict(f.request,branch='codex/other'),True)):
            with self.assertRaises(ValueError):f.dispatch.rebase_queued(request,authorized=auth)
        self.assertEqual(before,(f.runtime.path.read_bytes(),f.controller.path.read_bytes()))
        self.assertEqual(f.calls,[])

    def test_mismatched_remote_and_nonancestor_main_are_rejected(self):
        f=self.f;self.advance(push=False)
        with self.assertRaisesRegex(ValueError,'remote'):f.dispatch.rebase_queued(f.request,authorized=True)
        f.git._git('update-ref','refs/heads/main',f.base)
        self.advance(parent=False)
        before=f.runtime.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'ancestor'):f.dispatch.rebase_queued(f.request,authorized=True)
        self.assertEqual(before,f.runtime.path.read_bytes())
        self.assertFalse(Path(f.request['worktree']).exists())

    def test_existing_branch_tree_or_host_intent_never_qualifies(self):
        f=self.f;self.advance()
        f.git._git('branch',f.request['branch'],f.base)
        before=f.runtime.path.read_bytes()
        with self.assertRaises(ValueError):f.dispatch.rebase_queued(f.request,authorized=True)
        self.assertEqual(before,f.runtime.path.read_bytes())
        f.git._git('branch','-D',f.request['branch'])
        Path(f.request['worktree']).mkdir()
        with self.assertRaises(ValueError):f.dispatch.rebase_queued(f.request,authorized=True)
        Path(f.request['worktree']).rmdir()
        f.dispatch._record('dispatch-A',launch_started=True,status='unknown')
        before=f.runtime.path.read_bytes()
        with self.assertRaises(ValueError):f.dispatch.rebase_queued(f.request,authorized=True)
        self.assertEqual(before,f.runtime.path.read_bytes())
        self.assertEqual(f.calls,[])

    def test_repeated_advances_append_evidence_and_expiry_blocks_original_run(self):
        f=self.f;first=self.advance()
        f.dispatch.rebase_queued(f.request,authorized=True)
        second=self.advance()
        result=f.dispatch.rebase_queued(f.request,authorized=True)
        revisions=result['record']['base_revisions']
        self.assertEqual(len(revisions),2)
        self.assertEqual(load_bound(revisions[0])['effective_base_main'],first)
        self.assertEqual(load_bound(revisions[1])['effective_base_main'],second)
        Path(revisions[0]['path']).write_text('changed proof',encoding='utf-8')
        with self.assertRaises(ValueError):f.run_dispatch()
        self.assertEqual(f.calls,[])
        self.assert_frozen()

    def test_host_rebase_action_has_no_native_or_worktree_side_effect(self):
        f=self.f;current=self.advance()
        config=f.json('rebase-host-config.json',dict(python=sys.executable,
            profile=dict(project_root=str(f.runtime.project_root),state_root=str(f.root/'authority'),
                resource_state=str(f.controller.path),evidence_root=str(f.bridge.evidence),integration_channel='native_agent',integration_target='integration-fixture'),
            request=f.json('same-bound-request.json',f.request),requestMeta=dict(threadId=f.actor),approvalOrigin=f.origin,
            availableTools=['multi_agent_v1__spawn_agent','multi_agent_v1__wait_agent','send_input'],rebaseQueued=True))
        module=(test_dispatch.ROOT/'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs').as_uri()
        code="""
import fs from 'node:fs';
const {DispatchSession}=await import(MODULE);
const session=new DispatchSession(JSON.parse(fs.readFileSync(process.argv[1],'utf8')));
const packet=await session.next();
await session.exited;
console.log(JSON.stringify({packet,calls:session.calls}));
""".replace('MODULE',json.dumps(module))
        result=subprocess.run(['node','--input-type=module','-e',code,config['path']],cwd=test_dispatch.ROOT,
            capture_output=True,encoding='utf-8',timeout=55)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['calls'],0)
        self.assertEqual(value['packet']['result']['effective_base_main'],current)
        self.assertEqual(value['packet']['result']['status'],'queued')
        self.assertFalse(Path(f.request['worktree']).exists())
        self.assert_frozen()

    def test_resource_granted_before_resume_keeps_same_request_and_permit(self):
        f=self.f;self.advance();self.release_fixture_slot()
        before=f.controller.path.read_bytes()
        permit=f.controller.read()[0]['capacity_requests']['dispatch-A:capacity']['permit_id']
        self.assertTrue(permit)
        self.assertEqual(f.dispatch.rebase_queued(f.request,authorized=True)['status'],'queued')
        self.assertEqual(before,f.controller.path.read_bytes())
        self.assertEqual(f.run_dispatch()['status'],'dispatched')
        self.assertEqual(f.runtime._load()['runtime']['operations']['dispatch-A:launch']['permit_id'],permit)
        self.assertEqual(len(f.calls),3)
        self.assert_frozen()

    def test_concurrent_change_rejects_append_and_explicit_retry_keeps_orphan_evidence(self):
        f=self.f;self.advance()
        original=f.dispatch._preflight
        def racing(request,previous,**options):
            result=original(request,previous,**options)
            f.runtime.apply(dict(op='enqueue',item=dict(id='concurrent',kind='discussion',title='Concurrent fixture',
                prompt='Preserve this actual concurrent change')),f.actor)
            return result
        f.dispatch._preflight=racing
        resource_before=f.controller.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'changed'):
            f.dispatch.rebase_queued(f.request,authorized=True)
        state=f.runtime._load()
        self.assertIn('concurrent',state['items'])
        self.assertNotIn('base_revisions',state['runtime']['operations']['dispatch-A'])
        self.assertEqual(resource_before,f.controller.path.read_bytes())
        f.dispatch._preflight=original
        result=f.dispatch.rebase_queued(f.request,authorized=True)
        self.assertEqual(len(result['record']['base_revisions']),1)
        self.assertEqual(len(list(f.bridge.evidence.glob('dispatch-*-baseline-*.json'))),2)
        self.assertEqual(f.calls,[])
        self.assert_frozen()


if __name__=='__main__':unittest.main()
