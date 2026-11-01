"""Same frozen real ledger, old/new scoped save timings; no host or live writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'skills/isolated-parallel-development-coordination/scripts'
sys.path[:0] = [str(ROOT/'tools'), ('\\\\?\\' if os.name == 'nt' else '')+str(SCRIPTS), str(ROOT/'src')]
import evidence
from core_runtime import Runtime
from codex_parallel_coordination.dispatch import StandardDispatch
from heavy import lease


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    assert args.output.is_absolute()
    args.output.mkdir(parents=True)
    objects = args.output/'objects'
    objects.mkdir()
    raw = args.input.read_bytes()
    (args.output/'INPUT_CAPTURE.json').write_bytes(raw)
    state = json.loads(raw)
    baseline = subprocess.run(['git', '-c', 'core.longpaths=true', 'show',
        args.baseline+':src/codex_parallel_coordination/dispatch.py'], cwd=ROOT, check=True, capture_output=True).stdout
    (args.output/'BASELINE_DISPATCH.py').write_bytes(baseline)
    namespace = dict(__name__='baseline_dispatch')
    exec(compile(baseline, 'baseline_dispatch.py', 'exec'), namespace)
    original_artifact = evidence.original_artifact
    captured = {}
    def frozen(reference):
        key = (reference['path'], reference['sha256'])
        if key not in captured:
            try:
                data = original_artifact(reference).read_bytes()
                saved = objects/reference['sha256']
                if not saved.exists(): saved.write_bytes(data)
                captured[key] = dict(original=reference, preserved=evidence.ref(saved), status='captured')
            except evidence.ERRORS as error:
                captured[key] = dict(original=reference, status='unavailable', error=type(error).__name__)
        row = captured[key]
        if row['status'] != 'captured': raise ValueError('frozen artifact unavailable')
        return original_artifact(row['preserved'])
    rt = Runtime.__new__(Runtime)
    rt.project_root = Path(state['project']['root'])
    rt.project_id = state['project']['id']
    rt.directory = args.output/'isolated-runtime'
    rt.directory.mkdir()
    rt.path = rt.directory/'ledger.json'
    host = SimpleNamespace(actor_id=state['coordinator_id'])
    dispatch = StandardDispatch(rt, None, None, host, host, None, 'fixture-unused')
    measurements = []
    with lease(), patch.object(evidence, 'original_artifact', frozen):
        for label, record in [('old_unscoped_save', namespace['StandardDispatch']._record),
                              ('new_task_scoped_save', StandardDispatch._record)]:
            rt.path.write_bytes(raw)
            started = time.perf_counter()
            record(dispatch, args.request, reason='Isolated same-state metadata timing')
            measurements.append(dict(label=label, elapsed_seconds=time.perf_counter()-started))
            saved = rt._load()
            if label.startswith('new'):
                key = state['runtime']['operations'][args.request]['task_id']
                assert all(saved['tasks'][k] == v for k,v in state['tasks'].items() if k != key)
                before = state['runtime'].get('stage_feedback', {})
                after = saved['runtime'].get('stage_feedback', {})
                assert all(after['events'][k] == v for k,v in before.get('events', {}).items() if v['task_id'] != key)
    manifest = dict(entries=list(captured.values()), input=evidence.ref(args.output/'INPUT_CAPTURE.json'),
        boundary='Complete captured ledger bytes plus exact evidence read by the comparison; historical embedded pointers are not dependencies.')
    evidence.write_atomic(args.output/'MANIFEST.json', manifest)
    result = dict(input_sha256=hashlib.sha256(raw).hexdigest(), revision=state['revision'], task_count=len(state['tasks']),
        baseline=args.baseline, measurements=measurements, host_calls=0,
        boundary='Actual Runtime._save plus StandardDispatch._record component walltime, not complete dispatch or native acceptance.')
    evidence.write_atomic(args.output/'RESULT.json', result)
    print(json.dumps(result))


if __name__ == '__main__': main()
