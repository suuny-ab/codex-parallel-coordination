#!/usr/bin/env python3
"""Offline single-writer task ledger; no dispatch, network, timers or deployment."""
import argparse
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

from evidence import (ERRORS, artifact, digest, fingerprint, load, load_bound, pack, presentation_view, ref,
                      report_for, require, write_atomic)

SCHEMA = "parallel-coordination/2"
PHASES = ("discussion", "backlog", "developing", "awaiting_acceptance",
          "awaiting_integration", "integrating", "awaiting_merge", "completed")
RIGHTS = ("commit", "push", "mr", "merge", "deploy")


def new_state(coordinator, project_id, main_repo=None, main_branch="main", protected=()):
    require(coordinator and project_id, "project and coordinator required")
    return {"schema": SCHEMA, "project": {"id": project_id, "provider": "gitlab",
            "main_branch": main_branch, "local_main_repo": main_repo, "protected": list(protected)},
            "coordinator_id": coordinator, "revision": 0, "active_human_item": None,
            "priority_item": None, "tasks": {}, "items": {}, "receipts": {}, "history": [],
            "merge_reservation": None, "last_reconcile": None, "unverified_receipts": {}}


def deny_rights():
    return {r: {"allowed": False, "user_reference": None} for r in RIGHTS}


def task_base(owner, worktree, branch, resources):
    return {"owner_agent_id": owner, "worktree": str(Path(worktree).resolve()),
            "branch": branch, "resources": resources, "phase": "discussion", "blocker": None,
            "requirements": None, "start_reference": None, "delivery_revision": 0,
            "implementation": "pending", "self_test": "pending", "quality_review": "pending",
            "user_acceptance": "pending", "integration": {"status": "pending"}, "review": None,
            "merge": None, "mr": None, "delivery": None, "authorization": deny_rights(),
            "history": [], "report_path": None, "report_task_id": None, "report_format": "v2",
            "blocker_followup": None}


def validate(state):
    require(state["schema"] == SCHEMA, "migrate a v1 COPY to a new ledger first")
    require(type(state["revision"]) is int and state["revision"] >= 0, "bad ledger revision")
    require(state["project"]["id"] and state["coordinator_id"], "ledger identity missing")
    if state.get("delivery"):
        d = state["delivery"]
        require(d["agent_id"] != state["coordinator_id"] and set(d["task_ids"]).issubset(state["tasks"]) and
                len(d["merge_order"]) == len(d["task_ids"]) and set(d["merge_order"]) == set(d["task_ids"]), "damaged delivery delegation")
    if state["project"].get("worker_capacity") is not None:
        require(type(state["project"]["worker_capacity"]) is int and state["project"]["worker_capacity"] > 0,
                "positive integer worker_capacity required")
    require(all(isinstance(state[k], dict) for k in ("tasks", "items", "receipts")), "damaged collections")
    require(isinstance(state["history"], list), "history missing")
    active = [k for k, i in state["items"].items() if i["status"] in ("active", "waiting_user")]
    require(active == ([] if state["active_human_item"] is None else
                       [state["active_human_item"]]), "inconsistent human focus")
    for task in state["tasks"].values():
        require(task["phase"] in PHASES and set(task["authorization"]) == set(RIGHTS), "bad task/rights")
        require(type(task["delivery_revision"]) is int and task["delivery_revision"] >= 0, "bad task revision")
        require(isinstance(task["history"], list), "task history missing")
        for right in task["authorization"].values():
            require(type(right["allowed"]) is bool and (not right["allowed"] or right["user_reference"]),
                    "unattributed authorization")
        if task["phase"] == "completed":
            require(task.get("merge", {}).get("verified") is True, "main not verified")
    for key, item in state["items"].items():
        require(key == item["id"] and item["status"] in
                ("queued", "active", "waiting_user", "closed", "superseded"), "bad human item")
    for key, receipt in state["receipts"].items():
        require(receipt["status"] in ("buffered", "held", "processed", "stale", "legacy"), "bad receipt")
        if receipt["status"] != "legacy":
            require(key == receipt["payload"]["event_id"] and
                    fingerprint(receipt["payload"]) == receipt["fingerprint"], "damaged receipt payload")
    for key, arrival in state.get("unverified_receipts", {}).items():
        require(key == arrival["fingerprint"] == fingerprint(arrival["payload"]) and
                arrival["status"] in ("unverified", "resolved"), "damaged unverified receipt")


def enqueue(state, item):
    require(item["id"] not in state["items"], "human item exists")
    require(item["kind"] in ("discussion", "decision", "assistance", "acceptance", "release"), "bad item kind")
    require(all(isinstance(item.get(k), str) and item[k].strip() for k in ("title", "prompt")),
            "title and prompt required")
    if item["kind"] in ("decision", "release"):
        require(item.get("options") and all(isinstance(x, str) and x.strip()
                                           for x in item["options"]), "choices required")
    item = copy.deepcopy(item)
    item.update(status="queued", sequence=max((i["sequence"] for i in state["items"].values()), default=-1) + 1)
    state["items"][item["id"]] = item


def verify_review(report_ref, review, conclusion):
    require(review.get("conclusion") == conclusion and review.get("summary") and
            review.get("report_sha256") == report_ref["sha256"] and review.get("evidence"),
            "human escalation requires coordinator-reviewed technical investigation evidence")
    for value in review["evidence"]:
        artifact(value)


def verify_assistance(request):
    require(all(isinstance(request.get(k), str) and request[k].strip()
                for k in ("title", "prompt", "recipient", "request_text")) and
            isinstance(request.get("required_materials"), list) and request["required_materials"] and
            all(isinstance(x, str) and x.strip() for x in request["required_materials"]),
            "concrete external recipient, request and materials required")


def receipt_context(state, p):
    """Revalidate current recipient/scope and immutable native/App final provenance."""
    from delivery import receipt_scope
    receipt_scope(state, p)
    t = state["tasks"][p["task_id"]]
    require(p["project_id"] == state["project"]["id"] and p["channel"] in ("native_agent", "app_thread") and
            p["owner_agent_id"] == t["owner_agent_id"], "native receipt identity mismatch")
    if p["channel"] == "app_thread" or state.get("delivery", {}).get("transport", {}).get("mode") == "mixed_verified":
        d = state.get("delivery")
        require(d and d.get("transport", {}).get("mode") in ("fixture", "app_thread_verified", "mixed_verified"), "App thread receipt route not declared")
        raw = load_bound(p["transport_evidence"])
        route = d["routes"][t["owner_agent_id"]]
        target = route.get("channels", {}).get(p["channel"], route)["target_id"]
        require(raw["sender_id"] == t["owner_agent_id"] and raw["recipient_id"] == d["agent_id"] and
                raw["sender_target_id"] == target and
                raw["event_id"] == p["event_id"] and raw["kind"] == "task_final",
                "send acceptance or peer acknowledgment is not a task final")
    elif state.get("delivery", {}).get("transport", {}).get("mode") == "app_thread_verified":
        require(False, "use declared App thread task-final mapping; do not fabricate native child events")
    r, kind = p["delivery_revision"], p["kind"]
    require(type(r) is int and r > 0 and kind in ("completed", "blocked", "integrated"), "invalid revision/kind")
    key = f'{p["task_id"]}:{r}:{kind}'
    require(p["event_id"] == key, "stable event id must be task:revision:kind")
    require(Path(p["report"]["path"]).is_absolute() and len(p["report"]["sha256"]) == 64, "report path/hash required")


