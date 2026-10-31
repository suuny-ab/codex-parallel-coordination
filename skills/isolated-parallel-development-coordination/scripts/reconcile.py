"""One-shot native inbox / worker / registered-report reconciliation before closeout.

The caller supplies real native observations (or explicitly labeled fixtures). This
module neither queries an agent host nor pretends a file is a delivered native event.
"""
import copy
from pathlib import Path

from evidence import ERRORS, artifact, fingerprint, load, load_bound, ref, require


def current_observation_issues(state, by_task):
    """Read-only checks shared by closeout and later projections of its observation."""
    issues = []
    discoveries = {}
    for task_id, task in state["tasks"].items():
        if task["phase"] == "completed":
            continue
        worker = by_task.get(task_id)
        if task.get("worker_run", {}).get("status") == "queued":
            if worker and (worker["status"] == "running" or
                    worker.get("last_delivery_revision", 0) >= task["worker_run"]["expected_delivery_revision"]):
                issues.append(task_id + ": queued work contradicts native observation; reconcile dispatch")
            continue
        if not task["start_reference"] and task["implementation"] == "pending":
            continue
        if not worker:
            issues.append(task_id + ": worker final state not observed")
            continue
        require(worker["owner_agent_id"] == task["owner_agent_id"], "worker identity mismatch")
        require(worker["status"] in ("running", "completed", "blocked", "unknown"), "bad worker observation")
        try:
            artifact(worker["evidence"])
        except ERRORS as error:
            issues.append(task_id + ": worker observation evidence invalid: " + str(error))
        if worker["status"] == "unknown":
            issues.append(task_id + ": native worker state unavailable")
        path = task.get("report_path")
        if not path:
            issues.append(task_id + ": dispatch report discovery path not registered")
            continue
        try:
            require(Path(path).is_absolute(), "report discovery path not absolute")
            if not Path(path).exists():
                require(worker["status"] == "running" and task["delivery_revision"] == 0,
                        "terminal/previous delivery report missing")
                discoveries[task_id] = {"status": "not_written_yet"}
                continue
            report_ref, report = ref(path), load(path)
            require(report["task_id"] == (task["report_task_id"] or task_id), "discovered report task mismatch")
            revision = report["delivery_revision"]
            require(type(revision) is int and revision > 0, "discovered revision invalid")
            discoveries[task_id] = dict(report_ref, delivery_revision=revision)
            matching = [r for r in state["receipts"].values() if r["status"] in ("processed", "stale") and
                        r.get("payload", {}).get("task_id") == task_id and
                        r["payload"]["delivery_revision"] == revision and r["payload"]["report"] == report_ref]
            require(revision <= task["delivery_revision"] and matching,
                    "unregistered final report/native message; reconcile terminal receipt before ending")
            from coordination import receipt_context
            for receipt in matching:
                receipt_context(state, receipt["payload"])
            if worker["status"] == "running" and revision > worker.get("last_delivery_revision", 0):
                issues.append(task_id + ": report newer than running observation; refresh one-shot worker state")
            if worker["status"] in ("completed", "blocked"):
                require(worker.get("last_delivery_revision") == revision, "worker terminal revision/report mismatch")
                run = task.get("worker_run", {})
                require(run.get("status") != "running" or revision >= run["expected_delivery_revision"],
                        "terminal observation predates redispatch; verify the current worker run")
        except ERRORS as error:
            issues.append(task_id + ": " + str(error))
    from coordination import blocked_issues
    for task_id, reason in blocked_issues(state).items():
        issues.append(task_id + ": " + reason)
    for value in state["project"]["protected"]:
        try:
            artifact(value)
        except ERRORS as error:
            issues.append("protected data changed: " + str(error))
    return discoveries, issues


