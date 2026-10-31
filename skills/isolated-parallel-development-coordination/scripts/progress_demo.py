#!/usr/bin/env python3
"""Two finite, isolated reusable progress exercises; all native observations are fixtures."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from evidence import ERRORS, load, pack, ref, require, write_atomic


class Demo:
    def __init__(self, output, project):
        self.root = Path(output).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.project = project
        self.state = self.root / "state.json"
        self.commands = []
        self.serial = 0
        self.snapshots = {}
        config = self.save("project-config.json", {"worker_capacity": 3})
        self.cli("init", "--project", project, "--coordinator", "fixture-main", "--project-config", config["path"])

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, value)
        return ref(path)

    def cli(self, *args, expected=0):
        command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).with_name("coordination.py")),
                   "--state", str(self.state), *map(str, args)]
        result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=20)
        self.commands.append({"command": command, "exit_code": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr})
        self.save("commands.json", self.commands)
        require(result.returncode == expected, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def op(self, **op):
        self.serial += 1
        p = self.save(f"operations/{self.serial:03}.json", op)
        return self.cli("apply", p["path"])

    def task(self, task_id, title, queued=False):
        work = self.root / "sources" / task_id
        work.mkdir(parents=True)
        (work / "feature.py").write_text("def normalize(value):\n    return value.strip().lower()\n", encoding="utf-8")
        data = dict(task_id=task_id, owner_agent_id="fixture-worker-" + task_id,
            worktree=str(work), branch="fixture/" + task_id, title=title,
            report_path=str(self.root / "reports" / (task_id + "-1.json")))
        if queued:
            self.op(op="register", **data)
            self.op(op="confirm", task_id=task_id, scope=title, acceptance=["normalization preserves empty input"], user_reference="fixture confirmed scope")
            self.op(op="queue", task_id=task_id, reason="explicit fixture resource queue", user_reference="fixture queue instruction")
        else:
            self.op(op="dispatch", **data, scope=title, acceptance=["normalization preserves empty input"], user_reference="fixture authorized isolated work")
        return work

    def complete(self, task_id):
        task = load(self.state)["tasks"][task_id]
        code = "from feature import normalize; assert normalize(' A ')== 'a'; assert normalize('')==''; print('2 fixture assertions passed')"
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=task["worktree"], capture_output=True, encoding="utf-8", timeout=20)
        require(result.returncode == 0, result.stderr)
        log = self.root / "reports" / (task_id + ".log")
        log.parent.mkdir(exist_ok=True)
        log.write_text(result.stdout, encoding="utf-8")
        report = pack({"project_id": self.project, "task_id": task_id, "delivery_revision": 1,
            "summary": task["progress"]["title"] + "交付", "source_root": task["worktree"], "evidence": [str(log)],
            "self_test": {"status": "passed", "command": [{"command": [sys.executable, "-B", "-c", code], "exit_code": 0}]},
            "quality_review": {"status": "passed"}, "entry": {"kind": "file", "target": str(log)},
            "steps": ["查看规范化结果"], "expected": ["空值保持为空", "大小写符合已确认格式"]})
        report_ref = self.save("reports/" + task_id + "-1.json", report)
        event = {"project_id": self.project, "task_id": task_id, "owner_agent_id": task["owner_agent_id"], "channel": "native_agent",
                 "event_id": task_id + ":1:completed", "kind": "completed", "delivery_revision": 1, "report": report_ref}
        self.op(op="receive", receipt=event)
        self.op(op="drain")
        self.cli("next")
        self.cli("close", event["event_id"], "--outcome", "accepted", "--user-reference", "fixture user accepts confirmed task scope")
        self.snapshots[task_id] = ("completed", 1)

    def block(self, task_id, external=False):
        task = load(self.state)["tasks"][task_id]
        report = {"project_id": self.project, "task_id": task_id, "delivery_revision": 1, "status": "held",
                  "self_test": {"status": "passed_for_independent_scope_only"},
                  "blocker": {"category": "dependency" if external else "technical", "owner": "upstream",
                              "reason": "fixture external material needed" if external else "fixture technical comparison pending"}}
        r = self.save("reports/" + task_id + "-1.json", report)
        event = {"project_id": self.project, "task_id": task_id, "owner_agent_id": task["owner_agent_id"], "channel": "native_agent",
                 "event_id": task_id + ":1:blocked", "kind": "blocked", "delivery_revision": 1, "report": r}
        proof = self.save("proofs/" + task_id + "-technical.json", {"fixture": True, "population_and_parser_compared": True})
        if external:
            event.update(assistance={"title": "取得外部接口说明", "prompt": "请协调提供一份接口资料包", "recipient": "上游接口负责人",
                "request_text": "请提供字段来源及取值说明", "required_materials": ["字段来源", "取值说明"]},
                technical_review={"conclusion": "external_materials_required", "summary": "fixture technical comparison complete",
                                  "report_sha256": r["sha256"], "evidence": [proof]})
        self.op(op="receive", receipt=event)
        self.op(op="drain")
        if external:
            self.cli("next")
            sent = self.save("proofs/" + task_id + "-request.json", {"fixture": True, "user_confirms_forwarded": True})
            self.cli("close", event["event_id"] + ":materials", "--outcome", "requested", "--user-reference", "fixture request sent",
                     "--request-evidence", sent["path"])
        else:
            self.op(op="route-blocker", task_id=task_id, delivery_revision=1, owner=task["owner_agent_id"],
                    action="Compare fixture parser and source definitions", evidence=proof)
        self.snapshots[task_id] = ("blocked", 1)

    def closeout(self):
        state = load(self.state)
        workers = []
        for task_id, task in state["tasks"].items():
            if task.get("worker_run", {}).get("status") == "queued":
                continue
            status, revision = self.snapshots.get(task_id, ("running", 0))
            proof = self.save("observations/" + task_id + ".json", {"fixture": True, "status": status, "delivery_revision": revision})
            workers.append({"task_id": task_id, "owner_agent_id": task["owner_agent_id"], "status": status,
                            "last_delivery_revision": revision, "evidence": proof})
        inbox = self.save("observations/inbox.json", {"fixture": True, "visible_messages_reconciled": True})
        observation = self.save("closeout-input.json", {"schema": "coordination-closeout/1", "project_id": self.project,
            "coordinator_id": "fixture-main", "based_on_revision": state["revision"],
            "inbox": {"mode": "fixture", "complete": True, "evidence": inbox, "messages": []}, "workers": workers})
        result = self.cli("closeout", observation["path"])
        require(result["user_handoff"]["kind"] == "progress", "expected no-human-item progress branch")
        require(result["user_handoff"]["progress"]["verified"], "fixture progress did not verify")
        self.save("result.json", result)
        (self.root / "USER_PROGRESS.txt").write_text(result["user_handoff"]["text"] + "\n", encoding="utf-8")
        return result


def demo(output, scenario):
    demo = Demo(output, "warehouse-operations" if scenario == "warehouse" else "service-portal")
    if scenario == "warehouse":
        for task_id, title in (("STOCK_IMPORT", "库存导入"), ("BATCH_LABELS", "批次标签"), ("LOAD_POLICY", "负载规则")):
            demo.task(task_id, title)
        demo.task("QUALITY_OPTIONS", "候选质量改进", queued=True)
        demo.op(op="describe", task_id="QUALITY_OPTIONS", reference="fixture UI-only acceptance",
            facts=[{"text": "界面流程已接受，整体质量尚未通过", "reference": "fixture explicit limited acceptance"}])
        demo.task("STOCK_REVIEW", "库存导入独立复核", queued=True)
        demo.op(op="describe", task_id="STOCK_REVIEW", business_task_id="STOCK_IMPORT", task_role="review", reference="same fixture requirement")
    else:
        demo.task("ACCOUNT_PORTAL", "账号门户改造")
        demo.task("AUDIT_REPAIR", "审计映射核查")
        demo.block("AUDIT_REPAIR")
        demo.task("UPSTREAM_SCHEMA", "上游接口适配")
        demo.block("UPSTREAM_SCHEMA", external=True)
        demo.task("USER_EXPORT", "用户清单导出")
        demo.complete("USER_EXPORT")
        demo.task("SEARCH_POLICY", "查询策略", queued=True)
    result = demo.closeout()
    counts = result["user_handoff"]["progress"]["counts"]
    require(counts["in_progress"] == (3 if scenario == "warehouse" else 1) and counts["queued"] == 1,
            "cross-project execution and queue counts differ")
    return {"output": str(demo.root), "scenario": scenario, "counts": counts, "scope": "offline-process-and-CLI-fixture-only"}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=("warehouse", "portal"), required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(demo(args.output, args.scenario), ensure_ascii=False))
    except (*ERRORS, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
