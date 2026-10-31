"""Two finite CLI delivery-role fixtures; never creates agents or performs Git/release."""
import argparse
from pathlib import Path

from delivery import CONTROL
from evidence import load, require
from progress_demo import Demo


class DelegatedDemo(Demo):
    def cli(self, *args, expected=0):
        state = load(self.state) if self.state.exists() else {}
        if state.get("delivery"):
            actor = "fixture-main" if args[0] in ("next", "wait", "close", "priority") else "fixture-delivery"
            args = ("--actor", actor, *args)
        return super().cli(*args, expected=expected)

    def op(self, **op):
        if load(self.state).get("delivery") or op["op"] == "delegate":
            op["actor_id"] = "fixture-main" if op["op"] in CONTROL else "fixture-delivery"
            if op["op"] == "receive":
                op["receipt"]["recipient_agent_id"] = "fixture-delivery"
        return super().op(**op)

    def save(self, name, value):
        if name == "closeout-input.json":
            value["recipient_agent_id"] = "fixture-delivery"
        return super().save(name, value)

    def delegate(self):
        tasks = load(self.state)["tasks"]
        routes, authors = {}, {}
        for key, task in tasks.items():
            owner = task["owner_agent_id"]
            route = self.save("routes/" + key + ".json", {"fixture": True, "authorized_existing_author": owner})
            routes[owner] = {"target_id": "existing-fixture-" + key, "existing_work_agent": True, "evidence": route}
            outbound = self.save("routes/" + key + "-send.json", {"mode": "fixture", "sender_id": "fixture-delivery",
                "recipient_id": owner, "target_id": routes[owner]["target_id"], "status": "accepted", "correlation_id": key + "-handoff"})
            inbound = self.save("routes/" + key + "-ack.json", {"mode": "fixture", "sender_id": owner,
                "recipient_id": "fixture-delivery", "status": "acknowledged", "correlation_id": key + "-handoff"})
            authors[owner] = {"outbound": outbound, "inbound": inbound}
        authority = self.save("authority.json", {"fixture": True, "same_ledger": True, "deployment_authorized": False})
        self.op(op="delegate", agent_id="fixture-delivery", task_ids=list(tasks), merge_order=list(tasks), routes=routes,
                required_ci_jobs=["tests", "build"], authority_evidence=authority, user_reference="fixture explicit sole delivery delegation; no Git rights granted")
        self.op(op="delivery-transport", transport={"mode": "fixture", "send_tool": "fixture-existing-message",
                "receive_tool": "fixture-existing-read", "creates_conversation": False, "authors": authors})


def demo(output, scenario):
    run = DelegatedDemo(output, "warehouse-delivery" if scenario == "warehouse" else "portal-delivery")
    names = [("RECEIVING", "收货导入"), ("LABEL", "批次标签")] if scenario == "warehouse" else [
            ("ACCOUNTS", "账号目录"), ("AUDIT", "审计字段"), ("UPSTREAM", "上游资料")]
    for key, title in names:
        run.task(key, title)
    run.task("RESOURCE_QUEUE", "资源等待项", queued=True)
    run.delegate()
    if scenario == "warehouse":
        run.complete("RECEIVING")  # Actual finite source test, delivery intake, original-main user acceptance.
    else:
        run.block("AUDIT")  # Technical assignment, no root confirmation.
        run.block("UPSTREAM", external=True)  # One legitimate human material request, then wait external.
    result = run.closeout()
    require(result["technical_owner"] == "fixture-delivery" and result["routine_root_confirmation_required"] is False and
            result["notify_main"] is None, "routine stages incorrectly relayed to root")
    return {"project": run.project, "output": str(run.root), "scope": "offline CLI/finite process fixture only", "counts": result["user_handoff"]["progress"]["counts"]}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=("warehouse", "portal"), required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.output, args.scenario)))
