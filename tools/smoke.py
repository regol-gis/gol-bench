"""Check each selected adapter's complete feature-ID and work fingerprints on a tiny GOL."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from paths import ROOT, WORKER

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--engines', default='rust-mmap', help='comma-separated persistent adapters')
p.add_argument('--rust', type=Path, default=WORKER)
p.add_argument('--python', type=Path)
p.add_argument('--output', type=Path, required=True, help='new directory')
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
raw = a.output/'fixture.jsonl'
subprocess.run([sys.executable, ROOT/'tools/benchmark_stacks.py', ROOT/'tests/fixtures/query_semantics.gol',
                '--spec',ROOT/'benchmarks/fixture.json','--preserve-order','--output',raw,
                '--affinity','one','--samples','1','--target-ms','0','--engines',a.engines,
                '--rust-worker',a.rust,*(['--python-worker',a.python] if a.python else [])], check=True)
gold = json.loads((ROOT/'benchmarks/fixture-signatures.json').read_text())
rows = [json.loads(line) for line in raw.read_text().splitlines()]
for engine in a.engines.split(','):
    seen = [r for r in rows if r['stack']==engine]
    if [r['name'] for r in seen] != list(gold):
        raise SystemExit(f'{engine}: incomplete fixture')
    for row in seen:
        want = gold[row['name']]
        row.setdefault('work_items',row.get('items'))
        if row.get('error') or any(row.get(k) != v for k,v in want.items()):
            raise SystemExit(f"{engine}/{row['name']}: fixture fingerprint mismatch; see {raw}")
print(f'{len(rows)} verified results in {raw}')
