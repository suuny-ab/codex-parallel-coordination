"""Finite isolated resource tests; all artifacts and temp processes belong here."""
import io
import json
import os
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
import uuid
from batch_mutex import BatchMutex, NAME

ROOT = Path(__file__).resolve().parents[1]
def main():
    with BatchMutex() as mutex:
        started = datetime.now(timezone.utc).isoformat()
        run = ROOT / 'evidence' / ('r-' + uuid.uuid4().hex[:8])
        run.mkdir(parents=True)
        shutil.copytree(ROOT / 'component', run / 'source')
        (run / 'source-manifest.json').write_text(json.dumps([
            {'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((run / 'source').glob('*.py'))
        ], indent=2), encoding='utf-8')
        os.environ['COORDINATION_RESOURCE_TEST_EVIDENCE'] = str(run / 'fx')
        sys.path.insert(0, str(ROOT / 'tests'))
        log = io.StringIO()
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (run / 'run.log').write_text(log.getvalue(), encoding='utf-8')
    summary = {'command': 'python -X utf8 -B tools/run_tests.py', 'tests_run': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors), 'passed': result.wasSuccessful(),
               'run_root': str(run), 'started_utc': started, 'finished_utc': datetime.now(timezone.utc).isoformat(),
               'batch_mutex': NAME, 'mutex_released': mutex.released,
               'scope': 'New owned temporary Windows jobs and explicit fixtures only; no native agents or real tasks'}
    (run / 'results.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    if not result.wasSuccessful():
        print(log.getvalue()[-18000:])
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
