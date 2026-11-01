"""File lifecycle tests only: these do not load Codex or create an executor."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import package
import profile


class Packaging(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="coordination-", dir=profile.filesystem(Path(tempfile.gettempdir())))
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        root = Path(__file__).resolve().parents[1]
        for name in package.FILES:
            dest = self.source / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((root / name).read_bytes())
        skill = self.source / "skills/test/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: test\ndescription: File fixture only.\n---\n", encoding="utf-8")
        self.archive = self.base / "candidate.zip"
        self.first = package.build(self.source, self.archive)
        self.profile = self.base / "profile"
        profile.action("init", self.profile)

    def junction(self, alias, target):
        self.assertTrue(alias.parent.is_relative_to(self.base))
        def quote(path):
            return "'" + str(profile.ordinary(path)).replace("'", "''") + "'"
        script = "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path " + quote(alias) + " -Target " + quote(target) + " | Out-Null"
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script], check=True, capture_output=True)

    def test_deterministic_and_excludes_local_secrets(self):
        (self.source / ".env").write_text("synthetic-not-a-secret", encoding="utf-8")
        another = package.build(self.source, self.base / "again.zip")
        self.assertEqual(self.first["sha256"], another["sha256"])
        _, files = package.inspect(self.archive)
        self.assertNotIn(".env", files)

    def test_public_root_documents_and_license_are_distributed_unchanged(self):
        manifest, files = package.inspect(self.archive)
        root = Path(__file__).resolve().parents[1]
        for name in ("README.md", "LICENSE", "CONTRIBUTING.md", "CHANGELOG.md"):
            with self.subTest(name=name):
                expected = (root / name).read_bytes()
                self.assertEqual(files[name], expected)
                self.assertEqual(manifest["files"][name], package.digest(expected))

    def test_finite_dispatch_host_module_is_in_the_portable_archive(self):
        module = self.source / 'skills/test/dispatch_host.mjs'
        module.write_text('export const fixture = true;\n', encoding='utf-8')
        archive = self.base / 'with-host.zip'
        package.build(self.source, archive)
        _, files = package.inspect(archive)
        self.assertEqual(files['skills/test/dispatch_host.mjs'], module.read_bytes())

    def test_skill_runtime_ledger_and_cache_are_excluded_from_archive(self):
        for directory in ["Evidence", "RUNTIME", "profiles", "__pycache__", "Cache"]:
            data = self.source / "skills/test" / directory / "state.json"
            data.parent.mkdir(parents=True)
            data.write_bytes(b'{"synthetic_private_runtime_state":true}')
        result = package.build(self.source, self.base / "without-runtime.zip")
        self.assertEqual(self.first["sha256"], result["sha256"])

    @unittest.skipUnless(os.name == "nt", "Windows source junction boundary")
    def test_source_parent_junction_cannot_relabel_excluded_local_data(self):
        local = self.source / "local"
        local.mkdir()
        (local / "private.json").write_bytes(b'{"synthetic_private_runtime_state":true}')
        alias = self.source / "skills/test/assets-alias"
        self.junction(alias, local)
        output = self.base / "must-not-package.zip"
        try:
            with self.assertRaises(ValueError):
                package.build(self.source, output)
            self.assertFalse(output.exists())
        finally:
            if alias.is_junction():
                self.assertTrue(alias.parent.is_relative_to(self.source))
                os.rmdir(alias)

    def test_install_upgrade_rollback_uninstall_preserve_user_state(self):
        user = self.profile / "user.txt"
        ledger = self.profile / "state/project-ledger.json"
        ledger.parent.mkdir()
        user.write_bytes(b"owned by user")
        ledger.write_bytes(b"synthetic task state")
        before = {user: user.read_bytes(), ledger: ledger.read_bytes()}
        profile.action("install", self.profile, self.archive)
        (self.source / "README.md").write_text("Changed local candidate\n", encoding="utf-8")
        upgraded = self.base / "upgrade.zip"
        package.build(self.source, upgraded)
        result = profile.action("install", self.profile, upgraded)
        self.assertEqual((result["active"], result["revisions"]), (1, 2))
        profile.action("rollback", self.profile)
        self.assertEqual(profile.load(self.profile)["active"], 0)
        profile.action("uninstall", self.profile)
        self.assertIsNone(profile.load(self.profile)["active"])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.profile / "config.toml").exists())
        self.assertFalse((self.profile / "hooks.json").exists())

    def test_unknown_metadata_is_preserved(self):
        state = self.profile / profile.STATE
        state.write_bytes(b'{"owner":"someone-else"}')
        before = state.read_bytes()
        with self.assertRaises(ValueError):
            profile.action("init", self.profile)
        self.assertEqual(state.read_bytes(), before)

    def test_changed_owned_files_refuse_upgrade_without_overwrite(self):
        profile.action("install", self.profile, self.archive)
        state = profile.load(self.profile)
        installed = self.profile / state["revisions"][0]["directory"] / "README.md"
        installed.write_bytes(b"user edit")
        before = (self.profile / profile.STATE).read_bytes()
        with self.assertRaises(ValueError):
            profile.action("install", self.profile, self.archive)
        self.assertEqual(installed.read_bytes(), b"user edit")
        self.assertEqual((self.profile / profile.STATE).read_bytes(), before)

    def test_duplicate_or_traversal_archive_refused_before_install(self):
        before = (self.profile / profile.STATE).read_bytes()
        bad = self.base / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("../escaped.txt", "no")
        with self.assertRaises(ValueError):
            profile.action("install", self.profile, bad)
        self.assertFalse((self.base / "escaped.txt").exists())
        self.assertEqual((self.profile / profile.STATE).read_bytes(), before)

    def test_corrupt_payload_preserves_active_revision(self):
        profile.action("install", self.profile, self.archive)
        before = (self.profile / profile.STATE).read_bytes()
        bad = self.base / "corrupt.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(bad, "w") as dest:
            for name in source.namelist():
                dest.writestr(name, b"tampered" if name == "README.md" else source.read(name))
        with self.assertRaises(ValueError):
            profile.action("install", self.profile, bad)
        self.assertEqual((self.profile / profile.STATE).read_bytes(), before)

    def test_live_home_and_relative_profile_refused(self):
        for path in [Path.home(), profile.filesystem(Path.home()), Path.home() / ".codex/test", Path("relative")]:
            with self.assertRaises(ValueError):
                profile.profile_path(path)

    def test_concurrent_mutation_refused(self):
        lock = self.profile / ".coordination-install.lock"
        lock.write_text("another owner", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            profile.action("install", self.profile, self.archive)
        self.assertEqual(lock.read_text(encoding="utf-8"), "another owner")

    def test_version_traversal_refused_without_new_files(self):
        for name in ["plugin.json", ".codex-plugin/plugin.json", "capabilities.json"]:
            path = self.source / name
            data = json.loads(path.read_bytes())
            data["version"] = "../../user-content/new-owned-escape"
            path.write_bytes(package.encoded(data))
        with self.assertRaises(ValueError):
            package.build(self.source, self.base / "unsafe.zip")
        self.assertFalse((self.profile / "user-content").exists())

    def test_manifest_matching_unsafe_version_cannot_install(self):
        manifest, files = package.inspect(self.archive)
        for name in ["plugin.json", ".codex-plugin/plugin.json", "capabilities.json"]:
            data = json.loads(files[name])
            data["version"] = "../../user-content/new-owned-escape"
            files[name] = package.encoded(data)
        manifest["version"] = "../../user-content/new-owned-escape"
        manifest["files"] = {name: package.digest(data) for name, data in files.items()}
        bad = self.base / "unsafe-version.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr(package.MANIFEST, package.encoded(manifest))
        before = (self.profile / profile.STATE).read_bytes()
        with self.assertRaises(ValueError):
            profile.action("install", self.profile, bad)
        self.assertEqual((self.profile / profile.STATE).read_bytes(), before)
        self.assertFalse((self.profile / "user-content").exists())
        self.assertFalse((self.profile / "plugins").exists())

    def test_windows_deep_install_paths(self):
        reference = self.source / "skills/test/references" / ("long-name-" * 10 + ".md")
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"deep portable file")
        archive = self.base / "deep.zip"
        package.build(self.source, archive)
        deep = self.profile / ("nested-profile-" * 8)
        profile.action("init", deep)
        result = profile.action("install", deep, archive)
        self.assertEqual(result["active"], 0)
        profile.action("uninstall", deep)

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_owned_directory_cannot_redirect_into_user_content(self):
        user = self.profile / "user-content"
        user.mkdir()
        sentinel = user / "keep.txt"
        sentinel.write_bytes(b"user-owned sentinel")
        junction = self.profile / "plugins" / package.NAME
        junction.parent.mkdir()
        self.junction(junction, user)
        try:
            self.assertTrue(junction.is_junction())
            with self.assertRaises(ValueError):
                profile.action("install", self.profile, self.archive)
            self.assertEqual({p.name for p in user.iterdir()}, {"keep.txt"})
            self.assertEqual(sentinel.read_bytes(), b"user-owned sentinel")
            self.assertIsNone(profile.load(self.profile)["active"])
        finally:
            # Remove only this verified reparse point, never traverse its target.
            if junction.is_junction():
                self.assertTrue(junction.parent.is_relative_to(self.profile))
                os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