def receipt_report(state, payload):
    task = state["tasks"][payload["task_id"]]
    report = load_bound(payload["report"])
    require(report["task_id"] == (task["report_task_id"] or payload["task_id"]) and
            type(report["delivery_revision"]) is int and report["delivery_revision"] == payload["delivery_revision"],
            "report identity mismatch")
    if task["report_format"] == "v2":
        require(report["project_id"] == state["project"]["id"], "report project mismatch")
    if payload["kind"] == "completed":
        require(report.get("status") not in ("held", "blocked"), "held report cannot map to completed")
        presentation_view(report)  # Validate/adapt display fields before claiming canonical dedup.
        require(report["self_test"]["status"] in ("passed", "failed", "pending", "not_run"),
                "needs_reconcile: custom self-test conclusion requires explicit author contract; raw status retained")
    if payload["kind"] == "blocked":
        if task["report_format"] == "legacy-held":
            require(report.get("status") == "held" and report.get("open_questions"), "held report lacks business question")
            category = "business"
        else:
            blocker = report.get("blocker", {})
            require(blocker.get("reason") and blocker.get("owner") and blocker.get("category") in
                    ("technical", "business", "dependency", "evidence"), "blocker details required")
            category = blocker["category"]
        require(category != "business" or payload.get("decision"), "business blocker needs an actionable decision")
        require(not payload.get("decision") or category == "business", "technical receipts stay internal")
        require(not payload.get("assistance") or category != "business", "external materials are distinct from a business decision")
    if payload.get("decision"):
        require(payload["kind"] == "blocked" and not payload.get("assistance"), "one human purpose per receipt")
        decision = payload["decision"]
        require(all(isinstance(decision.get(k), str) and decision[k].strip() for k in ("title", "prompt"))
                and isinstance(decision.get("options"), list) and decision["options"] and
                all(isinstance(x, str) and x.strip() for x in decision["options"]), "actionable decision required")
        verify_review(payload["report"], payload.get("technical_review", {}), "business_decision_required")
    if payload.get("assistance"):
        require(payload["kind"] == "blocked" and not payload.get("decision"), "one human purpose per receipt")
        verify_assistance(payload["assistance"])
        verify_review(payload["report"], payload.get("technical_review", {}), "external_materials_required")
    return report


def verify_completed_origin(state, task_id):
    task = state["tasks"][task_id]
    matches = [r for r in state["receipts"].values() if r["status"] == "processed" and
               r.get("payload", {}).get("task_id") == task_id and r["payload"]["kind"] == "completed" and
               r["payload"]["delivery_revision"] == task["delivery_revision"] and r["payload"]["report"] == task["delivery"]]
    require(len(matches) == 1 and matches[0]["fingerprint"] == fingerprint(matches[0]["payload"]),
            "current completed delivery receipt provenance missing or changed")
    receipt_context(state, matches[0]["payload"])


def quarantine(state, payload, reason, prior=None):
    key = fingerprint(payload)
    arrivals = state.setdefault("unverified_receipts", {})
    if key not in arrivals:
        arrivals[key] = {"payload": copy.deepcopy(payload), "fingerprint": key,
                         "status": "unverified", "reason": reason, "history": []}
    arrival = arrivals[key]
    if not arrival["history"] or arrival["reason"] != reason:
        arrival["history"].append({"status": "unverified", "reason": reason})
    arrival["reason"] = reason
    if prior:
        arrival["prior_canonical"] = copy.deepcopy(prior)


def admit_receipt(state, payload):
    receipt_context(state, payload)
    key, value = payload["event_id"], fingerprint(payload)
    old = state["receipts"].get(key)
    if old and old["fingerprint"] == value and (old.get("report_verified") or
                                               old["status"] not in ("buffered", "held")):
        return "duplicate"
    if old and old["fingerprint"] != value:
        require(not old.get("report_verified") and old["status"] in ("buffered", "held"), "conflicting duplicate")
        try:
            receipt_report(state, old["payload"])
        except ERRORS:
            pass  # Only a pre-repair, unconsumed, currently invalid canonical record is recoverable.
        else:
            raise ValueError("conflicting duplicate")
    try:
        receipt_report(state, payload)
    except ERRORS as error:
        if old and old["fingerprint"] == value:
            quarantine(state, old["payload"], str(error), old)
            del state["receipts"][key]
        else:
            quarantine(state, payload, str(error))
        return "quarantined"
    if old and old["fingerprint"] != value:
        quarantine(state, old["payload"], "pre-repair unverified canonical mapping replaced", old)
    if old and old["fingerprint"] == value:
        old["report_verified"] = True
    else:
        state["receipts"][key] = {"payload": copy.deepcopy(payload), "fingerprint": value,
            "status": "buffered", "report_verified": True, "history": [{"status": "buffered"}]}
    if payload["kind"] == "completed":
        _, changes = presentation_view(load_bound(payload["report"]))
        if changes:
            state["receipts"][key]["presentation_adapter"] = {"version": 1, "source_report": payload["report"],
                                                              "changes": changes}
    for arrival in state.get("unverified_receipts", {}).values():
        if arrival["status"] == "unverified" and arrival["payload"]["event_id"] == key:
            arrival.update(status="resolved", resolved_by={"event_id": key, "fingerprint": value})
            arrival["history"].append({"status": "resolved", "fingerprint": value})
    return "received"


def human_receipt(state, item):
    origin = state["receipts"][item.get("source_receipt", item["id"])]
    payload = origin["payload"]
    receipt_context(state, payload)
    require(origin["status"] == "processed" and origin["fingerprint"] == fingerprint(payload) and
            payload["kind"] == "blocked" and payload["task_id"] == item["task_id"] and
            payload["report"] == item["report"] and payload["delivery_revision"] == item["delivery_revision"],
            "human item receipt provenance changed")
    receipt_report(state, payload)
    if item["kind"] == "decision":
        require(all(item[k] == payload["decision"][k] for k in ("title", "prompt", "options")),
                "decision changed from reviewed receipt")
        verify_review(item["report"], payload.get("technical_review", {}), "business_decision_required")
    else:
        verify_assistance(item)
        verify_review(item["report"], item["technical_review"], "external_materials_required")
    return payload


