"""R4 one-shot fallback progress, queue and latest native handoff scenarios."""
import copy
import json
from pathlib import Path

from coordination import checkpoint, load, ref, write_atomic
from test_coordination import LedgerFixture
import test_external_followup


class ProgressFallbackTests(LedgerFixture):
    def observed_worker(self, task="A", status="running", revision=0, receipt=None):
        value = {"task_id": task, "owner_agent_id": "worker-" + task, "status": status,
                 "last_delivery_revision": revision, "evidence": self.file(task + "-native.json")}
        if receipt:
            value["receipt"] = receipt
        return value

    def test_zero_tasks_has_explicit_user_handoff(self):
        result = self.audit(self.observation([]))
        handoff = result["user_handoff"]
        self.assertEqual(handoff["kind"], "progress")
        self.assertEqual(handoff["progress"]["counts"]["in_progress"], 0)
        self.assertIn("没有已登记的业务任务", handoff["text"])
        self.assertEqual(handoff["revision"], result["revision"])

    def test_three_running_business_tasks_have_phase_facts_and_next_step_without_ids(self):
        for name in ("HISTORY", "MR_FIX", "LOGGING"):
            self.dispatch(name)
        result = self.audit(self.observation([self.observed_worker(name) for name in self.state["tasks"]]))
        handoff = result["user_handoff"]
        self.assertEqual(handoff["progress"]["counts"]["in_progress"], 3)
        self.assertEqual(len(handoff["progress"]["tasks"]), 3)
        self.assertTrue(handoff["text"].startswith("目前没有需要你验收或决策的事项"))
        for task in handoff["progress"]["tasks"]:
            self.assertTrue(task["stage"] and task["completed_facts"] and task["next_step"])
        self.assertNotIn("worker-", handoff["text"])
        self.assertNotIn("main-session", handoff["text"])
        self.assertNotIn("%", handoff["text"])

    def test_active_focus_suppresses_progress_and_ready_receipt_does_not_interrupt(self):
        self.human("current")
        self.apply(op="next")
        self.apply(op="wait", item_id="current", prompt="Answer current")
        event = self.delivery()
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=1)], [event]))
        self.assertEqual(result["user_handoff"]["kind"], "focus")
        self.assertIsNone(result["user_handoff"].get("progress"))
        self.assertIsNone(result["user_handoff"].get("text"))
        self.close("current")
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=1)]))
        self.assertEqual(result["user_handoff"]["kind"], "human_item")
        self.assertEqual(result["next"]["id"], event["event_id"])
        self.assertNotIn("progress", result["user_handoff"])

    def test_new_terminal_report_without_consumed_native_receipt_cannot_be_hidden_by_progress(self):
        event = self.delivery()
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=1)]))
        self.assertEqual(result["user_handoff"]["kind"], "needs_reconcile")
        self.assertIsNone(result["user_handoff"].get("progress"))
        self.assertFalse(result["allow_end"])
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=1, receipt=event)]))
        self.assertEqual(result["user_handoff"]["kind"], "human_item")

    def test_buffered_native_completion_blocks_read_only_progress_until_drain_and_reconcile(self):
        event = self.delivery()
        self.receive(event, drain=False)
        path = self.root / "buffered.json"
        write_atomic(path, self.state)
        handoff = json.loads(self.cli(path, "progress").stdout)
        self.assertEqual(handoff["kind"], "needs_reconcile")
        self.assertIsNone(handoff.get("progress"))
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=1)]))
        self.assertEqual(result["user_handoff"]["kind"], "human_item")

    def test_capacity_queue_and_partial_UI_acceptance_are_not_running_or_complete(self):
        self.state["project"]["worker_capacity"] = 3
        for name in ("HISTORY", "MR_FIX", "LOGGING"):
            self.dispatch(name)
        self.apply(op="register", task_id="WEIGHTS", owner_agent_id="worker-WEIGHTS",
                   worktree=str(self.root / "WEIGHTS"), branch="weights")
        self.apply(op="confirm", task_id="WEIGHTS", scope="Improve quality after accepted UI flow", acceptance=["quality tradeoff"], user_reference="scope confirmed")
        self.apply(op="describe", task_id="WEIGHTS", title="权重候选质量", reference="user accepted UI only",
            facts=[{"text": "界面流程已验收（仅界面）", "reference": "actual UI r3 user acceptance"}], next_step="等待资源后继续修正整体质量")
        self.apply(op="queue", task_id="WEIGHTS", reason="All three execution slots occupied", user_reference="same quality scope", mode="quality_rework")
        result = self.audit(self.observation([self.observed_worker(name) for name in ("HISTORY", "MR_FIX", "LOGGING")]))
        progress = result["user_handoff"]["progress"]
        self.assertEqual(progress["counts"]["in_progress"], 3)
        self.assertEqual(progress["counts"]["queued"], 1)
        weights = next(t for t in progress["tasks"] if t["title"] == "权重候选质量")
        self.assertEqual(weights["category"], "queued")
        self.assertIn("界面流程已验收（仅界面）", weights["completed_facts"])
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.apply(op="start", task_id="WEIGHTS", user_reference="start under original scope")

    def test_author_and_independent_review_are_one_business_task(self):
        self.dispatch("TOOLKIT")
        self.dispatch("TOOLKIT_REVIEW")
        self.apply(op="describe", task_id="TOOLKIT", title="协调工具", reference="confirmed scope")
        self.apply(op="describe", task_id="TOOLKIT_REVIEW", business_task_id="TOOLKIT", task_role="review", reference="same requirement independent review")
        result = self.audit(self.observation([self.observed_worker(name) for name in self.state["tasks"]]))
        progress = result["user_handoff"]["progress"]
        self.assertEqual(progress["counts"]["total"], 1)
        self.assertEqual(progress["counts"]["in_progress"], 1)

    def test_stale_or_changed_observation_after_restart_has_no_fake_precise_running_count(self):
        self.dispatch()
        result = self.audit(self.observation([self.observed_worker()]))
        path = self.root / "state.json"
        write_atomic(path, self.state)
        command = self.cli(path, "progress")
        self.assertEqual(command.returncode, 0, command.stderr)
        fresh = json.loads(command.stdout)
        self.assertEqual(fresh["progress"]["counts"]["in_progress"], 1)
        self.apply(op="describe", task_id="A", title="新公开任务名", reference="label update")
        write_atomic(path, self.state)
        stale = json.loads(self.cli(path, "progress").stdout)
        self.assertFalse(stale["progress"]["verified"])
        self.assertIsNone(stale["progress"]["counts"]["in_progress"])
        self.assertIn("最后登记", stale["text"])

    def test_history_r4_to_r5_latest_dispatch_and_native_final_override_old_report(self):
        self.dispatch("HISTORY")
        old = self.json("history-r4.json", {"project_id": "project-one", "task_id": "HISTORY", "delivery_revision": 4,
            "blocker": {"reason": "old external field dependency", "owner": "upstream", "category": "dependency"}})
        event = {"project_id": "project-one", "task_id": "HISTORY", "owner_agent_id": "worker-HISTORY",
            "channel": "native_agent", "event_id": "HISTORY:4:blocked", "kind": "blocked", "delivery_revision": 4, "report": old}
        self.receive(event)
        self.apply(op="redispatch", task_id="HISTORY", next_revision=5, reason="User narrowed scope; old external dependency withdrawn",
                   user_reference="actual scope narrowing, same owner")
        result = self.audit(self.observation([self.observed_worker("HISTORY", revision=4)]))
        self.assertEqual(result["user_handoff"]["progress"]["counts"]["in_progress"], 1)
        self.assertNotIn("等待外部", result["user_handoff"]["text"])
        final = self.delivery("HISTORY", revision=5)
        # Restore old discovery path to reproduce a latest native terminal arriving before bind-report.
        self.apply(op="bind-report", task_id="HISTORY", path=old["path"], reference="old current-report pointer in captured trajectory")
        result = self.audit(self.observation([self.observed_worker("HISTORY", "completed", 5, final)]))
        self.assertEqual(self.state["tasks"]["HISTORY"]["delivery_revision"], 5)
        self.assertEqual(result["user_handoff"]["kind"], "human_item")
        self.assertIsNone(self.state["tasks"]["HISTORY"]["blocker"])

    def test_accepted_waiting_integration_and_deployment_separate_from_running(self):
        event = self.delivery()
        self.receive(event)
        self.apply(op="next")
        self.close(event["event_id"], "accepted")
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=1)]))
        progress = result["user_handoff"]["progress"]
        self.assertEqual(progress["counts"]["in_progress"], 0)
        self.assertEqual(progress["counts"]["awaiting_integration"], 1)
        self.assertIn("部署", result["user_handoff"]["text"])

    def test_latest_real_trajectory_focus_acceptance_and_resumed_quality_survive_restart(self):
        history = self.delivery("HISTORY", revision=5)
        report = load(history["report"]["path"])
        report["expected"] = ["Two state restorations only", "Original scoring unchanged"]
        history["report"] = self.json("history-arrays-r5.json", report)
        self.receive(history)
        operation_log = self.delivery("OPERATION_LOG", revision=1)
        self.receive(operation_log)
        self.apply(op="activate", item_id=operation_log["event_id"], user_reference="user currently reviewing operation log page")
        self.apply(op="wait", item_id=operation_log["event_id"], prompt="验收操作日志页面")
        before_payload = copy.deepcopy(self.state["receipts"][history["event_id"]]["payload"])
        self.apply(op="record-acceptance", task_id="HISTORY", delivery_revision=5, report=history["report"],
            scope="confirmed_task_scope", user_reference="本轮历史接入验收通过",
            evidence=self.file("history-r5-user-accepted.json", '{"scope":"two restored states","accepted":true}'))
        self.dispatch("WEIGHTS")
        self.apply(op="describe", task_id="WEIGHTS", title="权重候选整体质量", reference="current user instructions",
            facts=[{"text": "r3界面流程已接受，整体质量尚未接受", "reference": "UI-only acceptance"}])
        # Simulate a stopped prior run before recording its explicit resource queue.
        weights = self.delivery("WEIGHTS", revision=3, quality="pending")
        self.receive(weights)
        self.apply(op="queue", task_id="WEIGHTS", mode="quality_rework", reason="previously no free executor",
                   user_reference="same confirmed quality scope")
        self.apply(op="start", task_id="WEIGHTS", user_reference="original owner resumed for real-input-2 r4 exploration")
        self.dispatch("MR_FIX")
        self.dispatch("TOOLKIT")
        self.apply(op="register", task_id="TOOLKIT_REVIEW", owner_agent_id="worker-TOOLKIT_REVIEW",
                   worktree=str(self.root / "review"), branch="review")
        self.apply(op="confirm", task_id="TOOLKIT_REVIEW", scope="same toolkit independent review", acceptance=["verify candidate"], user_reference="review scope")
        self.apply(op="queue", task_id="TOOLKIT_REVIEW", reason="wait for author R4", user_reference="same requirement")
        self.apply(op="describe", task_id="TOOLKIT_REVIEW", business_task_id="TOOLKIT", task_role="review", reference="one business requirement")
        workers = [self.observed_worker("HISTORY", "completed", 5), self.observed_worker("WEIGHTS", "running", 3),
                   dict(self.observed_worker("OPERATION_LOG", "completed", 1), lifecycle="closed", fixture_service_retained=True),
                   self.observed_worker("MR_FIX"), self.observed_worker("TOOLKIT")]
        result = self.audit(self.observation(workers))
        self.assertEqual(result["user_handoff"]["kind"], "focus")
        self.assertEqual(self.state["receipts"][history["event_id"]]["payload"], before_payload)
        self.close(operation_log["event_id"], "accepted")
        result = self.audit(self.observation(workers))
        progress = result["user_handoff"]["progress"]
        self.assertEqual(progress["counts"]["awaiting_integration"], 2)
        self.assertEqual(progress["counts"]["in_progress"], 3)
        self.assertEqual(progress["counts"]["total"], 5)
        self.assertEqual(progress["counts"]["queued"], 0)
        self.assertNotIn("等待外部", result["user_handoff"]["text"])
        path = self.root / "restarted.json"
        write_atomic(path, self.state)
        restored = json.loads(self.cli(path, "progress").stdout)
        self.assertEqual(restored["text"], result["user_handoff"]["text"])

    def test_changed_native_evidence_and_reordered_old_event_do_not_restore_false_blocker(self):
        self.dispatch()
        worker = self.observed_worker()
        self.audit(self.observation([worker]))
        Path(worker["evidence"]["path"]).write_text("changed observation", encoding="utf-8")
        path = self.root / "state.json"
        write_atomic(path, self.state)
        stale = json.loads(self.cli(path, "progress").stdout)
        self.assertFalse(stale["progress"]["verified"])
        self.assertIsNone(stale["progress"]["counts"]["in_progress"])

    def test_queued_work_can_start_after_verified_terminal_frees_capacity(self):
        self.state["project"]["worker_capacity"] = 1
        self.dispatch()
        self.apply(op="register", task_id="B", owner_agent_id="worker-B", worktree=str(self.root / "B"), branch="b")
        self.apply(op="confirm", task_id="B", scope="second task", acceptance=["result"], user_reference="confirmed")
        self.apply(op="queue", task_id="B", reason="one slot occupied", user_reference="start when available")
        with self.assertRaises(ValueError):
            self.apply(op="start", task_id="B", user_reference="start")
        self.receive(self.delivery())
        self.apply(op="start", task_id="B", user_reference="slot freed by completed A")
        self.assertEqual(self.state["tasks"]["B"]["worker_run"]["status"], "running")

    def test_technical_and_external_waiting_are_separate_progress_categories(self):
        event, request, review = test_external_followup.ExternalFollowupTests.external_event(self)
        self.receive(event)
        self.apply(op="route-blocker", task_id="A", delivery_revision=4, owner="upstream-technical-team",
                   action="Inspect producer code", evidence=self.file("assigned.json"))
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        self.assertEqual(result["user_handoff"]["progress"]["counts"]["technical_blocked"], 1)
        self.apply(op="request-materials", task_id="A", source_receipt=event["event_id"], request=request, technical_review=review)
        self.apply(op="next")
        self.apply(op="close", item_id="FIELD_SOURCE_REQUEST", outcome="requested", user_reference="Fixture user confirms external request sent",
                   request_evidence=self.file("sent.json"))
        result = self.audit(self.observation([self.observed_worker(status="completed", revision=4)]))
        self.assertEqual(result["user_handoff"]["progress"]["counts"]["external_wait"], 1)
        self.assertEqual(result["user_handoff"]["progress"]["counts"]["in_progress"], 0)

    def test_r3_U01_U02_known_limits_remain_visible_and_safe(self):
        event, request, review = test_external_followup.ExternalFollowupTests.external_event(self, reviewed=True)
        self.receive(event)
        self.apply(op="next")
        path = self.root / "known-limits.json"
        write_atomic(path, self.state)
        original = path.read_bytes()
        missing = self.cli(path, "close", "FIELD_SOURCE_REQUEST", "--outcome", "requested", "--user-reference", "no proof fixture")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("NoneType", missing.stderr)  # R3-U02 retained, not silently reported as fixed.
        self.assertEqual(path.read_bytes(), original)
        self.apply(op="close", item_id="FIELD_SOURCE_REQUEST", outcome="requested", user_reference="fixture sent",
                   request_evidence=self.file("actual-request.json"))
        with self.assertRaisesRegex(ValueError, "not active"):
            self.apply(op="close", item_id="FIELD_SOURCE_REQUEST", outcome="provided", user_reference="later materials",
                       request_evidence=self.file("materials.json"))
        self.apply(op="redispatch", task_id="A", next_revision=5, reason="verify received materials under existing scope",
                   user_reference="R3-U01 documented task-based path")
        self.assertEqual(self.state["tasks"]["A"]["worker_run"]["expected_delivery_revision"], 5)
