"""GitLab MR metadata gate and explicitly local bare-repository CAS demonstration."""
import copy
from pathlib import Path

from evidence import artifact, git, load_bound, report_for, require


def integration_result(state, task_id, report_ref):
    task, result = state["tasks"][task_id], load_bound(report_ref)
    from task_completion import definition
    require(definition(task)[0]=='code','read-only tasks do not use code integration')
    report, mr = report_for(state, task_id), task["mr"]
    require(mr and task["user_acceptance"] == "accepted", "acceptance/MR missing")
    require(result["task_id"] == task_id and result["project_id"] == state["project"]["id"] and
            result["delivery_revision"] == task["delivery_revision"], "integration identity mismatch")
    require(result["source_digest"] == report["source"]["digest"] and result["head_sha"] == mr["head_sha"]
            and result["target_sha"] == mr["target_sha"], "integration binding stale")
    require(result["status"] in ("passed", "failed") and result["command"].strip() and
            result["evidence"], "integration result/evidence required")
    for value in result["evidence"]:
        artifact(value)
    task.update(integration={"status": result["status"], "report": report_ref}, review=None,
                phase="awaiting_merge" if result["status"] == "passed" else "awaiting_integration",
                blocker=None if result["status"] == "passed" else {
                    "reason": "integration failed", "owner": task["owner_agent_id"], "category": "technical"})


def gate(state, task_id, target_override=None, actor_id=None):
    from coordination import validate
    validate(state)
    from delivery import merge_authority
    merge_authority(state, task_id, actor_id)
    for value in state["project"]["protected"]:
        artifact(value)
    task = state["tasks"][task_id]
    from task_completion import definition
    require(definition(task)[0]=='code','read-only tasks do not use code merge gates')
    require(task["phase"] == "awaiting_merge" and not task["blocker"], "task not ready for merge")
    for right in ("commit", "push", "mr", "merge"):
        require(task["authorization"][right]["allowed"] and task["authorization"][right]["user_reference"],
                "not authorized: " + right)
    require(task["user_acceptance"] == "accepted", "functional acceptance missing")
    report, mr = report_for(state, task_id), task["mr"]
    require(mr and mr["provider"] == state["project"].get('provider','gitlab') and mr["target_branch"] == state["project"]["main_branch"]
            and mr["source_branch"] == task["branch"], "MR identity mismatch")
    require(report.get("git", {}).get("head_sha") == mr["head_sha"] and
            report["git"]["target_sha"] == mr["target_sha"], "delivery Git binding stale")
    require(task["integration"]["status"] == "passed", "integration not passed")
    result = load_bound(task["integration"]["report"])
    require(result["status"] == "passed" and result["command"].strip() and result["evidence"], "integration incomplete")
    require(result["source_digest"] == report["source"]["digest"] and result["head_sha"] == mr["head_sha"]
            and result["target_sha"] == mr["target_sha"] and result["delivery_revision"] == task["delivery_revision"]
            and result["task_id"] == task_id and result["project_id"] == state["project"]["id"], "integration stale")
    for value in result["evidence"]:
        artifact(value)
    review = task["review"]
    require(review and review["source_digest"] == report["source"]["digest"] and review["head_sha"] == mr["head_sha"]
            and review["target_sha"] == mr["target_sha"], "coordinator review missing/stale")
    require(review.get("reviewer_id") == actor_id, "delivery technical independent review required")
    artifact(review["evidence"])
    root = task["worktree"]
    require(git(root, "branch", "--show-current") == task["branch"], "task branch changed")
    require(not git(root, "status", "--porcelain"), "task worktree dirty")
    require(git(root, "rev-parse", "HEAD") == mr["head_sha"], "task head changed")
    main = state["project"]["local_main_repo"]
    require(main and Path(main).is_absolute(), "local main ref repository required")
    current = target_override or git(main, "rev-parse", "refs/heads/" + mr["target_branch"])
    require(current == mr["target_sha"], "main changed; same worker must update same MR and retest")
    require(git(root, "merge-base", mr["target_sha"], mr["head_sha"]) == mr["target_sha"],
            "head must include tested main")
    return {"task_id": task_id, "delivery_revision": task["delivery_revision"], "source_digest": report["source"]["digest"],
            "head_sha": mr["head_sha"], "target_sha": mr["target_sha"], "mr_id": mr["id"],
            "revision": state["revision"], "scope": "offline-local-ref-only; no remote atomic guarantee"}