def readiness(state, item):
    try:
        require(not item.get("migration_hold"), "legacy focus needs explicit reconciliation")
        if item["kind"] == "acceptance":
            task = state["tasks"][item["task_id"]]
            require(item["delivery_revision"] == task["delivery_revision"], "obsolete revision")
            require(not item.get("legacy_unbound"), "legacy evidence needs v2 worker delivery")
            report_for(state, item["task_id"])
        elif item["kind"] == "decision" and item.get("report"):
            human_receipt(state, item)
            require(item.get("delivery_revision") == state["tasks"][item["task_id"]]["delivery_revision"],
                    "obsolete business decision")
        elif item["kind"] == "assistance":
            human_receipt(state, item)
            require(item["delivery_revision"] == state["tasks"][item["task_id"]]["delivery_revision"],
                    "obsolete external-material request")
        elif item["kind"] == "release":
            require(state["tasks"][item["task_id"]]["phase"] == "completed", "main verification still pending")
        return []
    except ERRORS as error:
        return [str(error)]


def add_assistance(state, task_id, source_receipt, request, review):
    task = state["tasks"][task_id]
    origin = state["receipts"][source_receipt]["payload"]
    require(task["implementation"] == "blocked" and origin["task_id"] == task_id and
            origin["kind"] == "blocked" and origin["delivery_revision"] == task["delivery_revision"],
            "material request needs current blocked delivery")
    verify_assistance(request)
    verify_review(origin["report"], review, "external_materials_required")
    item_id = request.get("id", source_receipt + ":materials")
    enqueue(state, dict(request, id=item_id, kind="assistance", task_id=task_id,
        source_receipt=source_receipt, report=origin["report"], delivery_revision=task["delivery_revision"],
        technical_review=copy.deepcopy(review), unblocks=True))
    task["blocker_followup"] = {"status": "human_pending", "owner": "human", "action": request["request_text"],
                                "delivery_revision": task["delivery_revision"], "item_id": item_id}


def blocked_issues(state):
    issues = {}
    for task_id, task in state["tasks"].items():
        if task["implementation"] != "blocked":
            continue
        try:
            followup = task.get("blocker_followup")
            require(followup and followup.get("owner") and followup.get("action") and
                    followup.get("delivery_revision") == task["delivery_revision"],
                    "blocked task needs current next owner, action and assignment/request evidence")
            if followup["status"] in ("human_pending", "external_requested"):
                item = state["items"][followup["item_id"]]
                require(item["task_id"] == task_id and not readiness(state, item), "blocked human item evidence expired")
                if followup["status"] == "human_pending":
                    require(item["status"] in ("queued", "active", "waiting_user"),
                            "resolved human item still needs worker dispatch or external request")
                else:
                    require(item["kind"] == "assistance" and item["status"] == "closed" and
                            item.get("outcome") == "requested", "external request not initiated")
                    artifact(followup["evidence"])
            else:
                require(followup["status"] == "assigned_technical", "received materials need worker redispatch")
                artifact(followup["evidence"])
        except ERRORS as error:
            issues[task_id] = str(error)
    return issues


def next_item(state):
    if state["active_human_item"] is not None:
        return None
    eligible = [i for i in state["items"].values() if i["status"] == "queued" and not readiness(state, i)]
    return min(eligible, key=lambda i: (0 if state["priority_item"] == i["id"] else 1,
               0 if i.get("unblocks") or i["kind"] == "acceptance" else 1, i["sequence"]), default=None)


def presentation(state, item):
    if not item:
        return None
    value = copy.deepcopy(item)
    if item["kind"] == "acceptance" and not readiness(state, item):
        report = report_for(state, item["task_id"])
        value.update({k: report[k] for k in ("entry", "steps", "expected")})
        value["report"] = state["tasks"][item["task_id"]]["delivery"]
    return value


def _checkpoint(state):
    """Ledger-only check: never evidence of a reconciled native worker inbox."""
    validate(state)
    buffered = [k for k, r in state["receipts"].items() if r["status"] == "buffered"]
    held = {i["id"]: reasons for i in state["items"].values() if i["status"] == "queued"
            and (reasons := readiness(state, i))}
    base = {"scope": "ledger_only", "revision": state["revision"], "next": None,
            "active_human_item": state["active_human_item"], "buffered": buffered,
            "unverified": {k: r["reason"] for k, r in state.get("unverified_receipts", {}).items()
                           if r["status"] == "unverified"}, "blocked_followup_issues": blocked_issues(state), "held": held}
    if buffered:
        return dict(base, allow_end=False, action="process_receipts")
    if base["blocked_followup_issues"]:
        return dict(base, allow_end=False, action="route_blocked_work")
    if state["active_human_item"]:
        item = state["items"][state["active_human_item"]]
        issues = readiness(state, item)
        waiting = item["status"] == "waiting_user" and not issues
        return dict(base, allow_end=waiting, action="wait_user" if waiting else "keep_focus",
                    issues=issues, prompt=item.get("waiting_prompt"))
    candidate = next_item(state)
    held = {i["id"]: reasons for i in state["items"].values() if i["status"] == "queued"
            and (reasons := readiness(state, i))}
    waiting = {k: {"phase": t["phase"], "blocker": t["blocker"]} for k, t in state["tasks"].items()
               if t["phase"] != "completed"}
    receipts = {k: r.get("reason") for k, r in state["receipts"].items() if r["status"] == "held"}
    return dict(base, allow_end=candidate is None, action="present_next" if candidate else
                "wait_internal" if waiting or held or receipts or base["unverified"] else "idle", next=presentation(state, candidate),
                held=held, held_receipts=receipts, waiting_tasks=waiting)


def checkpoint(state):
    result = _checkpoint(state)
    if state.get("delivery"):
        from delivery import buffered_authority_issues, release_issues, transport_issues
        transport, release = transport_issues(state), release_issues(state)
        scope = buffered_authority_issues(state)
        issues = transport + release + scope
        result.update(technical_owner=state["delivery"]["agent_id"], human_session=state["coordinator_id"],
                      routine_root_confirmation_required=False)
        result["action_owner"] = state["coordinator_id"] if result.get("active_human_item") or result.get("next") else state["delivery"]["agent_id"]
        if issues:
            result.update(allow_end=False, action="delivery_transport_blocked" if transport else "delivery_scope_reconcile" if scope else "release_evidence_invalid", delivery_evidence_issues=issues,
                          action_owner=state["delivery"]["agent_id"])
    return result


