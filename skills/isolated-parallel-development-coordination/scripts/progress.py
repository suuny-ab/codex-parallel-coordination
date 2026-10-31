"""User-facing end-of-round projection of the existing ledger, never a second ledger."""
from evidence import ERRORS, artifact, fingerprint, load_bound, report_for

CATEGORIES = ("in_progress", "queued", "technical_blocked", "external_wait", "awaiting_integration",
              "awaiting_acceptance", "quality_review", "awaiting_merge", "completed", "discussion", "uncertain")


def state_token(state):
    return fingerprint({k: state.get(k) for k in ("project", "coordinator_id", "revision", "tasks", "items",
                                                "receipts", "unverified_receipts", "delivery", "release_plan")})


def public_text(state, text):
    text = str(text)
    for value in [state["coordinator_id"], state.get("delivery", {}).get("agent_id", "\0")] + [t["owner_agent_id"] for t in state["tasks"].values()]:
        if isinstance(value,str) and value:
            text = text.replace(value, "执行者")
    return text


def verification_issues(state):
    from coordination import checkpoint
    from reconcile import current_observation_issues
    audit = state.get("last_reconcile")
    if not audit or audit["revision"] != state["revision"] or audit.get("state_token") != state_token(state):
        return ["本轮状态尚未对账"]
    issues = ["部分观察或后续责任尚未核对"] if audit["issues"] else []
    try:
        observation = load_bound(audit["observation"])
        if fingerprint(observation) != audit["observation_digest"]:
            issues.append("观察内容与本轮不一致")
        artifact(observation["inbox"]["evidence"])
        for worker in audit.get("workers", {}).values():
            artifact(worker["evidence"])
    except ERRORS:
        issues.append("本轮观察依据已失效")
    # A ledger revision alone cannot detect files arriving or evidence expiring.
    # Reuse the closeout checks without consuming messages or changing the ledger.
    discoveries, current = current_observation_issues(state, audit.get("workers", {}))
    if current or discoveries != audit.get("discoveries", {}):
        issues.append("报告发现、执行观察或后续责任依据已变化，需重新对账")
    check = checkpoint(state)
    if check.get("held") or check.get("blocked_followup_issues"):
        issues.append("当前就绪或阻塞后续证据尚未核清")
    from delivery import release_issues, transport_issues
    if transport_issues(state):
        issues.append("交付负责人的联系或收件通路尚未核实")
    if release_issues(state):
        issues.append("冻结发布的授权、制品或已执行阶段证据待核对")
    return issues


def task_row(state, task_id, members, observed):
    root = state["tasks"][task_id]
    info = root.get("progress", {})
    facts, issues = [], []
    for fact in info.get("facts", []):
        try:
            if fact.get("evidence"):
                artifact(fact["evidence"])
            facts.append(public_text(state, fact["text"]))
        except ERRORS:
            issues.append("既有阶段事实的依据待核对")
    if root.get("acceptance_record"):
        try:
            artifact(root["acceptance_record"]["evidence"])
        except ERRORS:
            issues.append("已记录验收的依据待核对")
    if root.get("delivery") and root["implementation"] != "running":
        try:
            report_for(state, task_id, ready=False)
            if root["self_test"] == "passed":
                facts.append("最近交付的实现与自测已完成")
        except ERRORS:
            issues.append("最近交付的呈现或证据待核对")
    if root["user_acceptance"] == "accepted":
        facts.append("当前修订的功能验收已通过")
    tasks = [(key, state["tasks"][key]) for key in members]
    running = [(key, t) for key, t in tasks if t.get("worker_run", {}).get("status") == "running"]
    queued = [(key, t) for key, t in tasks if t.get("worker_run", {}).get("status") == "queued"]
    if running:
        category = "in_progress"
        roles = {t.get("progress", {}).get("role") for _, t in running}
        modes = {t.get("worker_run", {}).get("mode") for _, t in running}
        stage = "独立复核中" if roles == {"review"} else "质量探索中" if modes == {"quality_rework"} else \
                "技术排查中" if modes == {"technical_investigation"} else "集成验证中" if modes == {"integration"} else "开发与自测中"
        next_step = "完成独立核证后组织下一关口" if roles == {"review"} else \
                    "核对同输入的质量取舍并形成可比较结果" if modes == {"quality_rework"} else "完成当前实现、验证后提交可验收结果"
        for key, task in running:
            observation = observed.get(key)
            if not observation or observation["status"] != "running":
                issues.append("已派发，当前执行状态待核实")
    elif queued and (any(key == task_id for key, _ in queued) or
                     root["phase"] not in ("awaiting_integration", "awaiting_merge", "completed")):
        category, stage, next_step = "queued", "排队等待执行资源", "获得执行资源后继续已确认范围"
    elif root["implementation"] == "blocked":
        followup = root.get("blocker_followup") or {}
        external = followup.get("status") == "external_requested"
        category = "external_wait" if external else "technical_blocked"
        stage = "等待外部资料" if external else "技术受阻，已安排后续核查" if followup.get("status") == "assigned_technical" else "受阻，后续责任待核对"
        next_step = "收到资料后交原执行者核对" if external else "由技术执行者完成核查并回报结果"
    elif root["phase"] == "completed":
        category, stage, next_step = "completed", "已合入并核对", "部署按独立授权另行安排"
        facts.append("开发交付流程已核对完成")
    elif root["phase"] in ("awaiting_integration", "integrating"):
        category = "in_progress" if root["phase"] == "integrating" else "awaiting_integration"
        stage = "集成验证中" if category == "in_progress" else "已验收，待整合"
        next_step = "对齐最新主线并完成本需求集成回归"
        if category == "in_progress" and observed.get(task_id, {}).get("status") != "running":
            issues.append("集成执行状态待核实")
    elif root["phase"] == "awaiting_merge":
        category, stage, next_step = "awaiting_merge", "集成通过，待合入核对", "按授权串行检查并合入"
    elif root["phase"] == "awaiting_acceptance":
        quality_pending = root["quality_review"] != "passed"
        category = "quality_review" if quality_pending else "awaiting_acceptance"
        stage = "整体质量待核查" if quality_pending else "交付待验收核证"
        next_step = "补齐整体质量和验收证据" if quality_pending else "核实交付入口及证据后组织验收"
    else:
        category, stage, next_step = "discussion", "范围已登记，尚待开工安排", "完成范围确认和实际派工"
    deployment = "部署未授权，需单独安排" if not root["authorization"]["deploy"]["allowed"] else "部署已单独授权，实际状态尚待核对"
    plan = state.get("release_plan")
    if plan and task_id in plan["included_task_ids"]:
        stages = {"tag": "打标", "backup": "备份", "deploy": "部署", "verify": "发布核验"}
        done = plan["completed_steps"]
        deployment = "冻结版本已部署并核验，后续主线变更不随发布" if done == plan["actions"] else \
                     "冻结版本已独立授权，下一步" + stages[plan["actions"][len(done)]]
    return {"title": public_text(state, info.get("title", task_id)), "category": category, "stage": stage,
            "completed_facts": list(dict.fromkeys(facts)) or ["尚无已核实的阶段完成记录"],
            "next_step": public_text(state, info.get("next_step") or next_step), "deployment": deployment,
            "certainty": "last_recorded" if issues else "verified", "issues": issues}


