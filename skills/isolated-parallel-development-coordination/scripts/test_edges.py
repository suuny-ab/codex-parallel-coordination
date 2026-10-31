"""Recovery, entry invalidation, scoped permission and protocol boundary regressions."""
import copy
from pathlib import Path

from coordination import checkpoint, load, migrate, new_state, ref, transition, validate, write_atomic
from test_coordination import LedgerFixture


class BoundaryTests(LedgerFixture):
    def test_url_probe_and_file_entry_changes_revoke_v2_readiness(self):
        event = self.delivery()
        report = load(event["report"]["path"])
        probe = self.file("probe.json", '{"status":200}')
        report["entry"] = {"kind": "url", "target": "http://localhost.invalid/review", "probe_evidence": probe}
        event["report"] = self.json("url-report.json", report)
        self.receive(event)
        self.assertEqual(checkpoint(self.state)["next"]["entry"]["kind"], "url")
        Path(probe["path"]).unlink()
        self.assertIsNone(checkpoint(self.state)["next"])

    def test_native_identity_and_same_revision_different_terminal_kind(self):
        event = self.delivery()
        for key, value in (("channel", "chat"), ("owner_agent_id", "another"), ("project_id", "another")):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.receive(dict(event, **{key: value}))
        self.receive(event)
        other = dict(event, event_id="A:1:blocked", kind="blocked", report=self.json("other-terminal.json", {
            "project_id": "project-one", "task_id": "A", "delivery_revision": 1,
            "blocker": {"reason": "technical recheck", "owner": "worker-A", "category": "technical"}}))
        self.receive(other)
        self.assertEqual(self.state["receipts"][other["event_id"]]["status"], "held")
        self.assertIn("same-revision", self.state["receipts"][other["event_id"]]["reason"])

    def test_source_delete_and_evidence_mutation_cannot_reuse_old_acceptance(self):
        event = self.delivery()
        self.receive(event)
        self.apply(op="next")
        self.apply(op="wait", item_id=event["event_id"], prompt="Inspect source")
        (self.root / "A" / "feature.py").unlink()
        self.assertFalse(checkpoint(self.state)["allow_end"])
        with self.assertRaises(ValueError):
            self.close(event["event_id"], "accepted")
        self.close(event["event_id"], "rejected")
        (self.root / "A" / "feature.py").write_text("print('reworked')\n", encoding="utf-8")
        changed = self.delivery(revision=2)
        self.receive(changed)
        report = load(changed["report"]["path"])
        Path(report["evidence"][0]["path"]).write_text("changed", encoding="utf-8")
        self.assertIsNone(checkpoint(self.state)["next"])

    def test_migration_keeps_active_held_and_old_receipt_outcomes_without_completion(self):
        old = {"schema": "parallel-coordination/1", "coordinator_id": "main-session", "revision": 8,
            "active_human_item": "old-active", "tasks": {"A": {"owner_agent_id": "worker-A",
                "worktree": str(self.root / "A"), "branch": "task/A", "resources": [], "delivery_revision": 2,
                "implementation": "completed", "self_test": "passed", "integration": "pending",
                "user_acceptance": "rejected", "release_authorization": "pending", "release": "pending"}},
            "items": {"old-active": {"id": "old-active", "kind": "discussion", "title": "Old",
                "prompt": "Old question", "status": "waiting_user", "sequence": 0,
                "waiting_prompt": "User pending"}, "held": {"id": "held", "kind": "acceptance", "task_id": "A",
                "delivery_revision": 2, "title": "Review", "prompt": "Review", "status": "queued", "sequence": 1,
                "report": {"path": str(self.root / "absent.json"), "sha256": "a" * 64}}},
            "receipts": {"A:2:completed": {"fingerprint": "a" * 64, "status": "received"}}}
        source = self.json("old.json", old)
        state = migrate(old, "new-project", source)
        self.assertEqual(state["legacy"], old)
        self.assertEqual(state["items"]["old-active"]["status"], "waiting_user")
        self.assertEqual(state["receipts"]["A:2:completed"]["legacy_status"], "received")
        self.assertEqual(state["tasks"]["A"]["phase"], "developing")
        self.assertTrue(checkpoint(state)["allow_end"])
        old["items"]["old-active"].update(status="closed", outcome="resolved")
        conflict = migrate(old, "new-project", self.json("focus-conflict-v1.json", old))
        self.assertFalse(checkpoint(conflict)["allow_end"])
        self.assertTrue(conflict["items"]["old-active"]["migration_hold"])
        conflict, _ = transition(conflict, {"op": "reconcile-focus", "item_id": "old-active", "user_reference": "review historical close"})
        conflict, _ = transition(conflict, {"op": "close", "item_id": "old-active", "outcome": "resolved", "user_reference": "historical resolution confirmed"})
        self.assertEqual(checkpoint(conflict)["held"]["held"], ["legacy evidence needs v2 worker delivery"])
        target = self.root / "migrated.json"
        result = self.cli(target, "migrate", "--from", source["path"], "--project", "copied")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load(source["path"]), load(target)["legacy"])
        before = target.read_bytes()
        self.assertEqual(self.cli(target, "migrate", "--from", str(target), "--project", "x").returncode, 1)
        self.assertEqual(target.read_bytes(), before)

    def test_new_event_invalidates_closeout_and_no_deployment_lumped_with_git(self):
        self.audit(self.observation([]))
        old_revision = self.state["revision"]
        self.dispatch()
        self.assertIsNone(self.state["last_reconcile"])
        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.apply(op="drain", expected_revision=old_revision)
        with self.assertRaisesRegex(ValueError, "own explicit scope"):
            self.apply(op="authorize", task_id="A", rights=["merge", "deploy"], user_reference="not separate")
        with self.assertRaisesRegex(ValueError, "Git rights only"):
            self.dispatch("B", authorized_rights=["commit", "deploy"])
        self.assertNotIn("B", self.state["tasks"])

    def test_protected_inventory_is_read_only_and_changed_data_blocks_closeout(self):
        protected = self.file("protected-input.bin", "frozen fixture input")
        self.state["project"]["protected"] = [protected]
        self.assertTrue(self.audit(self.observation([]))["allow_end"])
        Path(protected["path"]).write_text("external modification", encoding="utf-8")
        result = self.audit(self.observation([]))
        self.assertFalse(result["allow_end"])
        self.assertIn("protected data changed", str(result))
        self.assertEqual(Path(protected["path"]).read_text(), "external modification")

    def test_corrupt_receipt_and_focus_not_silently_reinitialized(self):
        event = self.delivery()
        self.receive(event)
        bad = copy.deepcopy(self.state)
        bad["receipts"][event["event_id"]]["payload"]["delivery_revision"] = 99
        with self.assertRaisesRegex(ValueError, "damaged receipt"):
            validate(bad)
        bad = copy.deepcopy(self.state)
        bad["active_human_item"] = "missing"
        with self.assertRaisesRegex(ValueError, "human focus"):
            validate(bad)
