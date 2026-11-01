"""Real finite Node/Python transport, synthetic native replies and approvals only."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch
from evidence import load_bound


@unittest.skipUnless(shutil.which('node'), 'Node required for finite host transport')
class HostTransport(unittest.TestCase):
    def setUp(self):
        self.fixture = test_dispatch.DispatchTests('test_fixed_bundle_derives_fields_from_frozen_materials')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.config = dict(python=sys.executable,
            profile=dict(project_root=str(f.runtime.project_root), state_root=str(f.root / 'authority'),
                         resource_state=str(f.controller.path), evidence_root=str(f.bridge.evidence), integration_channel='native_agent',
                         integration_target='integration-fixture'),
            request=f.json('host-request.json', f.request),
            requestMeta=dict(threadId=f.actor, sessionId='irrelevant-parent-session'),
            approvalOrigin=f.origin, availableTools=['multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent', 'send_input'])

    def run_host(self, spawn_reply=None):
        f = self.fixture
        config = f.json('host-config.json', self.config)
        module = (test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs').as_uri()
        program = """
import fs from 'node:fs';
const {DispatchSession} = await import(MODULE);
const config = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const session = new DispatchSession(config);
let packet = await session.next();
const calls = [];
const argumentsUsed = [];
while (packet.kind === 'host_call') {
  calls.push(packet.tool);
  argumentsUsed.push(packet.arguments);
  const replies = {multi_agent_v1__spawn_agent: SPAWN_REPLY,
    multi_agent_v1__wait_agent:{status:{'synthetic-stdio-agent':'running'},timed_out:true}, send_input:{submission_id:'fixture'}};
  packet = await session.reply(packet.tool, replies[packet.tool]);
}
console.log(JSON.stringify({packet, calls, argumentsUsed, stderr:session.stderr}));
""".replace('MODULE', json.dumps(module)).replace('SPAWN_REPLY', json.dumps(
            spawn_reply if spawn_reply is not None else dict(agent_id='synthetic-stdio-agent')))
        result = subprocess.run(['node', '--input-type=module', '-e', program, config['path']],
                                capture_output=True, encoding='utf-8', timeout=55)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_transport_preserves_actual_identity_even_with_capacity_error(self):
        result = self.run_host(dict(agent_id='synthetic-stdio-agent',
            error=dict(code='agent_capacity_exceeded', message='agent thread limit reached')))
        self.assertEqual(result['packet']['result']['status'], 'dispatched')
        self.assertEqual(result['calls'].count('multi_agent_v1__spawn_agent'), 1)
        self.assertEqual(load_bound(result['packet']['result']['transport_evidence'][0])['actual_agent_id'],
            'synthetic-stdio-agent')

    def test_transport_executes_bounded_fixture_actions_and_preserves_raw_receipts(self):
        result = self.run_host()
        self.assertEqual(result['packet']['result']['status'], 'dispatched')
        self.assertEqual(result['calls'], ['multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent', 'send_input'])
        self.assertEqual(result['argumentsUsed'][1], dict(targets=['synthetic-stdio-agent'], timeout_ms=10000))
        self.assertEqual(len(result['packet']['result']['host_evidence']), 6)
        from evidence import load_bound
        receipts = [load_bound(r) for r in result['packet']['result']['host_evidence']]
        self.assertEqual(receipts[0]['tool'], 'multi_agent_v1__spawn_agent')
        self.assertEqual(receipts[2]['tool'], 'multi_agent_v1__wait_agent')
        self.assertEqual(receipts[2]['arguments'], result['argumentsUsed'][1])
        self.assertEqual(receipts[3]['result'], dict(status={'synthetic-stdio-agent': 'running'}, timed_out=True))
        transport = [load_bound(r) for r in result['packet']['result']['transport_evidence']]
        self.assertEqual(len(transport), 3)
        self.assertEqual(transport[0]['response']['result'], dict(agent_id='synthetic-stdio-agent'))
        self.assertEqual(transport[0]['pending_packet']['context']['request_id'], self.fixture.request['request_id'])

    def test_real_thread_id_wins_over_parent_session_id(self):
        self.config['requestMeta'] = dict(threadId='developer', sessionId=self.fixture.actor)
        before = self.fixture.runtime.path.read_bytes()
        result = self.run_host()
        self.assertEqual(result['packet']['result']['status'], 'blocked')
        self.assertIn('role required', result['packet']['result']['reason'])
        self.assertEqual(result['calls'], [])
        self.assertEqual(self.fixture.runtime.path.read_bytes(), before)

    def test_available_app_tools_do_not_substitute_for_native_spawn(self):
        self.config['availableTools'] = ['mcp__codex_app__send_message_to_thread', 'mcp__codex_app__read_thread']
        result = self.run_host()
        self.assertEqual(result['packet']['result']['status'], 'blocked')
        self.assertIn('capability unavailable', result['packet']['result']['reason'])
        self.assertEqual(result['calls'], [])
        self.assertFalse(Path(self.fixture.request['worktree']).exists())

    def test_canonical_aliases_are_not_actual_native_tools(self):
        self.config['availableTools'] = ['spawn_agent', 'wait_agent', 'send_input']
        result = self.run_host()
        self.assertEqual(result['packet']['result']['status'], 'blocked')
        self.assertIn('capability unavailable', result['packet']['result']['reason'])
        self.assertEqual(result['calls'], [])


if __name__ == '__main__':
    unittest.main()
