"""Verify and compare an experimental reader with demand-only and ordinary hinted controls."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from benchmark_application_sessions import ROOT, SPEC as APPLICATION_SPEC, file_digest
from campaign import write_input_identities

SPEC = ROOT / 'benchmarks/uk-corpus-queries.json'


def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    selective = args.policy != 'tile-handoff'
    marker = {'leaf-window':b'GOL_BENCH_WINDOW_TRACE', 'root-gate':b'GOL_BENCH_GATE_TRACE',
              'tile-handoff':b'GOL_BENCH_TILE_FETCH'}[args.policy]
    if marker not in args.rust.read_bytes():
        raise ValueError(f'expected the experimental {args.policy} worker')
    policy_keys = {'GOL_BENCH_PREFETCH_CONTROL_V3', 'GOL_BENCH_TILE_FETCH',
                   'GOL_BENCH_HANDOFF_TRACE', 'GOL_BENCH_FIRST_MATCH', 'GOL_BENCH_WINDOW_TRACE', 'GOL_BENCH_GATE_TRACE'}
    environment = {k: v for k, v in os.environ.items() if k not in policy_keys}

    def run(tool, *items):
        subprocess.run([sys.executable, ROOT/'tools'/tool, *items], cwd=ROOT, check=True, env=environment)
    with tempfile.TemporaryDirectory() as temp:
        wrapper = Path(temp)/'handoff_corpus'
        def fixed_policy(name, mode):
            name.write_text('#!/bin/sh\nexport GOL_BENCH_PREFETCH_CONTROL_V3=' + mode +
                            '\nexec "$(dirname "$0")/query_benchmark_mmap" "$@"\n')
            name.chmod(0o755)
        demand = wrapper
        candidate_width = 32 if selective else 1
        if selective:
            demand = Path(temp)/'demand'
            fixed_policy(demand, 'neither')
            fixed_policy(wrapper, args.policy)
        else:
            shutil.copyfile(ROOT/'tools/controls/handoff_corpus.sh', wrapper)
        wrapper.chmod(0o755)
        (wrapper.parent/'query_benchmark_mmap').symlink_to(args.rust)
        raw = args.output/'timings.jsonl.gz'
        native = json.loads((ROOT/'benchmarks/uk-20261115-native-lfs.json').read_bytes())['default']
        lock = Path(temp)/'inputs.json'
        lock.write_text(json.dumps({
            'geodesk': {'bytes': APPLICATION_SPEC['input_bytes'], 'sha256': APPLICATION_SPEC['input_sha256']},
            'regol': {'bytes': native['bytes'], 'sha256': native['sha256']},
        }))
        run('benchmark_producer_corpus.py', '--spec', SPEC, '--input-lock', lock,
            '--cores', '1', '--rounds', str(args.rounds),
            '--file-cache', 'evict', '--engines', 'regol', 'pooled', 'baseline', '--regol', wrapper,
            '--rust-workers', str(candidate_width), '--pooled-worker', demand, '--pool-workers', '32', '--baseline', args.rust, '--baseline-workers', '32',
            '--library', f'geodesk={args.geodesk}', '--library', f'regol={args.native}', '--output', raw)
        ids = write_input_identities(raw, args.output)
        checks = []
        for label, gol in [('geodesk',args.geodesk), ('regol',args.native)]:
            for arm, worker, width in [('regol',wrapper,candidate_width), ('pooled',demand,32), ('baseline',args.rust,32)]:
                name = label if arm=='regol' else f'{label}-{arm}'
                key = label if arm=='regol' else f'{label}/{arm}'
                out = args.output/f'{name}.jsonl'
                engines = 'rust-mmap,python-current' if arm=='regol' and label=='geodesk' else 'rust-mmap'
                run('benchmark_stacks.py', gol, '--affinity', 'one', '--preserve-order', '--verified-input',
                    ids[label], '--spec', SPEC, '--engines', engines, '--rust-worker', worker,
                    '--python-worker', args.python, '--rust-workers', str(width),
                    '--samples', '1', '--target-ms', '0', '--output', out)
                checks.extend(['--checks', f'{key}={out}'])
        report = args.output/'summary.json'
        run('summarize_producer_corpus.py', raw, *checks, '--same-cases-as',
            ROOT/'benchmarks/uk-matched-cases.json', '--output', report)
        if any(json.loads(report.read_bytes())['alternate_reader_disagreements'].values()):
            raise ValueError('an experimental reader result differed from a control')
        (args.output/'arms.json').write_text(json.dumps({
            'regol':(f'32 workers, {args.policy}' if selective else 'one evaluator, 32 fetchers to first leaf, no explicit hints'),
            'demand_wrapper_sha256':file_digest(demand),
            'pooled':'32 demand workers, no explicit hints', 'baseline':'32 demand workers with ordinary hints',
            'binary_sha256':file_digest(args.rust), 'wrapper_sha256':file_digest(wrapper),
        }, indent=2)+'\n')


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['geodesk','native','rust','python','output']:
        p.add_argument('--'+name, required=True, type=Path)
    p.add_argument('--policy', choices=('tile-handoff','leaf-window','root-gate'), default='tile-handoff')
    p.add_argument('--rounds', type=int, default=6)
    args=p.parse_args()
    for name in ['geodesk','native','rust','output']:
        setattr(args,name,getattr(args,name).resolve())
    args.python=args.python.absolute()  # Preserve the venv selected by the link.
    main(args)
