"""Real evidence/transition gates, synthetic ref observations; no Git/CAS/remote proof."""
import copy
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from coordination import load
from merge_gate import gate as scoped_gate
from test_coordination import LedgerFixture


def gate(state, task_id):
    return scoped_gate(state, task_id, actor_id="delivery-worker")


class MetadataGateWithoutGitTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.main = "1" * 40
        self.state["project"]["local_main_repo"] = str(self.root / "synthetic-main-observation")
        self.heads = {"A": "a" * 40, "B": "b" * 40}
        from test_delivery import DeliveryFixture
        for key in ("A", "B"):
            self.dispatch(key, authorized_rights=["commit", "push", "mr", "merge"])
        DeliveryFixture.delegate(self)
        self.apply(op="delivery-transport", transport=DeliveryFixture.contacts(self))

    def apply(self, **op):
        from delivery import CONTROL
        if self.state.get("delivery") or op["op"] == "delegate":
            op.setdefault("actor_id", self.state["coordinator_id"] if op["op"] in CONTROL else "delivery-worker")
        return super().apply(**op)

    def receive(self, message, drain=True):
        message = dict(message, recipient_agent_id="delivery-worker")
        return super().receive(message, drain)

    def ci(self, sha):
        from test_delivery import DeliveryFixture
        return DeliveryFixture.ci(self, sha)

    def observed_ref(self, root, *args):
        """Only the external ref reader is substituted; the entire product gate runs."""
        if str(root) == self.state["project"]["local_main_repo"]:
            self.assertEqual(args, ("rev-parse", "refs/heads/main"))
            return self.main
        task_id = next(k for k, task in self.state["tasks"].items() if task["worktree"] == str(root))
        if args == ("branch", "--show-current"):
            return self.state["tasks"][task_id]["branch"]
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return self.heads[task_id]
        self.assertEqual(args, ("merge-base", self.main, self.heads[task_id]))
        return self.main

    def ready(self, name, revision=1):
        if name not in self.state["tasks"]:
            self.dispatch(name, authorized_rights=["commit", "push", "mr", "merge"])
        process = subprocess.run([sys.executable, "-B", "feature.py"], cwd=self.state["tasks"][name]["worktree"],
                                 capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "feature")
        event = self.delivery(name, revision)
        report = load(event["report"]["path"])
        report["git"] = {"head_sha": self.heads[name], "target_sha": self.main}
        report["self_test"]["command"] = "python -B feature.py"
        report["evidence"] = [self.file(name + f"-{revision}.log", process.stdout)]
        event["report"] = self.json(name + f"-{revision}-bound.json", report)
        self.receive(event)
        self.apply(op="activate", item_id=event["event_id"], user_reference="fixture acceptance")
        self.close(event["event_id"], "accepted")
        self.apply(op="mr", task_id=name, mr={"provider": "gitlab", "id": "fixture://MR/" + name,
            "source_branch": self.state["tasks"][name]["branch"], "target_branch": "main",
            "head_sha": self.heads[name], "target_sha": self.main})
        self.apply(op="integration-start", task_id=name)
        evidence = self.file(name + f"-{revision}-integration.log", process.stdout)
        self.apply(op="integration-result", task_id=name, report=self.json(name + f"-{revision}-integration.json",
            {"task_id": name, "project_id": self.state["project"]["id"], "delivery_revision": revision,
             "source_digest": report["source"]["digest"], "head_sha": self.heads[name], "target_sha": self.main,
             "status": "passed", "command": "python -B feature.py", "evidence": [evidence]}))
        self.apply(op="review", task_id=name, coordinator_id=self.state["coordinator_id"], evidence=self.file(name + "-review.txt"))
        from test_delivery import DeliveryFixture
        DeliveryFixture.check_delivery(self, name)

    def test_independent_MR_does_not_wait_for_unmerged_task_and_stale_main_returns_same_author(self):
        self.ready("A")
        with patch("merge_gate.git", side_effect=self.observed_ref):
            self.assertEqual(gate(self.state, "A")["mr_id"], "fixture://MR/A")
        self.state["tasks"]["A"].update(phase="completed", merge={"verified": True, "scope": "synthetic-no-Git", "head_sha": self.heads["A"]})
        self.ready("B")
        identity = {k: self.state["tasks"]["B"][k] for k in ("owner_agent_id", "branch", "authorization")}
        # Synthetic latest-main observation changes; no ref is actually created or merged.
        self.main = self.heads["A"]
        with patch("merge_gate.git", side_effect=self.observed_ref):
            with self.assertRaisesRegex(ValueError, "main changed"):
                gate(self.state, "B")
        self.apply(op="rework", task_id="B", reason="same author syncs latest main and updates same MR")
        self.heads["B"] = "c" * 40
        self.ready("B", revision=2)
        for key, value in identity.items():
            self.assertEqual(self.state["tasks"]["B"][key], value)
        with patch("merge_gate.git", side_effect=self.observed_ref):
            self.assertEqual(gate(self.state, "B")["mr_id"], "fixture://MR/B")
        self.assertFalse(self.state["tasks"]["B"]["authorization"]["deploy"]["allowed"])

    def test_no_authority_acceptance_or_current_evidence_means_no_merge_permission(self):
        self.ready("A")
        valid = copy.deepcopy(self.state)
        for change in (lambda t: t["authorization"]["merge"].update(allowed=False),
                       lambda t: t.update(user_acceptance="pending"), lambda t: t.update(review=None)):
            with self.subTest(change=change):
                self.state = copy.deepcopy(valid)
                change(self.state["tasks"]["A"])
                with patch("merge_gate.git", side_effect=self.observed_ref):
                    with self.assertRaises(ValueError):
                        gate(self.state, "A")
        self.state = valid
        report = load(self.state["tasks"]["A"]["delivery"]["path"])
        proof = Path(report["evidence"][0]["path"])
        original = proof.read_bytes()
        proof.write_text("expired actual test evidence", encoding="utf-8")
        with patch("merge_gate.git", side_effect=self.observed_ref):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                gate(self.state, "A")
        proof.write_bytes(original)
        self.apply(op="rework", task_id="A", reason="update by original submitter")
        changed = dict(self.state["tasks"]["A"]["mr"], id="fixture://new-combined-MR")
        with self.assertRaisesRegex(ValueError, "same MR"):
            self.apply(op="mr", task_id="A", mr=changed)
