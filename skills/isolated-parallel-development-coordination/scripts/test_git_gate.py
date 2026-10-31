"""Actual disposable Git refs, commits, worktrees and competing CAS processes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

from coordination import load, ref, transition
from evidence import pack
from merge_gate import gate as scoped_gate, local_merge as scoped_merge
from test_coordination import LedgerFixture


def gate(state, task_id):
    return scoped_gate(state, task_id, actor_id="delivery-worker")


def local_merge(state, token, recover=False):
    return scoped_merge(state, token, recover=recover, actor_id="delivery-worker")


class GitGateTests(LedgerFixture):
    def apply(self, **op):
        from delivery import CONTROL
        if self.state.get("delivery") or op["op"] == "delegate":
            op.setdefault("actor_id", self.state["coordinator_id"] if op["op"] in CONTROL or op["op"] == "enqueue" else "delivery-worker")
        return super().apply(**op)

    def receive(self, message, drain=True):
        return super().receive(dict(message, recipient_agent_id="delivery-worker"), drain)

    def run_git(self, root, *args):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        result = subprocess.run(["git", "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
            "-c", "commit.gpgsign=false", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-C", str(root), *args], capture_output=True, encoding="utf-8", env=env, timeout=20)
        self.git_attempts.append({"cwd": str(root), "args": args, "exit_code": result.returncode,
                                  "stdout": result.stdout, "stderr": result.stderr})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def setUp(self):
        super().setUp()
        self.git_attempts = []
        self.addCleanup(self.save_trace)
        self.bare = self.root / "sandbox" / "main.git"
        self.bare.mkdir(parents=True)
        self.run_git(self.bare, "init", "--bare", "--initial-branch=main")
        seed = self.root / "seed"
        seed.mkdir()
        self.run_git(seed, "init", "--initial-branch=main")
        (seed / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        self.run_git(seed, "add", "app.py")
        self.run_git(seed, "commit", "-m", "fixture base")
        self.base = self.run_git(seed, "rev-parse", "HEAD")
        self.run_git(self.bare, "fetch", str(seed), "main:refs/heads/main")
        self.state["project"].update(local_main_repo=str(self.bare), local_cas_root=str(self.root / "sandbox"))
        for name in ("A", "B"):
            work = self.root / name
            self.run_git(self.root, "clone", "--no-hardlinks", str(self.bare), str(work))
            self.run_git(work, "switch", "-c", "task/" + name)
            (work / (name + ".txt")).write_text("implemented " + name, encoding="utf-8")
            self.run_git(work, "add", ".")
            self.run_git(work, "commit", "-m", "fixture task " + name)
            self.apply(op="dispatch", task_id=name, owner_agent_id="worker-" + name, worktree=str(work),
                branch="task/" + name, scope="fixture feature " + name, acceptance=["answer is 42 and task file exists"],
                user_reference="explicit fixture-only Git exercise", authorized_rights=["commit", "push", "mr", "merge"],
                report_path=str(self.root / (name + ".json")))
        import test_delivery
        test_delivery.DeliveryFixture.delegate(self)
        self.apply(op="delivery-transport", transport=test_delivery.DeliveryFixture.contacts(self))

    def fresh_check(self, name):
        task = self.state["tasks"][name]
        report = load(task["delivery"]["path"])
        mr = task["mr"]
        value = {"project_id": self.state["project"]["id"], "task_id": name, "delivery_revision": task["delivery_revision"],
            "based_on_revision": self.state["revision"], "source_digest": report["source"]["digest"],
            "head_sha": mr["head_sha"], "main_sha": mr["target_sha"], "ci": {"head_sha": mr["head_sha"],
                "pipeline_id": "local-fixture-only", "status": "passed", "jobs": {"test": "passed", "build": "passed"},
                "evidence": report["evidence"]}}
        self.apply(op="delivery-check", task_id=name, observation=self.json(name + "-current-ci.json", value))

    def save_trace(self):
        target = os.environ.get("COORDINATION_TEST_EVIDENCE")
        if target:
            directory = Path(target)
            self.assertTrue(directory.is_absolute())
            directory.mkdir(parents=True, exist_ok=True)
            value = {"test": self.id(), "scope": "disposable-local-git-only", "commands": self.git_attempts,
                     "ledger": self.state}
            (directory / (self._testMethodName + ".json")).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def ready(self, name, revision=1, target=None):
        target = target or self.base
        root = self.root / name
        # An actual independent subprocess tests the actual checkout; log is then hash-bound.
        test_code = "from app import answer; from pathlib import Path; assert answer()==42; assert Path(" + repr(name + ".txt") + ").exists(); print('functional and integration fixture passed')"
        result = subprocess.run([sys.executable, "-B", "-c", test_code], cwd=root, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.file(f"{name}-{revision}.log", result.stdout)
        head = self.run_git(root, "rev-parse", "HEAD")
        report = pack(dict(project_id="project-one", task_id=name, delivery_revision=revision,
            summary="Accept " + name, source_root=str(root), evidence=[log],
            self_test={"status": "passed", "command": "python -B -c " + test_code}, quality_review={"status": "passed"},
            entry={"kind": "file", "target": str(root / (name + ".txt"))}, steps=["Inspect task file"],
            expected="implemented " + name, git={"head_sha": head, "target_sha": target}))
        delivery = self.json(f"{name}-{revision}.report.json", report)
        self.apply(op="bind-report", task_id=name, path=delivery["path"], reference="immutable revision path")
        event = dict(project_id="project-one", task_id=name, owner_agent_id="worker-" + name,
                     channel="native_agent", kind="completed", delivery_revision=revision,
                     event_id=f"{name}:{revision}:completed", report=delivery)
        self.receive(event)
        self.apply(op="activate", item_id=event["event_id"], user_reference="fixture chooses this acceptance")
        self.close(event["event_id"], "accepted")
        self.apply(op="mr", task_id=name, mr={"provider": "gitlab", "id": "fixture://gitlab/project/!" + name,
            "source_branch": "task/" + name, "target_branch": "main", "head_sha": head, "target_sha": target})
        self.apply(op="integration-start", task_id=name)
        integration = dict(project_id="project-one", task_id=name, delivery_revision=revision,
            source_digest=report["source"]["digest"], head_sha=head, target_sha=target,
            status="passed", command="python -B -c " + test_code, evidence=[log])
        # Run the integration command a second time on this exact candidate, not only a constant status.
        result = subprocess.run([sys.executable, "-B", "-c", test_code], cwd=root, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.apply(op="integration-result", task_id=name, report=self.json(f"{name}-{revision}.integration.json", integration))
        self.apply(op="review", task_id=name, coordinator_id="main-session", evidence=self.file(f"{name}-{revision}.review"))
        # Import objects locally; this does not change main or contact a service.
        self.run_git(self.bare, "fetch", str(root), head)
        self.fresh_check(name)
        return head

    def reserve(self, name, token):
        if not self.state["merge_reservation"]:
            self.fresh_check(name)
        return self.apply(op="reserve", task_id=name, token=token, expected_revision=self.state["revision"])

    def test_A_then_B_stale_target_rejected_same_worker_MR_updated_and_retested(self):
        a, b = self.ready("A"), self.ready("B")
        with self.assertRaisesRegex(ValueError, "merge order"):
            gate(self.state, "B")
        self.reserve("A", "a-token")
        with self.assertRaisesRegex(ValueError, "already held"):
            self.reserve("B", "b-token")
        self.state = local_merge(self.state, "a-token")
        self.assertEqual(self.run_git(self.bare, "rev-parse", "main"), a)
        self.assertEqual(self.state["tasks"]["A"]["phase"], "completed")
        with self.assertRaisesRegex(ValueError, "fresh same-revision|main changed"):
            gate(self.state, "B")
        self.apply(op="rework", task_id="B", reason="main moved; same worker updates existing MR")
        self.run_git(self.root / "B", "fetch", str(self.bare), "main")
        self.run_git(self.root / "B", "rebase", "FETCH_HEAD")
        b2 = self.ready("B", 2, a)
        self.assertNotEqual(b, b2)
        self.assertEqual(self.state["tasks"]["B"]["mr"]["id"], "fixture://gitlab/project/!B")
        self.assertEqual(self.state["tasks"]["B"]["owner_agent_id"], "worker-B")
        self.reserve("B", "b-token")
        self.state = local_merge(self.state, "b-token")
        self.assertEqual(self.run_git(self.bare, "rev-parse", "main"), b2)
        self.assertFalse(self.state["tasks"]["B"]["authorization"]["deploy"]["allowed"])

    def test_gate_rejects_permission_acceptance_evidence_and_MR_identity_changes(self):
        self.ready("A")
        good = copy.deepcopy(self.state)
        mutations = [lambda t: t["authorization"]["merge"].update(allowed=False),
                     lambda t: t.update(user_acceptance="pending"),
                     lambda t: t.update(review=None),
                     lambda t: t["integration"].update(status="failed")]
        for mutation in mutations:
            candidate = copy.deepcopy(good)
            mutation(candidate["tasks"]["A"])
            with self.assertRaises(ValueError):
                gate(candidate, "A")
        with self.assertRaisesRegex(ValueError, "same MR"):
            self.apply(op="mr", task_id="A", mr=dict(good["tasks"]["A"]["mr"], id="different-MR"))
        Path(good["tasks"]["A"]["integration"]["report"]["path"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            gate(good, "A")

    def test_ref_race_after_gate_cannot_overwrite_main_and_crash_recovery_is_verified(self):
        a, b = self.ready("A"), self.ready("B")
        self.reserve("A", "token")
        # A different writer changes main between fresh check and mutation.
        self.run_git(self.bare, "update-ref", "refs/heads/main", b, self.base)
        with self.assertRaisesRegex(ValueError, "main changed"):
            local_merge(self.state, "token")
        self.assertEqual(self.run_git(self.bare, "rev-parse", "main"), b)
        # Return only this disposable ref, then simulate crash after CAS and before state save.
        self.run_git(self.bare, "update-ref", "refs/heads/main", self.base, b)
        self.run_git(self.bare, "update-ref", "refs/heads/main", a, self.base)
        self.state = local_merge(self.state, "token", recover=True)
        self.assertTrue(self.state["tasks"]["A"]["merge"]["recovered"])
        self.assertFalse(self.state["tasks"]["A"]["authorization"]["deploy"]["allowed"])

    def test_simultaneous_real_update_ref_CAS_only_one_writer_wins(self):
        a, b = self.ready("A"), self.ready("B")
        commands = [["git", "-C", str(self.bare), "update-ref", "refs/heads/main", sha, self.base] for sha in (a, b)]
        processes = [subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for c in commands]
        outputs = [p.communicate(timeout=20) for p in processes]
        self.git_attempts.append({"simultaneous_CAS": [{"command": c, "exit_code": p.returncode,
            "stdout": out[0].decode(), "stderr": out[1].decode()} for c, p, out in zip(commands, processes, outputs)]})
        self.assertEqual(sum(p.returncode == 0 for p in processes), 1, outputs)
        self.assertIn(self.run_git(self.bare, "rev-parse", "main"), (a, b))

    def test_reservation_stale_revision_new_receipt_dirty_source_and_sandbox_denied(self):
        self.ready("A")
        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.apply(op="reserve", task_id="A", token="bad", expected_revision=self.state["revision"] - 1)
        self.reserve("A", "token")
        self.human("new-user-input")
        with self.assertRaisesRegex(ValueError, "ledger changed"):
            local_merge(self.state, "token")
        self.apply(op="release-reservation", token="token", reason="new input requires fresh check")
        self.reserve("A", "fresh")
        candidate = copy.deepcopy(self.state)
        candidate["project"]["local_cas_root"] = None
        with self.assertRaisesRegex(ValueError, "sandbox"):
            local_merge(candidate, "fresh")
        (self.root / "A" / "untracked.txt").write_text("new untested source", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            local_merge(self.state, "fresh")
