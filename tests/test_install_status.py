"""Deterministic file/host fixtures; never a live plugin or session proof."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import install_status as status
import package
from profile import filesystem

A, B = "a" * 40, "b" * 40
ENTRY = status.SKILL + "scripts/dispatch_entry.mjs"


class InstallationStatus(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="install-status-", dir=filesystem(Path(tempfile.gettempdir())))
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root, self.project = self.base / "installed", self.base / "project"
        self.project.mkdir()
        for name in status.REQUIRED:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture only", encoding="utf-8")
        for name in ("plugin.json", ".codex-plugin/plugin.json", "capabilities.json"):
            (self.root / name).write_bytes(package.encoded(dict(name=package.NAME, version="0.1.0")))
        self.binding = dict(source_root=str(self.root), source_head=A, files=status.inventory(self.root))
        for name in ("config.toml", "ledger.json", ".git/refs/heads/main", "business.sqlite"):
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"protected fixture")
        self.remote = A

    def fake_git(self, project, *args, **kwargs):
        if args[:2] == ("config", "--get"):
            return "https://github.com/example/plugin.git", None
        if args[0] == "ls-remote":
            return self.remote + "\trefs/heads/main", None
        if args[0] == "show":
            return json.dumps(dict(name=package.NAME, version="0.1.0")), None
        if args[0] == "rev-parse" and args[-1] == "HEAD":
            return A, None
        return B, None  # deliberately stale origin/main cache

    def check(self, **kwargs):
        with patch.object(status, "git", side_effect=self.fake_git):
            return status.check_status(self.project, self.binding, **kwargs)

    def session(self, **changes):
        value = dict(session_id="actual-host-fixture", kind="trusted_entry_use", entry=ENTRY,
            source_root=str(self.root), source_commit=A, manifest_version="0.1.0",
            loaded_sha256=self.binding["files"][ENTRY], observed_at=datetime.now(timezone.utc).isoformat(),
            evidence=dict(path=str(self.base / "host-receipt.json"), sha256="c" * 64))
        value.update(changes)
        return dict(session_id="actual-host-fixture", session_probe=lambda: value)

    def test_three_layers_same_commit_and_current_entry(self):
        result = self.check(**self.session())
        for key in ("cloud", "installation", "session", "main_vs_installation"):
            self.assertEqual(result[key]["status"], "consistent")
            self.assertIn("checked_at", result[key])
        self.assertEqual(result["cloud"]["cached_origin_main"]["commit"], B)
        self.assertIn("entry_only", result["session"]["scope"])

    def test_same_version_different_commit(self):
        self.remote = B
        result = self.check()
        self.assertEqual(result["cloud"]["manifest_version"], result["installation"]["manifest_version"])
        self.assertEqual(result["main_vs_installation"]["status"], "inconsistent")

    def test_modified_file(self):
        (self.root / ENTRY).write_bytes(b"changed")
        result = self.check(**self.session())
        self.assertEqual(result["installation"]["status"], "inconsistent")
        self.assertIn(ENTRY, result["installation"]["files"]["changed"])
        self.assertEqual(result["session"]["status"], "inconsistent")

    def test_modified_file_loaded_by_session_still_differs_from_review(self):
        (self.root / ENTRY).write_bytes(b"changed")
        result = self.check(**self.session(loaded_sha256=package.digest(b"changed")))
        self.assertEqual(result["session"]["status"], "inconsistent")

    def test_added_and_missing_files(self):
        (self.root / ENTRY).unlink()
        (self.root / "tools").mkdir()
        (self.root / "tools/added.py").write_bytes(b"extra")
        result = self.check()["installation"]
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["files"]["missing"], [ENTRY])
        self.assertEqual(result["files"]["added"], ["tools/added.py"])

    def test_corrupt_manifest_preserves_proven_file_mismatch(self):
        (self.root / "plugin.json").write_bytes(b"broken json")
        result = self.check()["installation"]
        self.assertEqual(result["status"], "inconsistent")
        self.assertIn("plugin.json", result["files"]["changed"])

    def test_incomplete_bindings_are_unknown(self):
        for binding in (None, {}, {"source_root": str(self.root)},
                        {**self.binding, "files": {"plugin.json": "a" * 64}},
                        {**self.binding, "source_head": "0.1.0"}):
            with self.subTest(binding=binding):
                self.binding = binding
                self.assertEqual(self.check()["installation"]["status"], "unknown")

    def test_offline_never_queries_remote_but_checks_installation(self):
        with patch.object(status, "git", side_effect=self.fake_git) as query:
            result = status.check_status(self.project, self.binding, offline=True)
        self.assertEqual(result["cloud"]["status"], "unknown")
        self.assertEqual(result["cloud"]["reason"], "offline")
        self.assertEqual(result["installation"]["status"], "consistent")
        self.assertFalse(any(call.args[1] in ("ls-remote", "show") for call in query.call_args_list))

    def test_remote_failure_never_uses_cache_to_pass(self):
        original = self.fake_git
        def unreachable(project, *args, **kwargs):
            return (None, "timeout") if args[0] == "ls-remote" else original(project, *args, **kwargs)
        with patch.object(status, "git", side_effect=unreachable):
            result = status.check_status(self.project, self.binding)
        self.assertEqual(result["cloud"]["reason"], "timeout")
        self.assertEqual(result["cloud"]["status"], "unknown")
        self.assertIsNone(result["cloud"]["main_commit"])
        self.assertEqual(result["main_vs_installation"]["status"], "unknown")
        self.assertEqual(result["installation"]["status"], "consistent")

    def test_remote_oid_without_local_object_keeps_version_unknown(self):
        original = self.fake_git
        def no_object(project, *args, **kwargs):
            return (None, "git_query_failed") if args[0] == "show" else original(project, *args, **kwargs)
        with patch.object(status, "git", side_effect=no_object):
            result = status.check_status(self.project, self.binding)
        self.assertEqual(result["cloud"]["main_commit"], A)
        self.assertIsNone(result["cloud"]["manifest_version"])

    def test_remote_missing_main_is_unknown(self):
        original = self.fake_git
        def missing_main(project, *args, **kwargs):
            return ("", None) if args[0] == "ls-remote" else original(project, *args, **kwargs)
        with patch.object(status, "git", side_effect=missing_main):
            result = status.check_status(self.project, self.binding)
        self.assertEqual(result["cloud"]["reason"], "remote_main_missing_or_invalid")
        self.assertEqual(result["cloud"]["status"], "unknown")

    def test_no_probe_is_not_inferred_from_installation_or_skill(self):
        result = self.check()
        self.assertEqual(result["session"]["status"], "unknown")
        self.assertIn("会话加载未确认", status.render_text(result))

    def test_wrong_session_or_incomplete_evidence_unknown(self):
        for changes in ({"session_id": "other"}, {"kind": "file_exists"},
                        {"loaded_sha256": None}, {"evidence": {}}, {"observed_at": "bad date"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(**self.session(**changes))["session"]["status"], "unknown")

    def test_old_session_commit_and_version_are_separate(self):
        result = self.check(**self.session(source_commit=B, manifest_version="0.0.9"))
        self.assertEqual(result["session"]["status"], "inconsistent")
        self.assertEqual(result["session"]["source_commit"], B)
        self.assertEqual(result["session"]["manifest_version"], "0.0.9")

    def test_read_only_all_fixture_protected_bytes_identical(self):
        def snapshot():
            return {str(path.relative_to(self.base)): package.digest(path.read_bytes())
                    for path in self.base.rglob("*") if path.is_file()}
        before = snapshot()
        self.check(**self.session())
        self.check(offline=True)
        self.assertEqual(snapshot(), before)

    def test_binding_does_not_select_or_execute_code(self):
        self.binding.update(script="evil.py", actor_id="pretend", project_root="elsewhere")
        self.assertEqual(self.check()["installation"]["status"], "consistent")
        self.assertEqual(self.check()["session"]["status"], "unknown")

    def test_unsafe_binding_and_remote_helper_are_unknown(self):
        self.binding["files"]["../escape"] = "a" * 64
        self.assertEqual(self.check()["installation"]["status"], "unknown")
        with patch.object(status, "git", return_value=("ext::evil-command", None)) as query:
            result = status.check_status(self.project, offline=False)
        self.assertEqual(result["cloud"]["reason"], "unsupported_remote_transport")
        self.assertEqual(query.call_count, 2)

    def test_installed_git_head_different_from_trusted_binding(self):
        (self.root / ".git").mkdir()
        original = self.fake_git
        def other_head(project, *args, **kwargs):
            return (B, None) if args[-1] == "HEAD" else original(project, *args, **kwargs)
        with patch.object(status, "git", side_effect=other_head):
            result = status.check_status(self.project, self.binding)
        self.assertEqual(result["installation"]["commit_binding"]["status"], "inconsistent")

    def test_inventory_limit_unknown(self):
        with patch.object(status, "MAX_BYTES", 1):
            result = self.check()
        self.assertEqual(result["installation"]["status"], "unknown")

    def test_link_in_installation_unknown_without_following(self):
        with patch.object(package, "source_path", side_effect=ValueError("symlink")):
            self.assertEqual(self.check()["installation"]["status"], "unknown")

    def test_deep_install_root(self):
        import shutil
        deep = filesystem(self.base / ("deep-" * 48) / "installed")
        shutil.copytree(self.root, deep)
        self.binding["source_root"] = str(deep)
        self.assertEqual(self.check()["installation"]["status"], "consistent")

    def test_timeout_and_transport_errors_never_echo_credentials(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("token=secret", 1)):
            self.assertEqual(status.git(self.project, "ls-remote", timeout=1), (None, "timeout"))
        raw = subprocess.CompletedProcess([], 128, b"", b"fatal https://user:private@host/repo?token=secret authentication")
        with patch.object(subprocess, "run", return_value=raw) as call:
            output, error = status.git(self.project, "ls-remote", timeout=1)
        self.assertEqual((output, error), (None, "authentication"))
        self.assertEqual(call.call_args.kwargs["timeout"], 1)
        self.assertEqual(call.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(call.call_args.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
        self.assertIn("credential.helper=", call.call_args.args[0])

    def test_redacts_remote_credentials_and_token(self):
        original = self.fake_git
        def credentials(project, *args, **kwargs):
            if args[0] == "config":
                return "https://user:credentialsecret@host/repo?private_token=supersecret&extra_key=anothersecret#fragmentsecret", None
            return original(project, *args, **kwargs)
        with patch.object(status, "git", side_effect=credentials):
            result = status.check_status(self.project, self.binding)
        text = json.dumps(result)
        self.assertNotIn("credentialsecret", text)
        self.assertNotIn("supersecret", text)
        self.assertNotIn("anothersecret", text)
        self.assertNotIn("fragmentsecret", text)
        self.assertIn("redacted", text)

    def test_new_entry_and_documentation_are_in_portable_archive(self):
        root = filesystem(Path(__file__).resolve().parents[1])
        archive = self.base / "candidate.zip"
        package.build(root, archive)
        _, files = package.inspect(archive)
        for name in ("tools/install_status.py", "docs/install-status.md", "tests/test_install_status.py"):
            self.assertEqual(files[name], (root / name).read_bytes())

    def test_cli_offline_json_and_bad_binding(self):
        script = Path(status.__file__)
        command = [sys.executable, "-X", "utf8", "-B", str(script), "--project", str(self.project),
                   "--installation-binding", str(self.base / "missing.json"), "--offline", "--format", "json"]
        raw = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(raw.returncode, 0, raw.stderr)
        result = json.loads(raw.stdout)
        self.assertEqual(result["installation"]["reason"], "installation_binding_unreadable")
        self.assertEqual(result["session"]["status"], "unknown")

    def test_cli_binding_provenance_and_offline_protected_tree(self):
        binding = self.base / "binding.json"
        binding.write_bytes(package.encoded(self.binding))
        before = {str(path): package.digest(path.read_bytes()) for path in self.base.rglob("*") if path.is_file()}
        command = [sys.executable, "-X", "utf8", "-B", status.__file__, "--project", str(self.project),
                   "--installation-binding", str(binding), "--offline", "--format", "json"]
        raw = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(raw.returncode, 0, raw.stderr)
        result = json.loads(raw.stdout)
        self.assertEqual(result["installation"]["status"], "consistent")
        self.assertEqual(result["installation"]["binding_evidence"]["sha256"], package.digest(binding.read_bytes()))
        after = {str(path): package.digest(path.read_bytes()) for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
