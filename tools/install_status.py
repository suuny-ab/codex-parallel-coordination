"""Bounded read-only cloud, installation and current-session observations.

Project, reviewed binding and optional session probe are trusted host sideband,
never fields taken from a task/report. No probe means no session-load claim.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import package
from profile import filesystem, ordinary

SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")
HASH = re.compile(r"[a-f0-9]{64}")
SKILL = "skills/isolated-parallel-development-coordination/"
REQUIRED = {"plugin.json", ".codex-plugin/plugin.json", "capabilities.json",
            SKILL + "SKILL.md", SKILL + "scripts/dispatch_entry.mjs",
            SKILL + "scripts/dispatch_host.mjs", SKILL + "scripts/standard_dispatch.py"}
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError)
MAX_FILES, MAX_BYTES, MAX_NODES = 1000, 30_000_000, 3000


def now():
    return datetime.now(timezone.utc).isoformat()


def observation(status="unknown", **values):
    return {"status": status, "checked_at": now(), **values}


def redact(value):
    # Do not echo arbitrary stderr, binding fields or exception strings. This
    # final scrub also covers credential-bearing host paths and URLs.
    value = re.sub(r"(?i)(https?|ssh)://[^\s/]*@", r"\1://[redacted]@", value)
    value = re.sub(r"([?&][^=&#\s]+=)[^&#\s]*", r"\1[redacted]", value)
    value = re.sub(r"((?:https?|ssh)://[^\s#]+)#[^\s]*", r"\1#[redacted]", value)
    value = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[redacted]", value)
    return value


def scrub(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(key): scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def read_bytes(path):
    path = filesystem(Path(path))
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("metadata_limit")
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("metadata_limit")
    return content


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8-sig"))


def inventory(root):
    """Use the portable-package source policy and link checks, with read limits."""
    root = filesystem(Path(root).absolute())
    package.source_path(root, root)
    candidates = [root / name for name in package.FILES if (root / name).exists()]
    nodes = 0
    for directory, extensions in package.TREES.items():
        tree = root / directory
        if not tree.exists():
            continue
        package.source_path(tree, root)
        def walk_error(error):
            raise error
        for parent, directories, files in os.walk(tree, followlinks=False, onerror=walk_error):
            nodes += len(directories) + len(files)
            if nodes > MAX_NODES:
                raise ValueError("inventory_limit")
            base = Path(parent)
            directories[:] = [name for name in directories if name.casefold() not in package.LOCAL_DIRS]
            for name in directories:
                package.source_path(base / name, root)
            candidates.extend(base / name for name in files if Path(name).suffix in extensions)
    if len(candidates) > MAX_FILES:
        raise ValueError("inventory_limit")
    result, size = {}, 0
    for path in sorted(candidates):
        package.source_path(path, root)
        size += path.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("inventory_limit")
        result[path.relative_to(root).as_posix()] = package.digest(path.read_bytes())
    return result


def git(project, *arguments, timeout=5):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
           "GCM_INTERACTIVE": "never", "GIT_CONFIG_NOSYSTEM": "1"}
    command = ["git", "-c", "core.longpaths=true", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.allow=never",
               "-c", "credential.helper=", "-c", "credential.interactive=false",
               "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
               "-c", "core.sshCommand=ssh -oBatchMode=yes -oConnectionAttempts=1 -oConnectTimeout=" + str(math.ceil(timeout)),
               "-C", str(ordinary(project)), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "git_unavailable"
    if result.returncode:
        message = result.stderr.lower()
        category = "authentication" if any(x in message for x in
            (b"authentication", b"permission denied", b"could not read username", b"403", b"401")) else "git_query_failed"
        return None, category
    if len(result.stdout) > 1_000_000:
        return None, "output_limit"
    return result.stdout.decode("utf-8", errors="replace").strip(), None


def cloud_status(project, offline, timeout):
    result = observation(reason="offline" if offline else "remote_unverified", main_commit=None,
                         manifest_version=None, source=None, cached_origin_main=None)
    cache, _ = git(project, "rev-parse", "--verify", "refs/remotes/origin/main", timeout=timeout)
    if cache and SHA.fullmatch(cache):
        result["cached_origin_main"] = {"commit": cache, "kind": "local_cache_only"}
    remote, error = git(project, "config", "--get", "remote.origin.url", timeout=timeout)
    if error:
        result["reason"] = "remote_configuration_missing"
        return result
    # Disallow local files and executable remote helpers. The host selects the
    # project; reports cannot select repositories, transports or executables.
    parsed = urlsplit(remote)
    if not (parsed.scheme in ("https", "ssh") and parsed.hostname or
            re.fullmatch(r"[\w.-]+@[\w.-]+:[\w./-]+", remote)):
        result["reason"] = "unsupported_remote_transport"
        return result
    result["source"] = remote
    if offline:
        return result
    output, error = git(project, "ls-remote", "--refs", remote, "refs/heads/main", timeout=timeout)
    rows = output.splitlines() if output else []
    if error or len(rows) != 1 or not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?\s+refs/heads/main", rows[0]):
        result["reason"] = error or "remote_main_missing_or_invalid"
        return result
    commit = rows[0].split()[0]
    result.update(status="consistent", reason="remote_main_observed", main_commit=commit,
                  evidence={"kind": "git_ls_remote", "ref": "refs/heads/main", "commit": commit})
    # Reading an object at the freshly queried OID does not fetch or move refs.
    # If it is unavailable locally, only the cloud version remains unknown.
    content, error = git(project, "show", commit + ":plugin.json", timeout=timeout)
    try:
        manifest = json.loads(content) if not error else {}
        if manifest.get("name") == package.NAME and isinstance(manifest.get("version"), str):
            result["manifest_version"] = manifest["version"]
            result["manifest_evidence"] = {"kind": "local_object_at_observed_remote_oid", "commit": commit}
    except ERRORS:
        pass
    return result


def installation_status(binding, timeout):
    result = observation(reason="installation_binding_missing", plugin_id=None, manifest_version=None,
                         source_commit=None, source_root=None, files=observation(reason="not_checked"))
    if not binding:
        return result
    root = binding.get("source_root")
    expected, head = binding.get("files"), binding.get("source_head")
    if (not isinstance(root, str) or not Path(root).is_absolute() or
            not isinstance(expected, dict) or not REQUIRED.issubset(expected) or
            len(expected) > MAX_FILES or not isinstance(head, str) or not SHA.fullmatch(head)):
        result["reason"] = "installation_binding_incomplete"
        return result
    result.update(source_root=root, source_commit=head)
    for name, value in expected.items():
        package.safe_name(name)
        if not isinstance(value, str) or not HASH.fullmatch(value):
            raise ValueError("invalid_binding_hash")
    actual = inventory(root)
    changed = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
    missing, added = sorted(expected.keys() - actual.keys()), sorted(actual.keys() - expected.keys())
    result["files"] = observation("inconsistent" if changed or missing or added else "consistent",
        basis="reviewed_source_root/source_head/files; portable-package source policy",
        expected_digest=package.digest(package.encoded(expected)), actual_digest=package.digest(package.encoded(actual)),
        checked_files=len(actual), changed=changed, missing=missing, added=added)
    try:
        manifest = read_json(Path(root) / "plugin.json")
        legacy = read_json(Path(root) / ".codex-plugin/plugin.json")
        capabilities = read_json(Path(root) / "capabilities.json")
        if not all(isinstance(item, dict) for item in (manifest, legacy, capabilities)):
            raise ValueError("manifest_schema")
    except ERRORS as error:
        result.update(status="inconsistent" if result["files"]["status"] == "inconsistent" else "unknown",
                      reason="installed_manifest_unreadable", error_category=type(error).__name__)
        return result
    result.update(plugin_id=manifest.get("name"), manifest_version=manifest.get("version"))
    identity_ok = (manifest.get("name") == legacy.get("name") == package.NAME and
                   isinstance(manifest.get("version"), str) and
                   manifest["version"] == legacy.get("version") == capabilities.get("version"))
    result["identity"] = observation("consistent" if identity_ok else "inconsistent",
        basis="plugin.json, .codex-plugin/plugin.json, capabilities.json")
    if (filesystem(Path(root)) / ".git").exists():
        git_head, error = git(root, "rev-parse", "--verify", "HEAD", timeout=timeout)
        result["git_head"] = git_head if not error else None
        result["commit_binding"] = observation("unknown" if error else "consistent" if git_head == head else "inconsistent",
            reason=error or "installed_git_head_vs_reviewed_binding")
    else:
        result["commit_binding"] = observation("consistent", reason="trusted_reviewed_binding; no_installed_git_metadata")
    checks = [result[key]["status"] for key in ("identity", "files", "commit_binding")]
    result.update(status="inconsistent" if "inconsistent" in checks else "unknown" if "unknown" in checks else "consistent",
                  reason="reviewed_installation_checked")
    return result


def session_status(installation, binding, session_id, probe):
    result = observation(reason="session_load_unconfirmed", entry=None, source_commit=None,
                         manifest_version=None, evidence=None)
    if not session_id or probe is None:
        return result
    # Only an in-process trusted host adapter may supply this observation. There
    # is intentionally no CLI session JSON/report flag or directory inference.
    value = probe()
    if (not isinstance(value, dict) or value.get("session_id") != session_id or
            value.get("kind") not in ("host_metadata", "trusted_entry_use") or
            not isinstance(value.get("loaded_sha256"), str) or not HASH.fullmatch(value["loaded_sha256"]) or
            not isinstance(value.get("source_commit"), str) or not SHA.fullmatch(value["source_commit"]) or
            not isinstance(value.get("manifest_version"), str) or not value["manifest_version"] or
            not isinstance(value.get("evidence"), dict) or
            not isinstance(value["evidence"].get("sha256"), str) or not HASH.fullmatch(value["evidence"]["sha256"]) or
            not Path(value["evidence"].get("path", "")).is_absolute() or
            not value.get("observed_at") or not value.get("entry") or not value.get("source_root")):
        result["reason"] = "session_evidence_incomplete_or_wrong_session"
        return result
    try:
        evidence_bytes = read_bytes(value["evidence"]["path"])
    except ERRORS as error:
        result.update(reason="session_evidence_unreadable_or_over_limit", error_category=type(error).__name__)
        return result
    if package.digest(evidence_bytes) != value["evidence"]["sha256"]:
        result["reason"] = "session_evidence_hash_mismatch"
        return result
    root, entry = Path(value["source_root"]), value["entry"]
    if not root.is_absolute():
        return result
    package.safe_name(entry)
    timestamp = datetime.fromisoformat(value["observed_at"])
    if timestamp.tzinfo is None or timestamp > datetime.now(timezone.utc):
        return result
    expected_hash = (binding or {}).get("files", {}).get(entry)
    if expected_hash is None:
        result["reason"] = "session_entry_not_in_reviewed_binding"
        return result
    path = filesystem(root) / entry
    package.source_path(path, filesystem(root))
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("session_entry_limit")
    disk_hash = package.digest(path.read_bytes())
    same_root = installation.get("source_root") and root.resolve() == Path(installation["source_root"]).resolve()
    match = (same_root and disk_hash == value["loaded_sha256"] == expected_hash and
             installation.get("source_commit") == value["source_commit"] and
             installation.get("manifest_version") == value["manifest_version"])
    result.update(status="consistent" if match else "inconsistent", reason="trusted_current_session_observation",
        entry=str(ordinary(path)), source_commit=value["source_commit"], manifest_version=value["manifest_version"],
        evidence={key: value["evidence"][key] for key in ("path", "sha256")},
        observed_at=value["observed_at"], kind=value["kind"],
        scope="observed_entry_only; not_entire_plugin_loaded",
        loaded_sha256=value["loaded_sha256"], current_file_sha256=disk_hash)
    return result


def check_status(project_root, installation=None, *, offline=False, timeout=5, session_id=None, session_probe=None):
    """Trusted host API. Never execute code or read task/ledger-selected settings."""
    if not Path(project_root).is_absolute() or not 0 < timeout <= 10:
        raise ValueError("absolute host-selected project and timeout in (0, 10] required")
    def guarded(call, reason):
        try:
            return call()
        except ERRORS as error:
            return observation(reason=reason, error_category=type(error).__name__)
    cloud = guarded(lambda: cloud_status(project_root, offline, timeout), "cloud_unverified")
    installed = guarded(lambda: installation_status(installation, timeout), "installation_unverified")
    session = guarded(lambda: session_status(installed, installation, session_id, session_probe), "session_load_unconfirmed")
    remote, local = cloud.get("main_commit"), installed.get("source_commit")
    comparison = observation("unknown" if not remote or not local else "consistent" if remote == local else "inconsistent",
        basis="observed_remote_main_vs_reviewed_installed_commit", remote_commit=remote, installed_commit=local)
    result = {"schema": "plugin-install-status/1", "checked_at": now(), "cloud": cloud,
              "installation": installed, "session": session, "main_vs_installation": comparison,
              "limits": {"git_call_timeout_seconds": timeout, "max_git_calls": 7,
                         "max_files": MAX_FILES, "max_inventory_bytes": MAX_BYTES},
              "scope": "read_only_observation; no_install_reload_or_task_side_effects"}
    # Whitelist-oriented construction plus a final scrub, not raw exceptions.
    return scrub(result)


def render_text(result):
    labels = {"consistent": "已核一致", "inconsistent": "已核不一致", "unknown": "资料不足"}
    lines = ["插件版本状态核验 " + result["checked_at"]]
    for key, title in (("cloud", "云端 main"), ("installation", "本机安装"), ("session", "当前会话入口")):
        item = result[key]
        lines.append(f"{title}：{labels[item['status']]}；清单 {item.get('manifest_version') or '未知'}；提交 " +
                     (item.get("main_commit") or item.get("source_commit") or "未知") + "；" + item["reason"])
    files = result["installation"].get("files", {})
    lines.append("安装指纹：" + labels.get(files.get("status"), "资料不足"))
    lines.append("main 与安装提交：" + labels[result["main_vs_installation"]["status"]])
    if result["session"]["status"] == "unknown":
        lines.append("会话加载未确认；安装、文件存在或技能可读取均不能证明当前加载。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="host-selected trusted repository")
    parser.add_argument("--installation-binding", type=Path, help="host-selected reviewed source_root/source_head/files JSON")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--format", choices=("text", "json", "both"), default="both")
    args = parser.parse_args()
    if not args.project.is_absolute() or not 0 < args.timeout <= 10:
        parser.error("absolute --project and --timeout in (0, 10] required")
    binding, binding_ref, failure = None, None, None
    if args.installation_binding:
        try:
            if not args.installation_binding.is_absolute():
                raise ValueError("absolute binding required")
            content = read_bytes(args.installation_binding)
            binding = json.loads(content.decode("utf-8-sig"))
            binding_ref = {"path": str(args.installation_binding), "sha256": package.digest(content)}
        except ERRORS as error:
            failure = type(error).__name__
    result = check_status(args.project, binding, offline=args.offline, timeout=args.timeout)
    if failure:
        result["installation"].update(reason="installation_binding_unreadable", error_category=failure)
    result["installation"]["binding_evidence"] = scrub(binding_ref)
    if args.format != "json":
        print(render_text(result))
    if args.format != "text":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
