"""Explicit recovery of a dead finite call, never automatic stale-lock stealing."""
import json
import os
from pathlib import Path

from evidence import artifact, io_path, load_bound, ref, require, write_atomic


def dead(pid):
    require(type(pid) is int and pid > 0, 'positive recorded owner PID required')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # nonexistent; access denied is unknown
        try:
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value != 259
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def recover(runtime, references, destination, actor_id, *, request_ref=None, dispatch_evidence_root=None):
    runtime._actor(runtime._load(), actor_id, control=True)
    require(references and Path(destination).is_absolute(), 'bound locks and absolute external backup required')
    destination = Path(destination).resolve()
    state = runtime._load()
    roots = [runtime.project_root, Path(state['runtime']['cleanup_root'])]
    roots += [Path(r['path']) for r in state['runtime'].get('resources', {}).values() if r.get('path')]
    require(all(not destination.is_relative_to(p.resolve()) for p in roots), 'lock backup must outlive managed resources')
    paths = [runtime.path, runtime.directory/'standard-dispatch-driver']
    allowed = {p.with_suffix(p.suffix+'.lock').resolve(): p.resolve() for p in paths}
    if dispatch_evidence_root is not None:
        require(request_ref and Path(dispatch_evidence_root).is_absolute() and
                Path(dispatch_evidence_root).resolve() == destination, 'fixed host dispatch evidence root required')
        request = load_bound(request_ref)
        if 'spec_check' in request:
            from codex_parallel_coordination.dispatch import approved_request
            request = approved_request(request)
        require(request.get('schema') != 'delivery-continuation/1', 'dispatch recovery cannot replace preservation recovery')
        require(all(Path(r['path']).resolve() in allowed and not Path(r['path']).is_symlink() for r in references),
                'only this runtime finite-call locks are recoverable')
        from dispatch_lock import dispatch_binding, recover_dispatch_locks
        recovered = recover_dispatch_locks(runtime, dispatch_binding(runtime, request, actor_id, request_ref),
                                           destination, references=references)
        return dict(status='recovered', locks=recovered, automatic_retry=False)
    checked = []
    for reference in references:
        path = Path(reference['path']).resolve()
        require(path in allowed and not Path(reference['path']).is_symlink(), 'only this runtime finite-call locks are recoverable')
        data = artifact(reference).read_bytes()
        value = json.loads(data)
        require(Path(value['state']).resolve() == allowed[path] and dead(value.get('pid')), 'owner alive/unknown or lock target differs')
        checked.append((path, reference, data, path.stat(), value['pid']))
    require(len({x[0] for x in checked}) == len(checked), 'duplicate lock reference')
    require(request_ref,'original StandardDispatch request required')
    request=load_bound(request_ref)
    if 'spec_check' in request:
        from codex_parallel_coordination.dispatch import approved_request
        request=approved_request(request)
    operation=state['runtime']['operations'].get(request.get('request_id'),{})
    require(request.get('project_id')==runtime.project_id and operation.get('task_id')==request.get('task_id'),
            'original request project/task differs')
    task=state['tasks'][request['task_id']]
    if request.get('schema')=='delivery-continuation/1':
        copy=state['runtime']['operations'].get(request['request_id']+':preservation',{})
        executor=state['runtime']['executors'].get(task['owner_agent_id'],{})
        require(operation.get('operation')=='delivery-continuation' and operation.get('request')==request_ref and
                copy.get('operation')=='continuation-effect' and copy.get('request')==request_ref and
                copy.get('status') in ('intent','unknown') and all(pid==copy.get('owner_pid') for _,_,_,_,pid in checked) and
                all(path==runtime.path.with_suffix('.json.lock').resolve() for path,_,_,_,_ in checked),
                'only the original dead local preservation call can recover its CORE lock')
        require(task['phase']=='completed' and task.get('merge',{}).get('verified') and
                executor.get('execution') in ('idle','completed') and executor.get('active_children')==[] and
                executor.get('pending_work') is False and executor.get('lease')=='open', 'active/unverified native work blocks recovery')
        artifact(executor['observation'])
        observed=load_bound(executor['observation'])
        require(observed.get('executor_id')==executor.get('id') and observed.get('generation')==executor.get('generation') and
                all(observed.get(k)==executor.get(k) for k in ('execution','active_children','pending_work')) and
                not executor.get('shared') and not executor.get('protected'), 'exclusive unchanged terminal observation required')
        from delivery_continuation import source
        require(copy.get('source')==source(task) and all(r.get('references')==[request['task_id']] and not r.get('shared') and
                not r.get('protected') for r in state['runtime'].get('resources',{}).values() if r.get('task_id')==request['task_id']) and
                not any(t.get('owner_agent_id')==task['owner_agent_id'] and key!=request['task_id'] and t['phase'] not in ('completed','cancelled')
                        for key,t in state['tasks'].items()), 'copy source or exclusive task ownership changed')
        from resource_lifecycle import preserved_binding
        marker=load_bound(ref(io_path(Path(request['preservation_destination'])/'preservation-intent.json')))
        require(marker.get('operation_id')==request['request_id']+':preservation' and
                marker.get('binding')==preserved_binding(task),'original preservation marker/binding required')
        require(not any(op.get('task_id')==request['task_id'] and op.get('operation') in ('close','resume') or
                        op.get('task_id')==request['task_id'] and op.get('operation') in ('spawn','send') and
                        op.get('status') in ('intent','unknown','accepted_unconfirmed') for op in state['runtime']['operations'].values()),
                'uncertain native effect requires separate reconciliation')
    else:
        require(False, 'dispatch recovery requires creation identity and the trusted host evidence root')
    destination.mkdir(parents=True, exist_ok=True)
    saved = []
    for path, reference, data, info, pid in checked:
        backup = io_path(destination/(path.name+'.'+reference['sha256']+'.original'))
        if backup.exists():
            require(backup.read_bytes() == data, 'preserved lock changed')
        else:
            with backup.open('xb') as stream: stream.write(data)
        saved.append(dict(original=reference, preserved=ref(backup), owner_pid=pid, confirmed_dead=True))
    proof = io_path(destination/'lock-recovery.json')
    write_atomic(proof, dict(schema='finite-call-lock-recovery/1', actor_id=actor_id, request=request_ref, locks=saved))
    for path, reference, data, info, pid in checked:
        current = path.stat()
        require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino) and
                artifact(reference).read_bytes() == data and dead(pid), 'lock changed or owner no longer confirmed dead')
        path.unlink()
    return dict(status='recovered', evidence=ref(proof), locks=saved,
                next_action='Continue the original request; reconcile any intent before host effects', automatic_retry=False)
