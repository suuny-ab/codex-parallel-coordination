// Bounded stdio bridge for the current node_repl + functions host. No server.
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {open, readFile, mkdir, link, unlink} from 'node:fs/promises';
import {createHash, randomUUID} from 'node:crypto';

const native = new Set(['multi_agent_v1__spawn_agent', 'multi_agent_v1__wait_agent']);
const handoff = new Set(['send_input', 'mcp__codex_app__send_message_to_thread']);
const allowed = new Set([...native, ...handoff]);
const stable = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ?
  Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);

export const HOST_DEADLINE_MS = 45000;
export const SHUTDOWN_GRACE_MS = 1000;
export const NODE_CALL_TIMEOUT_MS = 50000;

export function serializeHostError(error) {
  return {name: error?.name ?? null, message: error?.message ?? String(error),
    code: error?.code ?? null, string: String(error)};
}

export class DispatchSession {
  // profile, approvalOrigin and requestMeta are supplied by the trusted host,
  // separately from the task request. Never spread task JSON into this object.
  constructor({python, profile, request, requestMeta, approvalOrigin, availableTools, testingScope = 'fixture_only', reconcileResult = null, rebaseQueued = false}) {
    if (typeof rebaseQueued !== 'boolean' || rebaseQueued && reconcileResult) throw new Error('separate finite rebase or reconciliation action required');
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
    this.options = {python, profile: {...profile}, request: {...request}, requestMeta, approvalOrigin, availableTools, testingScope, rebaseQueued};
    this.actorId = requestMeta.threadId;
    this.evidenceRoot = profile.evidence_root;
    this.resultRefs = [];
    this.retainedResult = null;
    this.replying = false;
    this.integrationTarget = profile.integration_target;
    this.integrationTool = profile.integration_channel === 'app_thread' ?
      'mcp__codex_app__send_message_to_thread' : 'send_input';
    this.spawnedId = null;
    this.pending = null;
    this.queue = [];
    this.waiter = null;
    this.calls = 0;
    this.closed = false;
    this.callTimeoutMs = NODE_CALL_TIMEOUT_MS;
    this.exited = new Promise(resolve => { this.resolveExit = resolve; });
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
      this.resolveExit();
      clearTimeout(this.timer);
      clearTimeout(this.stopTimer);
      if (!this.closed) this.deliver({kind: 'result', result: {
        status: 'needs_reconcile', reason: 'finite host process exited before result', exit_code: code,
      }});
    });
    // This is a local deadline, not polling or a wakeup. A killed operation stays
    // intent/unknown in CORE; a subsequent session cannot send it again.
    this.timer = setTimeout(() => { void this.interrupt('45 second host deadline; observe original intent'); }, HOST_DEADLINE_MS);
    this.child.stdin.write(JSON.stringify({protocol: 'standard-dispatch-host/1', profile, request,
      reconcile_result: reconcileResult,
      rebase_queued: rebaseQueued,
      host: {actor_id: requestMeta.threadId, user_origin: approvalOrigin, available_tools: [...this.tools],
        scope: testingScope}}) + '\n');
  }

  async interrupt(reason = 'caller interrupted; observe original intent') {
    const packet = {kind: 'result', result: {status: 'needs_reconcile', reason, automatic_retry: false}};
    // EOF lets only this owned Python call unwind. Never remove writer locks.
    this.deliver(packet);
    this.stopTimer = setTimeout(() => this.child.kill(), SHUTDOWN_GRACE_MS);
    await this.exited;
    clearTimeout(this.stopTimer);
    return packet;
  }

  deliver(packet) {
    if (this.closed) return;
    if (packet.kind === 'result') {
      packet.result.transport_evidence = [...this.resultRefs];
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
      if (this.options.rebaseQueued || this.pending || !this.tools.has(packet.tool) || ++this.calls > 3 || !this.validAction(packet)) {
        this.child.kill();
        throw new Error('unexpected or unbounded host action; reconcile original intent');
      }
      const requestBytes = await readFile(this.options.request.path);
      if (createHash('sha256').update(requestBytes).digest('hex') !== this.options.request.sha256 ||
          stable(packet.context?.request_ref) !== stable(this.options.request) ||
          JSON.parse(requestBytes.toString('utf8')).request_id !== packet.context?.request_id) {
        this.child.stdin.end();
        throw new Error('host packet differs from bound dispatch request');
      }
      this.pending = JSON.parse(JSON.stringify(packet));
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
    if (!this.pending || this.pending.tool !== tool || this.replying) {
      throw new Error('no matching pending host action');
    }
    this.replying = true;
    try {
      const response = {kind: 'host_result', tool, ...(error ? {error} : {result})};
      // The callback can outlive either the deadline or the child. Persist the
      // exact matching result before inspecting closed or clearing pending.
      const evidence = await this.saveResult(this.pending, response);
      this.retainedResult = evidence;
      if (!this.resultRefs.some(item => item.path === evidence.path)) this.resultRefs.push(evidence);
      if (tool === 'multi_agent_v1__spawn_agent' && !error) {
        this.spawnedId = evidence.actual_agent_id;
      }
      if (this.closed) return {kind: 'result', result: {status: 'needs_reconcile',
        request_id: evidence.request_id, actual_agent_id: evidence.actual_agent_id,
        host_result: {path: evidence.path, sha256: evidence.sha256},
        next_action: 'verifyHostResult(nodeRepl.requestMeta)', automatic_retry: false}};
      this.pending = null;
      this.child.stdin.write(JSON.stringify(response) + '\n');
      return await this.next();
    } finally {
      this.replying = false;
    }
  }

  async saveResult(packet, response) {
    const context = packet.context;
    let raw = response.result;
    if (raw?.content) {
      const texts = raw.content.filter(block => block.type === 'text');
      if (texts.length === 1) { try { raw = JSON.parse(texts[0].text); } catch {} }
    }
    const actualAgent = response.error || response.result?.isError || raw?.error || raw?.isError ? null :
      packet.tool === 'multi_agent_v1__spawn_agent' ? raw?.agent_id ?? raw?.id ?? null :
      packet.tool === 'multi_agent_v1__wait_agent' ? packet.arguments.targets[0] : null;
    const value = {schema: 'dispatch-host-result/1', actor_id: this.actorId,
      request_id: context.request_id, pending_packet: packet, actual_agent_id: actualAgent, response};
    const bytes = JSON.stringify(value, null, 2) + '\n';
    await mkdir(this.evidenceRoot, {recursive: true});
    const key = createHash('sha256').update(stable([this.options.request, this.actorId, packet])).digest('hex');
    const target = path.join(this.evidenceRoot, 'host-result-' + key + '.json');
    const temporary = target + '.' + randomUUID() + '.tmp';
    const file = await open(temporary, 'wx');
    try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
    try {
      try { await link(temporary, target); }
      catch (failure) {
        if (failure.code !== 'EEXIST') throw failure;
        if (stable(JSON.parse(await readFile(target, 'utf8'))) !== stable(value)) {
          throw new Error('conflicting duplicate host result; original evidence retained');
        }
      }
    } finally { await unlink(temporary); }
    return {path: target, sha256: createHash('sha256').update(await readFile(target)).digest('hex'),
      request_id: context.request_id, actual_agent_id: actualAgent};
  }

  async verifyHostResult(requestMeta) {
    if (!this.closed || !this.retainedResult || requestMeta?.threadId !== this.actorId) {
      throw new Error('retained result and actual original caller required');
    }
    await this.exited; // This owned child has a bounded shutdown grace, not polling.
    const session = new DispatchSession({...this.options, requestMeta,
      reconcileResult: {path: this.retainedResult.path, sha256: this.retainedResult.sha256}});
    const packet = await session.next();
    if (packet.kind !== 'result') { session.child.kill(); throw new Error('verification cannot issue host actions'); }
    return packet;
  }
}
