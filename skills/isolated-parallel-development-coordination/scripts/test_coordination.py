"""Behavioral v2 tests in disposable directories; no project or host mutation."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from coordination import (checkpoint, fingerprint, load, migrate, new_state, next_item, ref,
                          summary, transition, validate, write_atomic, writer_lock)
from evidence import pack, snapshot
from reconcile import closeout
from stop_adapter import response

SCRIPT = Path(__file__).with_name("coordination.py")


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        if sys.platform == 'win32':
            # Only the owned TEMP teardown uses an extended alias; test inputs stay ordinary.
            parent = Path(tempfile.gettempdir()).resolve()
            assert self.root != parent and self.root.is_relative_to(parent)
            value = str(self.root)
            self.temp.name = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
        self.state = new_state("main-session", "project-one")

    def apply(self, **op):
        self.state, result = transition(self.state, op)
        return result

    def file(self, name, value="fixture evidence"):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding="utf-8")
        return ref(p)

    def json(self, name, value):
        return self.file(name, json.dumps(value, ensure_ascii=False))

    def dispatch(self, name="A", **kw):
        work = self.root / name
        work.mkdir(exist_ok=True)
        (work / "feature.py").write_text("print('feature')\n", encoding="utf-8")
        data = dict(op="dispatch", task_id=name, owner_agent_id="worker-" + name,
                    worktree=str(work), branch="task/" + name, resources=["data:" + name],
                    scope="Implement requested change", acceptance=["observable result"],
                    user_reference="actual user start instruction", report_path=str(self.root / (name + ".report.json")))
        data.update(kw)
        self.apply(**data)

    def delivery(self, name="A", revision=1, quality="passed", test="passed"):
        if name not in self.state["tasks"]:
            self.dispatch(name)
        report = pack(dict(project_id=self.state["project"]["id"], task_id=name, delivery_revision=revision,
            summary="Review " + name, source_root=str(self.root / name), evidence=[self.file(name + ".log")],
            self_test={"status": test, "command": "actual fixture command"}, quality_review={"status": quality},
            entry={"kind": "file", "target": self.file(name + ".html")["path"]},
            steps=["Open file", "Inspect result"], expected="Requested behavior"))
        report_ref = self.json(name + f".{revision}.json", report)
        # Discovery path follows the current immutable report via explicit binding.
        self.apply(op="bind-report", task_id=name, path=report_ref["path"], reference="worker report location")
        return dict(project_id=self.state["project"]["id"], task_id=name, owner_agent_id="worker-" + name,
                    channel="native_agent", kind="completed", delivery_revision=revision,
                    event_id=f"{name}:{revision}:completed", report=report_ref)

    def receive(self, message, drain=True):
        result = self.apply(op="receive", receipt=message)
        if drain:
            self.apply(op="drain")
        return result

    def human(self, name, kind="discussion", unblocks=False):
        item = dict(id=name, kind=kind, title=name, prompt="Resolve " + name, unblocks=unblocks)
        if kind == "decision":
            item["options"] = ["One", "Two"]
        self.apply(op="enqueue", item=item)

    def close(self, name, outcome="resolved"):
        return self.apply(op="close", item_id=name, outcome=outcome, user_reference="human resolution")

    def cli(self, state, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), "--state", str(state), *args],
                              capture_output=True, encoding="utf-8", timeout=20)

    def observation(self, workers, messages=(), mode="fixture"):
        return dict(schema="coordination-closeout/1", project_id=self.state["project"]["id"],
            coordinator_id=self.state["coordinator_id"], based_on_revision=self.state["revision"],
            inbox={"mode": mode, "complete": mode != "unavailable", "evidence": self.file("inbox.log"),
                   "messages": list(messages)}, workers=workers)

    def audit(self, value):
        self.state, result = closeout(self.state, value, self.json("observation.json", value))
        return result

class LedgerTests(LedgerFixture):
    def test_two_projects_never_share_receipts_or_focus(self):
        event = self.delivery()
        other = new_state("main-other", "project-two")
        before = copy.deepcopy(self.state)
        self.receive(event)
        self.assertEqual(other["receipts"], {})
        candidate = copy.deepcopy(before)
        candidate["project"]["id"] = "project-two"
        with self.assertRaisesRegex(ValueError, "identity"):
            transition(candidate, {"op": "receive", "receipt": event})
        self.assertEqual(other["revision"], 0)

    def test_disorder_duplicates_stale_and_conflict_preserve_receipt_history(self):
        a1, b1, a2 = self.delivery(), self.delivery("B"), self.delivery(revision=2)
        self.receive(a2, False)
        self.receive(b1, False)
        self.receive(a1, False)
        self.assertEqual(checkpoint(self.state)["action"], "process_receipts")
        self.apply(op="drain")
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 2)
        self.assertEqual(self.state["receipts"][a1["event_id"]]["status"], "stale")
        before = copy.deepcopy(self.state)
        self.assertEqual(self.receive(a2, False)["receipt"], "duplicate")
        self.assertEqual(before, self.state)
        conflict = dict(a2, report=b1["report"])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.receive(conflict, False)
        self.assertEqual(before, self.state)

    def test_fifo_unblocking_and_acceptance_equal_user_priority_and_focus(self):
        self.human("normal")
        self.human("decision", "decision", True)
        self.receive(self.delivery())
        self.human("later", "decision", True)
        self.assertEqual(next_item(self.state)["id"], "decision")
        self.apply(op="priority", item_id="later", user_reference="user says do later first")
        self.apply(op="next")
        self.assertEqual(self.state["active_human_item"], "later")
        self.apply(op="priority", item_id="A:1:completed", user_reference="user priority")
        self.assertIsNone(next_item(self.state))
        self.apply(op="wait", item_id="later", prompt="Choose")
        self.assertTrue(checkpoint(self.state)["allow_end"])
        result = self.close("later", "answered")
        self.assertEqual(result["next"]["id"], "A:1:completed")
        self.apply(op="next")
        self.close("A:1:completed", "accepted")
        self.assertEqual(next_item(self.state)["id"], "decision")
        self.assertEqual(self.state["tasks"]["A"]["phase"], "awaiting_integration")

    def test_active_acceptance_freezes_new_revision_and_source_mutation_revokes(self):
        self.receive(self.delivery())
        self.apply(op="next")
        new = self.delivery(revision=2)
        self.receive(new)
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 1)
        self.assertEqual(self.state["receipts"][new["event_id"]]["status"], "held")
        self.assertEqual(self.state["active_human_item"], "A:1:completed")
        (self.root / "A" / "new.py").write_text("new source", encoding="utf-8")
        self.assertFalse(checkpoint(self.state)["allow_end"])
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.close("A:1:completed", "accepted")
        self.close("A:1:completed", "rejected")
        self.apply(op="drain")
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 2)
        self.assertIsNone(next_item(self.state))  # Revision2 source snapshot is also obsolete.
        self.receive(self.delivery(revision=3))
        self.assertEqual(next_item(self.state)["id"], "A:3:completed")
        self.assertTrue(self.state["tasks"]["A"]["history"])

    def test_missing_evidence_quality_pending_and_failed_tests_are_distinct(self):
        event = self.delivery(quality="pending")
        self.receive(event)
        t = self.state["tasks"]["A"]
        self.assertEqual((t["self_test"], t["quality_review"]), ("passed", "pending"))
        self.assertIn("business/quality", checkpoint(self.state)["held"][event["event_id"]][0])
        failed = self.delivery("B", test="failed")
        self.receive(failed)
        self.assertEqual(self.state["tasks"]["B"]["self_test"], "failed")
        missing = self.delivery("C")
        Path(missing["report"]["path"]).unlink()
        self.receive(missing)
        self.assertNotIn(missing["event_id"], self.state["receipts"])
        self.assertEqual(self.state["unverified_receipts"][fingerprint(missing)]["status"], "unverified")
        self.assertIsNone(next_item(self.state))

    def test_requirements_and_no_forged_completion_or_permission(self):
        self.apply(op="register", task_id="A", owner_agent_id="worker-A", worktree=str(self.root / "A"), branch="a")
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.apply(op="start", task_id="A", user_reference="start")
        self.apply(op="confirm", task_id="A", scope="scope", acceptance=["result"], user_reference="confirmed")
        self.assertEqual(self.state["tasks"]["A"]["phase"], "backlog")
        self.apply(op="start", task_id="A", user_reference="user start")
        with self.assertRaises(ValueError):
            self.apply(op="complete", task_id="A")
        self.assertFalse(any(v["allowed"] for v in self.state["tasks"]["A"]["authorization"].values()))

    def test_cli_recovery_corruption_and_lock_preserve_bytes(self):
        self.human("discussion")
        self.apply(op="next")
        self.apply(op="wait", item_id="discussion", prompt="Answer")
        path = self.root / "state.json"
        write_atomic(path, self.state)
        self.assertEqual(self.cli(path, "show").returncode, 0)
        loaded = json.loads(self.cli(path, "show").stdout)
        self.assertEqual(loaded, self.state)
        with writer_lock(path):
            blocked = self.cli(path, "drain")
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("never auto-steal", blocked.stderr)
        original = path.read_bytes()
        self.assertEqual(self.cli(path, "init", "--project", "other", "--coordinator", "other").returncode, 1)
        self.assertEqual(path.read_bytes(), original)
        path.write_text('{"schema":', encoding="utf-8")
        broken = path.read_bytes()
        self.assertEqual(self.cli(path, "drain").returncode, 1)
        self.assertEqual(self.cli(path, "check").returncode, 1)
        self.assertEqual(path.read_bytes(), broken)

    def test_closeout_detects_unreceived_final_report_when_ledger_check_would_wait(self):
        event = self.delivery()
        # A final report exists but no native message was consumed.
        self.assertTrue(checkpoint(self.state)["allow_end"])
        self.assertEqual(checkpoint(self.state)["scope"], "ledger_only")
        worker = dict(task_id="A", owner_agent_id="worker-A", status="completed", last_delivery_revision=1,
                      evidence=self.file("native-observation.log"))
        result = self.audit(self.observation([worker]))
        self.assertFalse(result["allow_end"])
        self.assertIn("unregistered final", str(result))
        worker["receipt"] = event
        result = self.audit(self.observation([worker]))
        self.assertEqual(result["action"], "present_next")
        self.assertEqual(result["next"]["id"], "A:1:completed")
        self.assertEqual(result["revision"], self.state["last_reconcile"]["revision"])
        self.assertEqual(self.state["tasks"]["A"]["phase"], "awaiting_acceptance")

    def test_closeout_requires_worker_coverage_native_capability_and_fresh_revision(self):
        self.dispatch()
        result = self.audit(self.observation([], mode="unavailable"))
        self.assertFalse(result["allow_end"])
        self.assertIn("coverage unverified", str(result))
        observation = self.observation([])
        self.human("new-message")
        with self.assertRaisesRegex(ValueError, "revision stale"):
            self.audit(observation)
        self.assertIsNone(self.state["last_reconcile"])

    def test_closeout_consumes_business_blocker_without_interrupting_active(self):
        self.dispatch()
        report = dict(project_id="project-one", task_id="A", delivery_revision=3, summary="Need meaning",
                      blocker={"reason": "Which business meaning applies?", "owner": "human", "category": "business"},
                      self_test={"status": "passed_for_independent_scope_only"})
        r = self.json("A.report.json", report)
        event = dict(project_id="project-one", task_id="A", owner_agent_id="worker-A", channel="native_agent",
                     kind="blocked", delivery_revision=3, event_id="A:3:blocked", report=r,
                     technical_review={"conclusion": "business_decision_required", "summary": "Compare source and population",
                         "report_sha256": r["sha256"], "evidence": [self.file("investigation.log")]},
                     decision={"title": "Confirm meaning", "prompt": "Which meaning?", "options": ["One", "Two"]})
        self.human("current")
        self.apply(op="next")
        self.apply(op="wait", item_id="current", prompt="Answer current")
        worker = dict(task_id="A", owner_agent_id="worker-A", status="completed", last_delivery_revision=3,
                      evidence=self.file("native.log"))
        result = self.audit(self.observation([worker], [event]))
        self.assertTrue(result["allow_end"])
        self.assertEqual(self.state["active_human_item"], "current")
        self.assertEqual(self.state["tasks"]["A"]["self_test"], "passed_for_independent_scope_only")
        self.assertEqual(self.close("current")["next"]["id"], "A:3:blocked")
        self.assertIsNone(self.state["last_reconcile"])

    def test_stop_v2_is_only_fixture_continuation_and_needs_reconcile(self):
        event = dict(hook_event_name="Stop", session_id="main-session")
        self.assertEqual(response(self.state, event)["decision"], "block")
        self.audit(self.observation([], mode="native_snapshot"))
        self.assertEqual(response(self.state, event), {})
        self.human("work")
        self.assertIn("systemMessage", response(self.state, dict(event, stop_hook_active=True)))
        self.assertEqual(response(self.state, dict(event, session_id="worker-A")), {})


class CapturedMigrationTests(unittest.TestCase):
    def test_real_v1_copy_preserves_outcomes_focus_receipts_and_restrictions(self):
        root = Path(__file__).resolve().parents[2]
        source = root.parent / "before" / "state-v1.json"
        if not source.exists():
            self.skipTest("External captured v1 not shipped with portable skill; use supplied migration fixture")
        original_bytes, old = source.read_bytes(), load(source)
        state = migrate(old, "legacy-JLJ-copy", ref(source))
        self.assertEqual(state["legacy"], old)
        self.assertEqual(state["active_human_item"], old["active_human_item"])
        if old["active_human_item"]:
            item = old["items"][old["active_human_item"]]
            if item["status"] not in ("active", "waiting_user"):
                self.assertTrue(state["items"][old["active_human_item"]]["migration_hold"])
        self.assertEqual(state["tasks"]["HISTORY"]["delivery_revision"], 2)
        self.assertEqual(state["tasks"]["HISTORY"]["user_acceptance"], "rejected")
        self.assertEqual(state["tasks"]["RULES_DISPLAY"]["user_acceptance"], "accepted")
        self.assertEqual(state["items"]["WEIGHT_CHOICE_PHASE1:1:completed"]["status"], "queued")
        self.assertEqual(state["tasks"]["WEIGHT_CHOICE_PHASE1"]["self_test"], "pending")
        self.assertEqual(state["tasks"]["WEIGHT_CHOICE_PHASE1"]["quality_review"], "pending")
        self.assertTrue(all(not right["allowed"] for t in state["tasks"].values() for right in t["authorization"].values()))
        self.assertTrue(all(t["phase"] != "completed" for t in state["tasks"].values()))
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "migrated.json"
            write_atomic(target, state)
            validate(load(target))
            self.assertEqual(load(target), state)
        self.assertEqual(source.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
