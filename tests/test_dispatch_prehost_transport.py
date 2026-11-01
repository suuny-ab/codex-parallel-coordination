"""Unmodified 45s host module; fault endpoint and native replies are fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_dispatch_host
from evidence import io_path


@unittest.skipUnless(os.name == 'nt' and shutil.which('node'), 'Windows process identity and Node required')
class PrehostTransport(unittest.TestCase):
    def setUp(self):
        self.host = test_dispatch_host.HostTransport('test_real_thread_id_wins_over_parent_session_id')
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)

    def probe(self, legacy=False, negative=None):
        f = self.host.fixture
        folder = f.root/'owned-host'
        folder.mkdir()
        source = test_dispatch_host.test_dispatch.ROOT/'skills/isolated-parallel-development-coordination/scripts'
        raw = io_path(source/'dispatch_host.mjs').read_bytes()
        (folder/'dispatch_host.mjs').write_bytes(raw)
        if legacy:
            old = subprocess.run(['git', '-c', 'core.longpaths=true', 'show',
                '832ee238c806070a87531ab72a053df0016ce1ee:skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs'],
                cwd=test_dispatch_host.test_dispatch.ROOT, check=True, capture_output=True).stdout
            (folder/'old-host.mjs').write_bytes(old)
        scripts = str(source)
        if os.name == 'nt': scripts = '\\\\?\\'+scripts
        endpoint = """
import os,sys
from pathlib import Path
sys.path[:0]=[SCRIPTS,SRC]
import standard_dispatch
from core_runtime import Runtime
original=Runtime._save
def save(self,state,operation):
    original(self,state,operation)
    marker=Path(MARKER)
    if operation['op']=='launch-intent' and not marker.exists():
        marker.write_text('one isolated crash before any callback')
        os._exit(71)
Runtime._save=save
if HAS_CAPTURE:
    from coordination import writer_lock
    from evidence import write_atomic
    original_run=standard_dispatch.run_dispatch
    def run(bootstrap,profile,host,request,runtime,recovered):
        marker=Path(MARKER)
        if not marker.exists():
            with writer_lock(runtime.path):
                state=runtime._load()
                state['runtime']['operations'][request['request_id']]['driver_owner_pid']=os.getpid()
                write_atomic(runtime.path,state)
                marker.write_text('isolated relocation of captured terminal host identity')
                os._exit(71)
        return original_run(bootstrap,profile,host,request,runtime,recovered)
    standard_dispatch.run_dispatch=run
standard_dispatch.main()
""".replace('SCRIPTS', repr(scripts)).replace('SRC', repr(str(source.parents[2]/'src'))).replace(
            'MARKER', repr(str(folder/'crashed.txt'))).replace('HAS_CAPTURE', repr(getattr(self, 'has_capture', False)))
        (folder/'standard_dispatch.py').write_text(endpoint, encoding='utf-8')
        config = f.json('prehost-config.json', self.host.config)
        capacity_count = len(f.controller.read()[0]['capacity_requests'])
        code = """
