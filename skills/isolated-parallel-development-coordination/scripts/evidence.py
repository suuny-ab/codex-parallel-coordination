"""Source inventories and worker-owned immutable report construction (stdlib only)."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

from coordination_v1 import artifact, digest, load, require, write_atomic, io_path

ERRORS = (ValueError, KeyError, TypeError, AttributeError, OSError)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def ref(path):
    path = io_path(path).resolve(strict=True)
    return {"path": str(path), "sha256": digest(path)}


def load_bound(value):
    path = Path(value["path"])
    require(path.is_absolute(), "artifact missing or not absolute")
    path = io_path(path)
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
        paths = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    files = []
    for relative in paths:
        path = root / relative
        require(not path.is_symlink() and path.resolve().is_relative_to(root),
                "source symlinks/escaping paths unsupported")
        require(not path.is_dir(), "submodule source requires separate evidence")
        files.append({"relative": relative, "sha256": digest(path) if path.is_file() else None})
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
    task = state["tasks"][task_id]
    require(task["delivery"], "no bound delivery")
    from coordination import verify_completed_origin
    verify_completed_origin(state, task_id)
    report, _ = presentation_view(load_bound(task["delivery"]))
    require(report["project_id"] == state["project"]["id"] and report["task_id"] == task_id
            and report["delivery_revision"] == task["delivery_revision"], "report identity mismatch")
    require(Path(report["source"]["root"]).resolve() == Path(task["worktree"]).resolve(),
            "source outside registered worktree")
    if task.get('task_kind')=='read_only' and task['phase']=='completed' and not Path(task['worktree']).exists():
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
