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

    def remove_cancelled(self, record, expected_identity):
        """Caller verified explicit cancellation and author stop; discard only this tree."""
        target=self._target(record);observed=self.observe(record)
        if not observed['exists']:return dict(status='removed')
        if observed['identity']!=expected_identity or record.get('protected') or record.get('references'):
            return dict(status='failed_no_change',reason='cancelled tree identity/protection changed')
        for parent,directories,files in os.walk(target,followlinks=False):
            if any((Path(parent)/name).is_symlink() or (Path(parent)/name).is_junction() for name in directories+files):
                return dict(status='failed_no_change',reason='linked cancelled tree content protected')
        match=next(e for e in self._entries() if Path(e['worktree']).resolve()==target)
        if 'locked' in match:return dict(status='failed_no_change',reason='locked worktree protected')
        try:
            self._git('worktree','remove','--force','--',str(target))
            return dict(status='removed')
        except (subprocess.SubprocessError,OSError):
            return dict(status='unknown',reason='observe original cancelled worktree before further action')

    def branch_head(self, branch, *, remote=False):
        if not branch.startswith('codex/') or branch=='codex/':raise ValueError('dedicated task branch required')
        ref='refs/heads/'+branch
        if remote:
            rows=self._git('ls-remote','origin',ref).stdout.decode('ascii').strip().splitlines()
            if not rows:return None
            fields=rows[0].split()
            if len(rows)!=1 or len(fields)!=2 or fields[1]!=ref:raise ValueError('exact remote branch required')
            return fields[0]
        entries=self._git('for-each-ref','--format=%(objectname)',ref).stdout.decode('ascii').strip().splitlines()
        if not entries:return None
        if len(entries)!=1:raise ValueError('exact local branch required')
        return entries[0]

    def remove_branch(self, branch, expected_head, *, remote=False, cancelled=False, main_branch='main'):
        if branch==main_branch:raise ValueError('main branch protected')
        observed=self.branch_head(branch,remote=remote)
        if observed is None:return dict(status='removed')
        if observed!=expected_head:return dict(status='failed_no_change',reason='branch head changed')
        if not remote and any(e.get('branch')=='refs/heads/'+branch for e in self._entries()):
            return dict(status='failed_no_change',reason='branch still checked out')
        if not cancelled and self._git('merge-base',expected_head,'refs/heads/'+main_branch).stdout.decode().strip()!=expected_head:
            return dict(status='failed_no_change',reason='main does not contain task branch')
        try:
            if remote:
                self._git('push','--force-with-lease=refs/heads/'+branch+':'+expected_head,'origin',':refs/heads/'+branch)
            else:self._git('update-ref','-d','refs/heads/'+branch,expected_head)
            return dict(status='removed' if self.branch_head(branch,remote=remote) is None else 'unknown')
        except (subprocess.SubprocessError,OSError):return dict(status='unknown',reason='observe original branch deletion result')
