"""Freeze exact captured bytes and bound evidence; never read/write live ledgers."""
import argparse
import hashlib
import json
from pathlib import Path


def freeze(inputs, output):
    output.mkdir(parents=True)
    blobs = output/'objects'
    blobs.mkdir()
    entries, seen = [], set()
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
                capture(value)
            else:
                for item in value.values(): visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
    def capture(reference):
        key = (reference['path'], reference['sha256'])
        if key in seen: return
        seen.add(key)
        row = dict(original=reference)
        entries.append(row)
        try:
            data = Path(reference['path']).read_bytes()
        except OSError as error:
            row.update(status='unavailable', error=type(error).__name__)
            return
        actual = hashlib.sha256(data).hexdigest()
        if actual != reference['sha256']:
            row.update(status='source_changed', actual_sha256=actual)
            return
        saved = blobs/actual
        if not saved.exists(): saved.write_bytes(data)
        row.update(status='captured', preserved=dict(path=str(saved), sha256=actual), size=len(data))
    for path in inputs:
        data = path.read_bytes()
        capture(dict(path=str(path), sha256=hashlib.sha256(data).hexdigest()))
        try: visit(json.loads(data))
        except (UnicodeError, ValueError): pass
    manifest = dict(schema='dispatch-frozen-input/1', inputs=[str(p) for p in inputs], entries=entries,
        scope='Exact inputs and their direct bound artifacts only; never recursively expands historical captures.')
    (output/'MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(captured=sum(e['status']=='captured' for e in entries),
        unavailable=sum(e['status']=='unavailable' for e in entries), changed=sum(e['status']=='source_changed' for e in entries))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('inputs', type=Path, nargs='+')
    args = parser.parse_args()
    assert args.output.is_absolute()
    freeze(args.inputs, args.output)
