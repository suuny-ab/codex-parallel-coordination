// Bounded stdio bridge for the current node_repl + functions host. No server.
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const native = new Set(['multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent']);
const handoff = new Set(['send_input', 'mcp__codex_app__send_message_to_thread']);
const allowed = new Set([...native, ...handoff]);

export class DispatchSession {
  // profile, approvalOrigin and requestMeta are supplied by the trusted host,
  // separately from the task request. Never spread task JSON into this object.
  constructor({python, profile, request, requestMeta, approvalOrigin, availableTools, testingScope = 'fixture_only'}) {
    if (!requestMeta?.threadId || !path.isAbsolute(python) || !profile?.integration_target || !request?.sha256) {
      throw new Error('authenticated thread metadata, fixed profile and bound request required');
    }
    if (!['state_root', 'project_root', 'resource_state', 'evidence_root'].every(key =>
      typeof profile[key] === 'string' && path.isAbsolute(profile[key])) ||
      !['native_agent', 'app_thread'].includes(profile.integration_channel)) {
      throw new Error('fixed absolute host profile and explicit integration channel required');
    }
    if (!['fixture_only', 'host_verified'].includes(testingScope)) throw new Error('explicit testing boundary required');
    this.tools = new Set(availableTools.filter(name => allowed.has(name)));
    this.integrationTarget = profile.integration_target;
    this.integrationTool = profile.integration_channel === 'app_thread' ?
      'mcp__codex_app__send_message_to_thread' : 'send_input';
    this.spawnedId = null;
    this.pending = null;
    this.queue = [];
    this.waiter = null;
    this.calls = 0;
    this.closed = false;
    const endpoint = fileURLToPath(new URL('./standard_dispatch.py', import.meta.url));
    this.child = spawn(python, ['-X', 'utf8', '-B', endpoint], {
      cwd: profile.project_root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    });
    this.stderr = '';
    this.child.stderr.on('data', data => { this.stderr = (this.stderr + data).slice(-2000); });
    this.child.stdin.on('error', () => {});
    this.lines = createInterface({input: this.child.stdout});
    this.lines.on('line', line => {
      try { this.deliver(JSON.parse(line)); }
      catch { this.deliver({kind: 'result', result: {status: 'needs_reconcile', reason: 'invalid host protocol'}}); }
    });
    this.child.on('error', () => this.deliver({kind: 'result', result: {status: 'blocked', reason: 'Python could not start'}}));
    this.child.on('exit', code => {
      clearTimeout(this.timer);
      if (!this.closed) this.deliver({kind: 'result', result: {
        status: 'needs_reconcile', reason: 'finite host process exited before result', exit_code: code,
      }});
    });
    // This is a local deadline, not polling or a wakeup. A killed operation stays
    // intent/unknown in CORE; a subsequent session cannot send it again.
    this.timer = setTimeout(() => {
      this.deliver({kind: 'result', result: {status: 'needs_reconcile', reason: '45 second host deadline; observe original intent'}});
      this.child.kill();
    }, 45000);
    this.child.stdin.write(JSON.stringify({protocol: 'standard-dispatch-host/1', profile, request,
      host: {actor_id: requestMeta.threadId, user_origin: approvalOrigin, available_tools: [...this.tools],
        scope: testingScope}}) + '\n');
  }

  deliver(packet) {
    if (this.closed) return;
    if (packet.kind === 'result') {
      this.closed = true;
      clearTimeout(this.timer);
      this.child.stdin.end();
    }
    if (this.waiter) { const resolve = this.waiter; this.waiter = null; resolve(packet); }
    else this.queue.push(packet);
  }

  async next() {
    if (this.closed && !this.queue.length) throw new Error('finite dispatch session already ended');
    const packet = this.queue.length ? this.queue.shift() :
      await new Promise(resolve => { this.waiter = resolve; });
    if (packet.kind === 'host_call') {
      if (this.pending || !this.tools.has(packet.tool) || ++this.calls > 3 || !this.validAction(packet)) {
        this.child.kill();
        throw new Error('unexpected or unbounded host action; reconcile original intent');
      }
      this.pending = packet;
    }
    return packet;
  }

  validAction({tool, arguments: args}) {
    if (!args || typeof args !== 'object' || Array.isArray(args)) return false;
    const keys = Object.keys(args).sort().join(',');
    const message = value => typeof value === 'string' && value.trim().length > 0;
    if (this.calls === 1) return tool === 'multi_agent_v1__spawn_agent' && keys === 'message' && message(args.message);
    if (this.calls === 2) return tool === 'multi_agent_v1__wait_agent' && keys === 'targets,timeout_ms' &&
      Array.isArray(args.targets) && args.targets.length === 1 && args.targets[0] === this.spawnedId && args.timeout_ms === 10000;
    if (this.calls !== 3 || tool !== this.integrationTool) return false;
    return tool === 'send_input' ? keys === 'id,interrupt,message' && args.id === this.integrationTarget &&
      args.interrupt === false && message(args.message) : keys === 'prompt,threadId' &&
      args.threadId === this.integrationTarget && message(args.prompt);
  }

  async reply(tool, result, error = null) {
    if (!this.pending || this.pending.tool !== tool || this.closed) {
      throw new Error('no matching live host action');
    }
    if (tool === 'multi_agent_v1__spawn_agent' && !error) {
      let raw = result;
      if (raw?.content) {
        const blocks = raw.content.filter(block => block.type === 'text');
        if (blocks.length === 1) { try { raw = JSON.parse(blocks[0].text); } catch {} }
      }
      this.spawnedId = raw?.agent_id ?? raw?.id ?? null;
    }
    this.pending = null;
    this.child.stdin.write(JSON.stringify({kind: 'host_result', tool, ...(error ? {error: true} : {result})}) + '\n');
    return this.next();
  }
}
