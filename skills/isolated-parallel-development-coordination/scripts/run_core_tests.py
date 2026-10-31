"""Portable current-source regression; default never creates Git commits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


def flatten(suite):
    for entry in suite:
        if isinstance(entry,unittest.TestSuite):yield from flatten(entry)
        else:yield entry


def source(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--test-root',type=Path,required=True)
    p.add_argument('--evidence-root',type=Path,required=True)
    p.add_argument('--allow-disposable-git',action='store_true')
    args=p.parse_args()
    test=args.test_root.resolve();out=args.evidence_root.resolve()
    if test.exists() or out.exists():raise ValueError('new isolated test/evidence paths required')
    test.mkdir(parents=True);out.mkdir(parents=True)
    os.environ.update(TEMP=str(test),TMP=str(test),PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    tempfile.tempdir=str(test)
    root=Path(__file__).resolve().parents[1];scripts=root/'scripts'
    before=source(root)
    tests=list(flatten(unittest.defaultTestLoader.discover(str(scripts),pattern='test_*.py')))
    excluded=[t for t in tests if t.__class__.__module__=='test_git_gate' and not args.allow_disposable_git]
    selected=[t for t in tests if t not in excluded]
    start=time.monotonic()
    with (out/'unittest.log').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.TestSuite(selected))
    after=source(root)
    report=dict(status='passed' if result.wasSuccessful() and before==after else 'failed',
        discovered=len(tests),run=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        failures=[dict(test=t.id(),trace=msg) for t,msg in result.failures],errors=[dict(test=t.id(),trace=msg) for t,msg in result.errors],
        skipped=[dict(test=t.id(),reason=msg) for t,msg in result.skipped],
        not_run=[dict(test=t.id(),reason='No Git commit authorization in this run') for t in excluded],
        source_unchanged=before==after,seconds=time.monotonic()-start,source=before,
        command=[sys.executable,'-X','utf8','-B',str(Path(__file__).resolve()),*sys.argv[1:]],
        scope='Current portable component only; no real native spawn/close, customer ledger, remote Git/CI or global installation.')
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source','command','failures','errors')},ensure_ascii=False))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':sys.exit(main())
