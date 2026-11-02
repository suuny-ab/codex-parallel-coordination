"""Owned subprocess/lock faults. Native responses remain synthetic fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import unittest
from unittest.mock import patch

import test_dispatch_host
from coordination import writer_lock
from evidence import load_bound, ref


@unittest.skipUnless(os.name == 'nt' and shutil.which('node'), 'Windows process identity/handle semantics and Node required')
class DispatchLockRecovery(unittest.TestCase):
    def setUp(self):
        self.host = test_dispatch_host.HostTransport('test_real_thread_id_wins_over_parent_session_id')
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.f = self.host.fixture
        self.addCleanup(self.preserve_case)

    def preserve_case(self):
        output = os.environ.get('COORDINATION_TIMEOUT_EVIDENCE')
        if not output:
            return
        from evidence import io_path
        target = io_path(Path(output) / self._testMethodName)
        target.mkdir(parents=True, exist_ok=True)
        candidates = list(self.f.root.glob('*.json'))
        candidates += [p for p in self.f.bridge.evidence.rglob('*') if p.is_file()]
        for parent, directories, names in os.walk(self.f.runtime.directory):
            directories[:] = [name for name in directories if name != 'resources']
            candidates += [Path(parent)/name for name in names]
        manifest = []
        for source in sorted(set(candidates)):
            relative = source.relative_to(self.f.root)
            saved = io_path(target / relative)
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(io_path(source).read_bytes())
            manifest.append(dict(original=str(source), preserved=ref(saved)))
        io_path(target/'PATH_MAP.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

    def binding(self):
        from dispatch_lock import dispatch_binding
        return dispatch_binding(self.f.runtime, self.f.request, self.f.actor, self.host.config['request'])

    def dead_lock(self, target=None, change=None):
        target = target or self.f.runtime.path
        scripts = str(test_dispatch_host.test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts')
        if os.name == 'nt':
            scripts = '\\\\?\\' + scripts
        binding = self.binding()
        if change:
            change(binding)
        config = self.f.json('dead-config.json', dict(binding=binding, target=str(target), evidence=str(self.f.bridge.evidence)))
        code = """
import json,os,sys
sys.path.insert(0,sys.argv[1])
from dispatch_lock import dispatch_scope
from coordination import writer_lock
c=json.load(open(sys.argv[2]))
with dispatch_scope(c['binding'],c['evidence']):
    with writer_lock(c['target']):
        os._exit(71)
