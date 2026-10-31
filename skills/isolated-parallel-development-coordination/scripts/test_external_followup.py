"""External material requests are distinct from decisions and require real routing."""
import copy
import json
from pathlib import Path
import subprocess
import sys

from coordination import checkpoint, load, next_item, ref, write_atomic
from test_coordination import LedgerFixture
import test_review_regressions


class ExternalFollowupTests(LedgerFixture):
    def external_event(self, revision=4, reviewed=False):
        if "A" not in self.state["tasks"]:
            self.dispatch()
        # Synthetic finite process comparison, labeled as fixture rather than real customer facts.
        rows = [{"device": str(i), "current": False, "legacy": False, "previous": False if i < 11 else None}
                for i in range(15)]
        population = self.json("synthetic-15-devices.json", rows)
        code = "import json,sys; rows=json.load(open(sys.argv[1])); assert len({tuple(sorted(r)) for r in rows})==1; assert sum(r['previous'] is False for r in rows)==11; assert sum(r['previous'] is None for r in rows)==4; print('synthetic 11/4 comparison: same keys, distinct values; producer definition still required')"
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code, population["path"]],
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        technical = self.file("population-comparison.log", result.stdout)
        report = self.json(f"dependency-r{revision}.json", {"project_id": "project-one", "task_id": "A",
            "delivery_revision": revision, "status": "held", "not_a_new_business_decision": True,
            "self_test": {"status": "passed_for_independent_scope_only"},
            "blocker": {"category": "dependency", "owner": "upstream-IT", "reason": "Need field producer code/explanation"}})
        self.apply(op="bind-report", task_id="A", path=report["path"], reference="finite comparison result")
        request = {"id": "FIELD_SOURCE_REQUEST", "title": "Obtain upstream field definition",
            "prompt": "Please coordinate one source-material request with customer IT.", "recipient": "customer IT",
            "request_text": "Please provide the field-production query/source and the previous-production selection explanation, including the differing 11/4 fixture cases.",
            "required_materials": ["field-producing source/query", "value-selection and previous-production rule explanation"]}
        review = {"conclusion": "external_materials_required", "summary": "Technical population comparison completed; no business equivalence inferred",
                  "report_sha256": report["sha256"], "evidence": [technical, population]}
        event = {"project_id": "project-one", "task_id": "A", "owner_agent_id": "worker-A", "channel": "native_agent",
                 "event_id": f"A:{revision}:blocked", "kind": "blocked", "delivery_revision": revision, "report": report}
        if reviewed:
            event.update(assistance=request, technical_review=review)
        return event, request, review

    def test_UI_close_returns_reviewed_external_material_request_without_interrupting(self):
        event, request, review = self.external_event(reviewed=True)
        self.human("UI_REWORK")
        self.apply(op="next")
        self.apply(op="wait", item_id="UI_REWORK", prompt="Confirm weight-button rework")
        self.receive(event)
        self.assertEqual(self.state["active_human_item"], "UI_REWORK")
        self.assertIsNone(next_item(self.state))
        self.assertTrue(checkpoint(self.state)["allow_end"])
        result = self.close("UI_REWORK")
        self.assertEqual(result["next"]["id"], "FIELD_SOURCE_REQUEST")
        self.assertEqual(result["next"]["kind"], "assistance")
        self.assertFalse(checkpoint(self.state)["allow_end"])
        self.assertEqual(checkpoint(self.state)["action"], "present_next")
        self.apply(op="next")
        self.assertEqual(self.state["active_human_item"], "FIELD_SOURCE_REQUEST")
        self.assertEqual(self.state["tasks"]["A"]["implementation"], "blocked")
        self.assertEqual(self.state["tasks"]["A"]["user_acceptance"], "pending")
        self.assertFalse(any(v["allowed"] for v in self.state["tasks"]["A"]["authorization"].values()))

    def test_dependency_without_decision_or_assignment_cannot_silently_closeout(self):
        event, request, review = self.external_event()
        self.receive(event)
        self.assertEqual(self.state["items"], {})
        self.assertEqual(checkpoint(self.state)["action"], "route_blocked_work")
        self.assertFalse(checkpoint(self.state)["allow_end"])
        worker = {"task_id": "A", "owner_agent_id": "worker-A", "status": "completed", "last_delivery_revision": 4,
                  "evidence": self.file("native-terminal.json")}
        observed = self.audit(self.observation([worker]))
        self.assertFalse(observed["allow_end"])
        self.assertIn("next owner", str(observed["reconciliation"]["issues"]))
        # A reviewed coordinator action can route the already-consumed dependency; canonical event is unchanged.
        before = copy.deepcopy(self.state["receipts"][event["event_id"]])
        self.apply(op="request-materials", task_id="A", source_receipt=event["event_id"], request=request,
                   technical_review=review)
        self.assertEqual(self.state["receipts"][event["event_id"]], before)
        self.assertEqual(next_item(self.state)["id"], "FIELD_SOURCE_REQUEST")

    def test_technical_blocker_stays_internal_until_assignment_evidence_exists(self):
        event, _, _ = self.external_event()
        report = load(event["report"]["path"])
        report["blocker"].update(category="technical", owner="worker-A", reason="Recheck parser path")
        event["report"] = self.json("technical-only.json", report)
        self.receive(event)
        self.assertEqual(self.state["items"], {})
        self.assertFalse(checkpoint(self.state)["allow_end"])
        self.apply(op="route-blocker", task_id="A", delivery_revision=4, owner="worker-A",
            action="Inspect producer and parser mapping", evidence=self.file("native-assignment-proof.json"))
        self.assertTrue(checkpoint(self.state)["allow_end"])
        self.assertEqual(checkpoint(self.state)["action"], "wait_internal")

    def test_assistance_expired_evidence_and_uninitiated_request_cannot_wait_or_close(self):
        event, request, review = self.external_event(reviewed=True)
        self.receive(event)
        self.apply(op="next")
        self.apply(op="wait", item_id="FIELD_SOURCE_REQUEST", prompt="Coordinate the IT source request")
        Path(review["evidence"][0]["path"]).write_text("changed premise", encoding="utf-8")
        self.assertFalse(checkpoint(self.state)["allow_end"])
        with self.assertRaises(ValueError):
            self.apply(op="close", item_id="FIELD_SOURCE_REQUEST", outcome="requested", user_reference="claimed sent")
        self.apply(op="redispatch", item_id="FIELD_SOURCE_REQUEST", next_revision=5,
            reason="Recheck technical premise", user_reference="continue same technical scope")
        self.assertEqual(self.state["tasks"]["A"]["worker_run"]["expected_delivery_revision"], 5)

    def test_actual_request_evidence_persists_and_never_grants_semantic_or_Git_authority(self):
        event, _, _ = self.external_event(reviewed=True)
        self.receive(event)
        self.apply(op="next")
        rights = copy.deepcopy(self.state["tasks"]["A"]["authorization"])
        request_evidence = self.file("fixture-request-was-forwarded.txt", "Fixture only: user confirms request sent to IT")
        state_path = self.root / "state.json"
        write_atomic(state_path, self.state)
        close = self.cli(state_path, "close", "FIELD_SOURCE_REQUEST", "--outcome", "requested",
                         "--user-reference", "Fixture user confirms request forwarded", "--request-evidence", request_evidence["path"])
        self.assertEqual(close.returncode, 0, close.stderr)
        self.state = load(state_path)
        self.assertEqual(self.state["tasks"]["A"]["blocker_followup"]["status"], "external_requested")
        self.assertTrue(checkpoint(self.state)["allow_end"])
        self.assertEqual(self.state["tasks"]["A"]["authorization"], rights)
        self.assertEqual(self.state["tasks"]["A"]["user_acceptance"], "pending")
        Path(request_evidence["path"]).unlink()
        self.assertFalse(checkpoint(self.state)["allow_end"])

    def test_first_business_question_returns_to_technical_before_r4_material_request(self):
        event, _ = test_review_regressions.ReviewRegressionTests.business_event(self, revision=3)
        self.receive(event)
        self.apply(op="next")
        scope, rights = copy.deepcopy(self.state["tasks"]["A"]["requirements"]), copy.deepcopy(self.state["tasks"]["A"]["authorization"])
        self.apply(op="redispatch", item_id=event["event_id"], next_revision=4,
            reason="Compare all devices first", user_reference="Do the technical comparison yourself; fields should be consistent")
        external, request, review = self.external_event(reviewed=True)
        self.receive(external)
        self.assertEqual(next_item(self.state)["kind"], "assistance")
        self.assertEqual(self.state["items"][event["event_id"]]["outcome"], "withdrawn")
        self.assertEqual(self.state["tasks"]["A"]["requirements"], scope)
        self.assertEqual(self.state["tasks"]["A"]["authorization"], rights)

    def test_internal_reinvestigation_supersedes_queued_request_without_resurrecting_it(self):
        event, _, _ = self.external_event(reviewed=True)
        self.receive(event)
        self.apply(op="redispatch", task_id="A", next_revision=5, reason="Need another internal comparison",
                   user_reference="continue already confirmed technical scope")
        self.assertEqual(self.state["items"]["FIELD_SOURCE_REQUEST"]["status"], "superseded")
        self.assertIsNone(next_item(self.state))
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 4)
        self.assertEqual(self.state["tasks"]["A"]["worker_run"]["expected_delivery_revision"], 5)
        old = self.state["tasks"]["A"]["history"][-1]
        self.assertEqual(old["blocker_followup"]["item_id"], "FIELD_SOURCE_REQUEST")
