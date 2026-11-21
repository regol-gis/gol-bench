"""Render verified benchmark reports as static comparison figures."""
import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
import gzip
from html import escape
import json
import math
from pathlib import Path
from statistics import median
import tempfile
import textwrap


def load(path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as source:
            return json.load(source)
    return json.loads(path.read_bytes())


@dataclass
class Figure:
    title: str
    context: str
    unit: str = "Milliseconds"
    sample_kind: str = "round"
    values: dict = field(default_factory=dict)
    widths_m: dict = field(default_factory=dict)

    def add(self, case, engine, samples):
        numbers = tuple(samples)
        if not numbers or any(isinstance(n, bool) or not isinstance(n, (int, float))
                              or not math.isfinite(n) or n < 0 for n in numbers):
            raise ValueError(f"invalid samples for {case}/{engine}")
        key = (case, engine)
        if key in self.values:
            raise ValueError(f"duplicate series {case}/{engine}")
        self.values[key] = numbers


def timing_context(directory):
    raw = directory / 'timings.jsonl.gz'
    if not raw.is_file():
        return ''
    with gzip.open(raw, 'rt') as source:
        meta = json.loads(next(source))
    cpu = meta.get('cpu_model') or meta.get('machine', {}).get('machine', '')
    desc = f"{cpu} · cache {meta['file_cache']}"
    if meta.get('pooled_rust_workers'):
        desc += f" · Regol pool {meta['pooled_rust_workers']} workers"
    return desc


def scale_figures(path, index):
    run = load(path.with_name('run.json'))
    if run['status'] != 'complete' or set(index) != set(run['datasets']) or set(index) != set(run['completed']):
        raise ValueError('scale campaign is incomplete')
    figures = []
    notes = []
    for width in run['cores']:
        contexts = set()
        fig = Figure(f'Reader histories · {width} CPU' + ('s' if width != 1 else ''), '')
        for dataset in run['datasets']:
            row = index[dataset]
            if row['cache'] != run['cache']:
                raise ValueError(f'{dataset}: inconsistent cache protocol')
            contexts.add(timing_context(path.parent / dataset))
            for engine in run['engines']:
                samples = row['prepare_and_scan'][f'{engine}/{width}']['rounds_ms']
                if len(samples) != run['rounds']:
                    raise ValueError(f'incomplete rounds for {dataset}/{engine}')
                fig.add(f"{dataset} ({row['cases']} steps)", engine, samples)
            # Calibrated stack results use a different clock and cohort.
            audit = path.parent / dataset / f'stacks-{width}.audit.json'
            if audit.exists():
                extras, excluded = audit_figures(load(audit), f'{dataset} · {width} CPUs')
                figures.extend(extras)
                notes.extend(f'{dataset}: {n}' for n in excluded)
        fig.context = 'Preparation + first execution · ' + (' / '.join(sorted(contexts - {''})) or f"cache {run['cache']}")
        figures.insert(run['cores'].index(width), fig)
    return figures, notes


def corpus_figures(path, summary):
    figures, notes = {}, []
    context = timing_context(path.parent)
    for arm, statuses in summary.get('verification', {}).items():
        for status, count in sorted(statuses.items()):
            if status not in ('identities_match', 'content_match'):
                notes.append(f'{arm}: {count} cases with {status}. See the source summary for case names and errors.')
    for operation, row in summary['cohorts'].items():
        count = len(row['cases'])
        for entry in row.get('excluded_arms', []):
            notes.append(f"{operation} · {entry['arm']}: {entry['reason']}.")
        if not count:
            notes.append(f'{operation}: no eligible matched cases.')
            continue
        metric, = [k for k in ('prepare_and_scan_ms', 'open_prepare_and_scan_ms') if k in row]
        for arm, data in row[metric].items():
            producer, reader, cores = arm.rsplit('/', 2)
            key = producer, cores, metric
            interval = 'Open + preparation + scan' if metric.startswith('open_') else 'Preparation + scan'
            fig = figures.setdefault(key, Figure(f'Matched workloads · {producer} GOL · {cores} CPU(s)',
                                                  ' · '.join(filter(None, [interval, context]))))
            rounds = sorted(data['rounds'], key=int)
            fig.add(f'{operation} ({count} cases)', reader, [data['rounds'][r] for r in rounds])
    return list(figures.values()), notes


def cli_figures(data):
    context = f"{data['host']} · {len(data['cpus'])} CPU(s) · fresh process writing a regular file"
    wall = Figure('CLI count and export wall time', context)
    size = Figure('CLI export size', context, 'MiB')
    notes = []
    for group in data['results']:
        label = f"{group['query']} · {group['format']}"
        if not group['verified']:
            notes.append(f"{label}: {group['reason']}.")
            continue
        for engine, arm in group['arms'].items():
            runs = arm['runs']
            if len(runs) != data['samples'] or 'signature' not in arm:
                raise ValueError(f'{label}: missing verified observations')
            wall.add(label, engine, [run['wall_ns']/1e6 for run in runs])
            if group['format'] != 'count':
                size.add(label, engine, [run['bytes']/2**20 for run in runs])
    return [fig for fig in (wall, size) if fig.values], notes


def application_figures(data):
    # Historical reports predate the embedded replay kinds. Their application IDs
    # refer to the same pinned manifest.
    replay = data.get('workflows')
    if replay is None:
        manifest = load(Path(__file__).resolve().parents[1]/'benchmarks/application-sessions.json')
        replay = {case['id']:case['replay'] for case in manifest['cases']}
    figures, notes = {}, []
    for key, row in data['results'].items():
        parts = key.split('/')
        producer, case, engine = parts if len(parts) == 3 else ('geodesk', *parts)
        if not row.get('complete'):
            notes.append(f'{key}: incomplete application verification.')
            continue
        if len(row['runs']) != data['samples'] or not row.get('digest') or not row.get('signature'):
            raise ValueError(f'{key}: missing verified rounds')
        # Reader-only, full application and CLI exports have different boundaries.
        if engine in ('cli', 'rust-cli'):
            kind = 'CLI exports'
        elif engine.endswith('-reader') or replay.get(case) == 'reader':
            kind = 'matched reader'
        else:
            kind = 'application reference'
        fig = figures.setdefault((producer, kind), Figure(f'{kind.capitalize()} histories · {producer} GOL',
                                   f"{data['cpu']} · {len(data['affinity'])} CPU(s) · sum of timed session steps"))
        fig.add(case, engine.removesuffix('-reader'), [sum(step['elapsed_ns'] for step in run) / 1e6 for run in row['runs']])
    return list(figures.values()), notes


def writer_figures(data):
    metrics = [('wall_ns', 'Construction time', 'Seconds', 1e9),
               ('cpu', 'Writer CPU time', 'CPU seconds', 1),
               ('peak_rss_kib', 'Writer peak RSS', 'MiB', 1024),
               ('bytes', 'Output GOL size', 'MiB', 2**20)]
    figures, notes = [], []
    for key, title, unit, divisor in metrics:
        protocol = ' · '.join(str(s) for s in (data['dataset'], data.get('cache'), data.get('verification'), data.get('reference')) if s)
        fig = Figure(title, protocol, unit)
        for group in data['groups']:
            label = f"{group['producer']} / {group['cores']} CPU(s)"
            if not group['comparable'] or group['verified'] != group['completed']:
                if key == 'wall_ns':
                    notes.append(f'{label}: excluded from writer comparisons. See build-summary.json for validation.')
                continue
            values = [((s['user_s'] + s['system_s']) if key == 'cpu' else s[key]) / divisor for s in group['samples']]
            fig.add(f"{group['cores']} CPU(s)", group['producer'], values)
        if fig.values:
            figures.append(fig)
    return figures, notes


def writer_matrix(source):
    figures, notes = {}, []
    profiles = []
    for summary_path in source.glob('*/build-summary.json'):
        meta = load(summary_path.with_name('metadata.json'))
        profiles.append((meta['pbf_bytes'],summary_path,meta))
    protocol = None
    for _, path, meta in sorted(profiles):
        summary = load(path)
        controls = [meta[k] for k in ('machine','workers','cpus','spec_sha256','cache','verification','interval','output_device')]
        controls.append(summary['reference'])
        if protocol is not None and controls != protocol:
            raise ValueError('writer series have different hosts, executables, storage or verification protocols')
        protocol = controls
        panels, excluded = writer_figures(summary)
        notes.extend(f"{summary['dataset']}: {n}" for n in excluded)
        for panel in panels:
            for (cpu, engine), values in panel.values.items():
                case = summary['dataset']
                fig = figures.setdefault((panel.title,cpu), Figure(f'{panel.title} · {cpu}',
                    f"{meta['machine']['node']} · {meta['cache']} input · {meta['verification']} · {summary['reference']}",panel.unit))
                fig.add(case,engine,values)
    return list(figures.values()), notes


def audit_figures(data, context=''):
    groups = defaultdict(lambda: defaultdict(dict))
    omissions = Counter()
    scalar = data.get('consumer') == 'scalar_count'
    for row in data['audit']:
        status, engine = row['validation'], row['stack']
        if status not in ('identities_match', 'content_match', 'scalar_match') or row.get('median_ms') is None:
            omissions[(engine, status)] += 1
            continue
        operation = row.get('operation', 'count')
        if not scalar and not row['included_in_ratio_summary']:
            continue
        boundary = row.get('timing_boundary', 'cli' if scalar and (engine == 'regol' or engine.startswith('gol-')) else 'persistent')
        group = (row['affinity'],tuple(row.get('cpus', [])), operation, boundary)
        if row['name'] in groups[group][engine]:
            raise ValueError(f'duplicate audited case {engine}/{row["name"]}')
        groups[group][engine][row['name']] = row['median_ms']
    figures = []
    notes = [f'{engine}: {count} {status}.' for (engine, status), count in sorted(omissions.items())]
    for (affinity, cpus, operation, boundary), engines in groups.items():
        if cpus:
            affinity += ' · CPUs ' + cpu_label(cpus)
        common = set.intersection(*(set(cases) for cases in engines.values()))
        if not common:
            notes.append(f'{operation}/{affinity}/{boundary}: no verified cases common to all successful arms.')
            continue
        fig = Figure(f'{operation.capitalize()} · {boundary} · {affinity} affinity',
                     ' · '.join(filter(None, [context, f'{len(common)} common cases · sum of per-case medians'])),
                     sample_kind='summary')
        for engine, cases in engines.items():
            fig.add('Matched cases', engine, [sum(cases[name] for name in sorted(common))])
        figures.append(fig)
    return figures, notes


def cpu_label(cpus):
    if not cpus:
        return 'unspecified'
    numbers = sorted(set(cpus))
    if len(numbers) > 1 and numbers == list(range(numbers[0],numbers[-1]+1)):
        return f'{numbers[0]}–{numbers[-1]}'
    return ','.join(map(str,numbers))


def concurrent_figures(data):
    context = (f"{data['dataset']} · {len(data['cpu_affinity'])} shared CPUs · {data['cache']} input cache · "
               f"{data['jobs_per_round']} fresh histories per round")
    rate = Figure('Batch throughput', context, 'Histories per second')
    latency = Figure('Client latency · 95th percentile within each round', context)
    by_arm = defaultdict(list)
    for trial in data['rounds']:
        if len(trial['observations']) != data['jobs_per_round']:
            raise ValueError('incomplete client batch')
        by_arm[(trial['engine'],trial['clients'])].append(trial)
    for (engine, clients), trials in sorted(by_arm.items(), key=lambda arm:arm[0][1]):
        case = f'{clients} client(s)'
        rate.add(case,engine,[t['histories_per_second'] for t in trials])
        rank = math.ceil(.95*data['jobs_per_round'])-1
        latency.add(case,engine,[sorted(r['latency_ns'] for r in t['observations'])[rank]/1e6 for t in trials])
    return [rate,latency], []


def diagnostic_figures(data):
    if data.get('validation_failures'):
        raise ValueError('diagnostic run contains validation failures')
    grouped = defaultdict(lambda: defaultdict(list))
    notes = []
    def record(title, unit, case, engine, values):
        grouped[(title,unit)][(case,engine)].extend(values)
    ctx = data.get('cpu_model', data['file_sha256'][:12])
    for row in data['results']:
        case = row['name']
        cpu = cpu_label(row.get('cpus',data.get('cpus', [])))
        if 'limits_mib' in data:
            case += f" · {row['limit_mib']} MiB limit"
            result = row['supervisor']
            if result['outcome'] != 'success':
                notes.append(f"{case}/{row['engine']}: {result['outcome']} in sample {row['sample']}.")
                continue
            record(f'First execution under memory pressure · CPUs {cpu}', 'Milliseconds', case, row['engine'],
                   [row['worker']['first_execution_ns']/1e6])
            record(f'Cgroup peak memory · CPUs {cpu}', 'MiB', case, row['engine'],
                   [int(result['after']['memory.peak'])/2**20])
        elif 'advice' in data:
            record(f"File advice · {data['cache']} pages · CPUs {cpu}", 'First execution (ms)', case, row['advice'],
                   [row['worker']['first_execution_ns']/1e6])
        elif 'input_cache' in data:
            cache = data['input_cache'] + (' verified' if row.get('os_cache_before') else ' after input hash')
            record(f'Fresh process through parsed output · {cache} · CPUs {cpu}', 'Milliseconds', case, row['engine'],
                   [row['end_to_end_ns']/1e6])
        else:
            record(f"Prepared query · {row['operation']} · CPUs {cpu}", 'Milliseconds', case, row['engine'],
                   [n/1e6 for n in row['samples_wall_ns']])
    figs=[]
    for (title, unit), by_arm in grouped.items():
        fig = Figure(title, ctx, unit, 'sample')
        for (case,engine), values in by_arm.items():
            fig.add(case,engine,values)
        figs.append(fig)
    return figs, notes


def selectivity_figures(data):
    figures = {}
    coverage = Figure('Selected nodes by viewport size', f"{data['dataset']} · {data['city']}", 'Nodes', 'count')
    for row in data['cases']:
        width = row['width_m']
        label = f'{width:g} m'
        coverage.widths_m[label] = width
        coverage.add(label, row['selector'], [row['selected_count']])
        for arm, samples in row['arms'].items():
            reader, cores = arm.rsplit('/', 1)
            fig = figures.setdefault((row['selector'],cores), Figure(
                f"{row['query']} · {cores} CPU(s)", f"{data['dataset']} · {data['city']} · {data['cache']} · preparation + first execution"))
            fig.widths_m[label] = width
            fig.add(label,reader,[r['wall_ns']/1e6 for r in samples['rounds']])
    return [coverage, *figures.values()], []


def fdw_figures(data):
    if not data['verified'] or not data['cases'] or not all(row['verified'] for row in data['cases']):
        raise ValueError('FDW report lacks complete verification')
    figures = []
    for operation, title, note in [
        ('iterate', 'Feature iteration', 'SQL projects type and ID. Python consumes feature handles.'),
        ('count', 'Scalar counts', 'SQL counts foreign tuples. Python invokes the native count API.'),
    ]:
        fig = Figure(title, f"CPU {data['cpu']} · steady SQL executor / Python API · {note}",
                     sample_kind='within-process sample')
        for row in data['cases']:
            fig.add(row['query'], 'PostgreSQL FDW', [p['execution_ns']/1e6 for p in row[operation][1:]])
            fig.add(row['query'], 'GeoDesk Python', [n/1e6 for n in row['python'][operation]['samples_wall_ns']])
        figures.append(fig)
    return figures, []


def read_report(source):
    if source.is_dir():
        candidates = [source / n for n in ('index.json','build-summary.json','summary.json','selectivity.json') if (source / n).is_file()]
        if not candidates and any(source.glob('*/build-summary.json')):
            figures, notes = writer_matrix(source)
            return source, figures, notes
        if len(candidates) != 1:
            raise ValueError('choose a scale, corpus or writer run directory, or pass a specific report JSON')
        source, = candidates
    data = load(source)
    if source.name == 'index.json' and source.with_name('run.json').is_file():
        figures, notes = scale_figures(source, data)
    elif data.get('consumer') == 'spatial_selectivity':
        figures, notes = selectivity_figures(data)
    elif 'cohorts' in data:
        figures, notes = corpus_figures(source, data)
    elif 'groups' in data and 'schema_version' in data:
        figures, notes = writer_figures(data)
    elif data.get('consumer') == 'tag_sidecar_trial':
        if data.get('verified') is not True:
            raise ValueError('tag sidecar results lack verified payloads')
        fig = Figure('Tag sidecars · preparation and consumption',
                     f"CPU {data['cpu']} · {data['cache']} pages · " + ('fresh worker per case' if data['standalone'] else 'persistent history'))
        for row in data['cases']:
            for mode, arm in row['arms'].items():
                fig.add(row['name'],mode,[trial['elapsed_ns']/1e6 for trial in arm['rounds']])
        figures, notes = [fig], []
    elif data.get('consumer') == 'independent_fresh_process_histories':
        figures, notes = concurrent_figures(data)
    elif 'file_sha256' in data and 'results' in data and any(k in data for k in ('limits_mib','input_cache','advice','subprocess_json')):
        figures, notes = diagnostic_figures(data)
    elif 'fdw_module_sha256' in data:
        figures, notes = fdw_figures(data)
    elif data.get('suite') == 'cli-outputs':
        figures, notes = cli_figures(data)
    elif 'round_order' in data and 'manifest_sha256' in data:
        figures, notes = application_figures(data)
    elif 'audit' in data and 'overview' in data:
        figures, notes = audit_figures(data)
    else:
        raise ValueError(f'{source.name}: unsupported benchmark report')
    pages = []
    for fig in figures:
        cases = list(dict.fromkeys(case for case, _ in fig.values))
        if len(cases) <= 14:
            pages.append(fig)
            continue
        for start in range(0, len(cases), 14):
            selected = set(cases[start:start + 14])
            page = Figure(f'{fig.title} ({start + 1}–{start + len(selected)} of {len(cases)})',
                          fig.context, fig.unit, fig.sample_kind)
            page.values = {key:v for key,v in fig.values.items() if key[0] in selected}
            page.widths_m = {k:v for k,v in fig.widths_m.items() if k in selected}
            pages.append(page)
    return source, pages, notes


def plot(fig, path, colors):
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    if fig.widths_m:
        canvas, ax = plt.subplots(figsize=(8.5,4.5), layout='constrained')
        try:
            for engine in sorted({engine for _,engine in fig.values}):
                points = sorted((fig.widths_m[c],v) for (c,e),v in fig.values.items() if e==engine)
                ax.plot([x for x,_ in points],[median(v) for _,v in points],marker='D',markersize=4,
                        color=colors[engine],linewidth=1.4,label=engine)
                for width, samples in points:
                    ax.scatter([width]*len(samples),samples,s=12,alpha=.55,color=colors[engine])
            if len(fig.widths_m)>1:
                ax.set_xscale('log')
            ax.set_xticks(sorted(fig.widths_m.values()), [f'{x:g}' for x in sorted(fig.widths_m.values())])
            values = [x for sample in fig.values.values() for x in sample]
            if min(values)>0 and max(values)/min(values)>=20:
                ax.set_yscale('log')
                ax.set_ylabel(fig.unit+' (log scale)')
            else:
                ax.set_ylim(bottom=0)
                ax.set_ylabel(fig.unit)
            ax.set_xlabel('Viewport width (metres)')
            ax.legend(loc='upper left', bbox_to_anchor=(1.01,1), frameon=False)
            ax.grid(axis='y',alpha=.25)
            ax.set_title(fig.context,fontsize=9,loc='left')
            canvas.suptitle(fig.title, fontsize=14, fontweight='bold')
            if fig.sample_kind != 'count':
                canvas.supxlabel('Diamonds show round medians. Dots show each measured round.',fontsize=9)
            canvas.savefig(path, metadata={'Date':None} if path.suffix=='.svg' else {})
        finally:
            plt.close(canvas)
        return
    categories = list(dict.fromkeys(case for case, _ in fig.values))
    engines = sorted({engine for _, engine in fig.values})
    height = max(3.2, 2 + len(categories) * (.42 + .12 * len(engines)))
    canvas, ax = plt.subplots(figsize=(9, height), layout='constrained')
    try:
        for k, engine in enumerate(engines):
            first = True
            for j, case in enumerate(categories):
                samples = fig.values.get((case, engine))
                if samples is None:
                    continue
                y = j + .77 * ((k + .5) / len(engines) - .5)
                mid = median(samples)
                color = colors[engine]
                if len(samples) > 1:
                    ax.hlines(y, min(samples), max(samples), color=color, lw=1, alpha=.55)
                    ax.scatter(samples, [y] * len(samples), color=color, s=10, alpha=.68)
                ax.scatter(mid, y, color=color, edgecolors='white', linewidth=.6, marker='D', s=39,
                           label={'cpp':'GeoDesk C++','java':'GeoDesk Java','python':'GeoDesk Python',
                                  'regol':'Regol','pooled':'Regol pooled','geodesk':'GOL Tool 2.0.0',
                                  'geodesk-current':'GOL Tool 2.5.3'}.get(engine,engine) if first else None, zorder=3)
                first = False
        values = [x for samples in fig.values.values() for x in samples]
        if min(values) > 0 and max(values) / min(values) >= 20:
            ax.set_xscale('log')
            ax.set_xlabel(fig.unit + ' (log scale)')
        else:
            ax.set_xlim(left=0)
            ax.set_xlabel(fig.unit)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:,.4g}'))
        ax.set_yticks(range(len(categories)), categories)
        ax.set_ylim(len(categories) - .5, -.5)
        ax.xaxis.grid(True, alpha=.25)
        ax.set_axisbelow(True)
        ax.spines[['top', 'left', 'right']].set_visible(False)
        ax.tick_params(axis='y', length=0, pad=8)
        ax.legend(loc='upper left', bbox_to_anchor=(1.01,1), frameon=False, fontsize=9)
        ax.set_title('\n'.join(textwrap.wrap(fig.context, 94)), loc='left', fontsize=9, color='#3e4e59', pad=12)
        canvas.suptitle(fig.title, fontsize=15, fontweight='bold')
        canvas.supxlabel('Diamond = sum of case medians' if fig.sample_kind == 'summary' else
                         f'Diamond = median     Dots = measured {fig.sample_kind}s     Line = observed range', fontsize=9)
        canvas.savefig(path, metadata={'Date': None} if path.suffix == '.svg' else {})
    finally:
        plt.close(canvas)


