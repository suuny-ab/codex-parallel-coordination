"""Private finite stdio endpoint for dispatch_host.mjs; not a JSON authority CLI."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
from core_runtime import Runtime
from evidence import fingerprint, load_bound, ref, require, write_atomic
from host_adapter import HostAdapter, HostCallError
from codex_parallel_coordination.bridge import ResourceBridge
from codex_parallel_coordination.dispatch import StandardDispatch, approved_request
from codex_parallel_coordination.resources import ArtifactCleanup, Controller
from codex_parallel_coordination.worktrees import GitWorktrees

NATIVE_TRANSPORT = {'spawn_agent': 'multi_agent_v1__spawn_agent',
                    'wait_agent': 'multi_agent_v1__wait_agent'}


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    bootstrap = json.loads(sys.stdin.readline())
    if bootstrap.get('protocol') != 'standard-dispatch-host/1':
        raise ValueError('trusted host bootstrap required')
    profile, host = bootstrap['profile'], bootstrap['host']
    request = load_bound(bootstrap['request'])
    if 'spec_check' in request:
        request = approved_request(request)
    runtime = Runtime(profile['state_root'], profile['project_root'])
    runtime._actor(runtime._load(), host['actor_id'], control=True)
    tools = host['available_tools']
    receipts = []
    evidence = Path(profile['evidence_root'])
    def record(tool, kind, value):
        evidence.mkdir(parents=True, exist_ok=True)
        path = evidence / ('host-' + fingerprint([request['request_id'], tool, kind]) + '.json')
        if path.exists():
            raise ValueError('host effect already has evidence; reconcile original operation')
        write_atomic(path, value)
        receipts.append(ref(path))
        return receipts[-1]
    # No subprocess has implicit access to the surrounding agent's native tools.
    def invoke(tool, arguments):
        canonical = tool
        tool = NATIVE_TRANSPORT.get(canonical, canonical)
        require(tool in tools, 'host capability unavailable: ' + tool)
        if canonical == 'wait_agent':
            require(set(arguments) == {'ids', 'timeout_ms'} and len(arguments['ids']) == 1 and
                    arguments['timeout_ms'] == 0, 'unexpected canonical observation arguments')
            arguments = dict(targets=arguments['ids'], timeout_ms=10000)
        stage = {'spawn_agent': 'spawn', 'wait_agent': 'observe'}.get(canonical, 'handoff')
        context = dict(request_ref=bootstrap['request'], request_id=request['request_id'], task_id=request['task_id'],
            project_id=runtime.project_id, request_digest=fingerprint(request), stage=stage,
            operation_id=request['request_id'] + (':handoff' if stage == 'handoff' else ':launch'))
        intent = record(tool, 'intent', dict(canonical_tool=canonical, tool=tool, arguments=arguments, context=context))
        emit(dict(kind='host_call', tool=tool, arguments=arguments, context=dict(context, intent=intent)))
        reply = json.loads(sys.stdin.readline())
        if reply.get('kind') != 'host_result' or reply.get('tool') != tool:
            raise ValueError('host result does not match pending operation')
        record(tool, 'result', reply)
        if 'error' in reply:
            raise HostCallError(reply['error'])
        return reply['result']
    canonical_tools = [alias for alias, actual in NATIVE_TRANSPORT.items() if actual in tools]
    canonical_tools += [tool for tool in tools if tool not in NATIVE_TRANSPORT.values() and tool not in NATIVE_TRANSPORT]
    developer = HostAdapter('native_agent', canonical_tools, invoke, host['actor_id'], scope=host['scope'])
    integration = HostAdapter(profile['integration_channel'], canonical_tools, invoke, host['actor_id'], scope=host['scope'])
    controller = Controller(profile['resource_state'])
    git = GitWorktrees(runtime.project_root, runtime._load()['runtime']['cleanup_root'])
    artifacts = ArtifactCleanup(controller, {'git_worktree': git})
    bridge = ResourceBridge(runtime, controller, artifacts, profile['evidence_root'])
    dispatch = StandardDispatch(runtime, bridge, git, developer, integration, host['user_origin'], profile['integration_target'])
    try:
        result = dispatch.reconcile_host_result(request, bootstrap['reconcile_result']) if bootstrap.get('reconcile_result') else dispatch.run(request)
    except Exception as error:
        record = runtime._load()['runtime']['operations'].get(request['request_id'])
        if not record or record.get('operation') != 'standard-dispatch':
            raise
        result = dict(dispatch.status(request['request_id']), status='needs_reconcile', reason=str(error),
                      actual_native_dispatch_verified=False)
        if bootstrap.get('reconcile_result'):
            retained = load_bound(bootstrap['reconcile_result'])
            result.update(host_result=bootstrap['reconcile_result'], reported_actual_agent_id=retained.get('actual_agent_id'),
                identity_binding_verified=False, next_action='Inspect existing intent/lock owner; do not steal locks or resend',
                next_owner=profile['integration_target'])
    emit(dict(kind='result', result=dict(result, host_evidence=receipts)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit(dict(kind='result', result=dict(status='blocked', reason=str(error), error_type=type(error).__name__,
                                            actual_native_dispatch_verified=False)))
        sys.exit(1)
