"""Git worktree actions for an already authorized, registered cleanup handler.

The canonical CORE/RESOURCE cleanup gate must run before invoking this adapter.
This module has no CLI, task ledger, authority store, recursive delete or prune.
"""
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


class GitWorktrees:
    def __init__(self, repository_root, cleanup_root, runner=subprocess.run):
        self.repository = Path(repository_root).resolve()
        self.root = Path(cleanup_root).resolve()
        self.runner = runner
        if not self.repository.is_dir() or self.repository == self.root:
            raise ValueError("existing repository and separate cleanup root required")

    def _git(self, *args):
        return self.runner(["git", "-c", "core.longpaths=true", "-C", str(self.repository), *args], capture_output=True,
                           check=True, timeout=20)

    def create(self, record, branch, base):
        """One exact new worktree; caller persists intent and never blindly retries."""
        target = self._target(record)
        if target.exists() or not branch.startswith("codex/"):
            raise ValueError("new dedicated codex branch/worktree required")
        head = self._git("rev-parse", "--verify", base+"^{commit}").stdout.decode("ascii").strip()
        self._git("worktree", "add", "-b", branch, str(target), head)
        return {"repository_root": str(self.repository), "branch": "refs/heads/"+branch}

    def remote_completion(self, record, main_branch, pr_url, *, scope="host_verified"):
        """Observe actual origin main and ancestry; never fetch, push or back up code."""
        if scope not in ("host_verified","fixture_only"):
            raise ValueError("explicit remote verification scope required")
        observed = self.observe(record)
        if not observed["safe"] or not observed["exists"]:
            raise ValueError("uncommitted work or locked author tree must be resolved before preservation")
        head = record["metadata"]["expected_head"]
        origin = self._git("remote", "get-url", "origin").stdout.decode("utf-8").strip()
        def identity(url):
            if url.startswith("git@") and ":" in url:
                host,path=url[4:].split(":",1)
            else:
                parsed=urlsplit(url);host,path=parsed.hostname,parsed.path
                if parsed.scheme not in ("https","ssh") or not host:return None
            return (host.lower()+"/"+path.strip("/").removesuffix(".git")).casefold()
        repository_url=pr_url.split("/pull/")[0].split("/-/merge_requests/")[0]
        remote_identity=identity(origin)
        if remote_identity!=identity(repository_url) or remote_identity is None:
            if scope!="fixture_only" or not Path(origin).is_absolute():
                raise ValueError("origin is not the approved cloud PR repository")
            remote_identity="explicit-local-remote-fixture"
        target="refs/heads/"+main_branch
        raw=self._git("ls-remote","--exit-code","origin",target).stdout.decode("ascii").strip()
        fields=raw.split()
        if len(fields)!=2 or fields[1]!=target or not re.fullmatch("[0-9a-f]{40,64}",fields[0]):
            raise ValueError("one exact actual remote main observation required")
        main=fields[0]
        ancestor=self._git("merge-base",head,main).stdout.decode("ascii").strip()
        if ancestor!=head:raise ValueError("actual cloud main does not contain the tested source")
        return dict(schema="remote-main-completion/1",scope=scope,source_head=head,main_ref=target,main_sha=main,
            pr_url=pr_url,remote_identity=remote_identity,contains_source=True,author_clean=True,
            commands=[dict(arguments=["ls-remote","--exit-code","origin",target],stdout=raw,exit_code=0),
                      dict(arguments=["merge-base",head,main],stdout=ancestor,exit_code=0)])

    def _entries(self):
        raw = self._git("worktree", "list", "--porcelain", "-z").stdout.decode("utf-8")
        entries = []
        for block in raw.split("\0\0"):
            entry = {}
            for field in block.split("\0"):
                if field:
                    key, _, value = field.partition(" ")
                    entry[key] = value
            if "worktree" in entry:
                entries.append(entry)
        return entries

    def _target(self, record):
        registered_repository = record.get("metadata", {}).get("repository_root")
        if registered_repository is None or Path(registered_repository).resolve() != self.repository:
            raise ValueError("registered repository differs from trusted adapter scope")
        target = Path(record["path"])
        if not target.is_absolute():
            raise ValueError("absolute registered worktree required")
        target = target.resolve()
        if (target == self.root or not target.is_relative_to(self.root)
                or target == self.repository or self.repository.is_relative_to(target)):
            raise ValueError("worktree outside exact cleanup scope")
        expected_head = record.get("metadata", {}).get("expected_head")
        if expected_head is not None and not re.fullmatch("[0-9a-f]{40,64}", expected_head):
            raise ValueError("exact registered Git identity required")
        return target

    def observe(self, record):
        target = self._target(record)
        entries = self._entries()
        match = next((entry for entry in entries
                      if os.path.normcase(str(Path(entry["worktree"]).resolve())) == os.path.normcase(str(target))), None)
        if match is None:
            return {"exists": target.exists(), "identity": None, "safe": not target.exists(),
                    "reason": "no matching registered Git worktree"}
        expected_head = record.get("metadata", {}).get("expected_head")
        if entries[0] == match or expected_head is not None and match.get("HEAD") != expected_head:
            raise ValueError("primary checkout or changed worktree identity")
        branch = record.get("metadata", {}).get("branch")
        if branch is not None and match.get("branch") != branch:
            raise ValueError("dedicated worktree branch changed")
        info = target.stat()
        identity = {"repository": str(self.repository), "path": str(target), "head": match["HEAD"],
                    "device": info.st_dev, "inode": info.st_ino}
        if branch is not None:
            identity.pop("head")
            identity["branch"] = branch
        status = self.runner(["git", "-c", "core.longpaths=true", "--no-optional-locks", "-C", str(target), "status", "--porcelain=v1", "-z",
                              "--untracked-files=all"], capture_output=True, check=True, timeout=20)
        safe = "locked" not in match and not status.stdout
        return {"exists": True, "identity": identity, "head": match["HEAD"], "safe": safe,
                "reason": "" if safe else "worktree locked or has unpreserved changes"}

    def remove(self, record, expected_identity):
        """Called only after the trusted cleanup gate; unknown outcomes are observed."""
        try:
            target = self._target(record)
            observed = self.observe(record)
            if not observed["exists"]:
                return {"status": "removed"}
            if (not observed["safe"] or observed["identity"] != expected_identity
                    or record.get("references") or record.get("protected")):
                return {"status": "failed_no_change", "reason": "identity, protection or clean-worktree gate failed"}
            self._git("worktree", "remove", "--", str(target))
            return {"status": "removed"}
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason": "Git result unknown; observe before any further action",
                    "resource_id": record["resource_id"]}
        except ValueError as error:
            return {"status": "failed_no_change", "reason": str(error),
                    "resource_id": record["resource_id"]}
        except subprocess.CalledProcessError:
            return {"status": "unknown", "reason": "Git action failed; observe actual state before retry",
                    "resource_id": record["resource_id"]}
