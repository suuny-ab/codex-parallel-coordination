"""Real finite subprocess/deadline; all returned native identities are fixtures."""
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_dispatch_host


class LateHostResults(unittest.TestCase):
    def setUp(self):
        self.host = test_dispatch_host.HostTransport('test_transport_executes_bounded_fixture_actions_and_preserves_raw_receipts')
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)

    def probe(self, mode):
        fixture = self.host.fixture
        config = fixture.json('late-config.json', self.host.config)
        module = (test_dispatch_host.test_dispatch.ROOT /
                  'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs').as_uri()
        code = """
import fs from 'node:fs/promises';
import {once} from 'node:events';
const {DispatchSession} = await import(MODULE);
const config = JSON.parse(await fs.readFile(process.argv[1], 'utf8'));
const mode = process.argv[2];
const session = new DispatchSession(config);
const pending = await session.next();
const exited = once(session.child, 'exit');
let awaiting = pending;
if (mode === 'observe' || mode === 'handoff') {
  awaiting = await session.reply(awaiting.tool, {agent_id:'synthetic-late-agent'});
  if (mode === 'handoff') awaiting = await session.reply(awaiting.tool, {status:{'synthetic-late-agent':'running'}});
}
if (mode === 'deadline') {
  const expired = await session.next();
  if (!expired.result.reason.includes('45 second')) throw new Error('real deadline did not fire');
} else {
  if (mode === 'killed_child') session.child.kill();
  else session.child.stdin.end();
  await session.next();
}
await exited;
const originalRequest = mode === 'request_changed' ? await fs.readFile(config.request.path) : null;
if (originalRequest) await fs.writeFile(config.request.path, Buffer.concat([originalRequest, Buffer.from([10])]));
const raw = mode === 'observe' ? {status:{'synthetic-late-agent':'running'}} :
  mode === 'handoff' ? {submission_id:'fixture-late-handoff'} : {agent_id:'synthetic-late-agent',nickname:'fixture'};
const retained = await session.reply(awaiting.tool, raw);
if (originalRequest) await fs.writeFile(config.request.path, originalRequest);
const stored = JSON.parse(await fs.readFile(retained.result.host_result.path, 'utf8'));
const duplicate = await session.reply(awaiting.tool, raw);
let conflict = null;
try { await session.reply(awaiting.tool, {agent_id:'different-fixture'}); }
catch (error) { conflict = error.message; }
const verified = await session.verifyHostResult(config.requestMeta);
console.log(JSON.stringify({retained,stored,duplicate,conflict,verified,
  spawnedId:session.spawnedId,closed:session.closed,calls:session.calls}));
""".replace('MODULE', json.dumps(module))
        result = subprocess.run(['node', '--input-type=module', '-e', code, config['path'], mode],
                                capture_output=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        evidence_root = os.environ.get('COORDINATION_DISPATCH_LATE_EVIDENCE')
        if evidence_root:
            from evidence import io_path
            destination = io_path(Path(evidence_root) / mode)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / 'probe.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
            for original in [fixture.runtime.path, fixture.controller.path, *fixture.bridge.evidence.glob('*.json')]:
                preserved = io_path(destination / original.relative_to(fixture.root))
                preserved.parent.mkdir(parents=True, exist_ok=True)
                preserved.write_bytes(io_path(original).read_bytes())
        self.assertEqual(data['retained']['result']['status'], 'needs_reconcile')
        if mode not in ('observe', 'handoff'):
            self.assertEqual(data['retained']['result']['actual_agent_id'], 'synthetic-late-agent')
            self.assertEqual(data['stored']['response']['result'], dict(agent_id='synthetic-late-agent', nickname='fixture'))
            self.assertEqual(data['stored']['pending_packet']['arguments'], {'message': data['stored']['pending_packet']['arguments']['message']})
        self.assertEqual(data['stored']['request_id'], fixture.request['request_id'])
        self.assertEqual(data['duplicate']['result']['host_result'], data['retained']['result']['host_result'])
        self.assertIn('conflicting', data['conflict'])
        self.assertEqual(data['spawnedId'], 'synthetic-late-agent')
        self.assertEqual(data['calls'], 3 if mode == 'handoff' else 2 if mode == 'observe' else 1)
        self.assertEqual(data['verified']['result']['status'], 'needs_reconcile')
        state = fixture.runtime._load()
        self.assertEqual(state['tasks']['A']['worker_run']['status'], 'running' if mode == 'handoff' else 'queued')
        if mode == 'killed_child':
            self.assertIsNone(data['verified']['result']['owner_agent_id'])
            self.assertEqual(data['verified']['result']['reported_actual_agent_id'], 'synthetic-late-agent')
            self.assertIn('writer lock exists', data['verified']['result']['reason'])
            self.assertFalse(data['verified']['result']['identity_binding_verified'])
            with self.assertRaisesRegex(ValueError, 'writer lock exists'):
                fixture.dispatch.run(fixture.request)
            return
        self.assertEqual(data['verified']['result']['owner_agent_id'], 'native_agent:synthetic-late-agent', data['verified'])
        if mode != 'handoff':
            self.assertNotEqual(state['runtime']['executors']['native_agent:synthetic-late-agent']['execution'], 'running')
        self.assertIn('A:agent', state['runtime']['resources'])
        self.assertEqual(state['runtime']['operations']['dispatch-A:launch']['status'],
                         'running' if mode == 'handoff' else 'accepted_unconfirmed' if mode == 'observe' else 'unknown')
        before = len(fixture.calls)
        self.assertEqual(fixture.dispatch.run(fixture.request)['status'], 'needs_reconcile')
        self.assertEqual(len(fixture.calls), before)

    def test_child_exit_late_result_and_duplicate_conflict(self):
        self.probe('child_exit')

    def test_actual_45_second_deadline_retains_late_result(self):
        self.probe('deadline')

    def test_abrupt_child_exit_preserves_result_without_stealing_lock(self):
        self.probe('killed_child')

    def test_late_observation_does_not_infer_running(self):
        self.probe('observe')

    def test_late_handoff_does_not_infer_dispatched(self):
        self.probe('handoff')

    def test_late_result_is_saved_before_rechecking_changed_request_file(self):
        self.probe('request_changed')


if __name__ == '__main__':
    unittest.main()
