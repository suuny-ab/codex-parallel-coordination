"""Regression for observed dispatch faults; native replies remain fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_dispatch_host
test_dispatch = test_dispatch_host.test_dispatch


class HandoffFaults(unittest.TestCase):
    def setUp(self):
        self.host = test_dispatch_host.HostTransport('test_transport_executes_bounded_fixture_actions_and_preserves_raw_receipts')
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)

    def probe(self, body):
        f = self.host.fixture
        config = f.json('fault-config.json', self.host.config)
        module = (test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/scripts/dispatch_host.mjs').as_uri()
        code = """
import fs from 'node:fs/promises';
const host = await import(MODULE);
const config = JSON.parse(await fs.readFile(process.argv[1], 'utf8'));
const session = new host.DispatchSession(config);
const pending = await session.next();
BODY
""".replace('MODULE', json.dumps(module)).replace('BODY', body)
        result = subprocess.run(['node', '--input-type=module', '-e', code, config['path']],
                                capture_output=True, encoding='utf-8', timeout=55, cwd=test_dispatch.ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_outer_budget_covers_deadline_and_interruption_releases_owned_locks(self):
        data = self.probe("""
const budget = session.callTimeoutMs;
const packet = await session.interrupt('fixture caller cancellation');
await session.exited;
console.log(JSON.stringify({budget, deadline:host.HOST_DEADLINE_MS, grace:host.SHUTDOWN_GRACE_MS, packet}));
""")
        self.assertGreater(data['budget'], data['deadline'] + data['grace'])
        self.assertEqual(data['packet']['result']['status'], 'needs_reconcile')
        self.assertFalse(self.host.fixture.runtime.path.with_suffix('.json.lock').exists())
        self.assertFalse((self.host.fixture.runtime.directory / 'standard-dispatch-driver.lock').exists())
        self.assertEqual(self.host.fixture.dispatch.run(self.host.fixture.request)['status'], 'unknown')

    def test_original_capacity_exception_survives_and_is_not_effect_unknown(self):
        data = self.probe("""
const error = host.serializeHostError('collab spawn failed: agent thread limit reached');
const packet = await session.reply(pending.tool, null, error);
await session.exited;
console.log(JSON.stringify({error, packet}));
""")
        self.assertEqual(data['error']['message'], 'collab spawn failed: agent thread limit reached')
        self.assertEqual(data['packet']['result']['status'], 'capacity')
        self.assertIn('agent thread limit reached', data['packet']['result']['record']['reason'])
        state = self.host.fixture.runtime._load()
        launch = state['runtime']['operations']['dispatch-A:launch']
        self.assertEqual(launch['error']['message'], data['error']['message'])
        self.assertIsNone(state['tasks']['A']['owner_agent_id'])
        self.assertEqual(data['packet']['result']['record']['admission']['status'], 'granted')
        self.assertEqual(self.host.fixture.dispatch.run(self.host.fixture.request)['status'], 'capacity')

    def test_other_exception_keeps_original_detail_and_unknown_without_retry(self):
        data = self.probe("""
const error = host.serializeHostError(Object.assign(new Error('fixture response lost'), {code:'ETIMEDOUT'}));
const packet = await session.reply(pending.tool, null, error);
await session.exited;
console.log(JSON.stringify({error, packet}));
""")
        self.assertEqual(data['packet']['result']['status'], 'unknown')
        launch = self.host.fixture.runtime._load()['runtime']['operations']['dispatch-A:launch']
        self.assertEqual(launch['error']['message'], 'fixture response lost')
        self.assertEqual(launch['error']['code'], 'ETIMEDOUT')
        self.assertEqual(self.host.fixture.dispatch.run(self.host.fixture.request)['status'], 'unknown')

    def test_structured_native_message_without_code_is_capacity(self):
        f = self.host.fixture
        f.replies['spawn_agent'] = {'error': {'message': 'collab spawn failed: agent thread limit reached', 'code': None}}
        self.assertEqual(f.run_dispatch()['status'], 'capacity')
        self.assertEqual(f.run_dispatch()['status'], 'capacity')
        self.assertEqual(len(f.calls), 1)

    def test_documented_host_calls_use_budget_and_preserve_error(self):
        text = (test_dispatch.ROOT / 'skills/isolated-parallel-development-coordination/references/standard-dispatch.md').read_text(encoding='utf-8')
        self.assertNotIn('catch { error = true; }', text)
        self.assertIn('timeout_ms: 50000', text)

    def test_late_explicit_capacity_refusal_reconciles_without_retry_or_release(self):
        data = self.probe("""
session.child.stdin.end();
await session.next();
await session.exited;
const late = await session.reply(pending.tool, null,
  host.serializeHostError('collab spawn failed: agent thread limit reached'));
const verified = await session.verifyHostResult(config.requestMeta);
console.log(JSON.stringify({late, verified, calls:session.calls}));
""")
        self.assertEqual(data['late']['result']['status'], 'needs_reconcile')
        self.assertEqual(data['verified']['result']['status'], 'capacity')
        self.assertEqual(data['calls'], 1)
        self.assertIsNone(data['verified']['result']['owner_agent_id'])
        self.assertEqual(data['verified']['result']['record']['admission']['status'], 'granted')

    def test_late_mcp_capacity_refusal_preserves_exact_error_result(self):
        data = self.probe("""
session.child.stdin.end();
await session.next();
await session.exited;
const raw = {isError:true, content:[{type:'text',text:'collab spawn failed: agent thread limit reached'}]};
const late = await session.reply(pending.tool, raw);
const stored = JSON.parse(await fs.readFile(late.result.host_result.path,'utf8'));
const verified = await session.verifyHostResult(config.requestMeta);
console.log(JSON.stringify({stored, verified}));
""")
        self.assertEqual(data['verified']['result']['status'], 'capacity')
        self.assertTrue(data['stored']['response']['result']['isError'])


if __name__ == '__main__':
    unittest.main()
