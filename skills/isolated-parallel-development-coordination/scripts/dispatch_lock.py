"""Task-scoped dead dispatch writer recovery; never repairs ledger contents."""
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import uuid

from evidence import fingerprint, io_path, load_bound, ref, require, write_atomic

_scope = ContextVar('dispatch_writer_scope', default=None)


def process_identity(pid):
    """Unknown/access denied is not dead. Creation time prevents PID adoption."""
    require(type(pid) is int and pid > 0, 'positive owner PID required')
    if os.name != 'nt':
        return dict(pid=pid, creation=None, status='unknown')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return dict(pid=pid, creation=None, status='dead' if ctypes.get_last_error() == 87 else 'unknown')
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        code = wintypes.DWORD()
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)) or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return dict(pid=pid, creation=None, status='unknown')
        creation = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        return dict(pid=pid, creation=creation, status='alive' if code.value == 259 else 'dead')
    finally:
        kernel.CloseHandle(handle)


def dispatch_binding(runtime, request, actor, request_ref):
    require(request['project_id'] == runtime.project_id, 'request project binding differs')
    return dict(project_id=runtime.project_id, ledger=str(runtime.path), task_id=request['task_id'],
        request_id=request['request_id'], request_digest=fingerprint(request), actor_id=actor, request_ref=request_ref)


@contextmanager
def dispatch_scope(binding, evidence):
    identity = process_identity(os.getpid())
    owner = dict(schema='dispatch-writer-owner/1', binding=binding, process=identity)
    directory = io_path(evidence)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('host-owner-' + uuid.uuid4().hex + '.json')
    write_atomic(path, owner)
    token = _scope.set(dict(binding=binding, owner=ref(path), process=identity))
    try:
        yield
    finally:
        _scope.reset(token)


def lock_metadata(path):
    value = dict(pid=os.getpid(), state=str(path), token=uuid.uuid4().hex)
    if _scope.get():
        value.update(_scope.get())
    return value


def remove_exact(lock, original, file_id):
    """Windows denies concurrent replace/write while deleting this exact handle."""
    lock = io_path(lock)
    if os.name != 'nt':
        raise ValueError('atomic writer removal unavailable on this platform; inspect owner')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ | DELETE; sharing permits reads, never replacement or writing.
    handle = kernel.CreateFileW(str(lock), 0x80010000, 1, None, 3, 0x200080, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(len(original) + 1)
        count = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        require(lock.stat().st_ino == file_id and buffer.raw[:count.value] == original, 'writer lock changed or replaced')
        disposition = wintypes.BOOL(True)
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def release_owned(lock, original, file_id):
    try:
        if os.name == 'nt':
            remove_exact(lock, original, file_id)
        elif lock.stat().st_ino == file_id and lock.read_bytes() == original:
            lock.unlink()
    except (FileNotFoundError, ValueError):
        pass  # A replacement belongs to its new owner.


def preserve(directory, binding, lock, original, identity, ledger):
    directory = io_path(directory) / ('lock-recovery-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    saved = directory / (lock.name + '.original')
    with saved.open('xb') as stream:
        stream.write(original); stream.flush(); os.fsync(stream.fileno())
    proof = directory / 'precheck.json'
    write_atomic(proof, dict(schema='dispatch-lock-recovery/1', binding=binding, lock=str(lock),
        original=ref(saved), process=identity, ledger_sha256=hashlib.sha256(ledger).hexdigest()))
    return ref(proof)


def recover_dispatch_locks(runtime, binding, evidence, *, references=None):
    """Only two fixed paths, exact request/owner source, preserve then double-check."""
    require(binding['project_id'] == runtime.project_id and binding['ledger'] == str(runtime.path), 'recovery project binding differs')
    state = runtime._load()
    runtime._actor(state, binding['actor_id'], control=True)
    require(binding['request_id'] and binding['task_id'], 'original request/task required')
    request = load_bound(binding['request_ref'])
    if 'spec_check' in request:
        from codex_parallel_coordination.dispatch import approved_request
        request = approved_request(request)
    require(dispatch_binding(runtime, request, binding['actor_id'], binding['request_ref']) == binding,
            'original request binding differs')
    operation = state['runtime']['operations'].get(binding['request_id'])
    require(not operation or operation.get('operation') == 'standard-dispatch' and
        operation.get('task_id') == binding['task_id'] and operation.get('request_digest') == binding['request_digest'],
        'canonical dispatch binding differs')
    candidates = []
    expected = {Path(r['path']).resolve(): r['sha256'] for r in references} if references is not None else None
    require(references is None or len(expected) == len(references), 'duplicate lock reference')
    for target in (runtime.directory / 'standard-dispatch-driver', runtime.path):
        lock = io_path(target.with_suffix(target.suffix + '.lock'))
        if not lock.exists():
            continue
        require(not lock.is_symlink() and not lock.is_junction(), 'linked writer lock source refused')
        original = lock.read_bytes(); file_id = lock.stat().st_ino
        require(expected is None or expected.get(lock.resolve()) == hashlib.sha256(original).hexdigest(),
                'writer lock changed from original observation')
        value = json.loads(original)
        require(value.get('state') == str(target) and value.get('binding') == binding, 'writer recovery binding differs; inspect original owner')
        owner_ref = value.get('owner', {})
        require(Path(owner_ref.get('path', '')).resolve().is_relative_to(Path(evidence).resolve()), 'owner source outside trusted evidence root')
        owner = load_bound(owner_ref)
        require(owner == dict(schema='dispatch-writer-owner/1', binding=binding, process=value.get('process')) and
            value.get('pid') == value.get('process', {}).get('pid') and value['process'].get('creation') and
            value['process'].get('status') == 'alive', 'owner process/source binding insufficient')
        identity = process_identity(value['pid'])
        require(identity['status'] == 'dead' and identity['creation'] in (None, value['process']['creation']),
                'owner active, unknown or PID reused; recovery refused')
        candidates.append((lock, original, file_id, value, identity))
    ledger = io_path(runtime.path).read_bytes()
    proofs = [(item, preserve(evidence, binding, item[0], item[1], item[4], ledger)) for item in candidates]
    # All sources/owners were checked before touching either lock. Never edit the ledger.
    for (lock, original, file_id, value, _), proof in proofs:
        identity = process_identity(value['pid'])
        require(identity['status'] == 'dead' and identity['creation'] in (None, value['process']['creation']),
                'owner active, unknown or PID reused after preservation')
        require(io_path(runtime.path).read_bytes() == ledger, 'ledger changed during recovery; recheck original request')
        remove_exact(lock, original, file_id)
    results = []
    for (lock, _, _, _, _), proof in proofs:
        saved = load_bound(proof)
        result = io_path(proof['path']).parent / 'result.json'
        write_atomic(result, dict(saved, precheck=proof, status='released', lock_absent=not lock.exists()))
        results.append(ref(result))
    return results
