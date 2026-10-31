#!/usr/bin/env python3
"""One-shot unfamiliar-project process exercise; explicitly not native agent E2E."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from coordination import checkpoint, load, new_state, transition, write_atomic
from evidence import ref, require


def demo(output, prepare_only=False):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).with_name("coordination.py")
    state = new_state("demo-main", "warehouse-demo")
    state, _ = transition(state, {"op": "dispatch", "task_id": "IMPORT", "owner_agent_id": "worker-import",
        "worktree": str(root / "job" / "worker-source"), "branch": "fixture-import",
        "scope": "Normalize quantities within existing unit definition", "acceptance": ["numeric conversion preserves zero"],
        "user_reference": "Fixture user authorizes isolated implementation only", "report_path": str(root / "job" / "worker-report.json")})
    terminal = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("worker_fixture.py")),
        "--output", str(root / "job")], capture_output=True, encoding="utf-8", timeout=20)
    require(terminal.returncode == 0, terminal.stderr)
    stdout_path = root / "process-terminal.json"
    stdout_path.write_text(terminal.stdout, encoding="utf-8")
    packet = json.loads(terminal.stdout)
    path = root / "state.json"
    write_atomic(path, state)
    write_atomic(root / "initial-state.json", state)
    raw_check = checkpoint(state)
    write_atomic(root / "ledger-only-check.json", raw_check)
    worker = {"task_id": "IMPORT", "owner_agent_id": "worker-import", "status": "completed",
              "last_delivery_revision": 3, "evidence": ref(stdout_path)}

    def observation():
        return {"schema": "coordination-closeout/1", "project_id": "warehouse-demo", "coordinator_id": "demo-main",
            "based_on_revision": load(path)["revision"], "inbox": {"mode": "fixture", "complete": True,
                "evidence": ref(stdout_path), "messages": []}, "workers": [worker]}

    write_atomic(root / "unreconciled-observation.json", observation())
    worker_with_receipt = dict(worker, receipt=packet["receipt"])
    prepared = observation()
    prepared["workers"] = [worker_with_receipt]
    write_atomic(root / "received-observation.json", prepared)
    (root / "REVIEW_REQUEST.md").write_text(
        "# Warehouse import coordination exercise\n\nUse the candidate skill with this state and the process terminal/report files. "
        "Treat these as explicitly labeled fixtures. Manage outstanding work and a safe closeout, keeping one human focus. "
        "After presenting the relevant question, apply this user feedback: 'Investigate the whole data population yourself; "
        "all rows should use consistent fields.' Do not infer new business semantics or Git permissions. "
        "Leave a concise evidence-backed status and preserve the original scope. No real projects, agents, services or network calls.\n",
        encoding="utf-8")
    if prepare_only:
        return {"prepared": str(root), "scope": "process-fixture-only", "state": str(path)}
    attempts = []

    def cli(*args, expected=0):
        command = [sys.executable, "-B", str(script), "--state", str(path), *args]
        result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=20)
        attempts.append({"command": command, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
        write_atomic(root / "commands.json", attempts)
        require(result.returncode == expected, result.stderr or result.stdout)
        return json.loads(result.stdout)

    first = cli("closeout", str(root / "unreconciled-observation.json"), expected=2)
    require(first["action"] == "reconcile_required", "missing native receipt was not detected")
    worker["receipt"] = packet["receipt"]
    write_atomic(root / "ready-observation.json", observation())
    result = cli("closeout", str(root / "ready-observation.json"), expected=2)
    require(result["next"]["id"] == "IMPORT:3:blocked", "business item not surfaced")
    cli("next")
    cli("wait", "IMPORT:3:blocked", "--prompt", "Which quantity unit applies?")
    cli("redispatch", "IMPORT:3:blocked", "--next-revision", "4", "--reason", "Compare all rows and both parser paths",
        "--user-reference", "Fixture user: investigate all rows; consistent fields expected")
    final = cli("summary")
    require(final["tasks"]["IMPORT"]["implementation"] == "running" and final["focus"] is None,
            "redispatch did not restore worker responsibility")
    result = {"scope": "process-fixture-only; native host delivery not verified", "output": str(root),
              "ledger_only_before": raw_check, "missing_receipt": first, "reconciled": result, "final": final}
    write_atomic(root / "result.json", result)
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    try:
        result = demo(args.output, args.prepare_only)
        print(json.dumps({"output": result.get("output", result.get("prepared")), "scope": result["scope"]}, ensure_ascii=False))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