def summary(state):
    check = checkpoint(state)
    result = {"project_id": state["project"]["id"], "revision": state["revision"],
            "focus": state["active_human_item"], "action": check["action"],
            "next": check["next"]["id"] if check["next"] else None,
            "tasks": {k: {key: t[key] for key in ("phase", "implementation", "blocker", "delivery_revision",
                "self_test", "quality_review", "user_acceptance", "integration", "mr", "merge")}
                for k, t in state["tasks"].items()},
            "worker_runs": {k: t.get("worker_run") for k, t in state["tasks"].items()},
            "blocker_followups": {k: t.get("blocker_followup") for k, t in state["tasks"].items()
                                  if t["implementation"] == "blocked"},
            "blocked_followup_issues": check["blocked_followup_issues"],
            "unverified_receipts": check.get("unverified", {}),
            "receipt_counts": {s: sum(r["status"] == s for r in state["receipts"].values())
                               for s in ("buffered", "processed", "held", "stale", "legacy")}}
    if state.get("delivery"):
        result["routing"] = {key: check[key] for key in
                             ("technical_owner", "human_session", "action_owner", "routine_root_confirmation_required")}
        if state.get("release_plan"):
            result["release"] = {key: state["release_plan"][key] for key in
                                 ("release_sha", "included_task_ids", "environment", "completed_steps")}
    return result


def reset_delivery(task):
    task["history"].append(copy.deepcopy({k: task.get(k) for k in
        ("delivery_revision", "phase", "delivery", "implementation", "self_test", "quality_review",
         "user_acceptance", "integration", "review", "mr", "merge", "blocker", "blocker_followup", "worker_run", "acceptance_record", "delivery_check", "delivery_check_revision")}))
    task.update(user_acceptance="pending", integration={"status": "pending"}, review=None, merge=None,
                blocker=None, blocker_followup=None, acceptance_record=None, delivery_check=None, delivery_check_revision=None)


def ensure_capacity(state, task_id):
    capacity = state["project"].get("worker_capacity")
    if capacity is None:
        return
    allocated = sum(t.get("worker_run", {}).get("status") == "running" for key, t in state["tasks"].items()
                    if key != task_id)
    require(allocated < capacity, "worker capacity occupied; keep task queued until a slot is released")


def run_record(task, mode, reference):
    return {"status": "running", "mode": mode, "owner_agent_id": task["owner_agent_id"],
            "expected_delivery_revision": task["delivery_revision"] + (0 if mode == "integration" else 1),
            "user_reference": reference, "scope": "existing_confirmed_scope_only"}


def consume(state, payload):
    receipt_context(state, payload)
    task_id, revision, kind = (payload[k] for k in ("task_id", "delivery_revision", "kind"))
    task = state["tasks"][task_id]
    require(task["start_reference"], "task not authorized to start")
    if revision == task["delivery_revision"] and kind != "integrated":
        require(not any(r.get("payload", {}).get("task_id") == task_id and
                        r["payload"]["delivery_revision"] == revision and
                        r["payload"]["kind"] != kind and r["status"] == "processed"
                        for r in state["receipts"].values()), "conflicting same-revision terminal kinds")
    if revision < task["delivery_revision"] or (revision == task["delivery_revision"] and kind != "integrated"):
        return "stale"
    if task.get("worker_run", {}).get("status") == "queued":
        launch = state.get('runtime',{}).get('operations',{}).get(task['worker_run'].get('attempt_id'),{})
        require(launch.get('status')=='accepted_unconfirmed' and launch.get('task_id')==task_id and
                launch.get('executor_id')==task['owner_agent_id'], "terminal arrived for queued work; reconcile dispatch first")
        receipt_report(state,payload)  # A verified fast final proves execution, never a guessed running state.
    if task.get("worker_run", {}).get("status") == "running":
        require(revision >= task["worker_run"]["expected_delivery_revision"], "receipt predates current worker run")
    active = state["items"].get(state["active_human_item"], {})
    require(not (active.get("task_id") == task_id and active.get("kind") == "acceptance"),
            "active acceptance freezes delivery; finish current item first")
    require(not state["merge_reservation"] or state["merge_reservation"]["task_id"] != task_id,
            "release merge reservation first")
    report = receipt_report(state, payload)
    if kind == "integrated":
        from merge_gate import integration_result
        require(revision == task["delivery_revision"] and task["phase"] == "integrating", "integration out of sequence")
        integration_result(state, task_id, payload["report"])
        if task.get("worker_run"):
            task["worker_run"]["status"] = "reported"
        return "processed"
    require(task["phase"] != "completed", "completed task needs a new requirement")
    reset_delivery(task)
    task["delivery_revision"] = revision
    task["report_path"] = payload["report"]["path"]
    for item in state["items"].values():
        if item.get("task_id") == task_id and item["status"] == "queued":
            item["status"] = "superseded"
    if kind == "blocked":
        blocker = report.get("blocker")
        if task["report_format"] == "legacy-held":
            require(report["status"] == "held" and report["open_questions"], "held report lacks business question")
            blocker = {"reason": report["open_questions"][0]["question"], "category": "business",
                       "owner": "human", "raw_report": payload["report"]}
        require(blocker and blocker.get("reason") and blocker.get("owner") and blocker.get("category")
                in ("technical", "business", "dependency", "evidence"), "blocker details required")
        task.update(blocker=blocker, implementation="blocked", phase="developing",
                    self_test=report.get("self_test", {}).get("status", "pending"), quality_review="pending")
        if payload.get("decision"):
            require(blocker["category"] == "business", "technical receipts stay internal")
            enqueue(state, dict(payload["decision"], id=payload["event_id"], kind="decision", task_id=task_id,
                                report=payload["report"], delivery_revision=revision, unblocks=True))
            task["blocker_followup"] = {"status": "human_pending", "owner": "human", "action": payload["decision"]["prompt"],
                                        "delivery_revision": revision, "item_id": payload["event_id"]}
        elif payload.get("assistance"):
            require(blocker["category"] != "business", "external materials are distinct from a business decision")
            add_assistance(state, task_id, payload["event_id"], payload["assistance"], payload["technical_review"])
        else:
            require(blocker["category"] != "business", "business blocker needs an atomic actionable decision")
    else:
        require(task["report_format"] == "v2", "legacy completed report needs a v2 evidence package")
        task.update(delivery=payload["report"], implementation="completed", phase="awaiting_acceptance",
                    self_test=report.get("self_test", {}).get("status", "pending"),
                    quality_review=report.get("quality_review", {}).get("status", "pending") )
        # This candidate transaction has consumed the verified origin. Bind it before
        # readiness checks the new delivery; the outer transition saves atomically.
        state["receipts"][payload["event_id"]]["status"] = "processed"
        enqueue(state, {"id": payload["event_id"], "kind": "acceptance", "task_id": task_id,
            "delivery_revision": revision, "title": report["summary"],
            "prompt": "Review this revision using its entry, steps and expected result."})
        reasons = readiness(state, state["items"][payload["event_id"]])
        if reasons:
            task["blocker"] = {"reason": reasons[0], "owner": task["owner_agent_id"], "category": "evidence"}
    if task.get("worker_run"):
        task["worker_run"]["status"] = "reported"
        launch=state.get('runtime',{}).get('operations',{}).get(task['worker_run'].get('attempt_id'))
        if launch and launch.get('task_id')==task_id:
            launch.update(status='reported',final_receipt=payload['event_id'])
    return "processed"


