"""Replay frozen resource tests against bound source and explicit additive amendments."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def replay(output):
    if not output.is_absolute() or output.exists():
        raise ValueError("new explicit absolute output directory required")
    source = ROOT / "src/codex_parallel_coordination/resources"
    provenance = json.loads((ROOT / "provenance/resource.json").read_text(encoding="utf-8"))
    prefix = "src/codex_parallel_coordination/resources/"
    expected = {row["path"][len(prefix):]: row["sha256"] for row in provenance["files"] if row["path"].startswith(prefix)}
    for amendment in provenance.get('amendments', []):
        for row in amendment['files']:
            if not row['path'].startswith(prefix):raise ValueError('amendment outside resource source')
            name=row['path'][len(prefix):]
            if expected.get(name)!=row['previous_sha256']:raise ValueError('amendment does not continue frozen source')
            expected[name]=row['sha256']
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob("*.py")}
    if actual != expected:
        raise ValueError("integrated resource source differs from bound freeze")
    output.mkdir(parents=True)
    shutil.copytree(source, output / "component", ignore=shutil.ignore_patterns("__pycache__"))
    tests = output / "tests"
    tests.mkdir()
    for path in (ROOT / "tests/resource").glob("test_*.py"):
        shutil.copy2(path, tests / path.name)
    tools = output / "tools"
    tools.mkdir()
    runner = tools / "run_tests.py"
    shutil.copy2(ROOT / "tests/resource/author_run_tests.py", runner)
    shutil.copy2(ROOT / "tests/resource/author_batch_mutex.py", tools / "batch_mutex.py")
    (output / "integrated-source.json").write_text(json.dumps(actual, indent=2), encoding="utf-8")
    # The frozen R2 runner acquires the shared mutex itself. Do not acquire it
    # in this parent and deadlock its child process.
    return subprocess.run([sys.executable, "-X", "utf8", "-B", str(runner)], cwd=output).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.exit(replay(args.output))