def compared_scales(left, right, host_labels=None):
    def read(path):
        path = path / 'index.json' if path.is_dir() else path
        index = load(path)
        run = load(path.with_name('run.json'))
        # Apply the same completion and shape checks as single-run plots.
        scale_figures(path, index)
        return path, index, run
    a, b = read(left), read(right)
    figs = []
    notes = []
    for name in sorted(set(a[1]) ^ set(b[1])):
        notes.append(f'{name}: present in only one scale run.')
    common = [name for name in a[2]['datasets'] if name in b[1]]
    for dataset in common:
        if a[1][dataset]['gol_sha256'] != b[1][dataset]['gol_sha256']:
            raise ValueError(f'{dataset}: different GOL inputs')
        meta = []
        for path, index, run in (a,b):
            with gzip.open(path.parent/dataset/'timings.jsonl.gz','rt') as f:
                meta.append(json.loads(next(f)))
        for key in ('cases','spec_sha256','file_cache','warm_method','rust_workers'):
            if meta[0].get(key) != meta[1].get(key):
                raise ValueError(f'{dataset}: different {key} in scale runs')
        hosts = host_labels or [m.get('host_label') or m['machine']['node'] for m in meta]
        labels = [f"{host} · {r[2]['revision'][:8]}" for host,r in zip(hosts,(a,b), strict=True)]
        if labels[0] == labels[1]:
            labels = [f'{label} ({n})' for n,label in enumerate(labels,1)]
        arms = [r[1][dataset]['prepare_and_scan'] for r in (a,b)]
        for arm in sorted(set(arms[0]) ^ set(arms[1])):
            notes.append(f'{dataset}/{arm}: present in only one scale run.')
        widths = {}
        for arm in sorted(arms[0].keys() & arms[1].keys()):
            reader, width = arm.rsplit('/',1)
            if reader=='pooled' and meta[0].get('pooled_rust_workers') != meta[1].get('pooled_rust_workers'):
                notes.append(f'{dataset}/{arm}: unequal executor sizes.')
                continue
            fig = widths.setdefault(width, Figure(f'{dataset} · {width} CPU(s)',
                f"Preparation + first execution · {meta[0]['file_cache']} cache · input {a[1][dataset]['gol_sha256'][:12]}"))
            for label, timings in zip(labels,arms):
                fig.add(reader,label,timings[arm]['rounds_ms'])
        figs.extend(widths.values())
    if not figs:
        raise ValueError('no common verified scale arms')
    return a[0], figs, notes