def closeout(original, observation, observation_ref, actor_id=None):
    from coordination import checkpoint, transition, validate
    validate(original)
    if original.get("delivery"):
        from delivery import actor_for
        actor_for(original, {"op": "drain", "actor_id": actor_id})
        require(observation.get("recipient_agent_id") == actor_id, "closeout must observe delivery receiver inbox")
    require(observation["schema"] == "coordination-closeout/1" and
            observation["project_id"] == original["project"]["id"] and
            observation["coordinator_id"] == original["coordinator_id"], "closeout identity mismatch")
    require(observation["based_on_revision"] == original["revision"], "closeout observation revision stale")
    require(load_bound(observation_ref) == observation, "observation content differs from bound artifact")
    state, issues, consumed = copy.deepcopy(original), [], []
    inbox = observation["inbox"]
    require(inbox["mode"] in ("native_snapshot", "app_thread_snapshot", "fixture", "unavailable"), "unknown observation mode")
    if inbox["mode"] == "unavailable" or inbox.get("complete") is not True:
        issues.append("native inbox coverage unverified; ledger check alone is not a complete closeout")
    else:
        artifact(inbox["evidence"])
    # Consume messages already received before checking worker/report parity.
    for message in inbox.get("messages", []):
        try:
            state, received = transition(state, {"op": "receive", "receipt": message, "actor_id": actor_id})
            if received["receipt"] != "quarantined":
                consumed.append(message["event_id"])
        except ERRORS as error:
            issues.append("native message rejected: " + str(error))
    state, _ = transition(state, {"op": "drain", "actor_id": actor_id})
    workers = observation.get("workers", [])
    require(len({w["task_id"] for w in workers}) == len(workers), "duplicate worker observations")
    by_task = {w["task_id"]: w for w in workers}
    require(set(by_task).issubset(state["tasks"]), "unknown observed task")
    # Consume newly observed terminals before consulting a potentially old report pointer.
    for worker in workers:
        if not worker.get("receipt"):
            continue
        try:
            message = worker["receipt"]
            require(worker["status"] in ("completed", "blocked") and message["task_id"] == worker["task_id"],
                    "terminal observation/receipt mismatch")
            state, received = transition(state, {"op": "receive", "receipt": message, "actor_id": actor_id})
            if received["receipt"] != "quarantined":
                consumed.append(message["event_id"])
        except ERRORS as error:
            issues.append("native terminal rejected: " + str(error))
    state, _ = transition(state, {"op": "drain", "actor_id": actor_id})
    discoveries, current_issues = current_observation_issues(state, by_task)
    issues.extend(current_issues)
    # Held receipts, including active acceptance freezes, are visible reconciliation gaps.
    for key, receipt in state["receipts"].items():
        if receipt["status"] in ("held", "buffered"):
            issues.append(key + ": " + receipt.get("reason", "not consumed"))
    for arrival in state.get("unverified_receipts", {}).values():
        if arrival["status"] == "unverified":
            issues.append(arrival["payload"]["event_id"] + ": unverified arrival: " + arrival["reason"])
    state["revision"] += 1
    from progress import round_handoff, state_token
    state["last_reconcile"] = {"revision": state["revision"], "observation": observation_ref,
        "observation_digest": fingerprint(observation), "mode": inbox["mode"], "issues": issues,
        "discoveries": discoveries, "consumed": consumed, "workers": by_task, "state_token": state_token(state)}
    state["history"].append({"revision": state["revision"], "operation": {
        "op": "closeout", "observation": observation_ref, "issues": issues, "consumed": consumed}})
    validate(state)
    check = checkpoint(state)  # Exactly this saved revision; no stale pre-receipt check.
    result = dict(check, scope="reconciled_snapshot" if inbox["mode"] in ("native_snapshot", "app_thread_snapshot") else
                  "fixture_only" if inbox["mode"] == "fixture" else "incomplete_native_coverage",
                  reconciliation=state["last_reconcile"], allow_end=not issues and check["allow_end"])
    if issues:
        result["action"] = "reconcile_required"
    result["user_handoff"] = round_handoff(state, result)
    from delivery import closeout_route
    closeout_route(state, result)
    # Notification dedup is part of the same saved authority revision.
    state["last_reconcile"]["state_token"] = state_token(state)
    result["limitation"] = "Point-in-time declarations only; no host idle wakeup, transport authentication or future-event coverage."
    return state, result
