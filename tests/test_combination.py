"""Canonical CORE + RESOURCE integration, with synthetic host/main facts.

Only disposable registered files/SQLite/orphan Git worktrees are really removed.
No real Codex actor, task merge, customer data or host loading is exercised.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills/isolated-parallel-development-coordination/scripts"))
import test_core_runtime as core_tests
from evidence import load, ref
from codex_parallel_coordination.bridge import ResourceBridge
from codex_parallel_coordination.resources import ArtifactCleanup, Controller
from codex_parallel_coordination.resources.cleanup import TemporaryFilesystem
from codex_parallel_coordination.worktrees import GitWorktrees


class Combination(core_tests.CoreFixture):
    close_fixture = core_tests.CoreRuntimeTests.close_fixture

    def setUp(self):
        if os.name != "nt":
            self.skipTest("Windows resource component")
        provenance = json.loads((ROOT / "provenance/resource.json").read_text(encoding="utf-8"))
        if provenance.get("delivery_revision", 0) < 2:
            self.skipTest("RESOURCE R1 has independent findings; wait for frozen author revision 2")
        super().setUp()
        self.managed = Path(self.runtime._load()["runtime"]["cleanup_root"])
        self.managed.mkdir(parents=True)
        other = self.root / "other-project"
        other.mkdir()
        authority = self.json("resource-authority.json", {"scope": "explicit_temporary_managed_resources",
                              "launch": True, "cleanup_artifacts": True})
        policy = {"authority": authority, "max_parallel": 1, "max_agent_slots": 1,
                  "physical_reserve": 1024 * 1024, "commit_reserve": 1024 * 1024,
                  "projects": {self.runtime.project_id: [str(self.runtime.project_root), str(self.managed)],
                               "other": str(other)}, "cleanup_roots": {self.runtime.project_id: [str(self.managed)]},
                  "executables": [sys.executable], "exclusive_keys": ["fixture"],
                  "phase_resources": {"self_test": ["fixture"]}}
        self.controller = Controller(self.root / "resource-control/resources.json")
        self.controller.initialize(self.json("resource-policy.json", policy)["path"])
        self.artifacts = ArtifactCleanup(self.controller)
        self.bridge = ResourceBridge(self.runtime, self.controller, self.artifacts, self.root / "external-receipts")

    def completed(self, merged=True, close=True):
        preservation = self.close_fixture(merged=merged)
        spec = {"request_id": "request-original-run", "project_id": self.runtime.project_id,
                "task_id": "A", "attempt_id": "original-run", "launch_identity": "worker-A",
                "resource_class": "agent_execution_slot"}
        quota = self.controller.capacity_acquire(self.json("quota.json", spec)["path"])
        # Link the synthetic completed-run fixture to the actual RESOURCE permit.
        self.reload()
        self.state["runtime"]["operations"]["original-run"].update(permit_id=quota["permit_id"],
                                                                  launch_identity="worker-A")
        self.store()
        self.preservation = preservation
        if close:
            adapter, self.host_calls = self.adapter({"close_agent": {"closed": True}})
            self.runtime.lifecycle("close", "worker-A", "A", "close-integration", 1, adapter,
                                   preservation_ref=preservation, authorized=True)
        return quota

    def register(self, identity, path, core_kind="environment", resource_kind="temporary_directory", metadata=None):
        self.runtime.register_resource("A", {"id": identity, "kind": core_kind, "path": str(path)},
                                       self.file(identity + "-registration"), self.actor)
        spec = {"resource_id": identity, "project_id": self.runtime.project_id, "task_id": "A",
                "attempt_id": "original-run", "kind": resource_kind, "path": str(path),
                "references": [], "protected": False, "metadata": metadata or {}}
        self.artifacts.register(spec)
        return spec

    def cleanup(self, operation):
        return self.runtime.cleanup_task("A", operation, self.preservation, self.bridge.cleanup,
                                         self.actor, authorized=True)

    def test_actual_resource_admission_feeds_core_launch_and_queue_never_starts_host(self):
        self.prepare()
        self.bind()
        spec = {"request_id": "launch-request", "project_id": self.runtime.project_id, "task_id": "A",
                "attempt_id": "launch-1", "launch_identity": "worker-A", "resource_class": "agent_execution_slot"}
        source = self.json("launch-spec.json", spec)
        admission = self.bridge.admit(source)
        self.assertEqual(admission, self.bridge.admit(source))
        adapter, calls = self.adapter({"send_input": {"submission_id": "fixture-submit"},
                                       "wait_agent": {"status": {"native-A": "running"}}})
        result = self.runtime.launch("A", adapter, attempt_id="launch-1", admission=admission["admission"],
                                     authorized=True, message="synthetic implementation", executor_id="worker-A")
        self.assertEqual(result["status"], "running")
        queued = dict(spec, request_id="queued-request", project_id="other", task_id="B",
                      attempt_id="queued-1", launch_identity="other-worker")
        before = len(calls)
        waiting = self.bridge.admit(self.json("queued-spec.json", queued))
        self.assertEqual(waiting["status"], "queued")
        self.assertIsNone(waiting["admission"])
        self.assertEqual(len(calls), before)

    def test_core_confirmed_close_releases_real_quota_and_fifo_without_claiming_RAM(self):
        self.completed(close=False)
        second = {"request_id": "other-request", "project_id": "other", "task_id": "B", "attempt_id": "other-1",
                  "launch_identity": "other-worker", "resource_class": "agent_execution_slot"}
        queued = self.controller.capacity_acquire(self.json("other-quota.json", second)["path"])
        self.assertEqual(queued["status"], "queued")
        with self.assertRaises((KeyError, ValueError)):
            self.bridge.release_agent_quota("no-confirmed-close", self.actor)
        adapter, calls = self.adapter({"close_agent": {"closed": True}})
        self.runtime.lifecycle("close", "worker-A", "A", "close-real-quota", 1, adapter,
                               preservation_ref=self.preservation, authorized=True)
        result = self.bridge.release_agent_quota("close-real-quota", self.actor)
        self.assertEqual(result["newly_granted"], ["other-request"])
        self.assertFalse(result["process_memory_or_acceptance_storage_released"])
        self.assertTrue(self.bridge.release_agent_quota("close-real-quota", self.actor)["duplicate"])
        self.assertEqual(len(calls), 1)

    def test_premerge_refuses_every_cleanup_and_keeps_actor_context(self):
        self.completed(merged=False, close=False)
        path = self.managed / "premerge"
        path.mkdir()
        self.register("premerge", path)
        with self.assertRaises(ValueError):
            self.cleanup("forbidden")
        self.assertTrue(path.exists())
        self.assertEqual(self.runtime._load()["runtime"]["executors"]["worker-A"]["lease"], "open")

    def test_core_guard_cleans_real_registered_orphan_worktree_and_file(self):
        self.completed()
        repository = self.root / "git-repository"
        repository.mkdir()
        subprocess.run(["git", "-C", str(repository), "init", "--initial-branch=main"], check=True, capture_output=True)
        tree = self.managed / "worktree"
        subprocess.run(["git", "-C", str(repository), "worktree", "add", "--orphan", "-b", "codex/fixture", str(tree)],
                       check=True, capture_output=True)
        self.artifacts.adapters["git_worktree"] = GitWorktrees(repository, self.managed)
        self.register("checkout", tree, "worktree", "git_worktree",
                      {"repository_root": str(repository), "expected_head": "0" * 40})
        database = self.managed / "temporary.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE fixture(id INTEGER)")
            connection.commit()
        self.register("database", database, "database", "temporary_database")
        result = self.cleanup("combined-cleanup")
        self.assertEqual(result["status"], "cleanup_completed")
        self.assertFalse(tree.exists())
        self.assertFalse(database.exists())
        self.assertTrue((repository / ".git").exists())
        self.assertEqual(self.runtime._load()["tasks"]["A"]["phase"], "completed")
        self.assertEqual(len(self.host_calls), 1)

    def test_unknown_delete_is_observed_and_never_repeated_across_components(self):
        self.completed()
        path = self.managed / "unknown-file"
        path.write_bytes(b"synthetic temporary payload")
        calls = []
        class UnknownAfterRemove(TemporaryFilesystem):
            def remove(adapter, spec, identity):
                calls.append(spec["resource_id"])
                super().remove(spec, identity)
                return {"status": "unknown"}
        self.artifacts.adapters["temporary_file"] = UnknownAfterRemove()
        self.register("unknown", path, "artifact", "temporary_file")
        first = self.cleanup("uncertain")
        self.assertEqual(first["status"], "pending_cleanup")
        self.bridge.reconcile_cleanup("A", "unknown", self.actor)
        final = self.cleanup("reconciled")
        self.assertEqual(final["status"], "cleanup_completed")
        self.assertEqual(calls, ["unknown"])

    def test_known_open_database_failure_keeps_task_then_retries_new_authorized_round(self):
        self.completed()
        path = self.managed / "held.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE fixture(id INTEGER)")
            connection.commit()
        self.register("held", path, "database", "temporary_database")
        with closing(sqlite3.connect(path)) as held:
            held.execute("SELECT * FROM fixture").fetchall()
            first = self.cleanup("database-held")
            self.assertEqual(first["status"], "pending_cleanup")
            self.assertTrue(path.exists())
        final = self.cleanup("database-released")
        self.assertEqual(final["status"], "cleanup_completed")
        self.assertEqual(self.runtime._load()["tasks"]["A"]["phase"], "completed")

    def test_protected_resource_child_cannot_be_removed_by_completed_parent_task(self):
        self.completed()
        parent = self.managed / "parent"
        parent.mkdir()
        child = parent / "shared.txt"
        child.write_bytes(b"belongs to another active task")
        self.artifacts.register({"resource_id": "protected-child", "project_id": self.runtime.project_id,
                                 "task_id": "B", "attempt_id": "other-attempt", "kind": "temporary_file",
                                 "path": str(child), "references": ["B"], "protected": True})
        with self.assertRaises(ValueError):
            self.register("parent", parent)
        result = self.cleanup("must-preserve-child")
        self.assertEqual(result["status"], "pending_cleanup")
        self.assertEqual(child.read_bytes(), b"belongs to another active task")
