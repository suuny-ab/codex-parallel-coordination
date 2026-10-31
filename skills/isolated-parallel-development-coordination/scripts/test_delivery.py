"""R6 delegated delivery: actual source/evidence/CLI, synthetic refs, no Git/host writes."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from coordination import load, transition, write_atomic
from delivery import CONTROL, release_gate, transport_issues
from merge_gate import gate, local_merge
from reconcile import closeout
from test_coordination import LedgerFixture
import test_mr_metadata_no_git
import test_progress_fallback


class DeliveryFixture(LedgerFixture):
    ready = test_mr_metadata_no_git.MetadataGateWithoutGitTests.ready
    observed_ref = test_mr_metadata_no_git.MetadataGateWithoutGitTests.observed_ref
    observed_worker = test_progress_fallback.ProgressFallbackTests.observed_worker

    def apply(self, **op):
        if self.state.get("delivery") or op["op"] == "delegate":
            op.setdefault("actor_id", self.state["coordinator_id"] if op["op"] in CONTROL else "delivery-worker")
        return super().apply(**op)

    def receive(self, message, drain=True):
        message = copy.deepcopy(message)
        if self.state.get("delivery"):
            message["recipient_agent_id"] = "delivery-worker"
        return super().receive(message, drain)

    def delegate(self):
        routes = {"worker-" + key: {"target_id": "existing-thread-" + key, "existing_work_agent": True,
            "evidence": self.file(key + "-route-authority.txt", "Fixture existing-author-only message permission")} for key in ("A", "B")}
        self.apply(op="delegate", agent_id="delivery-worker", task_ids=["A", "B"], merge_order=["A", "B"],
            required_ci_jobs=["test", "build"], routes=routes, authority_evidence=self.file("delegate-authority.txt"),
            user_reference="Fixture user authorizes sole delivery executor; task rights and deploy remain independent")

    def contacts(self, acknowledged=True):
        authors = {}
        for key in ("A", "B"):
            authors["worker-" + key] = {"outbound": self.json(key + "-send.json", {
                "sender_id": "delivery-worker", "recipient_id": "worker-" + key, "target_id": "existing-thread-" + key,
                "status": "accepted", "mode": "fixture", "correlation_id": "fixture-request-" + key})}
            if acknowledged:
                authors["worker-" + key]["inbound"] = self.json(key + "-ack.json", {
                    "sender_id": "worker-" + key, "recipient_id": "delivery-worker", "status": "acknowledged",
                    "mode": "fixture", "correlation_id": "fixture-request-" + key})
        return {"mode": "fixture", "send_tool": "declared_existing_message_tool", "receive_tool": "declared_existing_read_tool",
                "creates_conversation": False, "authors": authors}

    def check_delivery(self, key="A", **changes):
        task = self.state["tasks"][key]
        report = load(task["delivery"]["path"])
        value = {"project_id": self.state["project"]["id"], "task_id": key, "delivery_revision": task["delivery_revision"],
                 "based_on_revision": self.state["revision"], "source_digest": report["source"]["digest"],
                 "head_sha": self.heads[key], "main_sha": self.main, "ci": self.ci(self.heads[key])}
        value.update(changes)
        observation = self.json(key + "-check.json", value)
        self.apply(op="delivery-check", task_id=key, observation=observation)
        return observation

    def ci(self, sha):
        return {"head_sha": sha, "pipeline_id": "fixture-pipeline-" + sha[:4], "status": "passed",
                "jobs": {"test": "passed", "build": "passed"}, "evidence": [self.file(sha[:4] + "-ci.log")]}

    def setUp(self):
        super().setUp()
        self.main, self.heads = "1" * 40, {"A": "a" * 40, "B": "b" * 40}
        self.state["project"]["local_main_repo"] = str(self.root / "synthetic-main")
        for key in ("A", "B"):
            self.dispatch(key, authorized_rights=["commit", "push", "mr", "merge"])
        self.delegate()
        self.apply(op="delivery-transport", transport=self.contacts())

    def delivery_audit(self):
        workers = [self.observed_worker(key, "completed" if self.state["tasks"][key]["delivery_revision"] else "running",
                                        self.state["tasks"][key]["delivery_revision"]) for key in ("A", "B")]
        observation = self.observation(workers)
        observation["recipient_agent_id"] = "delivery-worker"
        self.state, result = closeout(self.state, observation, self.json("audit.json", observation), actor_id="delivery-worker")
        return result

    def merged_fixture(self, key="A"):
        # Explicit synthetic already-merged observation, never a real Git merge/CAS.
        self.state["tasks"][key].update(phase="completed", merge={"verified": True, "head_sha": self.heads[key], "scope": "synthetic-no-Git"})

    def release_plan(self):
        self.merged_fixture()
        sha = self.heads["A"]
        artifact = self.file("frozen-release.zip", "frozen A bytes")
        plan = {"release_sha": sha, "included_task_ids": ["A"], "before_tasks": ["B"], "environment": "fixture-test",
                "tag": "fixture-v1", "artifact": artifact,
                "authority_evidence": self.file("release-separate-authority.txt", "User approves A SHA only, not B"),
                "manifest": self.json("release-manifest.json", {"release_sha": sha, "included_task_ids": ["A"], "artifact_sha256": artifact["sha256"]}),
                "actions": ["tag", "backup", "deploy", "verify"]}
        self.apply(op="authorize-release", plan=plan, user_reference="Separate fixture release approval for exact SHA and ordered actions")
        return plan

    def release_observation(self, **changes):
        plan = self.state["release_plan"]
        value = {"release_sha": plan["release_sha"], "checkout_sha": plan["release_sha"], "artifact": plan["artifact"],
                 "environment": plan["environment"], "ci": self.ci(plan["release_sha"]), "current_main_sha": self.main,
                 "active_creation": False, "data_preservation_verified": True, "safety_evidence": [self.file("safety.txt")]}
        value.update(changes)
        return self.json("release-observation.json", value)

    def release_step(self, action):
        plan = self.state["release_plan"]
        evidence = self.file(action + "-result.log", "Fixture " + action + " completed, no real external action")
        result = self.json(action + "-result.json", {"status": "passed", "action": action, "release_sha": plan["release_sha"],
            "artifact": plan["artifact"], "environment": plan["environment"], "tag": plan["tag"], "evidence": [evidence]})
        self.apply(op="release-step", action=action, observation=self.release_observation(), result=result)


class DeliveryTests(DeliveryFixture):
    def test_delivery_cannot_invent_unreviewed_business_decision_via_generic_enqueue(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.apply(op="enqueue", actor_id="delivery-worker", item={"id": "UNREVIEWED", "kind": "decision",
                "title": "Assume field semantics", "prompt": "Confirm unsupported parser assumption?", "options": ["Yes", "No"]})
        self.assertEqual(before, self.state)
    def test_release_summary_and_same_revision_expired_proof_cannot_disagree(self):
        from progress import round_handoff
        from coordination import checkpoint
        plan = self.release_plan()
        result = self.delivery_audit()
        self.assertTrue(result["user_handoff"]["progress"]["verified"])
        self.assertIn("冻结版本已独立授权", result["user_handoff"]["text"])
        Path(plan["artifact"]["path"]).write_text("future unapproved artifact", encoding="utf-8")
        self.assertFalse(checkpoint(self.state)["allow_end"])
        handoff = round_handoff(self.state, checkpoint(self.state))
        self.assertFalse(handoff["progress"]["verified"])
        self.assertIsNone(handoff["progress"]["counts"]["in_progress"])

    def test_uninstalled_stop_adapter_keeps_routine_technical_work_off_root(self):
        from stop_adapter import response
        self.assertEqual(response(self.state, {"hook_event_name": "Stop", "session_id": "main-session"}), {})
        self.assertEqual(response(self.state, {"hook_event_name": "Stop", "session_id": "delivery-worker"})["decision"], "block")
        self.assertEqual(response(self.state, {"hook_event_name": "SubagentStop", "session_id": "delivery-worker"}), {})

    def test_missing_report_headers_and_custom_test_conclusion_reconcile_before_canonical(self):
        event = self.delivery("A")
        raw = load(event["report"]["path"])
        for key in ("task_id", "project_id", "delivery_revision"):
            raw.pop(key)
        raw["self_test"]["status"] = "author_custom_local_and_CI_green"
        event["report"] = self.json("original-author-contract.json", raw)
        original = Path(event["report"]["path"]).read_bytes()
        self.receive(event)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 0)
        result = self.delivery_audit()
        self.assertEqual(result["user_handoff"]["kind"], "needs_reconcile")
        self.assertEqual(original, Path(event["report"]["path"]).read_bytes())
        # Even known headers cannot imply that the author's custom conclusion means passed.
        raw.update(task_id="A", project_id="project-one", delivery_revision=1)
        event["report"] = self.json("headers-known-custom-status.json", raw)
        self.receive(event)
        self.assertNotIn(event["event_id"], self.state["receipts"])
        self.assertTrue(any("custom self-test" in x["reason"] for x in self.state["unverified_receipts"].values()))

    def test_App_thread_final_requires_author_mapping_and_completion_not_send_or_ack(self):
        event = self.delivery("A")
        event.update(channel="app_thread", recipient_agent_id="delivery-worker")
        proof = {"sender_id": "worker-A", "recipient_id": "delivery-worker", "sender_target_id": "existing-thread-A",
                 "event_id": event["event_id"], "kind": "accepted"}
        for stage in ("accepted", "acknowledged"):
            proof["kind"] = stage
            event["transport_evidence"] = self.json(stage + "-not-final.json", proof)
            with self.assertRaises(ValueError):
                self.receive(event)
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 0)
        proof["kind"] = "task_final"
        event["transport_evidence"] = self.json("actual-fixture-final.json", proof)
        self.receive(event)
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 1)
        self.assertEqual(self.state["tasks"]["A"]["phase"], "awaiting_acceptance")

    def test_CLI_actor_and_persistent_delivery_closeout_do_not_need_root_relay(self):
        import subprocess
        import sys
        path = self.root / "cli-state.json"
        write_atomic(path, self.state)
        observation = self.observation([self.observed_worker(key) for key in ("A", "B")])
        observation["recipient_agent_id"] = "delivery-worker"
        input_ref = self.json("cli-observation.json", observation)
        argv = [sys.executable, "-B", str(Path(__file__).with_name("coordination.py")), "--state", str(path),
                "--actor", "delivery-worker", "closeout", input_ref["path"]]
        result = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertIsNone(value["notify_main"])
        self.assertEqual(load(path)["delivery"]["agent_id"], "delivery-worker")
        argv[argv.index("delivery-worker")] = "main-session"
        before = path.read_bytes()
        denied = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(before, path.read_bytes())

    def test_absent_delegation_root_or_other_writer_cannot_merge_and_rights_not_inferred(self):
        self.ready("A")
        self.check_delivery()
        with patch("merge_gate.git", side_effect=self.observed_ref):
            self.assertEqual(gate(self.state, "A", actor_id="delivery-worker")["task_id"], "A")
            for actor in (None, "main-session", "worker-A", "another-delivery"):
                with self.subTest(actor=actor), self.assertRaises(ValueError):
                    gate(self.state, "A", actor_id=actor)
            no_delegate = copy.deepcopy(self.state)
            no_delegate.pop("delivery")
            with self.assertRaises(ValueError):
                gate(no_delegate, "A", actor_id="main-session")
            self.state["tasks"]["A"]["authorization"]["merge"]["allowed"] = False
            with self.assertRaisesRegex(ValueError, "not authorized"):
                gate(self.state, "A", actor_id="delivery-worker")

    def test_single_executor_reservation_and_explicit_revocation(self):
        self.ready("A")
        self.check_delivery()
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.delegate()
        self.assertEqual(self.state, before)
        with patch("merge_gate.git", side_effect=self.observed_ref):
            self.apply(op="reserve", task_id="A", token="one-writer", expected_revision=self.state["revision"])
            with self.assertRaises(ValueError):
                self.apply(op="reserve", task_id="B", token="other", expected_revision=self.state["revision"])
        with self.assertRaises(ValueError):
            self.apply(op="revoke-delivery", user_reference="revoke", evidence=self.file("revoke.txt"))
        with self.assertRaises(ValueError):
            local_merge(self.state, "one-writer", actor_id="main-session")

    def test_source_main_CI_changes_and_function_rework_go_to_original_author(self):
        self.ready("A")
        observation = self.check_delivery()
        accepted = copy.deepcopy(self.state)
        for mutation in (lambda x: x.update(head_sha="f" * 40), lambda x: x.update(main_sha="f" * 40),
                         lambda x: x["ci"]["jobs"].update(build="failed")):
            with self.subTest(mutation=mutation):
                value = load(observation["path"])
                mutation(value)
                write_atomic(observation["path"], value)
                with patch("merge_gate.git", side_effect=self.observed_ref), self.assertRaises(ValueError):
                    gate(self.state, "A", actor_id="delivery-worker")
        self.state = accepted
        task = self.state["tasks"]["A"]
        old = {k: copy.deepcopy(task[k]) for k in ("owner_agent_id", "requirements", "authorization", "mr")}
        self.apply(op="rework", task_id="A", reason="CI/source/main or genuine functional issue: original author fixes and resubmits same MR")
        for key, value in old.items():
            self.assertEqual(self.state["tasks"]["A"][key], value)
        self.assertEqual(self.state["tasks"]["A"]["worker_run"]["owner_agent_id"], "worker-A")
        self.assertEqual(self.state["items"]["A:1:completed"]["outcome"], "accepted")
        self.assertIsNone(self.state["tasks"]["A"].get("delivery_check"))

    def test_current_CI_required_job_and_actual_source_file_are_checked_not_just_packet_hash(self):
        self.ready("A")
        before = copy.deepcopy(self.state)
        failed = self.ci(self.heads["A"])
        failed["jobs"]["build"] = "failed"
        with self.assertRaisesRegex(ValueError, "CI stale"):
            self.check_delivery(ci=failed)
        self.assertEqual(before, self.state)
        self.check_delivery()
        feature = Path(self.state["tasks"]["A"]["worktree"]) / "feature.py"
        feature.write_text("print('untested source change')", encoding="utf-8")
        with patch("merge_gate.git", side_effect=self.observed_ref), self.assertRaisesRegex(ValueError, "inventory changed"):
            gate(self.state, "A", actor_id="delivery-worker")

    def test_delivery_handles_technical_receipts_without_root_poll_or_confirmation(self):
        result = self.delivery_audit()
        self.assertIsNone(result["notify_main"])
        self.assertFalse(result["routine_root_confirmation_required"])
        self.assertEqual(result["technical_owner"], "delivery-worker")
        self.assertEqual(self.state["items"], {})
        observation = self.observation([])
        observation["recipient_agent_id"] = "main-session"
        with self.assertRaises(ValueError):
            closeout(self.state, observation, self.json("root-audit.json", observation), actor_id="main-session")
        event = self.delivery("A")
        with self.assertRaises(ValueError):
            transition(self.state, {"op": "receive", "receipt": event, "actor_id": "main-session"})
        self.receive(event)
        self.assertIn("A:1:completed", self.state["receipts"])

    def test_user_acceptance_stays_original_main_and_atomic_focus_is_not_interrupted(self):
        self.apply(op="enqueue", actor_id="main-session", item={"id": "BUSINESS", "kind": "discussion", "title": "Current scope", "prompt": "Resolve current scope"})
        self.apply(op="next")
        self.receive(self.delivery("A"))
        result = self.delivery_audit()
        self.assertEqual(result["user_handoff"]["kind"], "focus")
        self.assertIsNone(result["notify_main"])
        self.close("BUSINESS")
        result = self.delivery_audit()
        self.assertEqual(result["notify_main"]["kind"], "human_item")
        self.assertEqual(result["notify_main"]["recipient_id"], "main-session")
        self.apply(op="next")
        with self.assertRaises(ValueError):
            self.apply(op="close", actor_id="delivery-worker", item_id="A:1:completed", outcome="accepted", user_reference="technical review only")
        self.close("A:1:completed", "accepted")
        self.assertEqual(self.state["tasks"]["A"]["phase"], "awaiting_integration")

    def test_acceptance_acknowledgment_and_completion_are_separate_and_targets_scoped(self):
        pending = self.contacts(acknowledged=False)
        self.apply(op="delivery-transport", transport=pending)
        self.assertTrue(transport_issues(self.state))
        self.assertEqual(self.state["tasks"]["A"]["delivery_revision"], 0)
        first = self.delivery_audit()
        self.assertEqual(first["notify_main"]["kind"], "important_delivery_exception")
        self.assertIsNone(self.delivery_audit()["notify_main"])
        self.apply(op="delivery-transport", transport=self.contacts())
        self.assertFalse(transport_issues(self.state))
        self.assertEqual(self.state["tasks"]["A"]["phase"], "developing")
        self.assertEqual(self.state["receipts"], {})
        invalid = self.contacts()
        invalid["authors"]["unrelated-author"] = invalid["authors"]["worker-A"]
        with self.assertRaises(ValueError):
            self.apply(op="delivery-transport", transport=invalid)
        invalid = self.contacts()
        invalid["send_tool"] = "create_thread"
        with self.assertRaises(ValueError):
            self.apply(op="delivery-transport", transport=invalid)
        # Unrelated correlation cannot count as peer acknowledgment.
        invalid = self.contacts()
        ack = load(invalid["authors"]["worker-A"]["inbound"]["path"])
        ack["correlation_id"] = "some-other-handoff"
        invalid["authors"]["worker-A"]["inbound"] = self.json("wrong-ack.json", ack)
        with self.assertRaises(ValueError):
            self.apply(op="delivery-transport", transport=invalid)

    def test_release_needs_separate_authority_and_frozen_SHA_excludes_future_main(self):
        with self.assertRaises(ValueError):
            release_gate(self.state, "delivery-worker", "tag", self.file("not-release.json"))
        plan = self.release_plan()
        self.main = "f" * 40  # A future main must not replace the selected release.
        release_gate(self.state, "delivery-worker", "tag", self.release_observation())
        with self.assertRaises(ValueError):
            release_gate(self.state, "delivery-worker", "tag", self.release_observation(checkout_sha=self.main))
        with self.assertRaises(ValueError):
            release_gate(self.state, "main-session", "tag", self.release_observation())
        for action in plan["actions"]:
            self.release_step(action)
        self.assertEqual(self.state["release_plan"]["completed_steps"], ["tag", "backup", "deploy", "verify"])
        self.assertEqual(self.state["release_plan"]["release_sha"], "a" * 40)
        self.assertEqual(self.state["release_plan"]["included_task_ids"], ["A"])
        self.assertFalse(self.state["tasks"]["B"]["authorization"]["deploy"]["allowed"])

    def test_release_order_evidence_activity_and_scope_cannot_be_bypassed(self):
        self.release_plan()
        with self.assertRaises(ValueError):
            release_gate(self.state, "delivery-worker", "deploy", self.release_observation())
        self.release_step("tag")
        self.release_step("backup")
        for changes in ({"active_creation": True}, {"data_preservation_verified": False}, {"artifact": self.file("future.zip")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release_gate(self.state, "delivery-worker", "deploy", self.release_observation(**changes))
        proof = Path(self.state["release_plan"]["results"]["backup"]["result"]["path"])
        proof.write_text("expired backup proof", encoding="utf-8")
        with self.assertRaises(ValueError):
            release_gate(self.state, "delivery-worker", "deploy", self.release_observation())

    def test_delegation_restart_source_evidence_and_native_recipient_stay_bound(self):
        path = self.root / "restore.json"
        write_atomic(path, self.state)
        self.state = load(path)
        event = self.delivery("A")
        with self.assertRaises(ValueError):
            transition(self.state, {"op": "receive", "receipt": event, "actor_id": "delivery-worker"})
        event["recipient_agent_id"] = "delivery-worker"
        self.receive(event)
        before = copy.deepcopy(self.state)
        self.receive(event)
        self.assertEqual(before, self.state)
        Path(self.state["delivery"]["authority_evidence"]["path"]).write_text("revoked external authority", encoding="utf-8")
        self.assertTrue(transport_issues(self.state))