import fs from 'node:fs';
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const hostModule = await import(MODULE);
const {DispatchSession,HOST_DEADLINE_MS} = hostModule;
const OriginalSession = LEGACY ? (await import(OLD_MODULE)).DispatchSession : DispatchSession;
const config=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const original=new OriginalSession(config);
const initial=await original.next();
if (NEGATIVE) {
  const fake={...original};
  if (NEGATIVE==='plain-child') fake.child={pid:original.child.pid,exitCode:original.child.exitCode,signalCode:original.child.signalCode};
  const before=fs.readFileSync(LEDGER,'utf8');
  let safeRefused=false,refused=false,spawnedChildren=0,hostActions=0;
  let continuation=null;
  const spawn=childProcess.spawn;
  childProcess.spawn=(...args)=>{spawnedChildren++;return spawn(...args);};
  syncBuiltinESMExports();
  try {
    try { await DispatchSession.prototype.resumePrehost.call(fake,config.requestMeta); } catch { safeRefused=true; }
    try {
      continuation=await DispatchSession.prototype.continuePrehost.call(fake,config.requestMeta);
      const packet=await continuation.next();
      if(packet.kind==='host_call') hostActions++;
    } catch { refused=true; }
    if(continuation) await continuation.interrupt('Stop owned negative fixture; never invoke native');
  } finally {
    childProcess.spawn=spawn;
    syncBuiltinESMExports();
  }
  console.log(JSON.stringify({initial,negative:NEGATIVE,safeRefused,refused,spawnedChildren,hostActions,
    ledgerUnchanged:fs.readFileSync(LEDGER,'utf8')===before,exportedHelper:typeof hostModule.continuePrehost==='function'}));
} else {
let plainRefused=false;
try { await DispatchSession.prototype.resumePrehost.call({...original},config.requestMeta); } catch { plainRefused=true; }
const started=performance.now();
const resume=()=>DispatchSession.prototype.resumePrehost.call(original,config.requestMeta);
const [resumed,duplicate]=await Promise.all([resume(),resume()]);
let packet=await resumed.next();
const calls=[];
while(packet.kind==='host_call') {
  calls.push(packet.tool);
  const results={multi_agent_v1__spawn_agent:{agent_id:'recovered-fixture'},
    multi_agent_v1__wait_agent:{status:{'recovered-fixture':'running'}},send_input:{submission_id:'fixture'},
    mcp__codex_app__send_message_to_thread:{threadId:config.profile.integration_target}};
  packet=await resumed.reply(packet.tool,results[packet.tool]);
}
await resumed.exited;
let refused=false;
try { await resumed.resumePrehost(config.requestMeta); } catch { refused=true; }
console.log(JSON.stringify({initial,packet,calls,originalCalls:original.calls,same:resumed===duplicate,
  elapsedMs:performance.now()-started,deadlineMs:HOST_DEADLINE_MS,refused,plainRefused,stderr:resumed.stderr}));
}
""".replace('OLD_MODULE', json.dumps((folder/'old-host.mjs').as_uri())).replace(
            'MODULE', json.dumps((folder/'dispatch_host.mjs').as_uri())).replace('LEGACY', json.dumps(legacy)).replace(
            'NEGATIVE', json.dumps(negative)).replace('LEDGER', json.dumps(str(f.runtime.path)))
        result = subprocess.run(['node', '--input-type=module', '-e', code, config['path']],
            cwd=f.root, capture_output=True, encoding='utf-8', timeout=55)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        output = os.environ.get('COORDINATION_TIMEOUT_EVIDENCE')
        case = 'helper-'+negative if negative else 'captured' if getattr(self, 'has_capture', False) else 'legacy' if legacy else 'current'
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output)/('probe-'+case+'.json')).write_text(
                json.dumps(data, indent=2), encoding='utf-8')
            (Path(output)/('probe-'+case+'.mjs')).write_text(code, encoding='utf-8')
        if negative:
            self.assertTrue(data['safeRefused'], data)
            self.assertTrue(data['refused'], data)
            self.assertEqual(data['spawnedChildren'], 0, data)
            self.assertEqual(data['hostActions'], 0, data)
            self.assertTrue(data['ledgerUnchanged'], data)
            self.assertFalse(data['exportedHelper'], data)
            return
        self.assertEqual(data['originalCalls'], 0)
        self.assertEqual(data['deadlineMs'], 45000)
        self.assertLess(data['elapsedMs'], 45000)
        self.assertTrue(data['same'])
        self.assertTrue(data['plainRefused'])
        self.assertEqual(data['packet']['result']['status'], 'dispatched', data)
        self.assertTrue(data['refused'])
        self.assertEqual(data['calls'], ['multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent', self.host.config['availableTools'][-1]])
        state = f.runtime._load()
        launch = state['runtime']['operations'][f.request['request_id']+':launch']
        self.assertEqual(len(launch['prehost_recoveries']), 1)
        self.assertEqual(launch['prehost_recoveries'][0]['original']['status'], 'intent')
        self.assertEqual(state['tasks'][f.request['task_id']]['owner_agent_id'], 'native_agent:recovered-fixture')
        self.assertEqual(len(f.controller.read()[0]['capacity_requests']), capacity_count or 1)
        self.assertEqual((folder/'dispatch_host.mjs').read_bytes(), raw)
        output = os.environ.get('COORDINATION_TIMEOUT_EVIDENCE')
        if output:
            target = Path(output)/('prehost-'+case)
            target.mkdir(parents=True)
            (target/'RESULT.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
            for file in (f.runtime.path, f.controller.path, *f.bridge.evidence.glob('*.json')):
                saved = target/file.relative_to(f.root)
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(file.read_bytes())

    def test_actual_terminal_child_recovers_original_intent_once_within_original_deadline(self):
        self.probe()

    def test_original_base_host_instance_can_resume_through_reviewed_new_prototype(self):
        self.probe(legacy=True)

    def test_direct_helper_refuses_plain_session_without_child_or_host_action(self):
        self.probe(legacy=True, negative='plain-session')

    def test_direct_helper_refuses_plain_child_without_child_or_host_action(self):
        self.probe(legacy=True, negative='plain-child')


if __name__ == '__main__':
    unittest.main()