"""
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, scripts, config['path']],
                               cwd=self.f.root, capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 71, child.stderr)
        return target.with_suffix(target.suffix + '.lock')

    def recover(self):
        from dispatch_lock import recover_dispatch_locks
        return recover_dispatch_locks(self.f.runtime, self.binding(), self.f.bridge.evidence)

    def test_abrupt_death_preserves_lock_bytes_and_recovers_only_original_paths(self):
        lock = self.dead_lock()
        original = lock.read_bytes()
        before = self.f.runtime.path.read_bytes()
        result = self.recover()
        self.assertFalse(lock.exists())
        self.assertEqual(self.f.runtime.path.read_bytes(), before)
        self.assertEqual(len(result), 1)
        proof = load_bound(result[0])
        self.assertEqual(Path(proof['original']['path']).read_bytes(), original)
        self.assertEqual(proof['binding'], self.binding())
        self.assertEqual(self.recover(), [])

    def test_existing_runtime_recovery_checks_bound_observation_and_repeats_without_effect(self):
        lock = self.dead_lock()
        original = lock.read_bytes()
        reference = ref(lock)
        options = dict(request_ref=self.host.config['request'], dispatch_evidence_root=str(self.f.bridge.evidence))
        wrong = dict(reference, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.f.runtime.recover_call_locks([wrong], str(self.f.bridge.evidence), self.f.actor, **options)
        self.assertEqual(lock.read_bytes(), original)
        first = self.f.runtime.recover_call_locks([reference], str(self.f.bridge.evidence), self.f.actor, **options)
        self.assertEqual(len(first['locks']), 1)
        self.assertEqual(self.f.runtime.recover_call_locks([reference], str(self.f.bridge.evidence), self.f.actor, **options)['locks'], [])

    def test_live_owner_and_wrong_request_project_task_source_are_refused(self):
        from dispatch_lock import dispatch_scope
        with dispatch_scope(self.binding(), self.f.bridge.evidence):
            with writer_lock(self.f.runtime.path):
                original = self.f.runtime.path.with_suffix('.json.lock').read_bytes()
                with self.assertRaisesRegex(ValueError, 'active|alive'):
                    self.recover()
                self.assertEqual(self.f.runtime.path.with_suffix('.json.lock').read_bytes(), original)
        for key in ('project_id', 'task_id', 'request_id', 'request_digest'):
            lock = self.dead_lock(change=lambda b: b.update({key: 'other'}))
            original = lock.read_bytes()
            with self.assertRaisesRegex(ValueError, 'binding'):
                self.recover()
            self.assertEqual(lock.read_bytes(), original)
            lock.unlink()  # Only this test's explicit dead fixture.
        lock = self.dead_lock()
        data = json.loads(lock.read_bytes()); data['owner']['sha256'] = '0' * 64
        lock.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.recover()
        self.assertTrue(lock.exists())

    def test_pid_reuse_lock_replacement_and_ledger_change_refuse_after_preservation(self):
        import dispatch_lock
        for mode in ('pid_reuse', 'replacement', 'ledger_change'):
            lock = self.dead_lock()
            original = lock.read_bytes()
            if mode == 'pid_reuse':
                with patch.object(dispatch_lock, 'process_identity', return_value=dict(pid=json.loads(original)['pid'], creation='new', status='alive')):
                    with self.assertRaisesRegex(ValueError, 'active|alive|reused'):
                        self.recover()
            else:
                write = dispatch_lock.preserve
                def changed(*args, **kwargs):
                    result = write(*args, **kwargs)
                    if mode == 'replacement':
                        lock.unlink(); lock.write_bytes(b'{"replacement":true}')
                    else:
                        self.f.runtime.path.write_bytes(self.f.runtime.path.read_bytes() + b'\n')
                    return result
                with patch.object(dispatch_lock, 'preserve', side_effect=changed):
                    with self.assertRaisesRegex(ValueError, 'changed|replaced'):
                        self.recover()
            self.assertTrue(lock.exists())
            lock.unlink()

    def test_normal_owner_does_not_unlink_a_replacement(self):
        lock = self.f.runtime.path.with_suffix('.json.lock')
        close = os.close
        def replaced(fd):
            close(fd)
            lock.unlink(); lock.write_bytes(b'replacement')
        with patch('coordination.os.close', side_effect=replaced):
            with writer_lock(self.f.runtime.path):
                pass
        self.assertEqual(lock.read_bytes(), b'replacement')

    def fault_process(self, mode):
        scripts = str(test_dispatch_host.test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts')
        if os.name == 'nt':
            scripts = '\\\\?\\' + scripts
        config = self.host.config
        bootstrap = dict(protocol='standard-dispatch-host/1', profile=config['profile'], request=config['request'],
            host=dict(actor_id=self.f.actor, user_origin=self.f.origin, available_tools=config['availableTools'], scope='fixture_only'))
        code = """
import os,sys
sys.path.insert(0,sys.argv[1])
import standard_dispatch
from coordination import writer_lock
from core_runtime import Runtime
from codex_parallel_coordination.dispatch import StandardDispatch
from codex_parallel_coordination.worktrees import GitWorktrees
from codex_parallel_coordination.bridge import ResourceBridge
mode=sys.argv[2]
original_create=GitWorktrees.create
original_register=ResourceBridge.observe_worktree_creation
original_save=Runtime._save
original_record=StandardDispatch._record
def create(self,*args,**kwargs):
    if mode=='tree_before': os._exit(71)
    return original_create(self,*args,**kwargs)
