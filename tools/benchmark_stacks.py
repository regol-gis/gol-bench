#!/usr/bin/env python3
"""Run persistent reader stacks sequentially; retain per-case errors and provenance.

Dependency builds are separate. The GOL must remain immutable. This harness never
uses native count shortcuts. Whole-file SHA-256 validation also pre-reads the file:
this suite measures warm-service use, not cold storage or subprocess startup.
"""
from provenance import harness_source
import argparse,hashlib,json,math,os,pathlib,platform,random,queue,signal,statistics,subprocess,time,threading
from paths import ROOT, SDK as REF, WORKER, dotnet_runtime
from worker_identity import identities
from input_identity import identify, unchanged
WIDTH=4294967294.9999

def resolve(cases):
    result=[]
    for case in cases:
        s=dict(case)
        if 'bbox_lonlat' in s:
            w,so,e,n=s['bbox_lonlat']
            x=lambda a:math.floor(a*WIDTH/360+0.5)
            y=lambda a:math.floor(math.log(math.tan(math.pi/4+math.radians(a)/2))*WIDTH/(2*math.pi)+0.5)
            s['bounds']=[x(w),y(so),x(e),y(n)]
        result.append(s)
    return result

def tsv(cases):
    return ''.join('\t'.join([c['name'],c.get('operation','iterate'),*[str(x) for x in c.get('bounds',['']*4)],c['query'],*[str(x) for x in c.get('bbox_lonlat',[-180,-85.05112877,180,85.05112877])]])+'\n' for c in cases)

def command(engine,file,spec,tab,samples,target,cpus):
    base=[str(file),str(tab),str(samples),str(target)]
    cp=f'{REF}/classes:{REF}/java-legacy/*:{REF}/*'
    if engine.startswith('rust-') and engine!='rust-ffi':
        mode={'rust-paged':'paged','rust-safe':'cached','rust-mmap':'mmap-immutable'}[engine]
        return [str(WORKER),str(file),str(spec),mode,str(samples),str(target),'-','0','verify',str(len(cpus) if len(cpus)>1 else 0)]
    if engine=='python-current':return [str(REF/'python-current313/bin/python'),str(ROOT/'tools/query_benchmark_python.py'),str(file),str(spec),'--samples',str(samples),'--target-ms',str(target)]
    if engine in ('java-current','clojure-current'):
        cp=f'{REF}/classes:{REF}/java-current/*:{REF}/*'
        entry=['CorpusJava'] if engine=='java-current' else ['clojure.main',str(ROOT/'tools/stack_benchmark/corpus_clojure.clj')]
        return ['java','-Xms128m','-Xmx2g','-cp',cp,*entry,*base]
    if engine=='python':return [str(REF/'python-legacy/bin/python'),str(ROOT/'tools/query_benchmark_python.py'),str(file),str(spec),'--samples',str(samples),'--target-ms',str(target)]
    if engine=='java':return ['java','-Xms128m','-Xmx2g','-cp',cp,'CorpusJava',*base]
    if engine=='clojure':return ['java','-Xms128m','-Xmx2g','-cp',cp,'clojure.main',str(ROOT/'tools/stack_benchmark/corpus_clojure.clj'),*base]
    if engine=='cpp':return [str(REF/'cpp-worker'),*base]
    if engine=='dotnet':return [dotnet_runtime(),str(REF/'net-worker/CorpusNet.dll'),*base]
    if engine=='php':
        mounts=[]
        for source,dest in [(file,'input.gol'),(tab,'spec.tsv'),(ROOT/'tools/stack_benchmark/corpus_php.php','worker.php'),(REF/'php-source/modules/libgeodesk.so','libgeodesk.so')]:
            mounts+=['--mount',f'type=bind,src={source},dst=/benchmark/{dest},readonly']
        return ['docker','run','--rm','--name',f'gol-corpus-php-{os.getpid()}','--ulimit','core=0','--network','none','--user','1000:1000','--cpuset-cpus',','.join(map(str,cpus)),*mounts,'gol-benchmark-php-toolchain:local','php','-n','-d','extension=/benchmark/libgeodesk.so','-d','memory_limit=2G','/benchmark/worker.php','/benchmark/input.gol','/benchmark/spec.tsv',str(samples),str(target)]
    if engine == 'php-ffi':
        files = [(file, 'input.gol'), (spec, 'spec.json'),
                 (ROOT/'tools/stack_benchmark/corpus_php_ffi.php', 'worker.php'),
                 (ROOT/'tools/stack_benchmark/php_ffi_api.h', 'php_ffi_api.h'),
                 (REF/'php-ffi/libgol_php_ffi.so', 'libgol_php_ffi.so')]
        mounts = [arg for source, dest in files
                  for arg in ('--mount', f'type=bind,src={source},dst=/benchmark/{dest},readonly')]
        return ['docker', 'run', '--rm', '--name', f'gol-corpus-php-{os.getpid()}',
                '--ulimit', 'core=0', '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
                '--cpuset-cpus', ','.join(map(str, cpus)), *mounts,
                'gol-benchmark-php-ffi:local', 'php', '-d', 'ffi.enable=true', '-d', 'memory_limit=2G',
                '/benchmark/worker.php', '/benchmark/input.gol', '/benchmark/spec.json',
                str(samples), str(target), 'iterate']
    if engine=='rust-ffi':return [str(REF/'sources/Sifir-io__geodesk-rs/target/release/examples/corpus'),*base]
    raise ValueError(engine)