def transition(original, op):
    """Pure transitions; failed operations leave input unchanged."""
    validate(original)
    from delivery import OPS, actor_for, apply as delivery_apply
    actor_for(original, op)
    if "expected_revision" in op:
        require(op["expected_revision"] == original["revision"], "ledger revision changed; check again")
    state = copy.deepcopy(original)
    name, task_id = op["op"], op.get("task_id")
    receipt_result = "received"
    task = state["tasks"].get(task_id)
    if name in OPS:
        delivery_apply(state, op)
    elif name in ("register", "dispatch"):
        require(task_id and task_id not in state["tasks"], "task missing/already registered")
        require((op.get("owner_agent_id") or (op.get("pending_executor") is True and name == "register")) and op.get("branch") and Path(op["worktree"]).is_absolute(),
                "owner, branch and absolute worktree required")
        root, resources = Path(op["worktree"]).resolve(), op.get("resources", [])
        require(isinstance(resources, list) and all(isinstance(x, str) and x for x in resources)
                and len(set(resources)) == len(resources), "unique resource IDs required")
        for t in state["tasks"].values():
            other = Path(t["worktree"]).resolve()
            require(not root.is_relative_to(other) and not other.is_relative_to(root), "overlapping worktrees")
            require(t["branch"] != op["branch"] and (not op.get("owner_agent_id") or
                    t["owner_agent_id"] != op["owner_agent_id"] or t["phase"] == "completed"), "shared owner/branch")
            require(not set(t["resources"]) & set(resources), "shared resources")
        task = task_base(op["owner_agent_id"], str(root), op["branch"], resources)
        task.update(report_path=op.get("report_path"), report_task_id=task_id)
        task["progress"] = {"title": op.get("title", task_id), "business_task_id": task_id,
                            "role": "implementation", "facts": [], "next_step": op.get("next_step")}
        state["tasks"][task_id] = task
        if name == "dispatch":
            require(op.get("scope") and op.get("acceptance") and op.get("user_reference"),
                    "confirmed scope/acceptance/start reference required")
            ensure_capacity(state, task_id)
            task.update(requirements={k: op[k] for k in ("scope", "acceptance", "user_reference")},
                        start_reference=op["user_reference"], phase="developing", implementation="running")
            task["worker_run"] = run_record(task, "implementation", op["user_reference"])
            for right in op.get("authorized_rights", []):
                require(right in RIGHTS and right != "deploy", "dispatch grants Git rights only; deployment needs separate scope")
                task["authorization"][right] = {"allowed": True, "user_reference": op["user_reference"]}
    elif name == "adopt-legacy":
        require(task and task.get("legacy") and op.get("scope") and op.get("acceptance") and
                op.get("user_reference"), "legacy scope/start must be explicitly reconciled")
        task.update(requirements={k: op[k] for k in ("scope", "acceptance", "user_reference")},
                    start_reference=op["user_reference"])
    elif name == "confirm":
        require(task and task["phase"] in ("discussion", "backlog"), "already started")
        require(op.get("scope") and op.get("acceptance") and op.get("user_reference"), "requirements required")
        task.update(requirements={k: op[k] for k in ("scope", "acceptance", "user_reference")}, phase="backlog")
    elif name == "start":
        require(task and task["phase"] in ("backlog", "awaiting_integration") and task["requirements"] and op.get("user_reference"),
                "confirmed scope/start instruction required")
        ensure_capacity(state, task_id)
        mode = task.get("worker_run", {}).get("mode", "implementation")
        if mode == "integration":
            from merge_gate import merge_transition
            merge_transition(state, {"op": "integration-start", "task_id": task_id})
        else:
            task.update(phase="developing", implementation="running")
        task.update(start_reference=op["user_reference"], worker_run=run_record(task, mode, op["user_reference"]))
    elif name == "describe":
        require(task and op.get("reference"), "task description needs an actual source reference")
        description = copy.deepcopy(task.get("progress", {"title": task_id, "business_task_id": task_id,
                                                          "role": "implementation", "facts": []}))
        root_id = op.get("business_task_id", description["business_task_id"])
        require(root_id in state["tasks"] and (root_id == task_id or
                state["tasks"][root_id].get("progress", {}).get("business_task_id", root_id) == root_id),
                "business task must name an existing root, without cycles")
        require(root_id == task_id or not any(key != task_id and t.get("progress", {}).get("business_task_id") == task_id
                                              for key, t in state["tasks"].items()),
                "rebind supporting tasks before moving their business root")
        role = op.get("task_role", description["role"])
        require(role in ("implementation", "self_test", "review"), "unknown task role")
        for fact in op.get("facts", []):
            require(isinstance(fact.get("text"), str) and fact["text"].strip() and fact.get("reference"),
                    "completed fact requires public text and source reference")
            if fact.get("evidence"):
                artifact(fact["evidence"])
        description.update(business_task_id=root_id, role=role, reference=op["reference"])
        for key in ("title", "facts", "next_step"):
            if key in op:
                if key != "facts":
                    require(op[key] is None and key == "next_step" or isinstance(op[key], str) and op[key].strip(),
                            "public title/next_step must be nonempty text")
                description[key] = copy.deepcopy(op[key])
        task["progress"] = description
    elif name == "queue":
        require(task and task["requirements"] and task["phase"] != "completed" and op.get("user_reference")
                and op.get("reason"), "queue requires confirmed scope and actual assignment reference")
        require(state["items"].get(state["active_human_item"], {}).get("task_id") != task_id,
                "finish active item before queuing the next run")
        require(task.get("worker_run", {}).get("status") != "running", "running worker must stop/report before queuing")
        mode = op.get("mode", "implementation")
        require(mode in ("implementation", "quality_rework", "integration", "review"), "unknown queued work mode")
        task["worker_run"] = dict(run_record(task, mode, op["user_reference"]), status="queued", reason=op["reason"])
        task.update(phase="awaiting_integration" if mode == "integration" else "backlog", implementation="queued",
                    start_reference=task["start_reference"] or op["user_reference"], blocker=None, blocker_followup=None)
        for pending in state["items"].values():
            if pending.get("task_id") == task_id and pending["status"] == "queued":
                pending["status"] = "superseded"
    elif name == "refresh-presentation":
        origin = state["receipts"][op["source_receipt"]]
        payload = origin["payload"]
        require(task and op.get("reference") and payload["task_id"] == task_id and payload["kind"] == "completed"
                and payload["delivery_revision"] == task["delivery_revision"] and task["delivery"] == payload["report"],
                "presentation refresh must bind the existing current canonical delivery")
        receipt_report(state, payload)
        _, changes = presentation_view(load_bound(payload["report"]))
        origin["presentation_adapter"] = {"version": 1, "source_report": payload["report"], "changes": changes,
                                          "reference": op["reference"]}
        item = state["items"][payload["event_id"]]
        require(item["status"] in ("queued", "active", "waiting_user"), "do not reopen a resolved human outcome")
        if not readiness(state, item):
            task["blocker"] = None
    elif name == "record-acceptance":
        require(task and op.get("user_reference") and op.get("scope") == "confirmed_task_scope" and
                op["delivery_revision"] == task["delivery_revision"] and op["report"] == task["delivery"],
                "recorded acceptance must bind the current confirmed task scope and delivery")
        run = task.get("worker_run", {})
        require(run.get("status") not in ("running", "queued") or run.get("expected_delivery_revision", 0) <= task["delivery_revision"],
                "old acceptance cannot replace a newer assigned or queued run")
        require(state["items"].get(state["active_human_item"], {}).get("task_id") != task_id,
                "resolve this task's active acceptance with close")
        artifact(op["evidence"])
        report_for(state, task_id)  # Presentation adaptation does not waive source/test/quality checks.
        task.update(user_acceptance="accepted", phase="awaiting_integration", blocker=None,
                    acceptance_record={"evidence": op["evidence"], "user_reference": op["user_reference"],
                                       "delivery_revision": task["delivery_revision"], "report": op["report"]})
        for item in state["items"].values():
            if (item.get("task_id") == task_id and item["kind"] == "acceptance" and item["status"] == "queued" and
                    item["delivery_revision"] == task["delivery_revision"]):
                item.update(status="closed", outcome="accepted", resolution_reference=op["user_reference"])
    elif name == "bind-report":
        require(task and op.get("reference") and Path(op["path"]).is_absolute(), "explicit report mapping required")
        require(op.get("format", "v2") in ("v2", "legacy-held"), "unsupported report format")
        task.update(report_path=op["path"], report_task_id=op.get("report_task_id", task_id),
                    report_format=op.get("format", "v2"))
    elif name == "authorize":
        require(task and op.get("user_reference") and op.get("rights"), "scoped human authorization required")
        for right in op["rights"]:
            require(right in RIGHTS and type(op.get("allowed", True)) is bool, "invalid right")
            if right == "deploy":
                require(op["rights"] == ["deploy"] and op.get("deployment_scope"), "deployment needs its own explicit scope")
            task["authorization"][right] = {"allowed": op.get("allowed", True), "user_reference": op["user_reference"]}
            if right == "deploy":
                task["authorization"][right]["scope"] = op["deployment_scope"]
    elif name == "receive":
        p = op["receipt"]
        receipt_context(state, p)
        receipt_result = admit_receipt(state, p)
        if state == original:
            return state, {"receipt": receipt_result, "revision": state["revision"], "next": None}
    elif name == "drain":
        for raw_key in list(state.get("unverified_receipts", {})):
            raw = state["unverified_receipts"][raw_key]
            if raw["status"] == "unverified":
                try:
                    admit_receipt(state, raw["payload"])
                except ERRORS as error:
                    quarantine(state, raw["payload"], str(error))
        for key in list(state["receipts"]):
            r = state["receipts"][key]
            if r["status"] not in ("buffered", "held"):
                continue
            previous = (r["status"], r.get("reason"))
            candidate = copy.deepcopy(state)
            try:
                status, reason = consume(candidate, r["payload"]), None
                state = candidate
            except ERRORS as error:
                status, reason = "held", str(error)
            r = state["receipts"][key]
            if previous != (status, reason):
                r["history"].append({"status": status, "reason": reason})
            r.update(status=status, reason=reason)
        # A newer verified handoff in this same drain makes earlier held revisions obsolete.
        for r in state["receipts"].values():
            if (r["status"] == "held" and r.get("report_verified") and
                    r["payload"]["delivery_revision"] < state["tasks"][r["payload"]["task_id"]]["delivery_revision"]):
                try:
                    receipt_context(state, r["payload"])
                except ERRORS:
                    continue
                r.update(status="stale", reason=None)
                r["history"].append({"status": "stale", "reason": "newer verified delivery consumed"})
        if state == original:
            return state, {"receipt": "unchanged", "revision": state["revision"], "next": None}
    elif name == "enqueue":
        require(op["item"]["kind"] not in ("acceptance", "assistance"), "use native delivery or reviewed request-materials")
        enqueue(state, op["item"])
    elif name == "request-materials":
        origin = state["receipts"][op["source_receipt"]]
        require(origin["status"] == "processed", "consume blocked receipt before reviewing external materials")
        receipt_report(state, origin["payload"])
        require(not task.get("blocker_followup") or task["blocker_followup"]["status"] not in ("human_pending", "external_requested"),
                "finish existing human item before routing external materials")
        add_assistance(state, task_id, op["source_receipt"], op["request"], op["technical_review"])
    elif name == "route-blocker":
        require(task and task["implementation"] == "blocked" and op["delivery_revision"] == task["delivery_revision"]
                and op.get("owner") and op.get("action"), "current technical assignment required")
        require(not task.get("blocker_followup") or task["blocker_followup"]["status"] != "human_pending",
                "finish current human item or use atomic redispatch")
        artifact(op["evidence"])
        task["blocker_followup"] = {"status": "assigned_technical", "owner": op["owner"], "action": op["action"],
                                    "delivery_revision": op["delivery_revision"], "evidence": op["evidence"]}
    elif name == "priority":
        require(op.get("user_reference") and state["items"][op["item_id"]]["status"] == "queued", "user priority required")
        state["priority_item"] = op["item_id"]
    elif name in ("activate", "next", "switch"):
        item = next_item(state) if name == "next" else state["items"][op["item_id"]]
        require(item and item["status"] == "queued" and not readiness(state, item), "no eligible item")
        if state["active_human_item"]:
            require(name == "switch" and op.get("user_reference"), "active cannot be interrupted")
            state["items"][state["active_human_item"]]["status"] = "queued"
        elif name == "activate":
            require(next_item(state)["id"] == item["id"] or op.get("user_reference"), "honor FIFO/user selection")
        item["status"], state["active_human_item"] = "active", item["id"]
        if state["priority_item"] == item["id"]:
            state["priority_item"] = None
    elif name in ("wait", "resume", "close", "reconcile-focus"):
        require(state["active_human_item"] == op["item_id"], "item not active")
        item = state["items"][op["item_id"]]
        if name == "wait":
            require(op.get("prompt") and not readiness(state, item), "valid item/user prompt required")
            item.update(status="waiting_user", waiting_prompt=op["prompt"])
        elif name == "resume":
            item["status"] = "active"
            item.pop("waiting_prompt", None)
        elif name == "reconcile-focus":
            require(item.get("migration_hold") and op.get("user_reference"), "explicit reconciliation required")
            item.pop("migration_hold")
            item["reconciliation_reference"] = op["user_reference"]
        else:
            require(not item.get("migration_hold") and op.get("user_reference"), "human resolution required")
            outcomes = {"acceptance": ("accepted", "rejected", "withdrawn"), "discussion": ("resolved", "withdrawn"),
                        "decision": ("answered", "withdrawn"), "release": ("authorized", "declined", "withdrawn"),
                        "assistance": ("requested", "provided", "withdrawn")}
            require(op["outcome"] in outcomes[item["kind"]], "bad outcome")
            if item["kind"] == "decision" and op["outcome"] == "answered":
                require(not readiness(state, item), "business decision evidence changed")
            if item["kind"] == "assistance":
                task = state["tasks"][item["task_id"]]
                if op["outcome"] == "withdrawn":
                    task["blocker_followup"] = None
                else:
                    require(not readiness(state, item), "external request evidence changed")
                    artifact(op["request_evidence"])
                    task["blocker_followup"] = {"status": "external_requested" if op["outcome"] == "requested" else "materials_received",
                        "owner": item["recipient"] if op["outcome"] == "requested" else task["owner_agent_id"],
                        "action": item["request_text"] if op["outcome"] == "requested" else "Verify received materials and redispatch worker",
                        "delivery_revision": task["delivery_revision"], "item_id": item["id"],
                        "evidence": op["request_evidence"], "user_reference": op["user_reference"]}
            if item["kind"] == "acceptance":
                t = state["tasks"][item["task_id"]]
                require(item["delivery_revision"] == t["delivery_revision"], "obsolete active delivery")
                if op["outcome"] == "accepted":
                    require(not readiness(state, item), "evidence changed; cannot accept")
                    t.update(user_acceptance="accepted", phase="awaiting_integration", blocker=None)
                else:
                    t.update(user_acceptance=op["outcome"], phase="developing", implementation="running",
                             blocker={"reason": "functional rework", "category": "business", "owner": t["owner_agent_id"]})
            if item["kind"] == "release" and op["outcome"] == "authorized":
                require(not readiness(state, item), "main verification required")
                require(item.get("deployment_scope"), "deployment scope required")
                state["tasks"][item["task_id"]]["authorization"]["deploy"] = {
                    "allowed": True, "user_reference": op["user_reference"], "scope": item["deployment_scope"]}
            item.update(status="closed", outcome=op["outcome"], resolution_reference=op["user_reference"])
            state["active_human_item"] = None
    elif name == "rework":
        require(task and task["phase"] != "completed" and op.get("reason"), "rework reason required")
        require(state["items"].get(state["active_human_item"], {}).get("task_id") != task_id,
                "finish active item first")
        require(not state["merge_reservation"], "release merge reservation first")
        ensure_capacity(state, task_id)
        reset_delivery(task)
        task.update(phase="developing", implementation="running")
        task["worker_run"] = run_record(task, "implementation", op["reason"])
        for item in state["items"].values():
            if item.get("task_id") == task_id and item["status"] == "queued":
                item["status"] = "superseded"
    elif name == "redispatch":
        item = state["items"].get(op.get("item_id"))
        task_id = item["task_id"] if item else task_id
        task = state["tasks"][task_id]
        require(task["start_reference"] and task["phase"] != "completed" and op.get("reason") and
                op.get("user_reference"), "existing scope and redispatch instruction required")
        require(not state["merge_reservation"], "release merge reservation before redispatch")
        active = state["items"].get(state["active_human_item"], {})
        require(active.get("task_id") != task_id or (item and item["id"] == active.get("id")),
                "finish active task item before internal redispatch")
        require(op["next_revision"] == task["delivery_revision"] + 1, "next worker revision must follow current delivery")
        require(not task.get("worker_run") or task["worker_run"]["status"] != "running", "worker already redispatched")
        ensure_capacity(state, task_id)
        if item:
            require(item["kind"] in ("decision", "assistance") and (state["active_human_item"] == item["id"] or
                    (item["status"] == "closed" and item.get("outcome") in ("withdrawn", "answered", "provided"))),
                    "redispatch needs active decision or previously resolved decision for recovery")
            item.update(status="closed", outcome="withdrawn", resolution_reference=op["user_reference"],
                        disposition="returned_to_technical_investigation")
            if state["active_human_item"] == item["id"]:
                state["active_human_item"] = None
        for pending in state["items"].values():
            if pending.get("task_id") == task_id and pending["status"] == "queued":
                pending["status"] = "superseded"
        reset_delivery(task)
        task.update(phase="developing", implementation="running", blocker=None, worker_run={
            "status": "running", "mode": "technical_investigation", "owner_agent_id": task["owner_agent_id"],
            "expected_delivery_revision": op["next_revision"], "reason": op["reason"],
            "user_reference": op["user_reference"], "scope": "existing_confirmed_scope_only"})
    elif name in ("mr", "integration-start", "integration-result", "review", "reserve", "release-reservation"):
        from merge_gate import merge_transition
        merge_transition(state, op)
    else:
        raise ValueError("unknown operation; completed requires verified main inclusion")
    state["revision"] += 1
    state["last_reconcile"] = None
    state["history"].append({"revision": state["revision"], "operation": copy.deepcopy(op)})
    validate(state)
    return state, {"receipt": receipt_result, "revision": state["revision"],
                   "next": None if name in ("receive", "drain") else presentation(state, next_item(state)),
                   "active": presentation(state, state["items"].get(state["active_human_item"]))
                             if name in ("next", "activate", "switch") else None}


