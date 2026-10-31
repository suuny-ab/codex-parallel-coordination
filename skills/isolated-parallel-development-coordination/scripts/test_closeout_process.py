"""One-shot independent process / disk / CLI tests and preserved real-failure replay."""
import copy
import json
from pathlib import Path
import subprocess
import sys

from coordination import checkpoint, fingerprint, load, migrate, ref, summary, transition, write_atomic
from test_coordination import LedgerFixture


class ProcessCloseoutTests(LedgerFixture):
    def test_worker_process_idle_report_discovered_then_consumed_and_redispatched_atomically(self):
        output = self.root / "job"
        report_path = output / "worker-report.json"
        self.apply(op="dispatch", task_id="A", owner_agent_id="worker-A", worktree=str(output / "worker-source"),
            branch="task/A", scope="normalize imported quantities", acceptance=["numeric quantities preserved"],
            user_reference="original confirmed scope", report_path=str(report_path))
        command = [sys.executable, "-B", str(Path(__file__).with_name("worker_fixture.py")),
                   "--output", str(output), "--project", "project-one", "--task", "A", "--worker", "worker-A"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)  # One finite wait; no timer/poll loop.
        self.assertEqual(process.returncode, 0, stderr)
        terminal = json.loads(stdout)
        self.assertTrue(report_path.is_file())
        transcript = self.file("process-terminal.json", stdout)
        self.assertEqual(checkpoint(self.state)["action"], "wait_internal")
        worker = dict(task_id="A", owner_agent_id="worker-A", status="completed", last_delivery_revision=3,
                      evidence=transcript)
        lost = self.audit(self.observation([worker]))
        self.assertEqual(lost["action"], "reconcile_required")
        worker["receipt"] = terminal["receipt"]
        audit_input = self.observation([worker])
        state_path = self.root / "ledger.json"
        write_atomic(state_path, self.state)
        input_ref = self.json("closeout-input.json", audit_input)
        done = self.cli(state_path, "closeout", input_ref["path"])
        self.assertEqual(done.returncode, 2, done.stderr)
        result = json.loads(done.stdout)
        self.state = load(state_path)
        self.assertEqual(result["next"]["id"], "A:3:blocked")
        self.assertEqual(result["scope"], "fixture_only")
        self.apply(op="next")
        self.apply(op="wait", item_id="A:3:blocked", prompt="Which unit?")
        rights, scope = copy.deepcopy(self.state["tasks"]["A"]["authorization"]), copy.deepcopy(self.state["tasks"]["A"]["requirements"])
        self.apply(op="redispatch", item_id="A:3:blocked", next_revision=4,
            reason="Compare every record and both parser paths before inferring a data gap",
            user_reference="user: investigate yourself; all rows should have consistent fields")
        task = self.state["tasks"]["A"]
        self.assertEqual((task["phase"], task["implementation"], task["blocker"]), ("developing", "running", None))
        self.assertEqual(task["delivery_revision"], 3)  # r4 has been assigned, not delivered.
        self.assertEqual(task["worker_run"]["expected_delivery_revision"], 4)
        self.assertEqual(task["requirements"], scope)
        self.assertEqual(task["authorization"], rights)
        self.assertIsNone(self.state["active_human_item"])
        self.assertEqual(self.state["items"]["A:3:blocked"]["outcome"], "withdrawn")
        write_atomic(state_path, self.state)
        restored = json.loads(self.cli(state_path, "summary").stdout)
        self.assertEqual(restored["tasks"]["A"]["implementation"], "running")
        self.assertEqual(restored["worker_runs"]["A"]["mode"], "technical_investigation")
        self.assertEqual(restored["action"], "wait_internal")
        self.assertIsNone(restored["focus"])
        self.assertFalse(any(v["allowed"] for v in task["authorization"].values()))
        # A stale r3 terminal snapshot cannot attest that the newly assigned r4 run is idle/done.
        worker["receipt"] = terminal["receipt"]
        after_redispatch = self.audit(self.observation([worker]))
        self.assertFalse(after_redispatch["allow_end"])
        self.assertIn("predates redispatch", str(after_redispatch))

    def test_business_escalation_without_technical_review_is_held_internally(self):
        self.dispatch()
        r = self.json("A.report.json", dict(project_id="project-one", task_id="A", delivery_revision=1,
            blocker={"category": "business", "owner": "human", "reason": "Parser implies customer gap"}))
        event = dict(project_id="project-one", task_id="A", owner_agent_id="worker-A", channel="native_agent",
            kind="blocked", delivery_revision=1, event_id="A:1:blocked", report=r,
            decision={"title": "Missing?", "prompt": "Supply data?", "options": ["One", "Two"]})
        self.receive(event)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.assertEqual(self.state["unverified_receipts"][fingerprint(event)]["status"], "unverified")
        self.assertIsNone(next(iter(self.state["items"].values()), None))
        self.assertIn("technical investigation", self.state["unverified_receipts"][fingerprint(event)]["reason"])

    def test_captured_history_r3_real_report_replay_then_r4_recovery_without_scope_expansion(self):
        base = Path(__file__).resolve().parents[2]
        case = base / "evidence" / "real-failure"
        if not (case / "report.json").exists():
            self.skipTest("External preserved real case is not part of the portable installed skill")
        old, raw = load(case / "state.json"), load(case / "report.json")
        # Replay the real task only in a copy, retaining its original v1 history.
        old["tasks"] = {"HISTORY": old["tasks"]["HISTORY"]}
        old["items"] = {k: v for k, v in old["items"].items() if v.get("task_id") == "HISTORY"}
        old["receipts"] = {k: v for k, v in old["receipts"].items() if k.startswith("HISTORY:")}
        old["active_human_item"] = None
        self.state = migrate(old, "history-copy", self.json("history-only-v1.json", old))
        self.apply(op="adopt-legacy", task_id="HISTORY", scope="existing historical origin adaptation",
            acceptance=["same frozen input verification"], user_reference="original start instruction copied for replay")
        self.apply(op="bind-report", task_id="HISTORY", path=str(case / "report.json"), format="legacy-held",
            report_task_id="HISTORY_ORIGIN_ADAPTATION", reference="explicit existing report alias; not a new task")
        task = self.state["tasks"]["HISTORY"]
        event = dict(project_id="history-copy", task_id="HISTORY", owner_agent_id=task["owner_agent_id"], channel="native_agent",
            kind="blocked", delivery_revision=3, event_id="HISTORY:3:blocked", report=ref(case / "report.json"),
            technical_review={"conclusion": "business_decision_required", "summary": "Replay historical coordinator assessment only; later withdrawn for more investigation",
                "report_sha256": ref(case / "report.json")["sha256"], "evidence": [ref(case / "report.json")]},
            decision={"title": "Confirm old singeing field meaning", "prompt": raw["open_questions"][0]["question"],
                "options": ["Provide documented equivalence", "Provide correct previous-production source"]})
        self.receive(event)
        self.assertEqual(task["delivery_revision"], 2)  # Old python object was not mutated by transition.
        self.assertEqual(self.state["tasks"]["HISTORY"]["delivery_revision"], 3)
        self.assertEqual(self.state["tasks"]["HISTORY"]["self_test"], "passed_for_independent_scope_only")
        self.apply(op="next")
        self.close("HISTORY:3:blocked", "withdrawn")  # Reproduce legacy close-then-redispatch recovery.
        self.assertEqual(self.state["tasks"]["HISTORY"]["implementation"], "blocked")
        before = copy.deepcopy(self.state["tasks"]["HISTORY"]["authorization"])
        self.apply(op="redispatch", item_id="HISTORY:3:blocked", next_revision=4,
            reason="全量横向核对所有缸字段与解析路径，未确认字段语义", user_reference="你自己对一下，其它不缺数据，只有这4台缺不合理，所有缸字段应一致")
        saved = self.json("history-recovered.json", self.state)
        restored = summary(load(saved["path"]))
        self.assertEqual(restored["tasks"]["HISTORY"]["phase"], "developing")
        self.assertIsNone(restored["tasks"]["HISTORY"]["blocker"])
        self.assertEqual(restored["worker_runs"]["HISTORY"]["expected_delivery_revision"], 4)
        self.assertEqual(self.state["tasks"]["HISTORY"]["authorization"], before)
        self.assertIsNone(restored["focus"])