def register(self,*args,**kwargs):
    if mode=='tree_after':
        with writer_lock(self.runtime.path): os._exit(71)
    return original_register(self,*args,**kwargs)
def save(self,state,operation):
    result=original_save(self,state,operation)
    if mode=='native_before' and operation['op']=='launch-callback-intent': os._exit(71)
    return result
def record(self,*args,**kwargs):
    result=original_record(self,*args,**kwargs)
    if mode=='driver_gap' and kwargs.get('launch_started'): os._exit(71)
    return result
GitWorktrees.create=create
ResourceBridge.observe_worktree_creation=register
Runtime._save=save
StandardDispatch._record=record
standard_dispatch.main()
"""
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, scripts, mode],
            input=json.dumps(bootstrap) + '\n', capture_output=True, encoding='utf-8', cwd=self.f.root, timeout=15)
        self.assertEqual(child.returncode, 71, child.stderr + child.stdout)
        self.assertNotIn('host_call', child.stdout)

    def test_formal_entry_recovers_two_dead_locks_after_tree_without_recreating_it(self):
        self.fault_process('tree_after')
        self.assertTrue(Path(self.f.request['worktree']).is_dir())
        locks = [self.f.runtime.path.with_suffix('.json.lock'), self.f.runtime.directory/'standard-dispatch-driver.lock']
        self.assertTrue(all(p.exists() for p in locks))
        first = self.host.run_host()
        self.assertEqual(first['packet']['result']['status'], 'dispatched', first)
        self.assertEqual(len(first['packet']['result']['lock_recovery']), 2)
        self.assertTrue(all(not p.exists() for p in locks))
        self.assertEqual(first['calls'].count('multi_agent_v1__spawn_agent'), 1)
        self.assertEqual(self.host.run_host()['calls'], [])

    def test_tree_intent_without_result_observes_absence_and_never_recreates(self):
        self.fault_process('tree_before')
        self.assertFalse(Path(self.f.request['worktree']).exists())
        for _ in range(2):
            result = self.host.run_host()
            self.assertEqual(result['calls'], [])
            self.assertEqual(result['packet']['result']['status'], 'needs_reconcile', result)
        self.assertFalse(Path(self.f.request['worktree']).exists())

    def test_death_before_native_preserves_original_intent_without_respawn(self):
        self.fault_process('native_before')
        for _ in range(2):
            result = self.host.run_host()
            self.assertEqual(result['calls'], [])
            self.assertEqual(result['packet']['result']['status'], 'intent', result)
        self.assertIsNone(self.f.runtime._load()['tasks']['A']['owner_agent_id'])

    def test_pre_native_gap_without_core_or_host_intent_continues_same_attempt_once(self):
        self.fault_process('driver_gap')
        self.assertNotIn('dispatch-A:launch', self.f.runtime._load()['runtime']['operations'])
        result = self.host.run_host()
        self.assertEqual(result['packet']['result']['status'], 'dispatched', result)
        self.assertEqual(result['calls'].count('multi_agent_v1__spawn_agent'), 1)
        self.assertEqual(self.host.run_host()['calls'], [])

    def test_git_preflight_and_creation_wait_without_shared_writer_locks(self):
        runner = self.f.git.runner
        observed = []
        def checked(argv, **kw):
            if 'ls-remote' in argv or 'add' in argv:
                with writer_lock(self.f.runtime.directory/'standard-dispatch-driver'), writer_lock(self.f.runtime.path):
                    observed.append('add' if 'add' in argv else 'ls-remote')
            return runner(argv, **kw)
        self.f.git.runner = checked
        self.assertEqual(self.f.run_dispatch()['status'], 'dispatched')
        self.assertEqual(observed, ['ls-remote', 'add'])

    def test_legacy_unscoped_lock_refuses_without_deletion(self):
        lock = self.f.runtime.path.with_suffix('.json.lock')
        original = json.dumps(dict(pid=34908, state=str(self.f.runtime.path))).encode()
        lock.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'binding'):
            self.recover()
        self.assertEqual(lock.read_bytes(), original)

    def test_core_busy_after_native_result_retains_id_then_same_request_verifies(self):
        config = self.f.json('busy-config.json', self.host.config)
        module = (test_dispatch_host.test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs').as_uri()
        code = """
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
const {DispatchSession} = await import(MODULE);
const c=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const s=new DispatchSession(c);
const pending=await s.next();
const scripts=path.toNamespacedPath(path.dirname(fileURLToPath(MODULE)));
const py="import sys;sys.path.insert(0,sys.argv[1]);from core_runtime import Runtime;from coordination import writer_lock;r=Runtime(sys.argv[2],sys.argv[3]);c=writer_lock(r.path);c.__enter__();print('ready',flush=True);sys.stdin.readline();c.__exit__(None,None,None)";
const holder=spawn(c.python,['-X','utf8','-B','-c',py,scripts,c.profile.state_root,c.profile.project_root],
  {cwd:c.profile.project_root,stdio:['pipe','pipe','pipe'],windowsHide:true});
