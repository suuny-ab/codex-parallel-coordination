"""Scoped delivery delegation and offline release gates in the ONE task ledger.

Actor IDs and native observations are caller assertions, not host authentication.
No transport, GitLab, tag, backup or deployment is executed here.
"""
import copy

from evidence import ERRORS, artifact, fingerprint, load_bound, report_for, require

CONTROL = {"delegate", "revoke-delivery", "authorize-release", "authorize", "confirm", "dispatch", "register",
           "adopt-legacy", "next", "activate", "switch", "wait", "resume", "close", "priority", "reconcile-focus", "record-acceptance", "enqueue"}
OPS = {"delegate", "revoke-delivery", "delivery-transport", "delivery-check", "authorize-release", "release-step"}


def receipt_scope(state, payload):
    delegation = state.get("delivery")
    if delegation:
        require(payload["task_id"] in delegation["task_ids"], "receipt outside current delegated task scope")
        require(payload.get("recipient_agent_id") == delegation["agent_id"], "receipt recipient differs from current delivery receiver")


def buffered_authority_issues(state):
    pending = [r["payload"] for r in state["receipts"].values() if r["status"] in ("buffered", "held")]
    pending += [r["payload"] for r in state.get("unverified_receipts", {}).values() if r["status"] == "unverified"]
    issues = []
    for payload in pending:
        try:
            receipt_scope(state, payload)
        except ERRORS as error:
            issues.append(payload.get("event_id", "unknown receipt") + ": " + str(error))
    return issues


def actor_for(state, op):
    if not state.get("delivery") and op["op"] != "delegate":
        return
    actor = op.get("actor_id")
    if op["op"] in CONTROL:
        require(actor == state["coordinator_id"], "original main session controls user scope/decisions/acceptance only")
    elif op["op"] not in ("describe", "redispatch"):
        require(actor == state["delivery"]["agent_id"], "technical receipts and execution belong to delivery agent")
    else:
        require(actor in (state["coordinator_id"], state["delivery"]["agent_id"]), "unknown actor")
    if state.get("delivery") and actor == state["delivery"]["agent_id"]:
        targets = [op["task_id"]] if op.get("task_id") else []
        if op.get("business_task_id"):
            targets.append(op["business_task_id"])
        if op.get("item_id"):
            item = state["items"][op["item_id"]]
            if item.get("task_id"):
                targets.append(item["task_id"])
            require(op["op"] != "redispatch", "original main controls returning a human item to technical work")
        if op.get("source_receipt"):
            targets.append(state["receipts"][op["source_receipt"]]["payload"]["task_id"])
        require(all(key in state["delivery"]["task_ids"] for key in targets), "outside current delegated task scope")
    if state.get("delivery") and op["op"] == "receive":
        receipt_scope(state, op["receipt"])


