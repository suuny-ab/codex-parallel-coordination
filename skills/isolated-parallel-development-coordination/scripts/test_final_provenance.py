"""App final provenance expiry across intake, drain, queued/active use and restore."""
import copy
from pathlib import Path

from coordination import checkpoint, load, next_item
from evidence import report_for
from progress import round_handoff
import test_delivery
import test_review_regressions


class FinalProvenanceTests(test_delivery.DeliveryFixture):
    def test_business_answer_also_rechecks_App_final_source_without_changing_business_scope(self):
        event, _ = test_review_regressions.ReviewRegressionTests.business_event(self, revision=1)
        event.update(channel="app_thread", recipient_agent_id="delivery-worker")
        event["transport_evidence"] = self.json("business-final-proof.json", {"sender_id": "worker-A", "recipient_id": "delivery-worker",
            "sender_target_id": "existing-thread-A", "event_id": event["event_id"], "kind": "task_final"})
        self.receive(event)
        self.apply(op="next")
        proof = Path(event["transport_evidence"]["path"])
        original = proof.read_bytes()
        proof.unlink()
        with self.assertRaises(ValueError):
            self.apply(op="close", item_id=event["event_id"], outcome="answered", user_reference="source proof expired")
        self.assertEqual(self.state["active_human_item"], event["event_id"])
        proof.write_bytes(original)
        self.apply(op="close", item_id=event["event_id"], outcome="answered", user_reference="fixture real user answer after evidence restored")
        self.assertEqual(self.state["tasks"]["A"]["implementation"], "blocked")

    def final(self):
        event = self.delivery("A")
        event.update(channel="app_thread", recipient_agent_id="delivery-worker")
        event["transport_evidence"] = self.json("App-task-final.json", {"sender_id": "worker-A", "recipient_id": "delivery-worker",
            "sender_target_id": "existing-thread-A", "event_id": event["event_id"], "kind": "task_final"})
        self.proof = Path(event["transport_evidence"]["path"])
        self.original_proof = self.proof.read_bytes()
        self.original_report = Path(event["report"]["path"]).read_bytes()
        return event

    def expire(self, deletion=False):
        if deletion:
            self.proof.unlink()
        else:
            self.proof.write_text('{"kind":"acknowledged"}', encoding="utf-8")

    def test_receive_then_pre_drain_proof_change_holds_and_restore_consumes_same_canonical(self):
        event = self.final()
        self.receive(event, drain=False)
        payload = copy.deepcopy(self.state["receipts"][event["event_id"]]["payload"])
        self.expire()
        self.apply(op="drain")
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 0)
        self.assertEqual(self.state["receipts"][event["event_id"]]["status"], "held")
        self.assertEqual(self.state["receipts"][event["event_id"]]["payload"], payload)
        self.proof.write_bytes(self.original_proof)
        self.apply(op="drain")
        self.assertEqual(next_item(self.state)["id"], event["event_id"])

    def test_processed_queued_proof_changed_or_deleted_never_recommends_or_activates(self):
        event = self.final()
        self.receive(event)
        original = copy.deepcopy(self.state)
        for deletion in (False, True):
            with self.subTest(deletion=deletion):
                self.state = copy.deepcopy(original)
                self.proof.write_bytes(self.original_proof)
                self.expire(deletion)
                self.assertIsNone(next_item(self.state))
                self.assertIn(event["event_id"], checkpoint(self.state)["held"])
                with self.assertRaises(ValueError):
                    self.apply(op="next")
                result = self.delivery_audit()
                self.assertNotEqual(result["user_handoff"]["kind"], "human_item")
                handoff = round_handoff(self.state, checkpoint(self.state))
                if handoff["kind"] == "progress":
                    self.assertFalse(handoff["progress"]["verified"])
                    self.assertIsNone(handoff["progress"]["counts"]["in_progress"])
                self.assertEqual(Path(event["report"]["path"]).read_bytes(), self.original_report)
        self.proof.write_bytes(self.original_proof)
        self.assertEqual(next_item(self.state)["id"], event["event_id"])

    def test_active_wait_and_accept_recheck_final_origin_and_restore_focus(self):
        event = self.final()
        self.receive(event)
        self.apply(op="next")
        self.apply(op="wait", item_id=event["event_id"], prompt="Fixture current page acceptance")
        self.expire(deletion=True)
        before = copy.deepcopy(self.state)
        self.assertFalse(checkpoint(self.state)["allow_end"])
        for op in (dict(op="wait", item_id=event["event_id"], prompt="Still accept?"),
                   dict(op="close", item_id=event["event_id"], outcome="accepted", user_reference="cannot accept expired origin")):
            with self.subTest(op=op["op"]), self.assertRaises(ValueError):
                self.apply(**op)
            self.assertEqual(self.state, before)
        self.proof.write_bytes(self.original_proof)
        self.assertTrue(checkpoint(self.state)["allow_end"])
        self.close(event["event_id"], "accepted")
        self.assertEqual(self.state["tasks"]["A"]["phase"], "awaiting_integration")

    def test_even_after_user_acceptance_report_use_cannot_bypass_expired_final_proof(self):
        event = self.final()
        self.receive(event)
        self.apply(op="next")
        self.close(event["event_id"], "accepted")
        self.expire()
        with self.assertRaises(ValueError):
            report_for(self.state, "A")
        self.assertEqual(load(event["report"]["path"])["self_test"]["status"], "passed")
        self.assertEqual(self.state["tasks"]["A"]["user_acceptance"], "accepted")
        self.proof.write_bytes(self.original_proof)
        self.assertEqual(report_for(self.state, "A")["task_id"], "A")