def migrate(old, project_id, source_ref):
    require(old["schema"] == "parallel-coordination/1", "source not v1")
    require(load_bound(source_ref) == old, "migration source differs from preserved artifact")
    state = new_state(old["coordinator_id"], project_id)
    state.update(revision=old["revision"] + 1, legacy=copy.deepcopy(old), migration={"source": source_ref,
        "notes": [], "restrictions": "All new rights denied; legacy release fields confer no new authority."})
    for task_id, value in old["tasks"].items():
        t = task_base(value["owner_agent_id"], value["worktree"], value["branch"], value["resources"])
        t.update(delivery_revision=value["delivery_revision"], implementation=value["implementation"],
                 self_test="passed" if value["self_test"] == "passed" else "pending",
                 quality_review="pending", user_acceptance=value["user_acceptance"], legacy=copy.deepcopy(value),
                 phase="awaiting_integration" if value["user_acceptance"] == "accepted" else
                       "developing" if value["user_acceptance"] == "rejected" or value["implementation"] in ("running", "blocked")
                       else "awaiting_acceptance",
                 blocker={"reason": "legacy scope/start/evidence need reconciliation", "owner": value["owner_agent_id"],
                          "category": "evidence"})
        state["tasks"][task_id] = t
    state["items"] = copy.deepcopy(old["items"])
    for item in state["items"].values():
        if item["kind"] == "acceptance":
            item["legacy_unbound"] = True
    state["receipts"] = {k: dict(copy.deepcopy(v), status="legacy", legacy_status=v["status"])
                         for k, v in old["receipts"].items()}
    pointer = old["active_human_item"]
    state["active_human_item"] = pointer
    if pointer:
        require(pointer in state["items"], "v1 focus missing: manual recovery required")
        item = state["items"][pointer]
        if item["status"] not in ("active", "waiting_user"):
            item.update(status="active", migration_hold=True)
            state["migration"]["notes"].append("v1 active points to closed item; focus retained for reconciliation")
    state["history"].append({"revision": state["revision"], "operation": {"op": "migrate", "source": source_ref}})
    validate(state)
    return state


