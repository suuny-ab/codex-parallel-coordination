"""Task-owned bounded verification batches, with the existing heavy mutex."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tests'),
               str(ROOT / 'skills/isolated-parallel-development-coordination/scripts')]
if os.name == 'nt':
    sys.path[2] = '\\\\?\\' + sys.path[2]
from evidence import snapshot
from heavy import lease


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batch', required=True)
    parser.add_argument('--baseline', help='Replay only selected original functions from this Git commit')
    parser.add_argument('modules', nargs='+')
    args = parser.parse_args()
    assert args.output.is_absolute()
    args.output.mkdir(parents=True, exist_ok=True)
    temporary = args.output / 'tests'
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    os.environ.update(TEMP=str(temporary), TMP=str(temporary))
    scope = args.output / 'TEST_SCOPE_R1.json'
    if not scope.exists():
        scope.write_text(json.dumps(dict(task_id='DISPATCH-TIMEOUT', test_roots=[str(temporary)],
            author=os.environ.get('CODEX_THREAD_ID', 'standalone-fixture-runner'),
            boundary='Only owned fixture files/processes. No native spawn or live ledger writes.'), indent=2), encoding='utf-8')
    prefix = args.output / args.batch
    os.environ['COORDINATION_TIMEOUT_EVIDENCE'] = str(prefix.parent / (args.batch + '-cases'))
    if os.name == 'nt':
        os.environ['PYTHONPATH'] = sys.path[2] + os.pathsep + os.environ.get('PYTHONPATH', '')
        class LocalPython(subprocess.Popen):
            def __init__(self, command, *a, **kw):
                if isinstance(command, (list, tuple)) and command and str(command[0]) == sys.executable:
                    from evidence import io_path
                    command = [str(io_path(x)) if isinstance(x, str) and x.endswith('.py') and
                        Path(x).is_absolute() and Path(x).is_relative_to(ROOT) else x for x in command]
                super().__init__(command, *a, **kw)
        subprocess.Popen = LocalPython
    source = snapshot(str(ROOT))
    prefix.with_suffix('.source.json').write_text(json.dumps(source, indent=2), encoding='utf-8')
    baseline = []
    if args.baseline:
        import test_dispatch_timeout
        for relative, target in (
            ('skills/isolated-parallel-development-coordination/scripts/coordination.py', 'writer_lock'),
            ('src/codex_parallel_coordination/dispatch.py', 'dispatch')):
            data = subprocess.run(['git', '-c', 'core.longpaths=true', 'show', args.baseline + ':' + relative],
                cwd=ROOT, check=True, capture_output=True, timeout=20).stdout
            saved = prefix.parent / (args.batch + '-' + Path(relative).name)
            saved.write_bytes(data)
            from evidence import ref
            baseline.append(ref(saved))
            namespace = dict(__name__='baseline_' + target)
            exec(compile(data, str(saved), 'exec'), namespace)
            if target == 'writer_lock':
                test_dispatch_timeout.writer_lock = namespace['writer_lock']
            else:
                test_dispatch_timeout.test_dispatch_host.test_dispatch.StandardDispatch._run = namespace['StandardDispatch']._run
    with prefix.with_suffix('.log').open('x', encoding='utf-8') as log, lease():
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
        if os.name == 'nt' and 'test_public_docs' in sys.modules:
            sys.modules['test_public_docs'].ROOT = Path('\\\\?\\' + str(ROOT))
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    after = snapshot(str(ROOT))
    prefix.with_suffix('.result.json').write_text(json.dumps(dict(
        command=sys.orig_argv, tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        environment=dict(test_root=str(temporary), pythonpath=os.environ.get('PYTHONPATH'),
            windows_local_python_scripts='Extended-path aliases for this source root only; no product behavior patches'),
        skips=[dict(test=str(t), reason=r) for t, r in result.skipped],
        baseline_commit=args.baseline, baseline_sources=baseline,
        source_digest=source['digest'], source_after=after['digest'], source_unchanged=source == after,
        status='passed' if result.wasSuccessful() and source == after else 'failed'), indent=2), encoding='utf-8')
    print(prefix.with_suffix('.log').read_text(encoding='utf-8'))
    sys.exit(not (result.wasSuccessful() and source == after))