def render(source, output=None, fmt='svg', against=None, host_labels=None):
    source, figs, notes = compared_scales(source, against, host_labels) if against else read_report(source)
    if against and output is None:
        output = source.parent / 'comparison-charts'
    if source.is_dir() and output is None:
        output = source / 'charts'
    output = output or (source.parent / ('charts' if source.name in ('index.json','summary.json','build-summary.json','selectivity.json')
                                         else source.stem + '-charts'))
    if output.exists():
        raise FileExistsError(f'chart directory already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp) / 'charts'
        stage.mkdir()
        if figs:
            import matplotlib
            matplotlib.use('Agg')
            matplotlib.rcParams.update({'svg.fonttype':'none', 'svg.hashsalt':'gol-bench'})
            palette = list(matplotlib.colormaps['tab20'].colors)
            engines = sorted({engine for fig in figs for _, engine in fig.values})
            colors = {name:palette[(2*i % 20 + (i//10)%2) % 20] for i,name in enumerate(engines)}
            colors.update(regol='#0072b2', pooled='#56b4e9', python='#d55e00', cpp='#008565', java='#a04883')
        links = []
        with (stage/'measurements.csv').open('w', newline='') as out:
            table = csv.writer(out)
            table.writerow(('figure','protocol','case','engine','unit','observation','number','value'))
            for i, fig in enumerate(figs, 1):
                name = f'comparison-{i:02d}.{fmt}'
                plot(fig, stage / name, colors)
                links.append(f'<h2>{escape(fig.title)}</h2><a href="{name}"><img width="900" src="{name}" alt="{escape(fig.title)}"></a>')
                for (case, engine), samples in fig.values.items():
                    for n, value in enumerate(samples, 1):
                        table.writerow((fig.title,fig.context,case,engine,fig.unit,fig.sample_kind,n,value))
        notes_html = '<h2>Coverage</h2><ul>' + ''.join(f'<li>{escape(n)}</li>' for n in notes) + '</ul>' if notes else ''
        (stage/'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>GOL benchmark comparisons</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font:1rem system-ui;max-width:60rem;margin:2rem auto;padding:0 1rem}img{max-width:100%;height:auto}h2{font-size:1.15rem}</style>'
            f'<h1>GOL benchmark comparisons</h1><p>{escape(source.name)} · <a href="measurements.csv">Download chart data</a></p>'
            + notes_html + ''.join(links) + '</html>\n')
        stage.rename(output)
    return output/'index.html'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path, help='completed campaign directory or verified report JSON')
    p.add_argument('--output', type=Path, help='new chart directory (defaults to beside the report)')
    p.add_argument('--against', type=Path, help='second completed scale campaign for a matched-input host or revision comparison')
    p.add_argument('--labels', nargs=2, metavar=('LEFT','RIGHT'), help='display names for the two runs supplied with --against')
    p.add_argument('--format', choices=('svg','png'), default='svg')
    args = p.parse_args()
    if args.labels and not args.against:
        p.error('--labels requires --against')
    try:
        print(render(args.source, args.output, args.format, args.against, args.labels))
    except ImportError as exc:
        if exc.name != 'matplotlib':
            raise
        p.exit(1,'Charts require Matplotlib. Install python3-matplotlib or set GOL_BENCH_CHART_PYTHON to its interpreter.\n')
    except (KeyError, ValueError, OSError) as exc:
        p.exit(1,f'Chart error: {exc}\n')


if __name__ == '__main__':
    main()
