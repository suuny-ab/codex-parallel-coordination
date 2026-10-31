"""Behavioral tests in temporary directories; no project, network or Codex writes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from coordination_v1 import (artifact, checkpoint, digest, load, new_state, next_item,
                          transition, write_atomic)
from stop_adapter import response
from preservation_check import verify_bundle


SCRIPT = Path(__file__).with_name("coordination_v1.py")
ADAPTER = Path(__file__).with_name("stop_adapter.py")


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = new_state("original-main")

    def apply(self, **op):
        self.state, result = transition(self.state, op)
        return result

    def ref(self, name, text="verified fixture"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return {"path": str(path), "sha256": digest(path)}

    def register(self, task):
        self.apply(op="register", task_id=task, owner_agent_id="worker-" + task,
                   worktree=str(self.root / task), branch="isolated/" + task,
                   resources=["data:" + task])

    def completion(self, task, revision=1, change=None):
        if task not in self.state["tasks"]:
            self.register(task)
        report = {"summary": "审查 " + task,
                  "source": [self.ref(task + str(revision) + ".py", "print('fixture')")],
                  "evidence": [self.ref(task + str(revision) + ".log", "checks passed")],
                  "self_test": {"status": "passed", "command": "fixture verify"},
                  "entry": {"kind": "file", "target": self.ref(task + ".html")["path"]},
                  "steps": ["Open entry", "Inspect the displayed result"],
                  "expected": "The fixture result is visible"}
        if change:
            change(report)
        ref = self.ref(task + str(revision) + ".json", json.dumps(report, ensure_ascii=False))
        return dict(op="event", task_id=task, delivery_revision=revision, kind="completed",
                    event_id=f"{task}:{revision}:completed", report=ref)

    def human(self, item="A", kind="discussion", unblocks=False):
        value = dict(id=item, kind=kind, title=item, prompt="Resolve " + item, unblocks=unblocks)
        if kind == "decision":
            value["options"] = ["Proceed", "Revise scope"]
        self.apply(op="enqueue", item=value)

    def open_human(self, item="A"):
        self.human(item)
        self.apply(op="activate", item_id=item)

    def close(self, item, outcome="resolved"):
        return self.apply(op="close", item_id=item, outcome=outcome,
                          user_reference="human response in main conversation")

    def cli(self, state_path, *args, input_text=None, script=SCRIPT):
        return subprocess.run([sys.executable, "-B", str(script), "--state", str(state_path), *args],
                              input=input_text, capture_output=True, encoding="utf-8")

    def test_parallel_delivery_duplicates_are_idempotent_and_internal(self):
        events = [self.completion("B"), self.completion("C")]
        for event in events:
            self.assertIsNone(self.apply(**event)["next"])
        first = copy.deepcopy(self.state)
        for event in reversed(events):
            self.assertEqual(self.apply(**event)["receipt"], "duplicate")
        self.assertEqual(self.state, first)
        self.assertEqual(len(self.state["items"]), 2)

    def test_B_C_do_not_interrupt_A_and_A_close_recommends_only_B(self):
        self.open_human()
        self.apply(**self.completion("B"))
        self.apply(**self.completion("C"))
        self.assertEqual(self.state["active_human_item"], "A")
        self.assertIsNone(next_item(self.state))
        result = self.close("A")
        self.assertEqual(result["next"]["id"], "B:1:completed")
        self.assertIsNone(self.state["active_human_item"])
        self.apply(op="activate", item_id=result["next"]["id"])
        self.assertIsNone(next_item(self.state))
        self.assertEqual(self.state["items"]["C:1:completed"]["status"], "queued")

    def test_ready_flag_cannot_replace_real_artifacts_entry_steps_or_test(self):
        changes = [lambda r: r.pop("source"), lambda r: r.pop("evidence"),
                   lambda r: r.pop("entry"), lambda r: r.update(steps=[]),
                   lambda r: r.update(expected=""),
                   lambda r: r["self_test"].update(status="failed"),
                   lambda r: r["entry"].update(target=str(self.root / "missing.html"))]
        for number, change in enumerate(changes):
            with self.subTest(number=number):
                event = self.completion("bad" + str(number), change=change)
                self.apply(**event)
        check = checkpoint(self.state)
        self.assertIsNone(check["next"])
        self.assertEqual(len(check["held"]), len(changes))
        self.assertEqual(check["action"], "wait_internal")

    def test_end_without_active_refuses_silent_close_when_work_is_eligible(self):
        self.apply(**self.completion("B"))
        check = checkpoint(self.state)
        self.assertFalse(check["allow_end"])
        self.assertEqual(check["action"], "present_next")
        self.assertEqual(check["next"]["id"], "B:1:completed")

    def test_no_pending_allows_end_and_running_worker_allows_internal_wait(self):
        self.assertEqual(checkpoint(self.state)["action"], "idle")
        self.assertTrue(checkpoint(self.state)["allow_end"])
        self.register("B")
        check = checkpoint(self.state)
        self.assertEqual(check["waiting_tasks"], {"B": "running"})
        self.assertTrue(check["allow_end"])

    def test_wait_for_user_retains_focus_without_blocking_worker_completion(self):
        self.open_human()
        self.apply(op="wait", item_id="A", prompt="Choose the agreed scope")
        self.apply(**self.completion("B"))
        check = checkpoint(self.state)
        self.assertTrue(check["allow_end"])
        self.assertEqual(check["action"], "wait_user")
        self.assertEqual(check["active_human_item"], "A")
        self.assertIsNone(check["next"])
        self.assertEqual(self.state["tasks"]["B"]["implementation"], "completed")

    def test_human_answer_does_not_automatically_resume_blocked_development(self):
        self.register("B")
        report = self.ref("blocked.json", "{}")
        self.apply(op="event", event_id="B:1:blocked", task_id="B", delivery_revision=1,
                   kind="blocked", report=report,
                   decision={"title": "Scope", "prompt": "Choose scope", "options": ["One", "Two"]})
        self.apply(op="activate", item_id="B:1:blocked")
        self.close("B:1:blocked", "answered")
        self.assertEqual(self.state["tasks"]["B"]["implementation"], "blocked")
        self.apply(**self.completion("B", 2))
        self.assertEqual(self.state["tasks"]["B"]["implementation"], "completed")

    def test_priority_unblocking_or_acceptance_before_discussion_then_fifo(self):
        self.human("older-discussion")
        self.human("scope", "decision", True)
        self.apply(**self.completion("B"))
        self.assertEqual(next_item(self.state)["id"], "scope")
        self.apply(op="activate", item_id="scope")
        self.assertEqual(self.close("scope", "answered")["next"]["id"], "B:1:completed")

    def test_no_timeout_switch_only_by_explicit_user_direction(self):
        self.open_human()
        self.human("B")
        before = copy.deepcopy(self.state)
        for op in (dict(op="activate", item_id="B"), dict(op="switch", item_id="B")):
            with self.assertRaises(ValueError):
                transition(self.state, op)
        self.assertEqual(before, self.state)
        self.apply(op="switch", item_id="B", user_reference="human explicitly selected B")
        self.assertEqual(self.state["active_human_item"], "B")
        self.assertEqual(self.state["items"]["A"]["status"], "queued")

    def test_acceptance_and_technical_stages_cannot_expand_release_permission(self):
        self.apply(**self.completion("B"))
        task = self.state["tasks"]["B"]
        self.assertEqual([task[k] for k in ("self_test", "integration", "user_acceptance", "release")],
                         ["passed", "pending", "pending", "pending"])
        self.apply(op="activate", item_id="B:1:completed")
        with self.assertRaises(ValueError):
            transition(self.state, dict(op="close", item_id="B:1:completed", outcome="accepted"))
        self.close("B:1:completed", "accepted")
        self.assertEqual(self.state["tasks"]["B"]["release_authorization"], "pending")
        with self.assertRaises(ValueError):
            transition(self.state, dict(op="stage", task_id="B", delivery_revision=1,
                                       stage="release", status="passed", evidence=self.ref("release.log")))

    def test_conflicting_duplicate_and_out_of_order_event_do_not_revert_delivery(self):
        old = self.completion("B")
        self.apply(**self.completion("B", 2))
        self.apply(**old)
        self.assertEqual(self.state["tasks"]["B"]["delivery_revision"], 2)
        self.assertEqual(self.state["receipts"][old["event_id"]]["status"], "stale")
        conflict = dict(old, report=self.ref("other.json", "{}"))
        with self.assertRaises(ValueError):
            transition(self.state, conflict)

    def test_deleted_or_changed_source_and_changed_report_revoke_readiness(self):
        event = self.completion("B")
        self.apply(**event)
        report = load(event["report"]["path"])
        Path(report["source"][0]["path"]).unlink()
        self.assertIsNone(next_item(self.state))
        Path(report["source"][0]["path"]).write_text("changed", encoding="utf-8")
        self.assertIsNone(next_item(self.state))
        Path(event["report"]["path"]).write_text("{}", encoding="utf-8")
        self.assertIsNone(next_item(self.state))

    def test_old_active_acceptance_is_not_interrupted_but_cannot_accept_new_revision(self):
        self.apply(**self.completion("B"))
        self.apply(op="activate", item_id="B:1:completed")
        self.apply(**self.completion("B", 2))
        self.assertEqual(self.state["active_human_item"], "B:1:completed")
        with self.assertRaises(ValueError):
            self.close("B:1:completed", "accepted")
        result = self.close("B:1:completed", "withdrawn")
        self.assertEqual(result["next"]["id"], "B:2:completed")

    def test_independent_resources_and_owners_are_checked(self):
        self.register("B")
        base = dict(op="register", task_id="C", owner_agent_id="worker-C",
                    worktree=str(self.root / "C"), branch="isolated/C", resources=["data:C"])
        for field, value in (("owner_agent_id", "worker-B"), ("branch", "isolated/B"),
                             ("worktree", str(self.root / "B" / "nested")),
                             ("resources", ["data:B"])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                transition(self.state, dict(base, **{field: value}))

    def test_restart_in_fresh_process_retains_wait_focus_queue_and_dedup(self):
        self.open_human()
        self.apply(op="wait", item_id="A", prompt="用户待答")
        event = self.completion("B")
        self.apply(**event)
        path = self.root / "state.json"
        write_atomic(path, self.state)
        result = self.cli(path, "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["active_human_item"], "A")
        op = self.root / "event.json"
        write_atomic(op, event)
        result = self.cli(path, "apply", str(op))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["receipt"], "duplicate")
        self.assertEqual(load(path), self.state)

    def test_cli_check_exit_codes_and_failed_mutation_preserve_bytes(self):
        path = self.root / "state.json"
        write_atomic(path, self.state)
        self.assertEqual(self.cli(path, "check").returncode, 0)
        self.apply(**self.completion("B"))
        write_atomic(path, self.state)
        before = path.read_bytes()
        self.assertEqual(self.cli(path, "check").returncode, 2)
        op = self.root / "bad.json"
        write_atomic(op, {"op": "close", "item_id": "missing", "outcome": "accepted"})
        self.assertEqual(self.cli(path, "apply", str(op)).returncode, 1)
        self.assertEqual(path.read_bytes(), before)

    def test_busy_writer_lock_refuses_mutation(self):
        path = self.root / "state.json"
        write_atomic(path, self.state)
        path.with_suffix(".json.lock").write_text("busy", encoding="utf-8")
        op = self.root / "op.json"
        write_atomic(op, {"op": "register", "task_id": "X"})
        before = path.read_bytes()
        self.assertEqual(self.cli(path, "apply", str(op)).returncode, 1)
        self.assertEqual(before, path.read_bytes())

    def test_hook_fixture_continues_only_main_and_bounds_recursion(self):
        self.apply(**self.completion("B"))
        event = {"hook_event_name": "Stop", "session_id": "original-main", "stop_hook_active": False}
        self.assertEqual(response(self.state, event)["decision"], "block")
        self.assertEqual(response(self.state, dict(event, session_id="worker")), {})
        self.assertEqual(response(self.state, dict(event, hook_event_name="SubagentStop")), {})
        self.assertNotIn("decision", response(self.state, dict(event, stop_hook_active=True)))

    def test_hook_fixture_allows_human_wait_and_idle(self):
        event = {"hook_event_name": "Stop", "session_id": "original-main"}
        self.assertEqual(response(self.state, event), {})
        self.open_human()
        self.apply(op="wait", item_id="A", prompt="Answer A")
        self.apply(**self.completion("B"))
        self.assertEqual(response(self.state, event), {})

    def test_hook_subprocess_is_read_only_and_bad_state_is_disclosed(self):
        path = self.root / "state.json"
        self.apply(**self.completion("B"))
        write_atomic(path, self.state)
        before = path.read_bytes()
        fixture = json.dumps({"hook_event_name": "Stop", "session_id": "original-main"})
        result = self.cli(path, input_text=fixture, script=ADAPTER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        self.assertEqual(path.read_bytes(), before)
        path.write_text("corrupt", encoding="utf-8")
        result = self.cli(path, input_text=fixture, script=ADAPTER)
        self.assertIn("systemMessage", json.loads(result.stdout))
        self.assertNotIn("decision", json.loads(result.stdout))

    def test_url_requires_bound_probe_evidence(self):
        self.apply(**self.completion("B", change=lambda r: r.update(
            entry={"kind": "url", "target": "https://example.invalid/review"})))
        self.assertIsNone(next_item(self.state))
        self.apply(**self.completion("B", 2, change=lambda r: r.update(entry={
            "kind": "url", "target": "https://example.invalid/review",
            "probe_evidence": self.ref("probe.log", "fixture probe only")})))
        self.assertEqual(next_item(self.state)["id"], "B:2:completed")

    def test_release_authorization_is_separate_bound_to_revision_and_does_not_publish(self):
        self.apply(**self.completion("B"))
        self.apply(op="enqueue", item=dict(id="release-B", kind="release", task_id="B",
                   title="Release B", prompt="Approve the concrete release", options=["Approve", "Defer"]))
        self.apply(op="activate", item_id="B:1:completed")
        self.close("B:1:completed", "accepted")
        self.assertIsNone(next_item(self.state))  # integration not ready
        self.apply(op="stage", task_id="B", delivery_revision=1, stage="integration",
                   status="passed", evidence=self.ref("integration.log"))
        self.apply(op="activate", item_id="release-B")
        self.close("release-B", "authorized")
        self.assertEqual(self.state["tasks"]["B"]["release"], "pending")
        self.apply(**self.completion("B", 2))
        self.assertEqual(self.state["tasks"]["B"]["release_authorization"], "pending")

    def test_source_drift_prevents_recording_a_passed_integration(self):
        event = self.completion("B")
        self.apply(**event)
        report = load(event["report"]["path"])
        Path(report["source"][0]["path"]).write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.apply(op="stage", task_id="B", delivery_revision=1, stage="integration",
                       status="passed", evidence=self.ref("integration.log"))

    def test_stale_release_item_cannot_authorize_after_switching_to_accept_new_delivery(self):
        self.apply(**self.completion("B"))
        self.apply(op="activate", item_id="B:1:completed")
        self.close("B:1:completed", "accepted")
        self.apply(op="stage", task_id="B", delivery_revision=1, stage="integration",
                   status="passed", evidence=self.ref("integration.log"))
        self.apply(op="enqueue", item=dict(id="old-release", kind="release", task_id="B",
                   title="Release", prompt="Approve release", options=["Approve", "Defer"]))
        self.apply(op="activate", item_id="old-release")
        self.apply(**self.completion("B", 2))
        self.apply(op="switch", item_id="B:2:completed", user_reference="human switched to new version")
        self.close("B:2:completed", "accepted")
        self.apply(op="stage", task_id="B", delivery_revision=2, stage="integration",
                   status="passed", evidence=self.ref("integration2.log"))
        self.assertIsNone(next_item(self.state))
        with self.assertRaises(ValueError):
            self.apply(op="activate", item_id="old-release")

    def backup_fixture(self):
        managed = self.root / "managed"
        backup = self.root / "external-backup"
        managed.mkdir()
        backup.mkdir()
        items = []
        for kind in ("change", "patch", "evidence", "ignored_data"):
            source, target = managed / (kind + ".txt"), backup / (kind + ".txt")
            source.write_text(kind, encoding="utf-8")
            target.write_bytes(source.read_bytes())
            items.append(dict(source=str(source), copy=target.name, kind=kind, sha256=digest(source)))
        return dict(backup_root=str(backup), managed_roots=[str(managed)],
                    base="verified baseline", scope="explicit fixture inventory",
                    credential_review="completed", ignored_data="included", files=items)

    def test_preservation_verifies_external_copies_and_necessary_ignored_files(self):
        manifest = self.backup_fixture()
        self.assertEqual(verify_bundle(manifest)["verified_files"], 4)
        manifest["files"] = [x for x in manifest["files"] if x["kind"] != "ignored_data"]
        with self.assertRaises(ValueError):
            verify_bundle(manifest)

    def test_preservation_refuses_internal_backup_tampering_and_missing_source(self):
        manifest = self.backup_fixture()
        with self.assertRaises(ValueError):
            verify_bundle(dict(manifest, managed_roots=[str(self.root)]))
        target = Path(manifest["backup_root"]) / manifest["files"][0]["copy"]
        original = target.read_bytes()
        target.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_bundle(manifest)
        target.write_bytes(original)
        Path(manifest["files"][0]["source"]).unlink()
        with self.assertRaises(ValueError):
            verify_bundle(manifest)

    def test_preservation_refuses_path_escape_and_incomplete_credential_review(self):
        manifest = self.backup_fixture()
        with self.assertRaises(ValueError):
            verify_bundle(dict(manifest, credential_review="pending"))
        manifest["files"][0]["copy"] = "../managed/change.txt"
        with self.assertRaises(ValueError):
            verify_bundle(manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
