"""Owned scope/recovery regression batch; keeps original failures and sources."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'skills/isolated-parallel-development-coordination/scripts'
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'tests'),
    ('\\\\?\\' if os.name == 'nt' else '')+str(SCRIPTS), str(ROOT/'src')]
from evidence import snapshot
from heavy import lease


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batch', required=True)
    parser.add_argument('modules', nargs='+')
    args = parser.parse_args()
    assert args.output.is_absolute()
    args.output.mkdir(parents=True, exist_ok=True)
    prefix = args.output/args.batch
    temporary = args.output/'tests'
    temporary.mkdir(exist_ok=True)
    tempfile.tempdir = str(temporary)
    os.environ.update(TEMP=str(temporary), TMP=str(temporary), PYTHONDONTWRITEBYTECODE='1',
        PYTHONPATH=sys.path[2]+os.pathsep+str(ROOT/'src'))
    os.environ['COORDINATION_TIMEOUT_EVIDENCE'] = str(args.output/(args.batch+'-cases'))
    if os.name == 'nt':
        class LocalPython(subprocess.Popen):
            def __init__(self, command, *a, **kw):
                if isinstance(command, (list, tuple)) and command and str(command[0]) == sys.executable:
                    from evidence import io_path
                    command = [str(io_path(x)) if isinstance(x, str) and x.endswith('.py') and
                        Path(x).is_absolute() and Path(x).is_relative_to(ROOT) else x for x in command]
                super().__init__(command, *a, **kw)
        subprocess.Popen = LocalPython
    before = snapshot(str(ROOT))
    started = time.time()
    with prefix.with_suffix('.log').open('x', encoding='utf-8') as log, lease():
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
        if os.name == 'nt' and 'test_public_docs' in sys.modules:
            sys.modules['test_public_docs'].ROOT = Path('\\\\?\\'+str(ROOT))
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    after = snapshot(str(ROOT))
    report = dict(command=sys.orig_argv, workdir=str(ROOT), started_unix=started,
        elapsed_seconds=time.time()-started, tests=result.testsRun, failures=len(result.failures),
        errors=len(result.errors), skips=[dict(test=str(t), reason=r) for t,r in result.skipped],
        source=before, source_after=after['digest'], source_unchanged=before == after,
        boundary='Owned Git/files and synthetic native replies only; no live writes or native resources released.',
        status='passed' if result.wasSuccessful() and before == after else 'failed')
    prefix.with_suffix('.result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(prefix.with_suffix('.log').read_text(encoding='utf-8'))
    return not (result.wasSuccessful() and before == after)


if __name__ == '__main__':
    sys.exit(main())