def build_progress(state):
    issues = verification_issues(state)
    audit = state.get("last_reconcile") or {}
    observed = audit.get("workers", {})
    grouped = {}
    for task_id, task in state["tasks"].items():
        info = task.get("progress")
        if not info:
            issues.append("部分历史任务的业务归属尚未核清")
        root_id = info.get("business_task_id", task_id) if info else task_id
        grouped.setdefault(root_id, []).append(task_id)
    rows = []
    for root_id, members in grouped.items():
        if root_id not in state["tasks"]:
            issues.append("存在无效的业务任务关联")
            continue
        row = task_row(state, root_id, members, observed)
        rows.append(row)
        issues.extend(row["issues"])
    verified = not issues
    if not verified:
        for row in rows:
            row["certainty"] = "last_recorded"
    counts = {key: sum(t["category"] == key for t in rows) if verified else None for key in CATEGORIES}
    counts["total"] = len(rows) if not any("业务" in x for x in issues) else None
    result = {"revision": state["revision"], "verified": verified, "counts": counts, "tasks": rows,
              "issues": list(dict.fromkeys(issues)), "observation_source": audit.get("observation"),
              "scope": "fixture_only" if audit.get("mode") == "fixture" else "point_in_time_observation"}
    if verified:
        first = "目前没有需要你验收或决策的事项。"
        first += "当前没有已登记的业务任务。" if not rows else f"当前有 {counts['in_progress']} 项任务正在执行，另有 {counts['queued']} 项排队。"
    else:
        first = "目前没有已核实可递交的验收或决策事项。以下为最后登记阶段，当前执行数量尚待核实。"
    lines = [first]
    for row in rows:
        prefix = "最后登记：" if not verified or row["certainty"] != "verified" else ""
        lines.append(f"{row['title']}：{prefix}{row['stage']}；阶段事实：{'；'.join(row['completed_facts'])}；下一步：{row['next_step']}。")
    if rows:
        if all("未授权" in row["deployment"] for row in rows):
            lines.append("部署：均需单独授权和安排。")
        else:
            lines.append("部署：" + "；".join(row["title"] + "，" + row["deployment"] for row in rows) + "。")
    result["text"] = "\n".join(lines)
    return result


def round_handoff(state, check):
    from coordination import next_item, presentation
    base = {"revision": state["revision"]}
    if state["active_human_item"]:
        return dict(base, kind="focus", progress=None, text=None)
    pending = any(r["status"] in ("buffered", "held") for r in state["receipts"].values())
    pending |= any(r["status"] == "unverified" for r in state.get("unverified_receipts", {}).values())
    audit = state.get("last_reconcile") or {}
    terminal_gap = any(any(word in message for word in ("unregistered final", "terminal receipt/report", "terminal revision", "report newer", "report missing"))
                       for message in audit.get("issues", []))
    if pending or terminal_gap:
        return dict(base, kind="needs_reconcile", progress=None, text="存在尚未核对的新交付，先完成收件和证据核查。")
    candidate = next_item(state)
    if candidate:
        visible = presentation(state, candidate)
        visible = {k: v for k, v in visible.items() if k in ("title", "prompt", "options", "entry", "steps", "expected",
                                                          "request_text", "recipient", "required_materials")}
        return dict(base, kind="human_item", item=visible)
    progress = build_progress(state)
    return dict(base, kind="progress", progress=progress, text=progress["text"])
