"""Author R7: scope/receiver must be rechecked after buffering and before effects."""
import copy
from pathlib import Path
import subprocess
import sys

from coordination import load, transition
from evidence import ref
import test_delivery


class ConsumptionScopeTests(test_delivery.DeliveryFixture):
    def setUp(self):
        super().setUp()
        self.apply(op="revoke-delivery", user_reference="fixture prepare pre-delegation backlog", evidence=self.file("revoke.txt"))

    def event(self, task="A", recipient=None, bad_hash=False):
        event = self.delivery(task)
        # Execute actual dispatched source in a finite process, then bind its actual log.
        result = subprocess.run([sys.executable, "-B", "feature.py"], cwd=self.state["tasks"][task]["worktree"],
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "feature")
        report = load(event["report"]["path"])
        report["evidence"] = [self.file(task + "-actual.log", result.stdout)]
        report["self_test"]["command"] = "python -B feature.py"
        event["report"] = self.json(task + "-scope-source-bound.json", report)
        if recipient:
            event["recipient_agent_id"] = recipient
        if bad_hash:
            self.saved_report = Path(event["report"]["path"]).read_bytes()
            Path(event["report"]["path"]).unlink()
        return event

    def intake_before_delegate(self, event):
        # Bypass the fixture helper that adds a recipient; preserve the ACTUAL original envelope.
        self.apply(op="receive", receipt=event)

    def delegate_now(self):
        self.delegate()
        self.apply(op="delivery-transport", transport=self.contacts())

    def test_review7_outside_C_buffer_held_without_task_or_human_effects(self):
        event = self.event("C", recipient="delivery-worker")
        self.intake_before_delegate(event)
        before_task = copy.deepcopy(self.state["tasks"]["C"])
        before_payload = copy.deepcopy(self.state["receipts"][event["event_id"]]["payload"])
        self.delegate_now()
        self.apply(op="drain")
        self.assertEqual(self.state["tasks"]["C"], before_task)
        self.assertEqual(self.state["receipts"][event["event_id"]]["payload"], before_payload)
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "held")
        self.assertIn("scope", self.state["receipts"][event["event_id"]]["reason"])
        self.assertNotIn(event["event_id"], self.state["items"])

    def test_missing_or_old_receiver_on_in_scope_verified_buffer_never_silently_transferred(self):
        event = self.event("A", recipient="old-main-or-delivery")
        self.intake_before_delegate(event)
        before = copy.deepcopy(self.state["tasks"]["A"])
        fingerprint = self.state["receipts"][event["event_id"]]["fingerprint"]
        self.delegate_now()
        self.apply(op="drain")
        self.assertEqual(self.state["tasks"]["A"], before)
        self.assertEqual(self.state["receipts"][event["event_id"]]["fingerprint"], fingerprint)
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "held")
        self.assertIn("recipient", self.state["receipts"][event["event_id"]]["reason"])

    def test_missing_receiver_buffer_is_held_with_original_bytes(self):
        event = self.event("A")
        self.intake_before_delegate(event)
        self.delegate_now()
        self.apply(op="drain")
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "held")
        self.assertNotIn("recipient_agent_id", self.state["receipts"][event["event_id"]]["payload"])
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 0)

    def test_unverified_outside_scope_report_restore_does_not_claim_canonical(self):
        event = self.event("C", recipient="delivery-worker", bad_hash=True)
        self.intake_before_delegate(event)
        before = copy.deepcopy(self.state["tasks"]["C"])
        Path(event["report"]["path"]).write_bytes(self.saved_report)
        self.delegate_now()
        self.apply(op="drain")
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.assertEqual(self.state["tasks"]["C"], before)
        self.assertTrue(all(x["status"] == "unverified" for x in self.state["unverified_receipts"].values()))

    def test_unverified_old_receiver_restore_stays_unverified(self):
        event = self.event("A", recipient="old-receiver", bad_hash=True)
        self.intake_before_delegate(event)
        Path(event["report"]["path"]).write_bytes(self.saved_report)
        self.delegate_now()
        self.apply(op="drain")
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 0)

    def test_closeout_internal_drain_obeys_scope_and_can_still_consume_authorized_A(self):
        c, a = self.event("C", recipient="delivery-worker"), self.event("A", recipient="delivery-worker")
        self.intake_before_delegate(c)
        self.intake_before_delegate(a)
        before = copy.deepcopy(self.state["tasks"]["C"])
        self.delegate_now()
        result = self.delivery_audit()
        self.assertEqual(self.state["tasks"]["C"], before)
        self.assertNotIn(c["event_id"], self.state["items"])
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 1)
        self.assertFalse(result["allow_end"])
        self.assertEqual(result["user_handoff"]["kind"], "needs_reconcile")
        self.assertEqual(result["notify_main"]["kind"], "important_delivery_exception")

    def test_legitimate_in_scope_buffer_and_unverified_restore_remain_idempotent(self):
        event = self.event("A", recipient="delivery-worker", bad_hash=True)
        self.intake_before_delegate(event)
        Path(event["report"]["path"]).write_bytes(self.saved_report)
        self.delegate_now()
        self.apply(op="drain")
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 1)
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "processed")
        self.assertIsNone(self.state["tasks"]["A"]["blocker"])
        self.assertEqual(self.state["receipts"][event["event_id"]]["history"][-1]["status"], "processed")
        before = copy.deepcopy(self.state)
        self.apply(op="drain")
        self.assertEqual(self.state, before)

    def test_item_only_technical_target_cannot_escape_delegated_scope_or_close_user_focus(self):
        self.dispatch("C")
        report = self.json("C-blocked.json", {"project_id": "project-one", "task_id": "C", "delivery_revision": 1,
            "status": "held", "blocker": {"category": "technical", "owner": "worker-C", "reason": "Fixture compare parser paths"}})
        self.intake_before_delegate({"project_id": "project-one", "task_id": "C", "owner_agent_id": "worker-C", "channel": "native_agent",
            "event_id": "C:1:blocked", "kind": "blocked", "delivery_revision": 1, "report": report})
        self.apply(op="drain")
        self.apply(op="enqueue", item={"id": "C-QUESTION", "kind": "decision", "task_id": "C",
            "title": "Fixture scope question", "prompt": "Continue existing technical comparison?", "options": ["Continue", "Hold"]})
        self.apply(op="next")
        self.delegate_now()
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.apply(op="redispatch", actor_id="delivery-worker", item_id="C-QUESTION", next_revision=2,
                reason="unscoped item must not select C behind task_id guard", user_reference="not a new user decision")
        self.assertEqual(self.state, before)

    def test_business_root_reference_is_also_inside_delivery_metadata_scope(self):
        self.dispatch("C")
        self.delegate_now()
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.apply(op="describe", task_id="A", business_task_id="C", reference="unauthorized cross-scope business grouping")
        self.assertEqual(self.state, before)