def verify_transport(state, proof, allow_pending=False, task_id=None):
    delegation = state["delivery"]
    require(proof.get("mode") in ("fixture", "native_verified", "app_thread_verified", "mixed_verified") and
            proof.get("send_tool") and proof.get("receive_tool") and proof.get("creates_conversation") is False,
            "declare actual existing-agent send/receive capabilities; no new conversation")
    require(not any(word in (proof["send_tool"] + proof["receive_tool"]) for word in
                    ("create_thread", "fork_thread", "spawn_agent")), "conversation creation is not an existing-agent route")
    owners = {state["tasks"][key]["owner_agent_id"] for key in delegation["task_ids"]}
    require(set(proof["authors"]).issubset(owners), "unrelated task message target not authorized")
    keys = [task_id] if task_id else delegation["task_ids"]
    if proof["mode"] == "mixed_verified":
        for key in keys:
            owner = state["tasks"][key]["owner_agent_id"]
            channels = delegation["routes"][owner].get("channels", {})
            require(channels and set(channels) <= {"native_agent", "app_thread"}, "explicit mixed channel routes required")
            for channel, route in channels.items():
                artifact(route["evidence"])
                pair = proof["authors"].get(owner, {}).get("channels", {}).get(channel)
                if not pair and allow_pending:
                    continue
                require(pair and pair.get("outbound") and pair.get("inbound"), "mixed route acknowledgment missing")
                outgoing, incoming = load_bound(pair["outbound"]), load_bound(pair["inbound"])
                mode = "app_thread_verified" if channel == "app_thread" else "native_verified"
                require(outgoing["sender_id"] == delegation["agent_id"] and outgoing["recipient_id"] == owner and
                        outgoing["target_id"] == route["target_id"] and outgoing["status"] == "accepted" and
                        outgoing["mode"] == mode and outgoing.get("correlation_id"), "mixed send identity mismatch")
                require(incoming["sender_id"] == owner and incoming["recipient_id"] == delegation["agent_id"] and
                        incoming["status"] == "acknowledged" and incoming["mode"] == mode and
                        incoming["correlation_id"] == outgoing["correlation_id"], "mixed acknowledgment mismatch")
        return
    for key in keys:
        owner = state["tasks"][key]["owner_agent_id"]
        pair = proof["authors"].get(owner)
        if not pair and allow_pending:
            continue
        require(pair and pair.get("outbound"), "message acceptance evidence missing")
        route = delegation["routes"][owner]
        artifact(route["evidence"])
        outgoing = load_bound(pair["outbound"])
        require(outgoing["sender_id"] == delegation["agent_id"] and outgoing["recipient_id"] == owner and
                outgoing["target_id"] == route["target_id"] and outgoing["status"] == "accepted" and
                outgoing["mode"] == proof["mode"] and outgoing.get("correlation_id"), "message acceptance identity/target mismatch")
        if not pair.get("inbound") and allow_pending:
            continue
        incoming = load_bound(pair["inbound"])
        require(incoming["sender_id"] == owner and incoming["recipient_id"] == delegation["agent_id"] and
                incoming["correlation_id"] == outgoing["correlation_id"] and incoming["status"] == "acknowledged" and
                incoming["mode"] == proof["mode"], "peer acknowledgment missing or unrelated; accepted is not acknowledged/completed")


def transport_issues(state, task_id=None):
    delegation = state.get("delivery")
    if not delegation:
        return []
    try:
        artifact(delegation["authority_evidence"])
        verify_transport(state, delegation.get("transport") or {}, task_id=task_id)
        return []
    except ERRORS as error:
        return [str(error)]


def require_executor(state, actor_id, task_id=None):
    value = state.get("delivery")
    require(value and value["agent_id"] == actor_id, "explicit single delivery executor authorization required")
    require(not transport_issues(state, task_id), "delivery authority or direct contact/receipt evidence invalid")
    if task_id:
        require(task_id in value["task_ids"], "outside delegated task scope")
        require(actor_id != state["tasks"][task_id]["owner_agent_id"], "delivery cannot independently review its own implementation")
    return value


def verify_ci(ci, head, required):
    require(ci["head_sha"] == head and ci["pipeline_id"] and ci["status"] == "passed" and required and
            all(ci["jobs"].get(job) == "passed" for job in required), "source CI stale, incomplete or failed; return to original author")
    require(ci.get("evidence"), "source CI evidence missing")
    for value in ci["evidence"]:
        artifact(value)


def merge_authority(state, task_id, actor_id, for_merge=True):
    delegation = require_executor(state, actor_id, task_id)
    prior = delegation["merge_order"][:delegation["merge_order"].index(task_id)]
    require(not for_merge or all(state["tasks"][key]["phase"] == "completed" for key in prior), "honor authorized serial merge order")
    release = state.get("release_plan")
    require(not for_merge or not release or task_id not in release["before_tasks"] or release["completed_steps"] == release["actions"],
            "authorized release must finish before later main MRs")
    task = state["tasks"][task_id]
    reservation = state.get("merge_reservation") or {}
    require(task.get("delivery_check") and (task.get("delivery_check_revision") == state["revision"] or
            (reservation.get("task_id") == task_id and reservation.get("reserved_revision") == state["revision"] ==
             task.get("delivery_check_revision", -2) + 1)), "fresh same-revision source/main/CI check required")
    check = load_bound(task["delivery_check"])
    mr, report = task["mr"], report_for(state, task_id)
    require(check["project_id"] == state["project"]["id"] and check["task_id"] == task_id and
            check["delivery_revision"] == task["delivery_revision"] and check["source_digest"] == report["source"]["digest"] and
            check["head_sha"] == mr["head_sha"] and check["main_sha"] == mr["target_sha"],
            "source/main observation stale; return to original author")
    verify_ci(check["ci"], mr["head_sha"], delegation["required_ci_jobs"])
    return delegation


