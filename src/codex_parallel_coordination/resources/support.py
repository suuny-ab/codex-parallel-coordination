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
        'verified', 'task_completed_on_main', 'preservation_valid', 'no_active_work', 'no_active_descendants',
        'no_release_dependencies', 'exclusive_task_resource', 'cleanup_authorized')) and facts.get('protected') is False,
        'cleanup requires canonical target-main completion, handoff, exclusive ownership and authority; pending_cleanup')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def ref(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def artifact(value):
    path = Path(value['path'])
    require(path.is_absolute() and path.is_file(), 'artifact missing or not absolute')
    require(ref(path)['sha256'] == value['sha256'], 'artifact hash mismatch')
    return path


def load_bound(value):
    return load(artifact(value))


def write_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.res-' + uuid.uuid4().hex[:16] + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()  # Only the exact UUID file created by this call.


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