def merge_transition(state, op):
    name, task = op["op"], state["tasks"].get(op.get("task_id"))
    if name == "mr":
        from task_completion import definition
        require(task and definition(task)[0]=='code','read-only tasks do not create an MR')
        require(task and task["authorization"]["mr"]["allowed"], "MR maintenance not authorized")
        require(task["phase"] != "completed", "completed MR immutable")
        mr = op["mr"]
        from demand_routing import pr_guard
        pr_guard(state, op['task_id'], mr)
        require(mr["provider"] in ('gitlab','github') and mr["id"] and mr["source_branch"] == task["branch"] and
                mr["target_branch"] == state["project"]["main_branch"], "bad MR fields")
        for key in ("head_sha", "target_sha"):
            require(len(mr[key]) in (40, 64) and all(c in "0123456789abcdef" for c in mr[key]), "bad SHA")
        require(not task["mr"] or (task["mr"]["id"], task["mr"]["provider"]) == (mr["id"], mr["provider"]),
                "maintain the same MR")
        if task["mr"] != mr:
            require(task["phase"] not in ("integrating", "awaiting_merge"), "rework before changing tested MR")
            task.update(integration={"status": "pending"}, review=None)
        task["mr"] = copy.deepcopy(mr)
    elif name == "integration-start":
        require(task and task["phase"] == "awaiting_integration" and task["user_acceptance"] == "accepted", "acceptance required")
        report_for(state, op["task_id"])
        require(task["mr"], "MR required")
        task.update(phase="integrating", blocker=None)
    elif name == "integration-result":
        require(task and task["phase"] == "integrating", "integration not started")
        integration_result(state, op["task_id"], op["report"])
    elif name == "review":
        reviewer = state.get("delivery", {}).get("agent_id", state["coordinator_id"])
        require(task and task["phase"] == "awaiting_merge" and
                op.get("actor_id", op.get("coordinator_id")) == reviewer and reviewer != task["owner_agent_id"], "independent delivery reviewer required")
        report = report_for(state, op["task_id"])
        artifact(op["evidence"])
        task["review"] = {"evidence": op["evidence"], "reviewer_id": reviewer, "source_digest": report["source"]["digest"],
                          "head_sha": task["mr"]["head_sha"], "target_sha": task["mr"]["target_sha"]}
    elif name == "reserve":
        require(not state["merge_reservation"] and op.get("token"), "reservation already held/token missing")
        require("expected_revision" in op, "fresh ledger revision required")
        state["merge_reservation"] = dict(gate(state, op["task_id"], actor_id=op.get("actor_id")), token=op["token"],
                                           reserved_revision=state["revision"] + 1)
    elif name == "release-reservation":
        require(state["merge_reservation"] and state["merge_reservation"]["token"] == op["token"] and
                op.get("reason"), "reservation token/reason required")
        state["merge_reservation"] = None


def local_merge(state, token, recover=False, actor_id=None):
    from coordination import validate
    validate(state)
    from delivery import require_executor
    require_executor(state, actor_id)
    reservation = state["merge_reservation"]
    require(reservation and reservation["token"] == token, "reservation token mismatch")
    require(recover or state["revision"] == reservation["reserved_revision"], "ledger changed after reserve; recheck")
    task_id, root = reservation["task_id"], state["project"]["local_main_repo"]
    sandbox = state["project"].get("local_cas_root")
    require(sandbox and Path(sandbox).is_absolute() and root and Path(root).is_absolute()
            and Path(root).resolve().is_relative_to(Path(sandbox).resolve()), "explicit local CAS sandbox required")
    require(git(root, "rev-parse", "--is-bare-repository") == "true", "CAS requires a local bare repository")
    checked = gate(state, task_id, reservation["target_sha"] if recover else None, actor_id=actor_id)
    for key in ("task_id", "delivery_revision", "source_digest", "head_sha", "target_sha", "mr_id"):
        require(checked[key] == reservation[key], "reservation binding changed")
    branch = "refs/heads/" + state["project"]["main_branch"]
    require(git(root, "cat-file", "-t", reservation["head_sha"]) == "commit", "head object absent locally")
    if recover:
        require(git(root, "rev-parse", branch) == reservation["head_sha"], "recovery ref mismatch")
    else:
        git(root, "update-ref", branch, reservation["head_sha"], reservation["target_sha"])
    require(git(root, "rev-parse", branch) == reservation["head_sha"], "main verification failed")
    result = copy.deepcopy(state)
    result["tasks"][task_id].update(phase="completed", merge={"verified": True, "head_sha": reservation["head_sha"],
        "target_sha": reservation["target_sha"], "scope": "local-bare-CAS-only", "recovered": recover})
    result.update(merge_reservation=None, revision=state["revision"] + 1, last_reconcile=None)
    result["history"].append({"revision": result["revision"], "operation": {
        "op": "recover-merge" if recover else "local-cas-merge", "reservation": reservation}})
    validate(result)
    return result