def verify_release_history(plan):
    artifact(plan["authority_evidence"])
    artifact(plan["artifact"])
    manifest = load_bound(plan["manifest"])
    require(manifest["release_sha"] == plan["release_sha"] and manifest["included_task_ids"] == plan["included_task_ids"] and
            manifest["artifact_sha256"] == plan["artifact"]["sha256"], "release manifest scope/artifact changed")
    for step in plan["completed_steps"]:
        prior = load_bound(plan["results"][step]["result"])
        load_bound(plan["results"][step]["observation"])
        for value in prior["evidence"]:
            artifact(value)


def release_issues(state):
    try:
        if state.get("release_plan"):
            verify_release_history(state["release_plan"])
        return []
    except ERRORS as error:
        return [str(error)]


def release_gate(state, actor_id, action, observation_ref):
    require_executor(state, actor_id)
    plan = state.get("release_plan")
    require(plan and action in plan["actions"] and len(plan["completed_steps"]) < len(plan["actions"]) and
            plan["actions"][len(plan["completed_steps"])] == action,
            "separate exact-SHA release authority and ordered next action required")
    verify_release_history(plan)
    observation = load_bound(observation_ref)
    require(observation["release_sha"] == observation["checkout_sha"] == plan["release_sha"] and
            observation["artifact"] == plan["artifact"] and observation["environment"] == plan["environment"],
            "release must use frozen SHA/artifact/environment, never future main")
    verify_ci(observation["ci"], plan["release_sha"], state["delivery"]["required_ci_jobs"])
    if action == "deploy":
        require(observation.get("active_creation") is False and observation.get("data_preservation_verified") is True,
                "active creation or unverified data preservation; escalate exception")
        require(observation.get("safety_evidence"), "data/activity safety evidence required")
        for value in observation["safety_evidence"]:
            artifact(value)
    return plan