await once(holder.stdout,'data');
const stopped=await s.reply(pending.tool,{agent_id:'synthetic-busy-id'});
const proof=s.retainedResult;
if (!fs.existsSync(proof.path)) throw new Error('raw result lost');
const lock=fs.readFileSync(LEDGER+'.lock','utf8');
const exited=once(holder,'close');holder.stdin.end();await exited;
const verified=await s.verifyHostResult(c.requestMeta);
const repeated=await s.verifyHostResult(c.requestMeta);
console.log(JSON.stringify({stopped,proof,lock,verified,repeated,calls:s.calls}));
""".replace('MODULE', json.dumps(module)).replace('LEDGER', json.dumps(str(self.f.runtime.path)))
        child = subprocess.run(['node', '--input-type=module', '-e', code, config['path']],
            cwd=self.f.root, capture_output=True, encoding='utf-8', timeout=25)
        self.assertEqual(child.returncode, 0, child.stderr)
        value = json.loads(child.stdout)
        self.f.json('busy-probe.json', value)
        self.assertEqual(value['stopped']['result']['status'], 'needs_reconcile', value)
        self.assertEqual(value['calls'], 1)
        self.assertEqual(value['proof']['actual_agent_id'], 'synthetic-busy-id')
        for key in ('verified', 'repeated'):
            self.assertEqual(value[key]['result']['owner_agent_id'], 'native_agent:synthetic-busy-id', value)
            self.assertEqual(value[key]['result']['status'], 'needs_reconcile')
        state = self.f.runtime._load()
        launch = state['runtime']['operations']['dispatch-A:launch']
        self.assertEqual(launch['attempt_id'], 'dispatch-A:launch')
        self.assertEqual(launch['request_id'], 'dispatch-A:capacity')
        self.assertEqual(launch['permit_id'], self.f.controller.read()[0]['capacity_requests']['dispatch-A:capacity']['permit_id'])
        self.assertEqual(state['tasks']['A']['worker_run']['status'], 'queued')
        self.assertEqual(self.f.run_dispatch()['status'], 'needs_reconcile')
        self.assertEqual(self.f.calls, [])

    def test_controlled_deadline_kills_only_owned_stuck_child_and_recovers_two_locks(self):
        bridge = self.f.root / 'owned-bridge'
        bridge.mkdir()
        original = test_dispatch_host.test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts'
        from evidence import io_path
        shutil.copyfile(io_path(original/'dispatch_host.mjs'), bridge/'dispatch_host.mjs')
        # Exact production transport bytes; only this isolated endpoint injects
        # a stuck write boundary. No global/module installation is changed.
        self.assertEqual((bridge/'dispatch_host.mjs').read_bytes(), io_path(original/'dispatch_host.mjs').read_bytes())
        scripts = '\\\\?\\' + str(original) if os.name == 'nt' else str(original)
        wrapper = """
