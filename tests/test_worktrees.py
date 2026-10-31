"""Real isolated Git worktrees using unborn branches: no commit, push or tag."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_parallel_coordination.worktrees import GitWorktrees


class Worktrees(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="coordination-worktree-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.scope = self.base / "managed"
        self.scope.mkdir()
        self.tree = self.scope / "fixture"
        self.git("init", "--initial-branch=main")
        self.git("worktree", "add", "--orphan", "-b", "codex/fixture", str(self.tree))
        self.record = {"resource_id": "synthetic-worktree", "path": str(self.tree),
                       "metadata": {"repository_root": str(self.repo), "expected_head": "0" * 40},
                       "references": [], "protected": False}
        self.adapter = GitWorktrees(self.repo, self.scope)
        self.identity = self.adapter.observe(self.record)["identity"]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def test_clean_worktree_removed_without_touching_main(self):
        result = self.adapter.remove(self.record, self.identity)
        self.assertEqual(result["status"], "removed")
        self.assertFalse(self.tree.exists())
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertEqual(self.adapter.remove(self.record, self.identity)["status"], "removed")

    def test_dirty_and_locked_are_preserved(self):
        (self.tree / "untracked.txt").write_text("synthetic evidence not preserved", encoding="utf-8")
        self.assertEqual(self.adapter.remove(self.record, self.identity)["status"], "failed_no_change")
        self.assertTrue((self.tree / "untracked.txt").exists())
        self.git("worktree", "lock", str(self.tree))
        self.assertEqual(self.adapter.remove(self.record, self.identity)["status"], "failed_no_change")

    def test_shared_and_changed_identity_refused(self):
        for change in [{"references": ["other-task"]}, {"protected": True}]:
            record = dict(self.record, **change)
            self.assertEqual(self.adapter.remove(record, self.identity)["status"], "failed_no_change")
            self.assertTrue(self.tree.exists())
        result = self.adapter.remove(dict(self.record, metadata={"repository_root": str(self.repo),
                                                               "expected_head": "f" * 40}), self.identity)
        self.assertEqual(result["status"], "failed_no_change")
        self.assertTrue(self.tree.exists())

    def test_primary_checkout_and_scope_escape_refused(self):
        adapter = GitWorktrees(self.repo, self.base)
        self.assertEqual(adapter.remove(dict(self.record, path=str(self.repo)), self.identity)["status"], "failed_no_change")
        self.assertEqual(self.adapter.remove(dict(self.record, path=str(self.base / "outside")), self.identity)["status"], "failed_no_change")

    def test_unknown_response_is_reconciled_without_second_remove(self):
        calls = []
        def runner(argv, **kwargs):
            result = subprocess.run(argv, **kwargs)
            if "remove" in argv:
                calls.append(argv)
                raise subprocess.TimeoutExpired(argv, 20)
            return result
        adapter = GitWorktrees(self.repo, self.scope, runner)
        self.assertEqual(adapter.remove(self.record, self.identity)["status"], "unknown")
        self.assertFalse(adapter.observe(self.record)["exists"])
        self.assertEqual(adapter.remove(self.record, self.identity)["status"], "removed")
        self.assertEqual(len(calls), 1)

    def test_observation_preserves_git_index_bytes_and_timestamp(self):
        path = self.tree / "staged.txt"
        path.write_text("synthetic index fixture", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.tree), "add", "--", "staged.txt"], check=True, capture_output=True)
        result = subprocess.run(["git", "-C", str(self.tree), "rev-parse", "--git-path", "index"],
                                check=True, capture_output=True)
        index = Path(result.stdout.decode("utf-8").strip())
        if not index.is_absolute():
            index = self.tree / index
        before = index.read_bytes(), index.stat().st_mtime_ns
        observed = self.adapter.observe(self.record)
        self.assertFalse(observed["safe"])
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
