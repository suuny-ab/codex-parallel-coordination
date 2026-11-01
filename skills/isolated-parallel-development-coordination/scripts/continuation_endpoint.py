"""Private one-call endpoint for continuation_host.mjs, not task-supplied authority."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if sys.platform=='win32' and not str(ROOT).startswith('\\\\?\\'): ROOT=Path('\\\\?\\'+str(ROOT))
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'skills/isolated-parallel-development-coordination/scripts'))
from core_runtime import Runtime
from delivery_continuation import Continuation
from evidence import load_bound, ref, require
from codex_parallel_coordination.bridge import ResourceBridge
from codex_parallel_coordination.resources import ArtifactCleanup, Controller
from codex_parallel_coordination.worktrees import GitWorktrees


def main():
    value=json.loads(sys.stdin.readline())
    require(value.get('protocol')=='delivery-continuation-host/1','trusted host bootstrap required')
    profile,host=value['profile'],value['host']
    runtime=Runtime(profile['state_root'],profile['project_root'])
    actor=host['actor_id']
    if value['action']=='inspect-locks':
        runtime._actor(runtime._load(),actor,control=True)
        load_bound(value['request'])
        return dict(status='observed',request=value['request'],locks=[ref(p) for p in (
            runtime.path.with_suffix('.json.lock'),runtime.directory/'standard-dispatch-driver.lock') if p.exists()],automatic_retry=False)
    if value['action']=='recover-locks':
        return runtime.recover_call_locks(value['locks'],value['destination'],actor,request_ref=value['request'])
    options=dict(scope=host['scope'],reclaim_authorized=host.get('reclaim_authorized') is True,
        install_authorized=host.get('install_authorized') is True,
        successor_authorized=host.get('successor_authorized') is True,successor_origin=host.get('successor_origin'),
        available_tools=host['available_tools'])
    flow=Continuation(runtime,value['request'],actor,**options)
    flow.authorize(runtime._load())
    if value['action']=='claim-native':
        operation=runtime._load()['runtime']['operations'][value['operation_id']]
        require(operation['task_id']==flow.key,'close belongs to another task')
        return runtime.claim_native_close(value['operation_id'],actor,authorized=options['reclaim_authorized'])
    if value['action']=='claim-install':
        return flow.claim_install()
    require(value['action']=='run','unknown finite continuation action')
    controller=Controller(profile['resource_state'])
    git=GitWorktrees(runtime.project_root,runtime._load()['runtime']['cleanup_root'])
    bridge=ResourceBridge(runtime,controller,ArtifactCleanup(controller,{'git_worktree':git}),profile['evidence_root'])
    return runtime.continue_delivery(value['request'],actor,bridge=bridge,inputs=value.get('inputs'),
                                    max_steps=value.get('max_steps',8),**options)


if __name__=='__main__':
    try: result=main()
    except Exception as error:
        result=dict(status='blocked',reason=str(error),error_type=type(error).__name__,automatic_retry=False)
    print(json.dumps(result,ensure_ascii=False),flush=True)
