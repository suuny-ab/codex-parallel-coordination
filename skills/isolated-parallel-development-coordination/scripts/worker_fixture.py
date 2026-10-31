#!/usr/bin/env python3
"""Finite worker PROCESS fixture, not a native Codex agent/event transport."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from evidence import ref, write_atomic


def run(root, project="warehouse-demo", task="IMPORT", worker="worker-import"):
    root = Path(root).resolve()
    work = root / "worker-source"
    work.mkdir(parents=True, exist_ok=False)
    (work / "normalize.py").write_text("def quantity(row):\n    return int(row['quantity'])\n", encoding="utf-8")
    code = "from normalize import quantity; assert quantity({'quantity':'4'})==4; assert quantity({'quantity':'0'})==0; print('2 actual assertions passed; legacy unit semantics still unresolved')"
    test = subprocess.run([sys.executable, "-B", "-c", code], cwd=work, capture_output=True, encoding="utf-8")
    if test.returncode:
        raise RuntimeError(test.stderr)
    log = root / "worker-test.log"
    log.write_text(test.stdout, encoding="utf-8")
    investigation = root / "technical-review.txt"
    investigation.write_text("Compared six raw rows and both parser paths. Two legacy rows use a unit field with no authoritative conversion definition; values are present. Independent numeric conversion tests passed. Coordinator must evaluate whether more technical comparison is needed before escalating.", encoding="utf-8")
    report = {"project_id": project, "task_id": task, "delivery_revision": 3, "status": "held",
        "summary": "Independent conversion works; legacy unit meaning unresolved",
        "blocker": {"category": "business", "owner": "human", "reason": "Which quantity unit is authoritative?"},
        "self_test": {"status": "passed_for_independent_scope_only", "command": "python -B -c " + code},
        "technical_investigation": {"summary": "Raw values exist; legacy parser assumes a unit", "evidence": [ref(investigation)]}}
    path = root / "worker-report.json"
    write_atomic(path, report)
    r = ref(path)
    event = {"project_id": project, "task_id": task, "owner_agent_id": worker, "channel": "native_agent",
        "kind": "blocked", "delivery_revision": 3, "event_id": task + ":3:blocked", "report": r,
        "technical_review": {"conclusion": "business_decision_required", "summary": "Fixture coordinator review only",
            "report_sha256": r["sha256"], "evidence": [ref(investigation)]},
        "decision": {"title": "Confirm quantity unit", "prompt": "Which unit applies to the two legacy records?",
            "options": ["Use documented standard unit", "Provide legacy conversion definition"]}}
    # The host completed status and report blocked/held status are distinct.
    return {"fixture": True, "process_status": "completed", "receipt": event, "report": r,
            "source_root": str(work), "test_evidence": ref(log)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="warehouse-demo")
    parser.add_argument("--task", default="IMPORT")
    parser.add_argument("--worker", default="worker-import")
    args = parser.parse_args()
    print(json.dumps(run(args.output, args.project, args.task, args.worker), ensure_ascii=False))