def apply(state, op):
    name = op["op"]
    if name == "delegate":
        require(not state.get("delivery") and not state["merge_reservation"], "one delivery executor only; revoke explicitly first")
        require(op.get("user_reference") and isinstance(op["agent_id"], str) and op["agent_id"] and
                op["agent_id"] != state["coordinator_id"], "actual scoped delegation required")
        ids, order = op["task_ids"], op["merge_order"]
        require(ids and len(ids) == len(set(ids)) and set(ids).issubset(state["tasks"]) and
                len(order) == len(ids) and set(order) == set(ids), "concrete task scope and merge order required")
        require(all(state["tasks"][key]["owner_agent_id"] != op["agent_id"] for key in ids), "independent delivery reviewer required")
        artifact(op["authority_evidence"])
        jobs=op.get('required_ci_jobs')
        from task_completion import definition
        require(isinstance(jobs,list) and all(isinstance(x,str) and x.strip() for x in jobs) and
            (jobs or all(definition(state['tasks'][key])[0]=='read_only' for key in op['task_ids'])), 'required CI jobs must be explicit for code tasks')
        owners = {state["tasks"][key]["owner_agent_id"] for key in ids}
        require(set(op["routes"]) == owners, "explicit authorized existing author routes required")
        for route in op["routes"].values():
            require(route.get("target_id") and route.get("existing_work_agent") is True, "only authorized existing work agents")
            artifact(route["evidence"])
        state["delivery"] = {k: copy.deepcopy(op[k]) for k in
            ("agent_id", "task_ids", "merge_order", "required_ci_jobs", "authority_evidence", "user_reference", "routes")}
        state["delivery"]["transport"] = None
    elif name == "revoke-delivery":
        require(state.get("delivery") and not state["merge_reservation"] and op.get("user_reference"), "release reservation before explicit revocation")
        artifact(op["evidence"])
        state.setdefault("delivery_history", []).append(state.pop("delivery"))
        state.pop("release_plan", None)
    elif name == "delivery-transport":
        state["delivery"]["transport"] = copy.deepcopy(op["transport"])
        verify_transport(state, op["transport"], allow_pending=True)
    elif name == "delivery-check":
        require_executor(state, op["actor_id"], op["task_id"])
        require(load_bound(op["observation"])["based_on_revision"] == state["revision"], "CI observation predates ledger changes")
        state["tasks"][op["task_id"]]["delivery_check"] = copy.deepcopy(op["observation"])
        state["tasks"][op["task_id"]]["delivery_check_revision"] = state["revision"]
        merge_authority(state, op["task_id"], op["actor_id"], for_merge=False)
        state["tasks"][op["task_id"]]["delivery_check_revision"] = state["revision"] + 1
    elif name == "authorize-release":
        require(state.get("delivery") and not state.get("release_plan") and op.get("user_reference"), "independent release scope authority required")
        plan = copy.deepcopy(op["plan"])
        require(len(plan["release_sha"]) in (40, 64) and all(x in "0123456789abcdef" for x in plan["release_sha"]), "exact release SHA required")
        included = plan["included_task_ids"]
        require(included and len(set(included)) == len(included) and set(included).issubset(state["delivery"]["task_ids"]) and
                all(state["tasks"][key]["phase"] == "completed" for key in included), "release scope must be already merged and verified")
        require(set(plan["before_tasks"]).issubset(state["delivery"]["task_ids"]) and not set(included) & set(plan["before_tasks"]), "future merge barrier invalid")
        require(plan["actions"] == ["tag", "backup", "deploy", "verify"] and plan.get("environment") and plan.get("tag"), "explicit tag/backup/deploy/verify sequence required")
        artifact(plan["authority_evidence"])
        artifact(plan["artifact"])
        manifest = load_bound(plan["manifest"])
        require(manifest["release_sha"] == plan["release_sha"] and manifest["included_task_ids"] == included,
                "frozen release manifest mismatch")
        plan.update(user_reference=op["user_reference"], completed_steps=[], results={})
        verify_release_history(plan)
        state["release_plan"] = plan
    elif name == "release-step":
        plan = release_gate(state, op["actor_id"], op["action"], op["observation"])
        result = load_bound(op["result"])
        require(result["status"] == "passed" and result["action"] == op["action"] and result["release_sha"] == plan["release_sha"] and
                result["artifact"] == plan["artifact"] and result["environment"] == plan["environment"] and result.get("evidence"), "release result binding mismatch")
        if op["action"] == "tag":
            require(result["tag"] == plan["tag"], "tag outside authorization")
        for value in result["evidence"]:
            artifact(value)
        plan["completed_steps"].append(op["action"])
        plan["results"][op["action"]] = {"observation": op["observation"], "result": op["result"]}


def closeout_route(state, result):
    """Only user items/batch completion/important exceptions route to main."""
    value = state.get("delivery")
    if not value:
        return
    issues = transport_issues(state) + release_issues(state) + buffered_authority_issues(state)
    result["technical_owner"] = value["agent_id"]
    result["human_session"] = state["coordinator_id"]
    result["routine_root_confirmation_required"] = False
    notification = None
    if issues:
        result.update(allow_end=False, action="delivery_evidence_blocked")
        notification = {"kind": "important_delivery_exception", "evidence_issue": issues,
                        "next": "restore scoped authority/route/release evidence; report one important exception, never recurring root relay"}
    elif result["user_handoff"]["kind"] == "human_item":
        notification = {"kind": "human_item", "item_id": result["next"]["id"], "item": result["user_handoff"]["item"]}
    elif all(state["tasks"][key]["phase"] == "completed" for key in value["task_ids"]) and (
            not state.get("release_plan") or state["release_plan"]["completed_steps"] == state["release_plan"]["actions"]):
        notification = {"kind": "batch_complete"}
    if notification:
        token = fingerprint(notification)
        if token != value.get("last_notification"):
            result["notify_main"] = dict(notification, recipient_id=state["coordinator_id"])
            value["last_notification"] = token
        else:
            result["notify_main"] = None
    else:
        result["notify_main"] = None
