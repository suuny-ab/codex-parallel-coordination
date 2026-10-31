"""IR-001/002 behavioral regressions: files, persistence and receipt timing."""
import copy
import json
from pathlib import Path

from coordination import checkpoint, fingerprint, load, next_item, ref, transition, write_atomic
from test_coordination import LedgerFixture


class ReviewRegressionTests(LedgerFixture):
    def business_event(self, revision=1):
        if "A" not in self.state["tasks"]:
            self.dispatch()
        evidence = self.file(f"technical-evidence-{revision}.txt", "Checked actual input population and both parser paths")
        report = self.json(f"business-{revision}.json", {
            "project_id": "project-one", "task_id": "A", "delivery_revision": revision,
            "blocker": {"category": "business", "reason": "Choose confirmed presentation case", "owner": "human"},
            "self_test": {"status": "passed_for_independent_scope_only"}})
        event = {"project_id": "project-one", "task_id": "A", "owner_agent_id": "worker-A",
            "channel": "native_agent", "event_id": f"A:{revision}:blocked", "kind": "blocked",
            "delivery_revision": revision, "report": report,
            "decision": {"title": "Case", "prompt": "Uppercase or lowercase?", "options": ["Uppercase", "Lowercase"]},
            "technical_review": {"report_sha256": report["sha256"], "summary": "Checked existing definitions and data",
                "conclusion": "business_decision_required", "evidence": [evidence]}}
        self.apply(op="bind-report", task_id="A", path=report["path"], reference="immutable worker delivery")
        return event, evidence

    def test_IR001_bad_hash_does_not_claim_canonical_key_same_event_correction_works(self):
        event = self.delivery()
        bad = dict(event, report=dict(event["report"], sha256="0" * 64))
        self.receive(bad)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.assertTrue(self.state.get("unverified_receipts"))
        self.receive(event)
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 1)
        self.assertEqual(next_item(self.state)["id"], event["event_id"])
        before = copy.deepcopy(self.state)
        self.assertEqual(self.receive(event, False)["receipt"], "duplicate")
        self.assertEqual(self.state, before)
        self.assertTrue(all(r["status"] == "resolved" for r in self.state["unverified_receipts"].values()))

    def test_IR001_wrong_report_identity_and_business_mapping_can_be_corrected(self):
        event, _ = self.business_event()
        wrong_identity = dict(event, report=self.json("wrong-project.json", {
            "project_id": "different", "task_id": "A", "delivery_revision": 1}))
        self.receive(wrong_identity)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        bad_review = copy.deepcopy(event)
        bad_review["technical_review"]["report_sha256"] = "0" * 64
        self.receive(bad_review)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.receive(event)
        self.assertEqual(next_item(self.state)["id"], event["event_id"])
        self.assertEqual(len(self.state["unverified_receipts"]), 2)

    def test_IR001_pre_fix_unconsumed_poison_recovers_with_full_history(self):
        event = self.delivery()
        bad = dict(event, report=dict(event["report"], sha256="0" * 64))
        self.state["receipts"][event["event_id"]] = {"payload": bad, "fingerprint": fingerprint(bad),
            "status": "held", "reason": "artifact hash mismatch", "history": [{"status": "buffered"},
            {"status": "held", "reason": "artifact hash mismatch"}]}
        self.receive(event)
        self.assertEqual(next_item(self.state)["id"], event["event_id"])
        raw = self.state["unverified_receipts"][fingerprint(bad)]
        self.assertEqual(raw["payload"], bad)
        self.assertEqual(raw["prior_canonical"]["history"][1]["status"], "held")

    def test_IR001_verified_receipt_cannot_be_replaced_after_its_file_expires(self):
        event = self.delivery()
        self.receive(event, False)
        original = copy.deepcopy(self.state)
        report = load(event["report"]["path"])
        report["summary"] = "Different report under same event"
        corrected = dict(event, report=self.json("different-content.json", report))
        Path(event["report"]["path"]).unlink()
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            self.receive(corrected, False)
        self.assertEqual(self.state, original)

    def test_missing_report_restore_and_duplicate_raw_arrivals_are_idempotent(self):
        event = self.delivery()
        path = Path(event["report"]["path"])
        original_bytes = path.read_bytes()
        path.unlink()
        self.receive(event)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        before = copy.deepcopy(self.state)
        self.receive(event, False)
        self.assertEqual(self.state, before)
        path.write_bytes(original_bytes)
        self.apply(op="drain")
        self.assertEqual(next_item(self.state)["id"], event["event_id"])

    def test_IR002_changed_technical_evidence_holds_queued_decision_then_restores(self):
        event, evidence = self.business_event()
        self.receive(event)
        self.assertEqual(next_item(self.state)["id"], event["event_id"])
        path = Path(evidence["path"])
        old = path.read_bytes()
        path.write_text("Changed technical premise", encoding="utf-8")
        self.assertIsNone(next_item(self.state))
        self.assertIn("hash mismatch", str(checkpoint(self.state)["held"]))
        with self.assertRaises(ValueError):
            self.apply(op="next")
        # Replaying the same native event never repairs or bypasses stale evidence.
        self.receive(event)
        self.assertIsNone(next_item(self.state))
        path.write_bytes(old)
        self.apply(op="next")
        self.assertEqual(self.state["active_human_item"], event["event_id"])

    def test_IR002_active_waiting_decision_rechecks_all_refs_after_process_reload(self):
        event, evidence = self.business_event()
        second = self.file("second-technical-ref.txt", "Cross-population comparison")
        event["technical_review"]["evidence"].append(second)
        self.receive(event)
        self.apply(op="next")
        self.apply(op="wait", item_id=event["event_id"], prompt="Choose case")
        Path(second["path"]).unlink()
        state_path = self.root / "state.json"
        write_atomic(state_path, self.state)
        checked = self.cli(state_path, "check")
        self.assertEqual(checked.returncode, 2, checked.stdout)
        self.assertEqual(json.loads(checked.stdout)["active_human_item"], event["event_id"])
        before = state_path.read_bytes()
        answer = self.cli(state_path, "close", event["event_id"], "--outcome", "answered", "--user-reference", "Fixture answer")
        self.assertEqual(answer.returncode, 1)
        self.assertEqual(state_path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.apply(op="wait", item_id=event["event_id"], prompt="Invalid question")
        self.apply(op="redispatch", item_id=event["event_id"], next_revision=2,
                   reason="Recheck expired technical evidence", user_reference="Continue existing technical scope")
        self.assertIsNone(self.state["active_human_item"])
        self.assertEqual(self.state["tasks"]["A"]["implementation"], "running")

    def test_IR002_pre_drain_mutation_requires_recheck_and_same_event_mapping_correction(self):
        event, evidence = self.business_event()
        Path(evidence["path"]).write_text("Premise changed before receipt admission", encoding="utf-8")
        self.receive(event)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        fixed = copy.deepcopy(event)
        fixed["technical_review"]["evidence"] = [ref(evidence["path"])]
        self.receive(fixed)
        self.assertEqual(next_item(self.state)["id"], event["event_id"])

    def test_closeout_persists_valid_message_when_later_message_has_invalid_identity(self):
        event = self.delivery()
        invalid = dict(event, event_id="A:2:completed", delivery_revision=2, project_id="wrong-project")
        worker = {"task_id": "A", "owner_agent_id": "worker-A", "status": "completed",
                  "last_delivery_revision": 1, "evidence": self.file("native-snapshot.json")}
        result = self.audit(self.observation([worker], [event, invalid]))
        self.assertFalse(result["allow_end"])
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "processed")
        self.assertIn("identity", str(result["reconciliation"]["issues"]))
        self.assertEqual(result["revision"], self.state["revision"])

    def test_technical_evidence_expiring_after_verified_admission_needs_real_new_handoff(self):
        event, evidence = self.business_event()
        self.receive(event, False)
        Path(evidence["path"]).write_text("new technical findings", encoding="utf-8")
        self.apply(op="drain")
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "held")
        changed = copy.deepcopy(event)
        changed["technical_review"]["evidence"] = [ref(evidence["path"])]
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            self.receive(changed, False)
        revised, _ = self.business_event(revision=2)
        self.receive(revised)
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "stale")
        self.assertEqual(next_item(self.state)["id"], revised["event_id"])

    def test_internal_redispatch_cannot_bypass_active_acceptance_freeze(self):
        event = self.delivery()
        self.receive(event)
        self.apply(op="next")
        before = copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, "active task item"):
            self.apply(op="redispatch", task_id="A", next_revision=2, reason="new technical run",
                       user_reference="existing scope")
        self.assertEqual(self.state, before)