def worker_command(engine,args,spec,tab,cpus):
    worker=command(engine,args.file,spec,tab,args.samples,args.target_ms,cpus)
    if engine in ('rust-mmap','rust-safe','rust-paged'):
        if getattr(args,'rust_worker',None):
            worker[0]=str(args.rust_worker.resolve())
        if getattr(args,'rust_workers',None) is not None:
            worker[-1]=str(args.rust_workers)
    elif engine=='python' and getattr(args,'legacy_python_worker',None):
        worker[0]=str(args.legacy_python_worker.absolute())
    elif (engine=='python-current' or (engine=='python' and 'python-current' not in args.engines.split(','))) and getattr(args,'python_worker',None):
        # A symlink in a venv selects its installed extension modules.
        worker[0]=str(args.python_worker.absolute())
    return worker

def run_engine(engine,cases,args,cpus,out,records):
    pending=list(cases);segment=0
    while pending:
        segment+=1;spec=out/f'{engine}-{segment}.json';tab=spec.with_suffix('.tsv');spec.write_text(json.dumps(pending));tab.write_text(tsv(pending))
        env=dict(os.environ,GOL_READER_BENCH_CACHE_BYTES=str(getattr(args,'cache_mib',16)*1024*1024),DOTNET_CLI_HOME=str(REF/'dotnet-home'),DOTNET_CLI_TELEMETRY_OPTOUT='1')
        if engine=='cpp':env['LD_LIBRARY_PATH']=os.pathsep.join(filter(None,[str(REF/'geos-install/lib'),env.get('LD_LIBRARY_PATH')]))
        # Java/.NET worker counts are governed by affinity; no extra task parallelism.
        worker=worker_command(engine,args,spec,tab,cpus)
        cmd=['taskset','-c',','.join(map(str,cpus)),*worker]
        log=out/f'{engine}-{segment}.stderr';started=time.monotonic();deadline=started+args.timeout
        with log.open('w') as errors:
            p=subprocess.Popen(cmd,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=errors,text=True,start_new_session=True,bufsize=1)
            lines=queue.Queue()
            def read_lines():
                for line in p.stdout: lines.put(line)
                lines.put(None)
            reader=threading.Thread(target=read_lines,daemon=True);reader.start()
            while pending:
                try: line=lines.get(timeout=min(1,max(0,deadline-time.monotonic())))
                except queue.Empty:
                    if time.monotonic()>deadline:
                        os.killpg(p.pid,signal.SIGKILL);p.wait();r={'name':pending[0]['name'],'error':'case_timeout','timeout_seconds':args.timeout};break
                    if p.poll() is not None:r={'name':pending[0]['name'],'error':'worker_exit','exit_status':p.returncode};break
                    continue
                if not line:
                    # A worker can close stdout before it exits. Its remaining
                    # lifetime still counts against the current case deadline.
                    try:
                        p.wait(timeout=max(0, deadline - time.monotonic()))
                        r={'name':pending[0]['name'],'error':'worker_exit','exit_status':p.returncode}
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid,signal.SIGKILL)
                        p.wait()
                        r={'name':pending[0]['name'],'error':'case_timeout','timeout_seconds':args.timeout}
                    break
                try:r=json.loads(line)
                except ValueError:
                    errors.write('STDOUT '+line);continue
                if 'name' not in r:continue
                if r['name']!=pending[0]['name']:raise RuntimeError(f"unexpected worker result {r['name']} expected {pending[0]['name']}")
                r.update(stack=engine,affinity=args.affinity,cpus=cpus,segment=segment,worker_pid=p.pid)
                if 'samples_wall_ns' in r:r['median_wall_ns']=statistics.median(r['samples_wall_ns'])
                if engine=='rust-safe' and 'error' not in r:
                    from benchmark_storage import validate_cache
                    validate_cache(r,'cached',getattr(args,'cache_mib',16))
                records.append(r);pending.pop(0);deadline=time.monotonic()+args.timeout
                with args.output.open('a') as f:f.write(json.dumps(r)+'\n')
                if len(records)%20==0 or 'error' in r:print(engine,r['name'],r['error'] if 'error' in r else round(r['median_wall_ns']/1e6,3),len(records),flush=True)
            else:r=None
            if p.poll() is None:
                if pending:os.killpg(p.pid,signal.SIGKILL)
                p.wait(timeout=10)
            reader.join(timeout=2);p.stdout.close()
            if engine in ('php', 'php-ffi'):subprocess.run(['docker','rm','-f',f'gol-corpus-php-{os.getpid()}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if r is not None and pending:
                r.update(stack=engine,affinity=args.affinity,cpus=cpus,segment=segment,stderr=str(log.absolute()))
                records.append(r);pending.pop(0)
                with args.output.open('a') as f:f.write(json.dumps(r)+'\n')
                print(engine,r,flush=True)


def cpu_model():
    try:
        lines = pathlib.Path('/proc/cpuinfo').read_text().splitlines()
    except OSError:
        return platform.machine()
    return next((line.split(':', 1)[1].strip() for line in lines
                 if line.startswith(('model name', 'Hardware')) and ':' in line), platform.machine())


def main(consumer='feature_workload'):
    p=argparse.ArgumentParser();p.add_argument('file',type=pathlib.Path);p.add_argument('--spec',type=pathlib.Path,default=ROOT/'benchmarks/query-corpus-research.json');p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--engines',default='rust-mmap,python-current');p.add_argument('--affinity',choices=['one','all'],default='one');p.add_argument('--samples',type=int,default=5);p.add_argument('--target-ms',type=int,default=25);p.add_argument('--timeout',type=float,default=120);p.add_argument('--limit',type=int);p.add_argument('--resume',action='store_true');p.add_argument('--seed',type=int,default=20261117);p.add_argument('--cache-mib',type=int,default=16)
    p.add_argument('--verified-input',type=pathlib.Path,help='reuse an immutable input identity from this local timing run for an untimed verification pass')
    p.add_argument('--preserve-order', action='store_true', help='Run cases in the order supplied by --spec.')
    p.add_argument('--rust-worker', type=pathlib.Path, help='Use this build of the Rust example worker.')
    p.add_argument('--rust-workers', type=int,
                   help='Fix the Rust executor width independently of CPU affinity (0 uses the ordered iterator).')
    p.add_argument('--legacy-python-worker',type=pathlib.Path,help='Legacy GeoDesk Python environment for mixed-version runs.')
    p.add_argument('--python-worker', type=pathlib.Path, help='Python interpreter in the GeoDesk benchmark environment.')
    args=p.parse_args()
    if args.samples < 1 or args.target_ms < 0 or args.timeout <= 0:p.error('positive samples and timeout, nonnegative target required')
    if args.cache_mib < 1:p.error('cache size must be positive')
    if args.rust_workers is not None and args.rust_workers < 0:
        p.error('--rust-workers requires a nonnegative count')
    args.file=args.file.resolve();args.output=args.output.resolve();args.output.parent.mkdir(parents=True,exist_ok=True)
    data=json.loads(args.spec.read_text());cases=resolve(data if isinstance(data,list) else data['proposed_kernel_cases'])
    if consumer == 'scalar_count' and any(case.get('operation', 'iterate') != 'iterate' or case.get('limit') is not None for case in cases):
        p.error('scalar count workers require unlimited iteration selectors')
    if not args.preserve_order:
        random.Random(args.seed).shuffle(cases)
    if args.limit:cases=cases[:args.limit]
    if len({c['name'] for c in cases})!=len(cases):p.error('duplicate case names')
    cpus=sorted(os.sched_getaffinity(0));cpus=cpus[:1] if args.affinity=='one' else cpus
    if args.output.exists() and not args.resume:p.error('output exists; choose --resume explicitly')
    records=[json.loads(x) for x in args.output.read_text().splitlines()] if args.output.exists() else []
    out=args.output.parent/(args.output.name+'.work');out.mkdir(parents=True,exist_ok=args.resume)
    engines=args.engines.split(',')
    if len(engines)!=len(set(engines)):p.error('duplicate stack name')
    binaries=identities({e:worker_command(e,args,args.spec,args.spec.with_suffix('.tsv'),cpus) for e in engines})
    start=time.time()
    identity=json.loads(args.verified_input.read_bytes()) if args.verified_input else identify(args.file)
    unchanged(args.file,identity)
    metadata={'consumer':consumer,'workers':binaries,'file':str(args.file),'gol_sha256':identity['sha256'],'gol_bytes':args.file.stat().st_size,'args':vars(args),'cpu_affinity':cpus,'started_unix':start,'source_hashes':{str(x.relative_to(ROOT)):hashlib.sha256(x.read_bytes()).hexdigest() for x in [pathlib.Path(__file__),*sorted((ROOT/'tools/stack_benchmark').glob('*.*'))] if x.is_file()},'warm_file_preread':args.verified_input is None}
    metadata['harness_source'] = harness_source()
    metadata.update(machine=platform.uname()._asdict(), cpu_model=cpu_model())
    meta_path=args.output.with_suffix('.metadata.json')
    resolved = args.output.with_suffix('.queries.json')
    if args.resume and records:
        previous = json.loads(meta_path.read_bytes())
        if metadata['consumer'] != previous.get('consumer', 'feature_workload'):
            p.error('resume differs in workload consumer')
        for key in ('gol_sha256','source_hashes','cpu_affinity','workers'):
            if metadata[key] != previous[key]:
                p.error(f'resume differs in {key}')
        for key in ('engines','samples','target_ms','cache_mib','rust_workers','rust_worker','python_worker'):
            if json.loads(json.dumps(vars(args)[key],default=str)) != previous['args'].get(key):
                p.error(f'resume differs in {key}')
        if json.loads(resolved.read_bytes()) != cases:
            p.error('resume differs in query order or workload')
    meta_path.write_text(json.dumps(metadata,indent=2,default=str)+'\n');args.output.with_suffix('.queries.json').write_text(json.dumps(cases,indent=2)+'\n')
    for engine in args.engines.split(','):
        done={r['name'] for r in records if r['stack']==engine and r['affinity']==args.affinity}
        run_engine(engine,[c for c in cases if c['name'] not in done],args,cpus,out,records)
    unchanged(args.file,identity)
    print('complete',len(records),'records',flush=True)
if __name__=='__main__':main()
