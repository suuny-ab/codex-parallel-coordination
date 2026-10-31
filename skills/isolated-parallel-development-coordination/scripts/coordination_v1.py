#!/usr/bin/env python3
"""Offline, single-coordinator state transitions. No scheduling or external actions."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlparse

SCHEMA = "parallel-coordination/1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def io_path(path):
    """Use Windows extended paths for long local evidence I/O, not global settings."""
    path = Path(path).absolute()
    value = str(path)
    if os.name == 'nt' and len(value) >= 260 and not value.startswith('\\\\?\\'):
        return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return path


def load(path):
    return json.loads(io_path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    with io_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def artifact(ref):
    path = Path(ref["path"])
    require(path.is_absolute(), "artifact missing or not absolute")
    path = io_path(path)
    require(path.is_absolute() and path.is_file(), "artifact missing or not absolute")
    require(digest(path) == ref["sha256"], "artifact hash mismatch")
    return path


def new_state(coordinator):
    require(bool(coordinator), "coordinator id required")
    return {"schema": SCHEMA, "coordinator_id": coordinator, "revision": 0,
            "active_human_item": None, "tasks": {}, "items": {}, "receipts": {}}


def validate(state):
    require(state["schema"] == SCHEMA, "unsupported state schema")
    active = [i["id"] for i in state["items"].values()
              if i["status"] in ("active", "waiting_user")]
    require(active == ([] if state["active_human_item"] is None
                       else [state["active_human_item"]]), "inconsistent human focus")


def readiness(state, item):
    if item["kind"] == "release":
        task = state["tasks"][item["task_id"]]
        return [] if (item["delivery_revision"] == task["delivery_revision"] and
                      task["user_acceptance"] == "accepted" and
                      task["integration"] == "passed") else ["release not ready for authorization"]
    if item["kind"] != "acceptance":
        return []
    try:
        task = state["tasks"][item["task_id"]]
        require(task["delivery_revision"] == item["delivery_revision"], "obsolete delivery")
        report = load(artifact(item["report"]))
        require(report.get("source") and report.get("evidence"), "source/evidence required")
        for ref in report["source"] + report["evidence"]:
            artifact(ref)
        require(report["self_test"]["status"] == "passed" and
                bool(report["self_test"]["command"].strip()), "self-test not passed")
        require(report["steps"] and all(isinstance(x, str) and x.strip()
                                      for x in report["steps"]), "acceptance steps required")
        require(bool(report["expected"].strip()), "expected result required")
        entry = report["entry"]
        if entry["kind"] == "file":
            path = Path(entry["target"])
            require(path.is_absolute() and path.is_file() and path.stat().st_size > 0,
                    "acceptance entry unavailable")
        else:
            require(entry["kind"] == "url" and
                    urlparse(entry["target"]).scheme in ("http", "https") and
                    bool(urlparse(entry["target"]).netloc), "invalid acceptance URL")
            artifact(entry["probe_evidence"])
        return []
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as error:
        return [str(error)]


def next_item(state):
    if state["active_human_item"] is not None:
        return None
    eligible = [i for i in state["items"].values()
                if i["status"] == "queued" and not readiness(state, i)]
    return min(eligible, key=lambda i: (0 if i.get("unblocks") or
               i["kind"] == "acceptance" else 1, i["sequence"])) if eligible else None


def checkpoint(state):
    validate(state)
    active = state["active_human_item"]
    if active is not None:
        item = state["items"][active]
        waiting = item["status"] == "waiting_user"
        return {"allow_end": waiting, "action": "wait_user" if waiting else "keep_focus",
                "active_human_item": active, "prompt": item.get("waiting_prompt"),
                "next": None, "revision": state["revision"]}
    candidate = next_item(state)
    held = {i["id"]: readiness(state, i) for i in state["items"].values()
            if i["status"] == "queued" and readiness(state, i)}
    waiting_tasks = {k: v["implementation"] for k, v in state["tasks"].items()
                     if v["implementation"] in ("running", "blocked")}
    return {"allow_end": candidate is None,
            "action": "present_next" if candidate else
                      "wait_internal" if held or waiting_tasks else "idle",
            "next": candidate, "held": held, "waiting_tasks": waiting_tasks,
            "revision": state["revision"]}


def enqueue(state, item):
    require(item["id"] not in state["items"], "human item already exists")
    require(item["kind"] in ("discussion", "decision", "acceptance", "release"),
            "unknown human item kind")
    require(all(isinstance(item.get(k), str) and item[k].strip()
                for k in ("title", "prompt")), "title and prompt required")
    if item["kind"] in ("decision", "release"):
        require(isinstance(item.get("options"), list) and item["options"] and
                all(isinstance(x, str) and x.strip() for x in item["options"]),
                "actionable choices required")
    item = copy.deepcopy(item)
    item.update(status="queued", sequence=len(state["items"]))
    state["items"][item["id"]] = item


def transition(original, op):
    """Return a new state, leaving the input untouched even on errors."""
    validate(original)
    state = copy.deepcopy(original)
    name = op["op"]
    if name == "register":
        task_id = op["task_id"]
        require(task_id not in state["tasks"], "task already registered")
        for field in ("owner_agent_id", "worktree", "branch"):
            require(op.get(field), field + " required")
        root = Path(op["worktree"]).resolve()
        require(Path(op["worktree"]).is_absolute(), "worktree must be absolute")
        resources = op.get("resources", [])
        require(isinstance(resources, list) and len(set(resources)) == len(resources),
                "unique resource identifiers required")
        for task in state["tasks"].values():
            other = Path(task["worktree"]).resolve()
            require(not (root.is_relative_to(other) or other.is_relative_to(root)),
                    "overlapping worktrees")
            require(task["branch"] != op["branch"] and
                    task["owner_agent_id"] != op["owner_agent_id"], "shared task owner/branch")
            require(not set(resources).intersection(task["resources"]), "shared resource")
        state["tasks"][task_id] = {k: op[k] for k in
                                  ("owner_agent_id", "worktree", "branch")}
        state["tasks"][task_id].update(resources=resources, delivery_revision=0,
            implementation="running", self_test="pending", integration="pending",
            user_acceptance="pending", release_authorization="pending", release="pending")
    elif name == "event":
        task = state["tasks"][op["task_id"]]
        revision, kind = op["delivery_revision"], op["kind"]
        require(type(revision) is int and revision > 0, "positive delivery revision required")
        require(kind in ("completed", "blocked"), "unknown native event kind")
        key = f'{op["task_id"]}:{revision}:{kind}'
        require(op["event_id"] == key, "event id must be task:revision:kind")
        fingerprint = hashlib.sha256(json.dumps(op, sort_keys=True).encode()).hexdigest()
        if key in state["receipts"]:
            require(state["receipts"][key]["fingerprint"] == fingerprint,
                    "conflicting duplicate event")
            return state, {"receipt": "duplicate", "next": None}
        report = load(artifact(op["report"]))
        stale = revision <= task["delivery_revision"]
        state["receipts"][key] = {"fingerprint": fingerprint,
                                 "status": "stale" if stale else "received"}
        if not stale:
            task.update(delivery_revision=revision,
                        implementation="completed" if kind == "completed" else "blocked",
                        self_test=report.get("self_test", {}).get("status", "pending"),
                        integration="pending", user_acceptance="pending",
                        release_authorization="pending", release="pending")
            for item in state["items"].values():
                if item.get("task_id") == op["task_id"] and item["status"] == "queued":
                    item["status"] = "superseded"
            if kind == "completed":
                enqueue(state, {"id": key, "kind": "acceptance", "task_id": op["task_id"],
                    "delivery_revision": revision, "title": report.get("summary", key),
                    "prompt": "Review the bound delivery using its entry, steps and expected result.",
                    "report": op["report"]})
            elif op.get("decision"):
                item = dict(op["decision"], id=key, kind="decision", unblocks=True,
                            task_id=op["task_id"])
                enqueue(state, item)
    elif name == "enqueue":
        require(op["item"]["kind"] != "acceptance", "acceptance requires a delivery event")
        if op["item"]["kind"] == "release":
            require(op["item"]["task_id"] in state["tasks"], "release task missing")
            item = dict(op["item"], delivery_revision=
                        state["tasks"][op["item"]["task_id"]]["delivery_revision"])
        else:
            item = op["item"]
        enqueue(state, item)
    elif name in ("activate", "switch"):
        item = state["items"][op["item_id"]]
        require(item["status"] == "queued" and not readiness(state, item),
                "human item not eligible")
        active = state["active_human_item"]
        if active is not None:
            require(name == "switch" and op.get("user_reference"),
                    "finish current item or record explicit user switch")
            state["items"][active]["status"] = "queued"
            state["items"][active]["switch_reference"] = op["user_reference"]
        item["status"] = "waiting_user" if item.get("waiting_prompt") else "active"
        state["active_human_item"] = item["id"]
    elif name in ("wait", "resume", "close"):
        require(state["active_human_item"] == op["item_id"], "item is not active")
        item = state["items"][op["item_id"]]
        if name == "wait":
            require(op.get("prompt"), "pending user prompt required")
            item.update(status="waiting_user", waiting_prompt=op["prompt"])
        elif name == "resume":
            item["status"] = "active"
            item.pop("waiting_prompt", None)
        else:
            require(op.get("user_reference"), "human resolution reference required")
            outcomes = {"acceptance": ("accepted", "rejected", "withdrawn"),
                        "release": ("authorized", "declined", "withdrawn"),
                        "discussion": ("resolved", "withdrawn"),
                        "decision": ("answered", "withdrawn")}
            require(op["outcome"] in outcomes[item["kind"]], "invalid human outcome")
            if item["kind"] == "acceptance":
                if op["outcome"] == "accepted":
                    require(not readiness(state, item), "delivery no longer eligible")
                task = state["tasks"][item["task_id"]]
                if task["delivery_revision"] == item["delivery_revision"]:
                    task["user_acceptance"] = op["outcome"]
            if item["kind"] == "release" and op["outcome"] == "authorized":
                task = state["tasks"][item["task_id"]]
                require(item["delivery_revision"] == task["delivery_revision"],
                        "release authorization is for an obsolete delivery")
                require(task["user_acceptance"] == "accepted", "acceptance still pending")
                task["release_authorization"] = op["user_reference"]
            item.update(status="closed", outcome=op["outcome"],
                        resolution_reference=op["user_reference"])
            state["active_human_item"] = None
    elif name == "stage":
        task = state["tasks"][op["task_id"]]
        require(op["delivery_revision"] == task["delivery_revision"], "stage revision mismatch")
        require(op["stage"] in ("integration", "release") and
                op["status"] in ("passed", "failed"), "invalid technical stage")
        artifact(op["evidence"])
        if op["status"] == "passed":
            delivery = state["items"].get(f'{op["task_id"]}:{op["delivery_revision"]}:completed')
            require(delivery is not None and not readiness(state, delivery),
                    "technical stage requires current verified delivery")
        if op["stage"] == "release":
            require(task["release_authorization"] != "pending" and
                    task["integration"] == "passed" and
                    task["user_acceptance"] == "accepted", "release gates not satisfied")
        task[op["stage"]] = op["status"]
        task[op["stage"] + "_evidence"] = op["evidence"]
    else:
        raise ValueError("unknown operation")
    state["revision"] += 1
    validate(state)
    # Receiving an event is internal only, even while the coordinator is idle.
    return state, {"receipt": "received", "next": None if name == "event" else next_item(state)}


def write_atomic(path, state):
    path = io_path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--coordinator", required=True)
    sub.add_parser("apply").add_argument("operation", type=Path)
    sub.add_parser("check")
    sub.add_parser("show")
    args = parser.parse_args()
    if args.command in ("init", "apply"):
        args.state.parent.mkdir(parents=True, exist_ok=True)
        lock = args.state.with_suffix(args.state.suffix + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            if args.command == "init":
                require(not args.state.exists(), "state already exists; resume it")
                state, result = new_state(args.coordinator), {"receipt": "initialized"}
            else:
                state, result = transition(load(args.state), load(args.operation))
            write_atomic(args.state, state)
        finally:
            os.close(fd)
            lock.unlink()
        print(json.dumps(result, ensure_ascii=False))
    else:
        state = load(args.state)
        validate(state)
        result = checkpoint(state) if args.command == "check" else state
        print(json.dumps(result, ensure_ascii=False))
        if args.command == "check" and not result["allow_end"]:
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
