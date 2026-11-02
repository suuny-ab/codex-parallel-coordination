"""STATE/producer regressions with an explicit short owned fixture root on Windows."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'skills/isolated-parallel-development-coordination/scripts'
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'tests'),('\\\\?\\' if os.name=='nt' else '')+str(SCRIPTS),str(ROOT/'src')]
from evidence import io_path, ref, snapshot
from heavy import lease


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--temp-root',type=Path,required=True)
    parser.add_argument('--batch',required=True)
    parser.add_argument('modules',nargs='+')
    args=parser.parse_args()
    if not args.output.is_absolute() or not args.temp_root.is_absolute() or args.temp_root.exists():
        raise ValueError('absolute evidence and a new owned fixture root required')
    args.output.mkdir(parents=True,exist_ok=True);args.temp_root.mkdir(parents=True)
    tempfile.tempdir=str(args.temp_root)
    os.environ.update(TEMP=str(args.temp_root),TMP=str(args.temp_root),PYTHONDONTWRITEBYTECODE='1',
        PYTHONPATH=sys.path[2]+os.pathsep+str(ROOT/'src'))
    if os.name=='nt':
        class LocalPython(subprocess.Popen):
            def __init__(self,command,*a,**kw):
                if isinstance(command,(list,tuple)) and command and str(command[0])==sys.executable:
                    command=[str(io_path(x)) if isinstance(x,str) and x.endswith('.py') and
                        Path(x).is_absolute() and Path(x).is_relative_to(ROOT) else x for x in command]
                super().__init__(command,*a,**kw)
        subprocess.Popen=LocalPython
    before=snapshot(ROOT);started=time.monotonic();prefix=args.output/args.batch
    with prefix.with_suffix('.log').open('x',encoding='utf-8') as log,lease():
        suite=unittest.defaultTestLoader.loadTestsFromNames(args.modules)
        if os.name=='nt' and 'test_public_docs' in sys.modules:
            sys.modules['test_public_docs'].ROOT=Path('\\\\?\\'+str(ROOT))
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    after=snapshot(ROOT)
    report=dict(command=sys.orig_argv,workdir=str(ROOT),fixture_root=str(args.temp_root),source=before,
        source_after=after['digest'],source_unchanged=before==after,seconds=time.monotonic()-started,
        tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        inputs=[ref(os.environ['STATE_TOOL_LAUNCH_CAPTURE'])] if os.environ.get('STATE_TOOL_LAUNCH_CAPTURE') else [],
        skips=[dict(test=t.id(),reason=r) for t,r in result.skipped],
        scope='Disposable local Git/files/ledger and synthetic native/CI/display/merge only; no live task/resource writes.',
        status='passed' if result.wasSuccessful() and before==after else 'failed')
    prefix.with_suffix('.result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source'},ensure_ascii=False))
    if not result.wasSuccessful():print(prefix.with_suffix('.log').read_text(encoding='utf-8'))
    return report['status']!='passed'


if __name__=='__main__':sys.exit(main())
