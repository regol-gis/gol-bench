"""Build selected benchmark workers outside timed runs. Requires Python 3.11+ on Linux."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from urllib.request import urlopen
from urllib.parse import urlsplit

from paths import ROOT, SDK, dotnet_runtime
from prepare_regol import materialize, LOCK

STACKS = json.loads((ROOT/'benchmarks/sdk-pins.json').read_text())['stacks']
ADAPTERS = ROOT/'tools/stack_benchmark'
JARS = json.loads((ROOT/'benchmarks/jvm-dependencies.json').read_text())


def fetch(url, path, digest):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file():
        with path.open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest()==digest:
                return
        raise ValueError(f'existing file differs from pin: {path}')
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        out=Path(tmp)/'download'
        with urlopen(url,timeout=120) as response,out.open('wb') as dst:
            shutil.copyfileobj(response,dst)
        with out.open('rb') as f:
            if hashlib.file_digest(f,'sha256').hexdigest()!=digest:
                raise ValueError(f'download differs from pin: {url}')
        out.replace(path)


def checkout(key, dest):
    pin=STACKS[key]
    checkout_at(pin['repository_url'],pin['commit'],dest)


def checkout_at(url,revision,dest):
    if not dest.exists():
        dest.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            tree=Path(tmp)/'src'
            subprocess.run(['git','init','-q',tree],check=True)
            subprocess.run(['git','-C',tree,'fetch','--depth=1',url,revision],check=True)
            subprocess.run(['git','-C',tree,'checkout','--detach','FETCH_HEAD'],check=True)
            tree.rename(dest)
    if subprocess.check_output(['git','-C',dest,'rev-parse','HEAD'],text=True).strip()!=revision:
        raise ValueError(f'unexpected SDK revision in {dest}')


def cpp(a):
    source=SDK/'sources/clarisma__libgeodesk'
    checkout('clarisma/libgeodesk',source)
    build=SDK/'native-build'
    geos=(a.geos_prefix or SDK/'geos-install').resolve()
    if not (geos/'include/geos_c.h').is_file():
        pack=SDK/'geos-3.13.1.tar.bz2'
        fetch('https://download.osgeo.org/geos/geos-3.13.1.tar.bz2',pack,
              'df2c50503295f325e7c8d7b783aca8ba4773919cde984193850cf9e361dfd28c')
        with tarfile.open(pack) as arc:arc.extractall(SDK,filter='data')
        subprocess.run(['cmake','-S',SDK/'geos-3.13.1','-B',SDK/'geos-build','-DCMAKE_BUILD_TYPE=Release','-DBUILD_TESTING=OFF','-DCMAKE_INSTALL_LIBDIR=lib','-DCMAKE_INSTALL_PREFIX='+str(geos)],check=True)
        subprocess.run(['cmake','--build',SDK/'geos-build','-j',str(a.jobs),'--target','install'],check=True)
    subprocess.run(['cmake','-S',source,'-B',build,'-DCMAKE_BUILD_TYPE=Release','-DGEODESK_WITH_GEOS=ON','-DGEOS_INCLUDE_PATHS='+str(geos/'include')],check=True)
    subprocess.run(['cmake','--build',build,'--target','geodesk','-j',str(a.jobs)],check=True)
    sources=[(ADAPTERS/'corpus_cpp.cpp',SDK/'cpp-worker')]
    if (SDK/'counts/count_cpp.cpp').is_file():
        sources.append((SDK/'counts/count_cpp.cpp',SDK/'counts/cpp-count'))
    for src,out in sources:
        subprocess.run(['c++','-std=c++20','-O3','-DNDEBUG','-DGEODESK_WITH_GEOS',
                        '-I'+str(source/'include'),'-I'+str(build/'_deps/gtl-src/include'),
                        '-I'+str(geos/'include'),src,build/'libgeodesk.a','-L'+str(geos/'lib'),
                        '-Wl,--disable-new-dtags,-rpath,'+str(geos/'lib'),'-lgeos_c','-lgeos','-pthread','-o',out],check=True)


def java(variant):
    jarset='java-legacy' if variant=='java-legacy' else 'java-current'
    for group in [jarset]+(['clojure'] if variant=='clojure' else []):
        for item in JARS[group]:
            name=Path(urlsplit(item['url']).path).name
            fetch(item['url'],(SDK if group=='clojure' else SDK/jarset)/name,item['sha256'])
    (SDK/'classes').mkdir(exist_ok=True)
    subprocess.run(['javac','-cp',str(SDK/jarset/'*'),'-d',SDK/'classes',
                    ADAPTERS/'CorpusJava.java',ADAPTERS/'CorpusCountJava.java'],check=True)


def python_sdk(legacy, python):
    env=SDK/('python-legacy' if legacy else 'python-current313')
    subprocess.run([python,'-m','venv',env],check=True)
    pins=STACKS['python' if legacy else 'python-current']
    subprocess.run([env/'bin/python','-m','pip','install','--only-binary=:all:',
                    'geodesk=='+pins['geodesk'],'shapely=='+pins['shapely'],'numpy==2.5.5'],check=True)


def php(jobs):
    source=SDK/'php-source'
    checkout('zitsky/php-libgeodesk',source)
    subprocess.run(['git','-C',source,'submodule','update','--init','--recursive'],check=True)
    pins=STACKS['zitsky/php-libgeodesk']
    for subdir,key in [('libgeodesk','engine_commit'),('php-cpp','php_cpp_commit')]:
        actual=subprocess.check_output(['git','-C',source/subdir,'rev-parse','HEAD'],text=True).strip()
        if actual!=pins[key]:
            raise ValueError(f'PHP submodule differs from published pin: {subdir}')
    subprocess.run(['docker','build','-t','gol-benchmark-php-toolchain:local',
                    '-f',ADAPTERS/'php.Dockerfile',ADAPTERS],check=True)
    # The extension compiles the SDK sources directly. Its configure script
    # expects GTL's headers in the location normally created by CMake.
    checkout_at('https://github.com/greg7mdp/gtl.git','4c0a7a1d4a86143247b6ea7a5e4c7d6104d78590',
                source/'libgeodesk/build/_deps/gtl-src')
    include=source/'local-include'
    include.mkdir(exist_ok=True)
    if not (include/'phpcpp').exists():
        (include/'phpcpp').symlink_to('../php-cpp/include',target_is_directory=True)
    subprocess.run(['docker','run','--rm','--network','none','--user',f'{os.getuid()}:{os.getgid()}',
                    '-e','HOME=/tmp','-e','CXXFLAGS=-O3 -include cstdint -I/work/local-include',
                    '-v',f'{source}:/work','-w','/work','gol-benchmark-php-toolchain:local',
                    'sh','-ec','phpize && ./configure --enable-libgeodesk && make -j "$1"','sh',str(jobs)],check=True)


def php_ffi(jobs):
    source = SDK/'php-ffi'
    checkout('clarisma/libgeodesk', source/'libgeodesk')
    checkout_at('https://github.com/greg7mdp/gtl', '4c0a7a1d4a86143247b6ea7a5e4c7d6104d78590', source/'gtl')
    checkout_at('https://github.com/catchorg/Catch2', 'fa43b77429ba76c462b1898d6cd2f2d7a9416b14', source/'catch2')
    image = 'gol-benchmark-php-ffi:local'
    subprocess.run(['docker', 'build', '-t', image, '-f', ADAPTERS/'php-ffi.Dockerfile', ADAPTERS],
                   check=True)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none',
                    '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{source}:/work',
                    '-v', f'{ADAPTERS}:/adapters:ro', image,
                    'sh', '/adapters/php_ffi_build.sh', str(jobs)], check=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('components',nargs='+',choices=['regol','mmap-worker','safe-worker','cli','writer','sidecars','gol-tool','gol-tool-legacy','python','python-legacy','java','java-legacy','clojure','cpp','counts','dotnet','rust-ffi','php','php-ffi'])
    p.add_argument('--source',type=Path,help='external Regol Git checkout for an alternate revision')
    p.add_argument('--revision', help='override the pinned source for the selected components')
    p.add_argument('--jobs',type=int,default=1)
    p.add_argument('--python',default='python3')
    p.add_argument('--geos-prefix',type=Path,help='existing GEOS installation')
    a=p.parse_args()
    if a.jobs<1:p.error('jobs must be positive')
    source=ROOT/'.reference/regol'
    SDK.mkdir(parents=True,exist_ok=True)
    components=list(dict.fromkeys(a.components))
    rust={'regol','mmap-worker','safe-worker','cli','writer','counts','sidecars'}
    pins=json.loads(LOCK.read_text())
    def prepare_source(directory, revision, *, per_revision=False):
        expected=pins.get(revision,{}).get('revision',revision)
        if a.source:
            expected=subprocess.check_output(['git','-C',a.source,'rev-parse','--verify','--end-of-options',expected+'^{commit}'],text=True).strip()
        if per_revision:
            if len(expected) != 40 or not all(c in "0123456789abcdef" for c in expected):
                p.error("CLI source must be a locked revision or a commit in --source")
            directory = directory / expected
        if not directory.exists():
            materialize(directory,revision,a.source)
        elif (directory/'BENCHMARK-READER-REVISION').read_text().strip()!=expected:
            p.error(f'{directory} contains a different revision; use a separate checkout for this build')
        shutil.copytree(ROOT/'examples',directory/'examples',dirs_exist_ok=True)
        return directory

    if (rust-{'cli'}).intersection(components):
        prepare_source(source, a.revision or 'current')
    if 'cli' in components:
        cli_source=prepare_source(ROOT/'.reference/cli-source', a.revision or 'cli-current', per_revision=True)
    if 'counts' in components:
        subprocess.run([os.sys.executable,ROOT/'tools/prepare_count_adapters.py'],check=True)
    for comp in components:
        if comp in ('regol','mmap-worker','safe-worker','writer','cli','sidecars'):
            manifest_root = cli_source if comp == 'cli' else source
            has_gol_build = (manifest_root / 'src/bin/gol-build.rs').is_file()
            flags={'regol':['--features','mmap,parallel','--example','query_benchmark'],
                   'safe-worker':['--no-default-features','--features','parallel','--example','query_benchmark'],
                   'mmap-worker':['--features','mmap,parallel','--example','query_benchmark_mmap'],
                   'sidecars':['--features','experimental-tag-sidecars','--example','tag_sidecar_trial'],
                   'cli':['-p','regol-cli','--bin','regol'],
                   'writer':['--features','build','--bin','gol-build'] if has_gol_build else ['-p','regol-cli','--bin','regol']}[comp]
            subprocess.run(['cargo','build','--locked','--release','--manifest-path',manifest_root/'Cargo.toml',
                            '--target-dir',(ROOT/'target/safe' if comp=='safe-worker' else ROOT/'target'),'-j',str(a.jobs),*flags],check=True)
        elif comp=='counts':
            subprocess.run(['cargo','build','--locked','--release','--manifest-path',SDK/'counts/Cargo.toml','-j',str(a.jobs)],check=True)
        elif comp.startswith('gol-tool'):
            legacy=comp.endswith('legacy')
            version='2.0.0' if legacy else '2.5.3'
            digest=('d12563d6bc754592563331ee7da3a82b0883bedee29ea4d8424f2f3e916fa6f8' if legacy else
                    'cbceffcf0c42cd75098895eb744abc379083c02ed0be538f5fe93534c32c04c8')
            name=f'gol-{version}-linux.zip'
            archive=SDK/name
            fetch(f'https://github.com/clarisma/geodesk-gol/releases/download/v{version}/{name}',archive,digest)
            out=SDK/('gol-legacy' if legacy else 'gol-current')/'gol'
            out.parent.mkdir(parents=True,exist_ok=True)
            with zipfile.ZipFile(archive) as z:
                member,=[entry for entry in z.namelist() if Path(entry).name=='gol']
                out.write_bytes(z.read(member))
            out.chmod(0o755)
        elif comp.startswith('python'):python_sdk(comp=='python-legacy',a.python)
        elif comp in ('java','java-legacy','clojure'):java(comp)
        elif comp=='cpp':cpp(a)
        elif comp=='php':php(a.jobs)
        elif comp=='php-ffi':php_ffi(a.jobs)
        elif comp=='dotnet':
            src=SDK/'sources/sweepinc__geodesk-net'
            checkout('sweepinc/geodesk-net',src)
            dotnet=dotnet_runtime()
            subprocess.run([dotnet,'build',ADAPTERS/'CorpusNet.csproj','-c','Release','-o',SDK/'net-worker',
                            '-p:GeoDeskSource='+str(src/'src/GeoDesk/GeoDesk.csproj')],check=True)
            if (SDK/'counts/CountNet.csproj').exists():
                subprocess.run([dotnet,'build',SDK/'counts/CountNet.csproj','-c','Release','-o',SDK/'counts/net'],check=True)
        elif comp=='rust-ffi':
            src=SDK/'sources/Sifir-io__geodesk-rs'
            checkout('Sifir-io/geodesk-rs',src)
            # Build copies of the public-API examples in an isolated, pinned SDK checkout.
            manifest=subprocess.check_output(['git','-C',src,'show','HEAD:Cargo.toml'],text=True)
            (src/'Cargo.toml').write_text(manifest+('' if '[workspace]' in manifest else '\n[workspace]\n')+'\n[dev-dependencies]\nserde_json="1"\n')
            (src/'examples').mkdir(exist_ok=True)
            shutil.copyfile(ADAPTERS/'corpus_rust_ffi.rs',src/'examples/corpus.rs')
            if (SDK/'counts/count_ffi.rs').is_file():shutil.copyfile(SDK/'counts/count_ffi.rs',src/'examples/count-corpus.rs')
            subprocess.run(['cargo','build','--release','--examples','-j',str(a.jobs)],cwd=src,
                           env={**os.environ,'CXXFLAGS':'-include cstdint'},check=True)


if __name__=='__main__':
    main()
