"""R5 regression: real read-only CLI after filesystem-only changes, no Git."""
import copy
import json
from pathlib import Path
import subprocess
import sys

from coordination import ref, write_atomic
from evidence import pack
from test_coordination import LedgerFixture
import test_external_followup
import test_progress_fallback


class ProjectionFreshnessTests(LedgerFixture):
    observed_worker = test_progress_fallback.ProgressFallbackTests.observed_worker

    def progress(self):
        path = self.root / "ledger.json"
        write_atomic(path, self.state)
        before = path.read_bytes()
        result = self.cli(path, "progress")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), before)
        return json.loads(result.stdout)

    def unverified(self):
        value = self.progress()
        if value["kind"] == "needs_reconcile":
            self.assertIsNone(value.get("progress"))
        else:
            projection = value["progress"]
            self.assertFalse(projection["verified"])
            self.assertTrue(projection["issues"])
            self.assertTrue(all(v is None for k, v in projection["counts"].items() if k != "total"))
            self.assertTrue(all(row["certainty"] == "last_recorded" for row in projection["tasks"]))
            self.assertIn("最后登记", value["text"])
        return value

    def write_completed(self):
        task = self.state["tasks"]["A"]
        # An actual finite worker process runs the dispatched source; no ledger operation occurs.
        command = [sys.executable, "-X", "utf8", "-B", "feature.py"]
        result = subprocess.run(command, cwd=task["worktree"], capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "feature")
        report = pack(dict(project_id=self.state["project"]["id"], task_id="A", delivery_revision=1,
            summary="Review finite worker result", source_root=task["worktree"], evidence=[self.file("test.log", result.stdout)],
            self_test={"status": "passed", "command": "python -B feature.py"}, quality_review={"status": "passed"},
            entry={"kind": "file", "target": self.file("accept.html", "feature result")["path"]},
            steps=["Inspect feature output"], expected="feature"))
        write_atomic(task["report_path"], report)
        return dict(project_id=self.state["project"]["id"], task_id="A", owner_agent_id="worker-A",
                    channel="native_agent", kind="completed", delivery_revision=1,
                    event_id="A:1:completed", report=ref(task["report_path"]))

    def running_audit(self):
        self.dispatch()
        result = self.audit(self.observation([self.observed_worker()]))
        self.assertTrue(result["user_handoff"]["progress"]["verified"])
        return copy.deepcopy(self.state)

    def test_R4_001_registered_report_appears_after_audit_without_native_receipt(self):
        before = self.running_audit()
        event = self.write_completed()
        self.assertEqual(self.state, before)
        self.unverified()
        self.assertEqual(self.state["receipts"], {})  # A file is not a native completion.
        gap = self.audit(self.observation([self.observed_worker()]))
        self.assertFalse(gap["allow_end"])
        self.assertEqual(gap["user_handoff"]["kind"], "needs_reconcile")
        self.assertEqual(self.state["receipts"], {})
        received = self.audit(self.observation([self.observed_worker(status="completed", revision=1, receipt=event)]))
        self.assertEqual(received["user_handoff"]["kind"], "human_item")
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "processed")

    def test_report_invalid_json_is_unverified_without_mutation(self):
        self.running_audit()
        Path(self.state["tasks"]["A"]["report_path"]).write_text("{truncated", encoding="utf-8")
        self.unverified()

    def external_wait(self):
        event, request, review = test_external_followup.ExternalFollowupTests.external_event(self, reviewed=True)
        self.receive(event)
        self.apply(op="next")
        proof = self.file("request-sent.txt", "Fixture user forwarded the source request")
        self.apply(op="close", item_id=request["id"], outcome="requested", user_reference="fixture forwarded", request_evidence=proof)
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        self.assertTrue(result["allow_end"])
        self.assertTrue(result["user_handoff"]["progress"]["verified"])
        return event, review, proof

    def test_R4_002_request_proof_changed_same_revision_check_and_projection_agree(self):
        _, _, proof = self.external_wait()
        revision = self.state["revision"]
        Path(proof["path"]).write_text("request was not sent", encoding="utf-8")
        path = self.root / "ledger.json"
        write_atomic(path, self.state)
        check = self.cli(path, "check")
        self.assertEqual(check.returncode, 2)
        self.assertEqual(json.loads(check.stdout)["action"], "route_blocked_work")
        self.unverified()
        self.assertEqual(self.state["revision"], revision)
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        self.assertFalse(result["allow_end"])
        self.assertFalse(result["user_handoff"]["progress"]["verified"])
        # Exact original proof restored; existing hash-bound snapshot becomes valid again.
        Path(proof["path"]).write_text("Fixture user forwarded the source request", encoding="utf-8")
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        self.assertTrue(result["user_handoff"]["progress"]["verified"])
        self.assertEqual(result["user_handoff"]["progress"]["counts"]["external_wait"], 1)

    def test_technical_assignment_and_investigation_proofs_expire(self):
        event, _, review = test_external_followup.ExternalFollowupTests.external_event(self)
        self.receive(event)
        assignment = self.file("assigned.json")
        self.apply(op="route-blocker", task_id="A", delivery_revision=4, owner="worker-A",
                   action="Inspect field source", evidence=assignment)
        self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        Path(assignment["path"]).unlink()
        self.unverified()
        Path(assignment["path"]).write_text("fixture evidence", encoding="utf-8")
        event, request, review = test_external_followup.ExternalFollowupTests.external_event(self, reviewed=True)
        self.apply(op="request-materials", task_id="A", source_receipt=event["event_id"], request=request, technical_review=review)
        self.apply(op="next")
        self.apply(op="close", item_id=request["id"], outcome="requested", user_reference="sent", request_evidence=self.file("sent.txt"))
        self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        Path(review["evidence"][0]["path"]).unlink()
        self.unverified()

    def test_registered_consumed_report_replaced_or_deleted_invalidates_projection(self):
        self.running_audit()
        event = self.write_completed()
        self.audit(self.observation([self.observed_worker(status="completed", revision=1, receipt=event)]))
        self.apply(op="next")
        self.close(event["event_id"], "accepted")
        self.audit(self.observation([self.observed_worker(status="completed", revision=1)]))
        path = Path(event["report"]["path"])
        original = path.read_bytes()
        for content in (b'{"task_id":"A","delivery_revision":2}', None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                self.unverified()
                path.write_bytes(original)
        self.assertTrue(self.progress()["progress"]["verified"])

    def test_changed_protected_evidence_does_not_bypass_closeout_via_projection(self):
        proof = self.file("protected.txt")
        self.state["project"]["protected"].append(proof)
        self.running_audit()
        Path(proof["path"]).write_text("changed protected fixture", encoding="utf-8")
        self.unverified()

    def test_active_focus_and_one_eligible_remain_prioritized(self):
        self.running_audit()
        self.human("current")
        self.apply(op="next")
        self.write_completed()
        self.assertEqual(self.progress()["kind"], "focus")
        self.close("current")
        event = dict(project_id=self.state["project"]["id"], task_id="A", owner_agent_id="worker-A", channel="native_agent",
                     kind="completed", delivery_revision=1, event_id="A:1:completed", report=ref(self.state["tasks"]["A"]["report_path"]))
        self.receive(event)
        self.assertEqual(self.progress()["kind"], "human_item")