@contextmanager
def writer_lock(path):
    path = Path(path)
    require(path.is_absolute(), "state path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("writer lock exists; inspect owner/preserve state; never auto-steal: " + str(lock))
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "state": str(path)}).encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        lock.unlink()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--actor", help="actual host-mapped caller identity; JSON alone does not authenticate it")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--coordinator", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--main-repo")
    p.add_argument("--local-cas-root", help="explicit sandbox for local bare CAS exercises only")
    p.add_argument("--project-config", type=Path, help="instance-only main refs and protected file inventory")
    p = sub.add_parser("migrate")
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--project", required=True)
    for cmd in ("apply", "dispatch", "closeout", "reconcile"):
        sub.add_parser(cmd).add_argument("input", type=Path)
    p = sub.add_parser("pack")
    p.add_argument("input", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("receive")
    for flag in ("task", "worker", "report"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--kind", choices=("completed", "blocked", "integrated"), required=True)
    for cmd in ("check", "show", "summary", "progress", "drain", "next"):
        sub.add_parser(cmd)
    for cmd in ("wait", "resume", "close", "priority", "reconcile-focus", "redispatch"):
        p = sub.add_parser(cmd)
        p.add_argument("item_id")
        if cmd == "wait":
            p.add_argument("--prompt", required=True)
        if cmd in ("close", "priority", "reconcile-focus", "redispatch"):
            p.add_argument("--user-reference", required=True)
        if cmd == "redispatch":
            p.add_argument("--reason", required=True)
            p.add_argument("--next-revision", type=int, required=True)
        if cmd == "close":
            p.add_argument("--outcome", required=True)
            p.add_argument("--request-evidence", type=Path)
    for cmd in ("gate", "reserve"):
        p = sub.add_parser(cmd)
        p.add_argument("task_id")
        if cmd == "reserve":
            p.add_argument("--token", required=True)
            p.add_argument("--expected-revision", type=int, required=True)
    for cmd in ("local-cas-merge", "recover-merge", "release-reservation"):
        p = sub.add_parser(cmd)
        p.add_argument("--token", required=True)
        if cmd == "release-reservation":
            p.add_argument("--reason", required=True)
    args = parser.parse_args()
    cmd = args.command
    if cmd == "pack":
        require(not args.output.exists(), "immutable report already exists")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(args.output, pack(load(args.input)))
        print(json.dumps(ref(args.output), ensure_ascii=False))
        return 0
    require(args.state and args.state.is_absolute(), "absolute --state required")
    if cmd in ("show", "summary", "check", "gate", "progress"):
        state = load(args.state)
        validate(state)
        if cmd == "gate":
            from merge_gate import gate
            result = gate(state, args.task_id, actor_id=args.actor)
        elif cmd == "progress":
            from progress import round_handoff
            result = round_handoff(state, checkpoint(state))
        else:
            result = state if cmd == "show" else summary(state) if cmd == "summary" else checkpoint(state)
    else:
        with writer_lock(args.state):
            if cmd == "init":
                require(not args.state.exists(), "state exists; recover with show/check")
                state, result = new_state(args.coordinator, args.project, args.main_repo), {"receipt": "initialized"}
                state["project"]["local_cas_root"] = args.local_cas_root
                if args.project_config:
                    config = load(args.project_config)
                    require(set(config).issubset({"main_branch", "local_main_repo", "local_cas_root", "protected", "worker_capacity"}),
                            "unknown project instance field")
                    state["project"].update(config)
                    for value in state["project"]["protected"]:
                        artifact(value)
            elif cmd == "migrate":
                require(not args.state.exists() and args.state.resolve() != args.source.resolve(), "migration needs new path")
                state = migrate(load(args.source), args.project, ref(args.source))
                result = state["migration"]
            else:
                state = load(args.state)
                validate(state)
                if cmd in ("local-cas-merge", "recover-merge"):
                    from merge_gate import local_merge
                    state = local_merge(state, args.token, recover=cmd == "recover-merge", actor_id=args.actor)
                    result = {"receipt": "main_verified_locally", "revision": state["revision"]}
                elif cmd in ("closeout", "reconcile"):
                    from reconcile import closeout
                    state, result = closeout(state, load(args.input), ref(args.input), actor_id=args.actor)
                else:
                    if cmd in ("apply", "dispatch"):
                        op = load(args.input)
                        if cmd == "dispatch":
                            op["op"] = "dispatch"
                    elif cmd == "receive":
                        op = {"op": "receive", "receipt": {"project_id": state["project"]["id"],
                            "task_id": args.task, "owner_agent_id": args.worker, "channel": "native_agent",
                            "delivery_revision": args.revision, "kind": args.kind,
                            "event_id": f"{args.task}:{args.revision}:{args.kind}", "report": ref(args.report)}}
                        if state.get("delivery"):
                            op["receipt"]["recipient_agent_id"] = state["delivery"]["agent_id"]
                    else:
                        op = {k: v for k, v in vars(args).items() if k not in ("command", "state")}
                        op["op"] = cmd
                        if cmd == "close" and args.request_evidence:
                            op["request_evidence"] = ref(args.request_evidence)
                    op.pop("actor", None)
                    if args.actor:
                        require(not op.get("actor_id") or op["actor_id"] == args.actor, "caller identity mismatch")
                        op["actor_id"] = args.actor
                    state, result = transition(state, op)
            validate(state)
            write_atomic(args.state, state)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if cmd in ("check", "closeout", "reconcile") and not result["allow_end"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (*ERRORS, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