import sys,json,threading
sys.path.insert(0,SCRIPTS)
import standard_dispatch
from coordination import writer_lock
from codex_parallel_coordination.dispatch import StandardDispatch
from codex_parallel_coordination.bridge import ResourceBridge
original=ResourceBridge.observe_worktree_creation
def stuck(self,*args,**kwargs):
    with writer_lock(self.runtime.path):
        print(json.dumps(dict(kind='fixture_ready')),flush=True)
        threading.Event().wait()
    return original(self,*args,**kwargs)
ResourceBridge.observe_worktree_creation=stuck
standard_dispatch.main()
""".replace('SCRIPTS', repr(scripts))
        (bridge/'standard_dispatch.py').write_text(wrapper, encoding='utf-8')
        config = self.f.json('deadline-config.json', self.host.config)
        code = """
import fs from 'node:fs';
const {DispatchSession,HOST_DEADLINE_MS}=await import(MODULE);
let expire;
const real=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>{const t=real(fn,ms,...args);if(ms===45000)expire=()=>{clearTimeout(t);fn()};return t};
const c=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const s=new DispatchSession(c);
const ready=await s.next();
if(ready.kind!=='fixture_ready'||HOST_DEADLINE_MS!==45000)throw new Error('wrong controlled boundary');
expire();
const packet=await s.next();
await s.exited;
console.log(JSON.stringify({packet,calls:s.calls,stderr:s.stderr}));
""".replace('MODULE', json.dumps((bridge/'dispatch_host.mjs').as_uri()))
        child = subprocess.run(['node', '--input-type=module', '-e', code, config['path']],
            cwd=self.f.root, capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        value = json.loads(child.stdout)
        self.f.json('controlled-deadline-probe.json', value)
        self.assertEqual(value['calls'], 0)
        self.assertEqual(value['packet']['result']['status'], 'needs_reconcile')
        self.assertEqual(len(value['packet']['result']['recovery']['lock_recovery']), 2, value)
        self.assertFalse(self.f.runtime.path.with_suffix('.json.lock').exists())
        self.assertFalse((self.f.runtime.directory/'standard-dispatch-driver.lock').exists())
        self.assertTrue(Path(self.f.request['worktree']).exists())
        self.assertEqual(self.f.controller.read()[0]['capacity_requests']['dispatch-A:capacity']['status'], 'granted')
        resumed = self.host.run_host()
        self.assertEqual(resumed['packet']['result']['status'], 'dispatched', resumed)
        self.assertEqual(resumed['calls'].count('multi_agent_v1__spawn_agent'), 1)

    def test_deadline_discards_unconsumed_host_call_without_exposing_native_action(self):
        config = self.f.json('queued-packet-config.json', self.host.config)
        module = (test_dispatch_host.test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs').as_uri()
        code = """
import fs from 'node:fs';
import {once} from 'node:events';
const {DispatchSession}=await import(MODULE);
let expire;
const real=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>{const t=real(fn,ms,...args);if(ms===45000)expire=()=>{clearTimeout(t);fn()};return t};
const c=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const s=new DispatchSession(c);
await once(s.lines,'line');
if(s.queue[0]?.kind!=='host_call')throw new Error('missing queued fixture action');
expire();await s.interruption;
const packet=await s.next();
console.log(JSON.stringify({packet,calls:s.calls}));
""".replace('MODULE', json.dumps(module))
        child = subprocess.run(['node', '--input-type=module', '-e', code, config['path']],
            cwd=self.f.root, capture_output=True, encoding='utf-8', timeout=12)
        self.assertEqual(child.returncode, 0, child.stderr)
        value = json.loads(child.stdout)
        self.f.json('expired-queued-packet.json', value)
        self.assertEqual(value['packet']['kind'], 'result', value)
        self.assertEqual(value['calls'], 0)


if __name__ == '__main__':
    unittest.main()
