#!/usr/bin/env python3
"""Uninstalled Stop-hook prototype; state is read only, stdin is a native fixture."""
import argparse
import json
import sys
from pathlib import Path

from coordination import checkpoint as checkpoint_v2, load
from coordination_v1 import checkpoint as checkpoint_v1


def checkpoint(state):
    return (checkpoint_v1 if state.get("schema") == "parallel-coordination/1" else checkpoint_v2)(state)


def response(state, event):
    delivery = state.get("delivery")
    sessions = (state["coordinator_id"], delivery["agent_id"]) if delivery else (state["coordinator_id"],)
    if event.get("hook_event_name") != "Stop" or event.get("session_id") not in sessions:
        return {}  # Never apply the coordinator gate to another thread or SubagentStop.
    check = checkpoint(state)
    main_user_only = delivery and event.get("session_id") == state["coordinator_id"]
    if main_user_only and check.get("action_owner") == delivery["agent_id"]:
        return {}  # Technical closeout belongs to delivery; never loop root into CI/receipt relay.
    if state.get("schema") == "parallel-coordination/2" and not main_user_only:
        audit = state.get("last_reconcile")
        if not audit or audit["revision"] != state["revision"] or audit["issues"] or audit["mode"] not in ("native_snapshot", "app_thread_snapshot"):
            check = dict(check, allow_end=False)
    if check["allow_end"]:
        return {}
    if event.get("stop_hook_active", False):
        # Bound the continuation: do not create a prompt loop on an unchanged protocol error.
        return {"systemMessage": "Coordination check still unresolved after continuation; "
                                 "manual coordinator attention required. No hard gate is claimed."}
    return {"decision": "block", "reason":
            "Run one-shot closeout/reconcile before ending; a ledger-only check cannot cover undelivered final events. Preserve the active human item; "
            "if it awaits a user response, record wait without closing it. Otherwise present "
            "only the next eligible item. Do not accept or authorize on the user's behalf."}


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = response(load(args.state), json.load(sys.stdin))
    except (ValueError, KeyError, TypeError, OSError):
        result = {"systemMessage": "Coordination state unavailable or invalid; "
                                   "checkpoint was not verified. Manual review required."}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
