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
import test_read_only_completion as read_only_tests
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
        self.set_up_resources()

    def set_up_resources(self):
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

    def register(self, identity, path, core_kind="environment", resource_kind="temporary_directory", metadata=None, actor_id=None):
        self.runtime.register_resource("A", {"id": identity, "kind": core_kind, "path": str(path)},
                                       self.file(identity + "-registration"), actor_id or self.actor)
        spec = {"resource_id": identity, "project_id": self.runtime.project_id, "task_id": "A",
                "attempt_id": self.runtime._load()["tasks"]["A"].get("worker_run", {}).get("attempt_id", "original-run"),
                "kind": resource_kind, "path": str(path),
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
        result = self.launch_fixture("A", adapter, attempt_id="launch-1", admission=admission["admission"],
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


class ReadOnlyCombination(read_only_tests.ReadOnlyFixture):
    """Public completion plus real RESOURCE operations; host replies are fixtures."""

    set_up_resources = Combination.set_up_resources

    def setUp(self):
        super().setUp()
        self.set_up_resources()

    def register(self, identity, path, core_kind="environment", resource_kind="temporary_directory", metadata=None):
        return Combination.register(self, identity, path, core_kind, resource_kind, metadata, self.reviewer)

    def permit(self, task="A", attempt="attempt1", executor=None):
        spec = dict(request_id="request-" + attempt, project_id=self.runtime.project_id,
                    task_id=task, attempt_id=attempt, launch_identity=executor or "worker-A",
                    resource_class="agent_execution_slot")
        self.admission = self.bridge.admit(self.json(attempt + "-resource.json", spec))
        self.assertEqual(self.admission["status"], "granted")
        return self.admission["admission"]

    def complete_and_close(self):
        proof = self.proof()
        self.runtime.complete_read_only(proof, self.reviewer)
        self.preservation = self.preserve(proof)
        self.host_calls = []
        adapter = read_only_tests.HostAdapter("native_agent", ["close_agent"],
                    lambda *args: self.host_calls.append(args) or {"closed": True}, self.reviewer)
        self.runtime.lifecycle("close", "worker-A", "A", "readonly-close", 1, adapter,
                               preservation_ref=self.preservation, authorized=True)

    def cleanup(self, operation):
        return self.runtime.cleanup_task("A", operation, self.preservation, self.bridge.cleanup,
                                         self.reviewer, authorized=True)

    def test_current_read_only_completion_releases_actual_quota_and_cleans_owned_database(self):
        self.delivered()
        queued = dict(request_id="next-task", project_id="other", task_id="B", attempt_id="B-1",
                      launch_identity="worker-B", resource_class="agent_execution_slot")
        self.assertEqual(self.controller.capacity_acquire(self.json("next.json", queued)["path"])["status"], "queued")
        self.complete_and_close()
        task = self.runtime._load()["tasks"]["A"]
        self.assertEqual(task["phase"], "completed")
        self.assertIsNone(task["mr"])
        self.assertIsNone(task["merge"])
        released = self.bridge.release_agent_quota("readonly-close", self.reviewer)
        self.assertEqual(released["newly_granted"], ["next-task"])
        self.assertFalse(released["process_memory_or_acceptance_storage_released"])
        database = self.managed / "readonly.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE findings(id INTEGER)")
            connection.commit()
        self.register("readonly-db", database, "database", "temporary_database")
        self.assertEqual(self.cleanup("readonly-cleanup")["status"], "cleanup_completed")
        self.assertFalse(database.exists())
        self.assertEqual(len(self.host_calls), 1)
        self.assertTrue(self.bridge.release_agent_quota("readonly-close", self.reviewer)["duplicate"])

    def test_user_acceptance_protects_actual_resources_and_slot_until_current_acceptance(self):
        self.delivered("user_required")
        path = self.managed / "waiting-user"
        path.mkdir()
        self.register("waiting", path)
        with self.assertRaises(ValueError):
            self.runtime.complete_read_only(self.proof(), self.reviewer)
        self.assertTrue(path.exists())
        self.assertEqual(self.runtime._load()["runtime"]["executors"]["worker-A"]["lease"], "open")
        self.accept()
        self.complete_and_close()
        self.assertEqual(self.cleanup("after-acceptance")["status"], "cleanup_completed")
        self.assertFalse(path.exists())

    def test_read_only_unknown_remove_reconciles_without_second_remove(self):
        self.delivered()
        self.complete_and_close()
        path = self.managed / "unknown-readonly"
        path.write_text("disposable findings", encoding="utf-8")
        calls = []
        class UnknownAfterRemove(TemporaryFilesystem):
            def remove(adapter, spec, identity):
                calls.append(spec["resource_id"])
                super().remove(spec, identity)
                return {"status": "unknown"}
        self.artifacts.adapters["temporary_file"] = UnknownAfterRemove()
        self.register("unknown-readonly", path, "artifact", "temporary_file")
        self.assertEqual(self.cleanup("readonly-unknown")["status"], "pending_cleanup")
        self.bridge.reconcile_cleanup("A", "unknown-readonly", self.reviewer)
        self.assertEqual(self.cleanup("readonly-reconciled")["status"], "cleanup_completed")
        self.assertEqual(calls, ["unknown-readonly"])

    def test_partial_cleanup_of_audited_input_preserves_completion_and_recovers_remaining(self):
        self.delivered(managed_input=True)
        self.complete_and_close()
        self.register("audited-input", self.input)
        database = self.managed / "held-readonly.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE findings(id INTEGER)")
            connection.commit()
        self.register("held-readonly", database, "database", "temporary_database")
        with closing(sqlite3.connect(database)) as held:
            held.execute("SELECT * FROM findings").fetchall()
            self.assertEqual(self.cleanup("readonly-partial")["status"], "pending_cleanup")
            self.assertFalse(self.input.exists())
            self.assertTrue(database.exists())
        self.assertEqual(self.cleanup("readonly-recovered")["status"], "cleanup_completed")
        self.assertFalse(database.exists())
        self.assertEqual(self.runtime._load()["tasks"]["A"]["phase"], "completed")
