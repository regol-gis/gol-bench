"""Cross the boundary and road policies, both sharing and reopening the reader."""
import argparse
import json
import os
from itertools import product
from pathlib import Path
import subprocess
import tempfile

from benchmark_application_sessions import CASES, ROOT, digest, file_digest, reader_batches, signature, verified_inputs
from benchmark_cache import FileCache
from benchmark_producer_corpus import sync_inputs, williams_order
from prepare_prefetch_control import KEY


def measure(args):
    if args.output.exists() or args.rounds < 1:
        raise ValueError('use a fresh output and positive rounds')
    if b'stage-crossover' not in args.rust.read_bytes():
        raise ValueError('expected the root-gate diagnostic binary')
    identity = verified_inputs(args.file, None, None)['geodesk']
    cpu = min(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    cache = FileCache(args.file)
    sync_inputs({'uk':args.file})
    stages, = reader_batches(CASES['varna'])
    arms = list(product(('absent','neither','default','root-gate'), ('neither','default')))
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        specs = []
        for stage in stages:
            p = Path(tmp)/(stage['name']+'.json')
            p.write_text(json.dumps([stage]))
            specs.append(p)

        def launch(path, policy, indices, verify=False):
            flag = '1' if verify else '0'
            cmd = [args.rust, args.file, path, 'mmap-immutable', flag, '0', '-',
                   flag, 'verify' if verify else 'skip', '32']
            env = {k:v for k,v in os.environ.items() if not k.startswith(('GOL_BENCH_', 'GOL_READER_BENCH_'))}
            env.update({KEY:policy, 'GOL_READER_BENCH_AUTO_INDEX_BYTES':'0'})
            lines = subprocess.check_output(cmd, env=env, timeout=120).splitlines()
            data = []
            for i, line in zip(indices, lines, strict=True):
                result = json.loads(line)
                if (result['name'] != stages[i]['name'] or result['retained_index_bytes']
                        or (verify and result['threads'] != 32)):
                    raise ValueError('unexpected stage or execution plan')
                data.append(dict(name=result['name'], features=result['count'], items=result['work_items'],
                                 elapsed_ns=result['setup_ns']+result['first_execution_ns'], open_ns=result['open_ns'],
                                 fingerprint=digest([result['fingerprint'],result['work_fingerprint']]) if verify else None))
            return data

        def shared(before, after, verify=False):
            path = Path(tmp)/'both.json'
            path.write_text(json.dumps([dict(stage, benchmark_prefetch=policy)
                                        for stage, policy in zip(stages, (before, after), strict=True)]))
            return launch(path, 'stage-crossover', [0,1], verify)

        def reset():
            result = cache.prepare('cold')['resident_bytes']
            if result:
                raise ValueError('expected cold UK file cache')
            return result

        pin = json.loads((ROOT/'benchmarks/application-session-signatures.json').read_bytes())['varna/rust-reader']

        def validate(steps):
            sig = signature([dict(name='open-0', items=1, fingerprint=None),
                             *[{k:v for k,v in stage.items() if k != 'open_ns'} for stage in steps]])
            if digest(sig) != pin:
                raise ValueError('stage results differ from pinned Varna contents')
            return [(r['features'],r['items']) for r in steps]

        for policy in ('neither','default','root-gate'):
            expected = validate([launch(specs[i], policy, [i], True)[0] for i in (0,1)])
        for before, after in arms:
            if before != 'absent':
                validate(shared(before, after, True))
        for n in range(args.rounds):
            for before, after in williams_order(arms,n):
                row = dict(round=n+1, before=before, after=after)
                # Alternate which process-lifetime treatment goes first.
                designs = ['split'] if before == 'absent' else (
                    ['split','intact'] if n % 2 == 0 else ['intact','split'])
                for design in designs:
                    zero = reset()
                    if design == 'intact':
                        boundary, roads = shared(before, after)
                        row['intact'] = dict(boundary=boundary, roads=roads, resident_before=zero)
                    else:
                        boundary = launch(specs[0], before, [0])[0] if before != 'absent' else None
                        between = cache.snapshot()['resident_bytes']
                        roads = launch(specs[1], after, [1])[0]
                        row.update(boundary=boundary, roads=roads, resident_before=zero, resident_between=between)
                    if ((roads['features'],roads['items']) != expected[1] or
                        boundary and (boundary['features'],boundary['items']) != expected[0]):
                        raise ValueError('stage consumption changed during timing')
                rows.append(row)
    args.output.write_text(json.dumps(dict(input=identity, cpu=cpu, rounds=args.rounds, rows=rows,
                                           worker_sha256=file_digest(args.rust)))+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--rust', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--output', type=Path, required=True)
    measure(parser.parse_args())
