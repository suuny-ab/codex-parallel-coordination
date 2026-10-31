"""R4 report presentation adaptation preserves immutable source and evidence."""
import copy
from pathlib import Path

from coordination import load, next_item, ref
from evidence import presentation_view
from test_coordination import LedgerFixture


class PresentationContractTests(LedgerFixture):
    def test_command_object_array_and_expected_string_array_are_normalized_before_canonical(self):
        event = self.delivery()
        report = load(event["report"]["path"])
        report["self_test"]["command"] = [{"command": ["python", "-B", "actual-check.py"], "exit_code": 0}]
        report["expected"] = ["Existing facts unchanged", "New result visible"]
        event["report"] = self.json("array-fields-report.json", report)
        self.receive(event)
        item = next_item(self.state)
        self.assertIsNotNone(item)
        canonical = self.state["receipts"][event["event_id"]]
        self.assertEqual(canonical["payload"]["report"], event["report"])
        self.assertEqual(canonical["presentation_adapter"]["source_report"], event["report"])
        self.assertEqual(load(event["report"]["path"]), report)
        self.apply(op="next")
        self.close(event["event_id"], "accepted")

    def test_unsupported_display_field_quarantines_without_claiming_key_then_same_id_can_correct(self):
        event = self.delivery()
        report = load(event["report"]["path"])
        report["expected"] = {"unsupported": True}
        invalid = dict(event, report=self.json("bad-display.json", report))
        self.receive(invalid)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.receive(event)
        self.assertIsNotNone(next_item(self.state))

    def test_preexisting_canonical_display_problem_refreshes_same_revision_without_overwrite(self):
        event = self.delivery(revision=5)
        report = load(event["report"]["path"])
        report["expected"] = ["One expected result", "Second expected result"]
        event["report"] = self.json("pre-r4-display.json", report)
        self.receive(event)
        self.state["receipts"][event["event_id"]].pop("presentation_adapter", None)
        self.state["tasks"]["A"]["blocker"] = {"reason": "legacy display type mismatch", "category": "evidence", "owner": "worker-A"}
        before = copy.deepcopy(self.state["receipts"][event["event_id"]]["payload"])
        self.apply(op="refresh-presentation", task_id="A", source_receipt=event["event_id"], reference="coordinator confirms presentation-only adaptation")
        self.assertEqual(self.state["receipts"][event["event_id"]]["payload"], before)
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 5)
        self.assertIsNotNone(next_item(self.state))

    def test_captured_history_r5_reports_adapt_display_without_touching_18_source_622_evidence(self):
        captured = Path(__file__).resolve().parents[3] / "fallback-r4" / "before"
        p = captured / "history-r5-delivery-for-acceptance.json"
        if not p.exists():
            self.skipTest("Read-only real-r5 copies are external delivery evidence, not installed skill assets")
        original = load(p)
        before = p.read_bytes()
        adapted, changes = presentation_view(original)
        self.assertIsInstance(adapted["expected"], str)
        self.assertEqual(adapted["source"], original["source"])
        self.assertEqual(adapted["evidence"], original["evidence"])
        self.assertEqual(len(adapted["source"]), 18)
        self.assertEqual(len(adapted["evidence"]), 622)
        self.assertEqual(adapted["self_test"]["status"], original["self_test"]["status"])
        self.assertEqual(adapted["delivery_revision"], 5)
        self.assertTrue(any(x["field"] == "expected" for x in changes))
        self.assertEqual(p.read_bytes(), before)
