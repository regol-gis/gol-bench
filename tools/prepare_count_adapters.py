#!/usr/bin/env python3
"""Derive scalar-count variants of the public API adapters into ignored storage.

No engine/decoder source is changed. Source snapshots make the generated variant
reviewable. Build separately, never while timed benchmark workers are running.
"""
from paths import SDK
import json,pathlib,shutil
ROOT=pathlib.Path(__file__).resolve().parents[1];ref=SDK;src=ROOT/'tools/stack_benchmark';out=ref/'counts';out.mkdir(parents=True,exist_ok=True)
s=(src/'corpus_cpp.cpp').read_text();needle='Work w;for(Feature f:q)';assert needle in s;s=s.replace(needle,'Work w;if(op=="iterate"&&!verify){w.n=q.count();w.items=w.n;return w;}for(Feature f:q)');s=s.replace('#include "geometry_cpp.h"',f'#include "{src}/geometry_cpp.h"');(out/'count_cpp.cpp').write_text(s)
s=(src/'CorpusNet.cs').read_text();needle='long n=0,items=0;foreach(var f in q)';assert needle in s;s=s.replace(needle,'if(op=="iterate"){long count=((IFeatureQuery)q).Count();return [count,count];}long n=0,items=0;foreach(var f in q)');(out/'CountNet.cs').write_text(s)
(out/'CountNet.csproj').write_text(f'''<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable><EnableDefaultCompileItems>false</EnableDefaultCompileItems></PropertyGroup><ItemGroup><Compile Include="CountNet.cs"/><ProjectReference Include="{ref}/sources/sweepinc__geodesk-net/src/GeoDesk/GeoDesk.csproj"/></ItemGroup></Project>''')
s=(ROOT/'examples/support/benchmark_worker.rs').read_text().replace('"workload.rs"',f'"{src}/count_workload.rs"').replace('"session.rs"',f'"{ROOT}/examples/support/session.rs"');
needle = '        let execute = || {'
assert s.count(needle) == 1
s = s.replace(needle, '        let execute = || {\n            if operation == "iterate" && limit.is_none() {\n                let features = if let Some(executor) = &executor {\n                    executor.count(&prepared, bounds)?\n                } else {\n                    match bounds {\n                        Some(bounds) => prepared.count_in_bounds(bounds)?,\n                        None => prepared.count()?,\n                    }\n                };\n                return Ok(workload::Work { features, items: features, ..Default::default() });\n            }\n')
(out / 'benchmark_worker.rs').write_text(s)
(out / 'count_reader.rs').write_text((ROOT / 'examples/query_benchmark.rs').read_text().replace(
    '"support/benchmark_worker.rs"', '"benchmark_worker.rs"'))

(out/'Cargo.toml').write_text(f'''[workspace]\n[package]\nname="corpus-count-reader"\nversion="0.0.0"\nedition="2024"\n[features]\ndefault=["parallel","mmap"]\nparallel=[]\nmmap=[]\n[dependencies]\nregol={{path="{ROOT}/.reference/regol",features=["parallel","mmap"]}}\nserde_json="1"\nrustix={{version="1",features=["time","fs","use-libc"]}}\n[[bin]]\nname="count-reader"\npath="count_reader.rs"\n''')
s=(src/'corpus_rust_ffi.rs').read_text();needle='let result=lib.query(s[6],bbox)?.to_vec()?;let mut items=0;';assert needle in s;s=s.replace(needle,'if op=="iterate"{let n=lib.query(s[6],bbox)?.count() as u64;return Ok((n,n));}'+needle)
s=s.replace('if op=="geometry"{', '''if ["*","w[highway]","a[building]"].contains(&s[6]){return Err("resource_policy: eager API materializes millions of rich records for this scalar count; not attempted".into());}
  if op=="geometry"{''');(out/'count_ffi.rs').write_text(s)
# PHP must not call toArray() for scalar validation: that API crashed in all 12 UK smoke queries.
s=(src/'corpus_php.php').read_text();s=s.replace('$n=0;foreach($q->toArray() as $f){$n++;}return $n;','return $q->count();');begin=s.index('  $ids=[];');end=s.index('\n  echo json_encode',begin);s=s[:begin]+s[end:];s=s.replace("'ids_sha256'=>hash_final($hash),'validation_level'=>'sorted_identity_sha256'","'validation_level'=>'scalar_count_only'").replace("'payload'=>'materialized_feature_objects'","'payload'=>'scalar_count'");(out/'count_php.php').write_text(s)
shutil.copyfile(ROOT/'benchmarks/count-worker.lock',out/'Cargo.lock')
print('Prepared scalar count workers')
