"""Source inventories and worker-owned immutable report construction (stdlib only)."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

from coordination_v1 import artifact as original_artifact, digest, load, require, write_atomic, io_path

ERRORS = (ValueError, KeyError, TypeError, AttributeError, OSError)
_preserved = ContextVar('confirmed_cleanup_evidence', default=None)


def artifact(value):
    # Active sources remain live; cloud-bound completed tasks use durable task material.
    preserved=_preserved.get()
    if preserved is None:
        return original_artifact(value)
    state, task_id, inventory = preserved
    from cleanup import canonical_path
    original = canonical_path(value['path'])
    if inventory.get('remote_verification'):
        owned=any(r.get('task_id')==task_id and r.get('path') and
            original.is_relative_to(canonical_path(r['path'])) for r in state['runtime'].get('resources',{}).values())
        rows=[row for row in inventory['files'] if row.get('original')==value and row['sha256']==value['sha256']]
        if owned and len(rows)==1:return original_artifact(rows[0])
    if io_path(value['path']).exists():return original_artifact(value)
    cleaned = [r for r in state['runtime'].get('resources', {}).values() if
        r.get('task_id') == task_id and r.get('cleanup_status') == 'cleaned' and r.get('path') and
        original.is_relative_to(canonical_path(r['path']))]
    require(cleaned, 'missing evidence outside confirmed task cleanup')
    for resource in cleaned: original_artifact(resource['cleanup_result']['evidence'])
    rows = [row for row in inventory['files'] if row.get('original') == value and row['sha256'] == value['sha256']]
    require(len(rows) == 1, 'missing exact preserved evidence copy')
    return original_artifact(rows[0])


@contextmanager
def preserved_read(state, task_id):
    task = state['tasks'][task_id]
    reference = task.get('preservation')
    if not reference:
        yield
        return
    from resource_lifecycle import preserved_binding, remote_binding
    from cleanup import canonical_path
    inventory = json.loads(original_artifact(reference).read_text(encoding='utf-8-sig'))
    require(task['phase'] == 'completed' and task.get('merge', {}).get('verified') and
        inventory.get('project_id') == state['project']['id'] and inventory.get('task_id') == task_id and
        inventory.get('binding') == preserved_binding(task), 'preserved completed-task binding changed')
    if inventory.get('remote_verification'):
        remote_binding(state,task_id,inventory['source'],inventory['remote_verification'],inventory['scope'])
    for item in [reference, *inventory['files']]:
        path = canonical_path(original_artifact(item))
        for resource in state['runtime'].get('resources', {}).values():
            require(not resource.get('path') or not path.is_relative_to(canonical_path(resource['path'])),
                    'preserved evidence inside cleanup resource')
    token = _preserved.set((state, task_id, inventory))
    try: yield
    finally: _preserved.reset(token)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def ref(path):
    path = io_path(path).resolve(strict=True)
    return {"path": str(path), "sha256": digest(path)}


def load_bound(value):
    path = Path(value["path"])
    require(path.is_absolute(), "artifact missing or not absolute")
    path = artifact(value)
    require(path.is_absolute() and path.is_file(), "artifact missing or not absolute")
    content = path.read_bytes()
    require(hashlib.sha256(content).hexdigest() == value["sha256"], "artifact hash mismatch")
    return json.loads(content.decode("utf-8-sig"))


def presentation_view(original):
    """Adapt display types only; preserve raw report and all technical conclusions."""
    report = copy.deepcopy(original)
    changes = []
    command = report["self_test"]["command"]
    expected = report["expected"]
    if isinstance(command, list):
        require(command and all(isinstance(x, (str, dict)) and x for x in command),
                "presentation contract: command must contain strings or command objects")
        normalized = "\n".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, sort_keys=True) for x in command)
        report["self_test"]["command"] = normalized
        changes.append({"field": "self_test.command", "original": command, "normalized": normalized})
    if isinstance(expected, list):
        require(expected and all(isinstance(x, str) and x.strip() for x in expected),
                "presentation contract: expected must contain nonempty strings")
        normalized = "\n".join(expected)
        report["expected"] = normalized
        changes.append({"field": "expected", "original": expected, "normalized": normalized})
    require(isinstance(report["self_test"]["command"], str) and report["self_test"]["command"].strip(),
            "presentation contract: self_test.command must be text or a command list")
    require(isinstance(report["expected"], str) and report["expected"].strip(),
            "presentation contract: expected must be text or a string list")
    return report, changes


def git(root, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
                            "-C", str(root), *args], capture_output=True,
                            encoding="utf-8", timeout=20)
    require(result.returncode == 0, "git: " + result.stderr.strip())
    return result.stdout.strip()


def snapshot(root, kind=None, extra=()):
    root = Path(root).resolve(strict=True)
    require(root.is_dir(), "source root missing")
    kind = kind or ("git" if (root / ".git").exists() else "directory")
    require(kind in ("git", "directory"), "unknown inventory kind")
    if kind == "git":
        require(Path(git(root, "rev-parse", "--show-toplevel")).resolve() == root,
                "source must be repository root")
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached",
                                 "--others", "--exclude-standard"], capture_output=True,
                                check=True, timeout=20)
        paths = sorted(set(x.decode("utf-8") for x in result.stdout.split(b"\0") if x))
    else:
        paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if io_path(p).is_file())
    files = []
    for relative in paths:
        path = root / relative
        physical = io_path(path)
        require(not physical.is_symlink() and path.resolve().is_relative_to(root),
                "source symlinks/escaping paths unsupported")
        require(not physical.is_dir(), "submodule source requires separate evidence")
        files.append({"relative": relative, "sha256": digest(path) if physical.is_file() else None})
    require(files, "empty source inventory")
    value = {"root": str(root), "kind": kind, "files": files,
             "extra": [ref(artifact(x)) for x in extra]}
    value["digest"] = fingerprint(value)
    return value


def verify_source(value):
    require(snapshot(value["root"], value["kind"], value.get("extra", [])) == value,
            "source inventory changed (including added/deleted files)")


def pack(spec):
    report = copy.deepcopy(spec)
    report["source"] = snapshot(report.pop("source_root"), extra=report.pop("extra_sources", []))
    report["evidence"] = [ref(x) if isinstance(x, str) else ref(artifact(x)) for x in report["evidence"]]
    report["self_test"]["source_digest"] = report["source"]["digest"]
    if report["entry"]["kind"] == "file":
        report["entry"]["artifact"] = ref(report["entry"]["target"])
    return report


def report_for(state, task_id, ready=True):
    with preserved_read(state, task_id):
        return _report_for(state, task_id, ready)


def _report_for(state, task_id, ready=True):
    task = state["tasks"][task_id]
    require(task["delivery"], "no bound delivery")
    from coordination import verify_completed_origin
    verify_completed_origin(state, task_id)
    report, _ = presentation_view(load_bound(task["delivery"]))
    require(report["project_id"] == state["project"]["id"] and report["task_id"] == task_id
            and report["delivery_revision"] == task["delivery_revision"], "report identity mismatch")
    require(Path(report["source"]["root"]).resolve() == Path(task["worktree"]).resolve(),
            "source outside registered worktree")
    if _preserved.get() is not None and _preserved.get()[2].get('remote_verification'):
        require(_preserved.get()[2]['source']==report['source'],'cloud-bound preserved source inventory differs')
    elif _preserved.get() is not None and not io_path(task['worktree']).exists():
        _, _, inventory = _preserved.get()
        require(inventory['source'] == report['source'], 'preserved source inventory differs')
        cleaned=[r for r in state['runtime'].get('resources',{}).values() if r.get('task_id')==task_id and
            r.get('cleanup_status')=='cleaned' and r.get('kind')=='worktree' and
            Path(r.get('path','')).resolve()==Path(task['worktree']).resolve()]
        require(cleaned,'source disappeared outside confirmed owned worktree cleanup')
        for resource in cleaned:artifact(resource['cleanup_result']['evidence'])
    elif task.get('task_kind')=='read_only' and task['phase']=='completed' and not Path(task['worktree']).exists():
        cleaned=[r for r in state.get('runtime',{}).get('resources',{}).values() if r.get('task_id')==task_id and
            r.get('cleanup_status')=='cleaned' and r.get('path') and Path(task['worktree']).resolve().is_relative_to(Path(r['path']).resolve())]
        require(cleaned,'read-only source disappeared outside confirmed owned cleanup')
        for resource in cleaned:artifact(resource['cleanup_result']['evidence'])
    else:verify_source(report["source"])
    if task.get('task_kind')=='read_only':
        require(report['source']==task['requirements']['source_baseline'],'read-only source changed from confirmed baseline')
        require(report.get('task_kind')=='read_only' and report.get('acceptance_type')==task['acceptance_type'],'report task kind/acceptance type mismatch')
    require(report["evidence"], "test evidence required")
    for value in report["evidence"]:
        artifact(value)
    test = report["self_test"]
    require(test["source_digest"] == report["source"]["digest"], "self-test source binding stale")
    require(test["command"].strip(), "self-test command required")
    if ready:
        require(test["status"] == "passed", "technical self-test not passed")
        if task.get('independent_quality'):
            from real_use_closeout import quality
            quality(state, task_id)
        else:
            require(report.get("quality_review", {}).get("status") == "passed", "business/quality review pending")
    entry = report["entry"]
    if entry["kind"] == "file":
        require(artifact(entry["artifact"]).stat().st_size > 0, "empty acceptance entry")
    else:
        url = urlparse(entry["target"])
        require(entry["kind"] == "url" and url.scheme in ("http", "https") and url.netloc,
                "bad acceptance URL")
        artifact(entry["probe_evidence"])
    require(report["steps"] and all(isinstance(x, str) and x.strip() for x in report["steps"])
            and report["expected"].strip(), "acceptance steps/expected required")
    return report
