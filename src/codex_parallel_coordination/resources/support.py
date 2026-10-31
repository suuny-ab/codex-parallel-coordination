"""Small resource-only persistence/scope helpers; no task ledger dependency."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

ERRORS = (ValueError, KeyError, TypeError, AttributeError, OSError)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def cleanup_verified(facts):
    require(isinstance(facts, dict) and all(facts.get(k) is True for k in (
        'verified', 'preservation_valid', 'no_active_work', 'no_active_descendants',
        'no_release_dependencies', 'exclusive_task_resource', 'cleanup_authorized')) and facts.get('protected') is False,
        'cleanup requires canonical handoff, exclusive ownership and authority; pending_cleanup')
    if 'task_kind' not in facts:  # Legacy CORE facts have only the code/main completion gate.
        completed = facts.get('task_completed_on_main') is True
    else:
        kind = facts['task_kind']
        completed = facts.get('task_completion_verified') is True and (
            kind == 'code' and facts.get('task_completed_on_main') is True and facts.get('read_only_completion_verified') is False or
            kind == 'read_only' and facts.get('task_completed_on_main') is False and facts.get('read_only_completion_verified') is True)
    require(completed, 'cleanup requires canonical current completion: code on main or verified read_only; pending_cleanup')


def _io_path(path):
    path = Path(path)
    value = str(path)
    if os.name == 'nt' and path.is_absolute() and len(value) >= 260 and not value.startswith('\\\\?\\'):
        return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return path


def _open_read(path):
    if os.name != 'nt':
        return _io_path(path).open('rb')
    import _winapi
    import msvcrt
    # Read access only; SHARE_READ|SHARE_WRITE|SHARE_DELETE lets atomic rename
    # coexist with readers of the previous immutable file version. No ACL change.
    handle = _winapi.CreateFile(str(_io_path(path)), _winapi.GENERIC_READ, 0x7,
                               0, _winapi.OPEN_EXISTING, 0x80, 0)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        _winapi.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, 'rb')  # Closing this stream also owns/closes the handle.


def load(path):
    with _open_read(path) as stream:
        return json.loads(stream.read().decode('utf-8-sig'))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def ref(path):
    path = Path(path).resolve()
    with _open_read(path) as stream:
        return {'path': str(path), 'sha256': hashlib.sha256(stream.read()).hexdigest()}


def artifact(value):
    path = Path(value['path'])
    require(path.is_absolute() and _io_path(path).is_file(), 'artifact missing or not absolute')
    require(ref(path)['sha256'] == value['sha256'], 'artifact hash mismatch')
    return path


def load_bound(value):
    return load(artifact(value))


def _replace_atomic(temporary, path):
    if os.name == 'nt' and _io_path(path).exists():
        import ctypes
        replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
        replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = ctypes.c_int
        if not replace(str(_io_path(path)), str(_io_path(temporary)), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(_io_path(temporary), _io_path(path))


def write_atomic(path, value):
    path = Path(path)
    _io_path(path.parent).mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.res-' + uuid.uuid4().hex[:16] + '.tmp')
    preserve_temporary = False
    try:
        with _io_path(temporary).open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _replace_atomic(temporary, path)
        except OSError as error:
            # ReplaceFileW documents partial outcomes for these two codes. Keep
            # the new fsynced snapshot for explicit recovery, never retry/truncate.
            preserve_temporary = getattr(error, 'winerror', None) in (1176, 1177)
            if preserve_temporary:
                error.strerror = (error.strerror or 'Uncertain replacement') + '; preserved snapshot: ' + str(temporary)
            raise
    finally:
        if not preserve_temporary and _io_path(temporary).exists():
            _io_path(temporary).unlink()  # Only the exact UUID file created by this call.


@contextmanager
def writer_lock(path):
    path = Path(path)
    require(path.is_absolute(), 'absolute state required')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('writer lock exists; preserve state; never auto-steal: ' + str(lock))
    try:
        os.write(fd, json.dumps({'pid': os.getpid(), 'state': str(path)}).encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        lock.unlink()


def phase_keys(policy, spec=None):
    rules = policy.get('phase_resources')
    require(isinstance(rules, dict) and rules, 'phase resource policy missing; preserve instance and reconcile')
    for phase, keys in rules.items():
        require(text(phase) and isinstance(keys, list) and all(text(key) for key in keys)
                and len(keys) == len(set(keys)) and set(keys).issubset(policy['exclusive_keys']), 'invalid resource operation rule')
    if spec is not None:
        require(text(spec.get('phase')) and spec['phase'] in rules, 'execution phase not authorized by resource policy')
        keys = spec.get('exclusive_keys')
        require(isinstance(keys, list) and set(rules[spec['phase']]).issubset(keys), 'required phase resource keys missing')


def project_roots(policy, project_id):
    value = policy['projects'][project_id]
    roots = [value] if isinstance(value, str) else value
    require(isinstance(roots, list) and roots and all(text(x) and Path(x).is_absolute() for x in roots), 'explicit absolute project/work roots required')
    return [Path(x).resolve() for x in roots]
