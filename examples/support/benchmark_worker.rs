//! Reusable-query benchmark worker. JSON lines keep long runs observable.
//! mmap mode explicitly requires an immutable input for the process lifetime.
#![cfg_attr(not(feature = "mmap"), forbid(unsafe_code))]
use regol::{
    Bounds, Feature, FeatureId, FeatureType, Library, Query, Result, Types, source::Source,
};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    io::{self, Write},
    time::Instant,
};

#[path = "session.rs"]
mod session;
#[path = "workload.rs"]
mod workload;

fn cpu_ns() -> u64 {
    #[cfg(unix)]
    {
        let time = rustix::time::clock_gettime(rustix::time::ClockId::ProcessCPUTime);
        time.tv_sec as u64 * 1_000_000_000 + time.tv_nsec as u64
    }
    #[cfg(not(unix))]
    {
        0
    }
}
fn thread_cpu_ns() -> u64 {
    #[cfg(unix)]
    {
        let time = rustix::time::clock_gettime(rustix::time::ClockId::ThreadCPUTime);
        time.tv_sec as u64 * 1_000_000_000 + time.tv_nsec as u64
    }
    #[cfg(not(unix))]
    {
        0
    }
}
fn fingerprint<'a, S: Source + 'a>(
    iter: impl Iterator<Item = Result<Feature<'a, S>>>,
    wanted: &mut HashSet<FeatureId>,
) -> Result<Value> {
    let mut count = 0u64;
    let mut sum = 0u64;
    let mut xor = 0u64;
    for feature in iter {
        let feature = feature?;
        let id = feature.id();
        if !wanted.is_empty() {
            wanted.remove(&id);
        }
        let value = id.id.wrapping_mul(4)
            + match id.kind {
                FeatureType::Node => 0,
                FeatureType::Way => 1,
                FeatureType::Relation => 2,
            };
        // SplitMix64 finalizer: order-independent diagnostic fingerprint, not a
        // cryptographic proof. Both count and independent accumulators must match.
        let mut mix = value;
        mix = (mix ^ (mix >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        mix = (mix ^ (mix >> 27)).wrapping_mul(0x94d049bb133111eb);
        mix ^= mix >> 31;
        count += 1;
        sum = sum.wrapping_add(mix);
        xor ^= mix;
    }
    Ok(json!({"count":count,"sum":sum.to_string(),"xor":xor.to_string()}))
}
pub(super) struct Config {
    samples: u32,
    target_ns: u128,
    repeats: Option<u32>,
    only: Option<String>,
    verify: bool,
    pub(super) mode: String,
    threads: usize,
    once_json: bool,
    cache_stats: bool,
    pub(super) read_advice: String,
}

fn build_features() -> Value {
    json!({"parallel": cfg!(feature = "parallel"), "mmap": cfg!(feature = "mmap")})
}

// Stream the same ID strings as Python's str(feature), without allocating a
// result vector. The fixed kind names and decimal IDs need no JSON escaping.
fn write_ids<'a, S: Source + 'a>(
    iter: impl Iterator<Item = Result<Feature<'a, S>>>,
    output: &mut impl Write,
) -> Result<()> {
    write!(output, "[")?;
    let mut first = true;
    for feature in iter {
        let id = feature?.id();
        if !first {
            write!(output, ",")?;
        }
        first = false;
        let kind = match id.kind {
            FeatureType::Node => "node",
            FeatureType::Way => "way",
            FeatureType::Relation => "relation",
        };
        write!(output, "\"{kind}/{}\"", id.id)?;
    }
    writeln!(output, "]")?;
    output.flush()?;
    Ok(())
}

// The optional index is built once per process. Each record exposes its full
// time and retained payload so one-shot and amortised costs can be compared.
fn benchmark_text_index<S: Source>(library: &mut Library<S>) -> Result<Value> {
    let Some(config) = std::env::var_os("GOL_READER_BENCH_TEXT_INDEX") else {
        return Ok(Value::Null);
    };
    let (key, names, max_bytes): (String, Vec<String>, usize) =
        serde_json::from_str(&config.to_string_lossy()).map_err(io::Error::other)?;
    let invalid = || {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "expected node, way, area or relation",
        )
    };
    let types = names
        .iter()
        .map(|name| match name.as_str() {
            "node" => Ok(Types::NODE),
            "way" => Ok(Types::WAY),
            "area" => Ok(Types::AREA),
            "relation" => Ok(Types::RELATION),
            _ => Err(invalid()),
        })
        .collect::<io::Result<Vec<_>>>()?
        .into_iter()
        .reduce(Types::union)
        .ok_or_else(invalid)?;
    let start = Instant::now();
    let retained_bytes = library.index_text_key(&key, types, max_bytes)?;
    Ok(
        json!({"key": key, "types": names, "max_bytes": max_bytes, "retained_bytes": retained_bytes, "build_ns":start.elapsed().as_nanos()}),
    )
}

pub(super) fn run<S: Source + Sync>(
    mut library: Library<S>,
    specs: &[Value],
    config: &Config,
    opened_at: Instant,
    storage_stats: fn(&S) -> Value,
) -> Result<()> {
    let open_ns = opened_at.elapsed().as_nanos();
    let start = Instant::now();
    #[cfg(feature = "parallel")]
    let executor = if config.threads == 0 {
        None
    } else {
        Some(regol::QueryExecutor::new(config.threads)?)
    };
    #[cfg(not(feature = "parallel"))]
    if config.threads != 0 {
        return Err(
            io::Error::new(io::ErrorKind::Unsupported, "build with parallel feature").into(),
        );
    }
    let executor_setup_ns = start.elapsed().as_nanos();
    // Calibration and fingerprint passes repeat the same query. Measure the
    // underlying reader unless the experiment explicitly requests learning.
    let adaptive_bytes = std::env::var_os("GOL_READER_BENCH_AUTO_INDEX_BYTES")
        .map(|limit| limit.to_string_lossy().parse::<usize>())
        .transpose()
        .map_err(io::Error::other)?
        .unwrap_or(0);
    library.set_adaptive_text_index_bytes(adaptive_bytes);
    let text_index = benchmark_text_index(&mut library)?;
    #[cfg(not(feature = "parallel"))]
    let executor: Option<session::Executor> = None;
    let stdout = io::stdout();
    let mut output = stdout.lock();
    for spec in specs {
        let name = spec["name"].as_str().expect("name");
        if config.only.as_deref().is_some_and(|only| only != name) {
            continue;
        }
        if config.samples == 0 && (spec.get("steps").is_some() || spec.get("replay").is_some()) {
            return Err(io::Error::other("first-pass mode takes individual query cases").into());
        }
        if let Some(schedule) = spec.get("replay") {
            let record = session::replay(&library, executor.as_ref(), schedule, opened_at)?;
            serde_json::to_writer(
                &mut output,
                &json!({"name":name, "threads":config.threads,
                "open_ns":open_ns, "executor_setup_ns":executor_setup_ns, "text_index":text_index,
                "retained_index_bytes":library.text_index_bytes(), "requests":record}),
            )
            .map_err(io::Error::other)?;
            writeln!(output)?;
            output.flush()?;
            continue;
        }
        if spec.get("steps").is_some() {
            if config.once_json {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "JSON ID output requires an unlimited iterate workload",
                )
                .into());
            }
            let mut record = session::run(&library, executor.as_ref(), spec, config)?;
            record["open_ns"] = json!(open_ns);
            record["text_index"] = text_index.clone();
            record["threads"] = json!(config.threads);
            record["executor_setup_ns"] = json!(executor_setup_ns);
            record["storage_stats"] = storage_stats(library.source());
            record["build_features"] = build_features();
            record["read_advice"] = json!(config.read_advice);
            let memory = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
            record["process"] = json!(
                memory
                    .lines()
                    .filter(|s| {
                        ["VmRSS:", "VmHWM:", "RssAnon:", "RssFile:", "Threads:"]
                            .iter()
                            .any(|prefix| s.starts_with(prefix))
                    })
                    .collect::<Vec<_>>()
            );
            serde_json::to_writer(&mut output, &record).map_err(std::io::Error::other)?;
            writeln!(output)?;
            output.flush()?;
            continue;
        }
        let start = Instant::now();
        let query = Query::parse(spec["query"].as_str().expect("query"))?;
        let prepared = query.prepare(&library)?;
        let bounds = spec
            .get("bounds")
            .map(|v| {
                Bounds::new(
                    v[0].as_i64().unwrap() as i32,
                    v[1].as_i64().unwrap() as i32,
                    v[2].as_i64().unwrap() as i32,
                    v[3].as_i64().unwrap() as i32,
                )
            })
            .transpose()?;
        let operation = spec
            .get("operation")
            .and_then(Value::as_str)
            .unwrap_or("iterate");
        let limit = spec
            .get("limit")
            .map(|v| {
                v.as_u64()
                    .and_then(|n| usize::try_from(n).ok())
                    .ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "invalid result limit")
                    })
            })
            .transpose()?;
        if limit.is_some() && operation != "iterate" {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "limited benchmarks currently support handle iteration only",
            )
            .into());
        }
        let setup_ns = start.elapsed().as_nanos();
        if config.once_json {
            if operation != "iterate" || limit.is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "JSON ID output requires an unlimited iterate workload",
                )
                .into());
            }
            let start = Instant::now();
            let mut writer = io::BufWriter::new(&mut output);
            #[cfg(feature = "parallel")]
            if let Some(executor) = &executor {
                executor
                    .with_query(&prepared, bounds, |results| write_ids(results, &mut writer))??;
            } else {
                match bounds {
                    Some(b) => write_ids(prepared.in_bounds(b), &mut writer)?,
                    None => write_ids(prepared.iter(), &mut writer)?,
                }
            }
            #[cfg(not(feature = "parallel"))]
            match bounds {
                Some(b) => write_ids(prepared.in_bounds(b), &mut writer)?,
                None => write_ids(prepared.iter(), &mut writer)?,
            }
            let execute_write_ns = start.elapsed().as_nanos();
            let memory = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
            eprintln!(
                "{}",
                json!({"name":name,"open_ns":open_ns,"text_index":text_index,"setup_ns":setup_ns,
                    "executor_setup_ns":executor_setup_ns,"execute_write_ns":execute_write_ns,
                    "read_advice":config.read_advice,"build_features":build_features(),"storage_stats":if config.cache_stats { storage_stats(library.source()) } else { Value::Null },
                    "process":memory.lines().filter(|line| ["VmRSS:","VmHWM:","RssAnon:","RssFile:","Threads:"].iter().any(|prefix|line.starts_with(prefix))).collect::<Vec<_>>()})
            );
            continue;
        }
        let execute = || {
            if operation == "count" {
                let serial_count = || match bounds {
                    Some(bounds) => prepared.count_in_bounds(bounds),
                    None => prepared.count(),
                };
                #[cfg(feature = "parallel")]
                let count = match &executor {
                    Some(executor) => executor.count(&prepared, bounds),
                    None => serial_count(),
                }?;
                #[cfg(not(feature = "parallel"))]
                let count = serial_count()?;
                return Ok(workload::Work {
                    features: count,
                    items: count,
                    ..Default::default()
                });
            }
            #[cfg(feature = "parallel")]
            if let Some(executor) = &executor {
                return executor.with_query(&prepared, bounds, |results| {
                    workload::consume_limited(results, operation, limit)
                })?;
            }
            match bounds {
                Some(b) => workload::consume_limited(prepared.in_bounds(b), operation, limit),
                None => workload::consume_limited(prepared.iter(), operation, limit),
            }
        };
        let start = Instant::now();
        let result = execute();
        let first_ns = start.elapsed().as_nanos();
        // One pass in a single open library. Replaying a case even for
        // verification would change the query history seen by the next case.
        if config.samples == 0 {
            let mut row = json!({"name": name, "setup_ns": setup_ns, "first_execution_ns": first_ns,
                "open_ns": open_ns, "executor_setup_ns": executor_setup_ns,
                "retained_index_bytes": library.text_index_bytes()});
            match result {
                Ok(work) => {
                    row["count"] = json!(work.features);
                    row["work_items"] = json!(work.items);
                }
                Err(error) => row["error"] = json!(error.to_string()),
            }
            serde_json::to_writer(&mut output, &row).map_err(io::Error::other)?;
            writeln!(output)?;
            continue;
        }
        let expected = result?;
        // Calibrate warm batches separately from first execution: lazy page or
        // geometry-library initialization can be orders of magnitude slower.
        let calibration_start = Instant::now();
        let repeats = if let Some(repeats) = config.repeats {
            repeats
        } else if config.target_ns == 0 {
            1
        } else {
            let mut repeats = 1u32;
            loop {
                let start = Instant::now();
                for _ in 0..repeats {
                    assert_eq!(execute()?, expected, "work changed during calibration");
                }
                let elapsed = start.elapsed().as_nanos().max(1);
                let estimate = (config.target_ns.saturating_mul(u128::from(repeats)) / elapsed)
                    .clamp(1, 1000) as u32;
                if elapsed >= config.target_ns / 2 || repeats == 1000 {
                    break estimate;
                }
                repeats = estimate.max(repeats + 1).min(1000);
            }
        };
        let calibration_ns = calibration_start.elapsed().as_nanos();
        let mut wall = Vec::new();
        let mut cpu = Vec::new();
        let mut thread_cpu = Vec::new();
        for _ in 0..config.samples {
            let c = cpu_ns();
            let t = thread_cpu_ns();
            let start = Instant::now();
            for _ in 0..repeats {
                assert_eq!(
                    execute()?,
                    expected,
                    "result count changed during benchmark"
                );
            }
            wall.push(start.elapsed().as_nanos() as f64 / f64::from(repeats));
            cpu.push((cpu_ns() - c) as f64 / f64::from(repeats));
            thread_cpu.push((thread_cpu_ns() - t) as f64 / f64::from(repeats));
        }
        let mut wanted = HashSet::new();
        let mut first_item_ns = None;
        let mut release_total_ns = None;
        if let Some(limit) = limit {
            let start = Instant::now();
            let mut capture =
                |results: &mut dyn Iterator<Item = Result<Feature<'_, S>>>| -> Result<()> {
                    for (i, feature) in results.take(limit).enumerate() {
                        let feature = feature?;
                        if i == 0 {
                            first_item_ns = Some(start.elapsed().as_nanos());
                        }
                        assert!(wanted.insert(feature.id()), "duplicate limited result");
                    }
                    Ok(())
                };
            #[cfg(feature = "parallel")]
            if let Some(executor) = &executor {
                executor.with_query(&prepared, bounds, |results| capture(results))??;
            } else {
                match bounds {
                    Some(b) => capture(&mut prepared.in_bounds(b))?,
                    None => capture(&mut prepared.iter())?,
                }
            }
            #[cfg(not(feature = "parallel"))]
            match bounds {
                Some(b) => capture(&mut prepared.in_bounds(b))?,
                None => capture(&mut prepared.iter())?,
            }
            release_total_ns = Some(start.elapsed().as_nanos());
            assert_eq!(wanted.len() as u64, expected.features);
        }
        let mut verify = || {
            #[cfg(feature = "parallel")]
            if let Some(executor) = &executor {
                return executor.with_query(&prepared, bounds, |results| {
                    fingerprint(results, &mut wanted)
                })?;
            }
            match bounds {
                Some(b) => fingerprint(prepared.in_bounds(b), &mut wanted),
                None => fingerprint(prepared.iter(), &mut wanted),
            }
        };
        let fingerprint = if config.verify { Some(verify()?) } else { None };
        if let (Some(limit), Some(fingerprint)) = (limit, &fingerprint) {
            assert!(wanted.is_empty(), "limited results absent from full query");
            assert_eq!(
                expected.features,
                fingerprint["count"].as_u64().unwrap().min(limit as u64)
            );
        }
        let verify_work = || {
            #[cfg(feature = "parallel")]
            if let Some(executor) = &executor {
                return executor.with_query(&prepared, bounds, |results| {
                    workload::consume::<_, true>(results, operation)
                })?;
            }
            match bounds {
                Some(b) => workload::consume::<_, true>(prepared.in_bounds(b), operation),
                None => workload::consume::<_, true>(prepared.iter(), operation),
            }
        };
        let work_fingerprint = if config.verify && !matches!(operation, "iterate" | "count") {
            let value = verify_work()?;
            Some(
                json!({"features":value.features,"items":value.items,"sum":value.sum.to_string(),"xor":value.xor.to_string()}),
            )
        } else {
            None
        };
        let memory = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
        let memory: Vec<_> = memory
            .lines()
            .filter(|s| {
                ["VmRSS:", "VmHWM:", "RssAnon:", "RssFile:", "Threads:"]
                    .iter()
                    .any(|prefix| s.starts_with(prefix))
            })
            .collect();
        serde_json::to_writer(
            &mut output,
            &json!({"name":name,"query":spec["query"],"bounds":bounds.map(|b|[b.min.x,b.min.y,b.max.x,b.max.y]),"engine":config.mode,"count":expected.features,"work_items":expected.items,"operation":operation,"limit":limit,"first_item_ns":first_item_ns,"release_total_ns":release_total_ns,"membership_verified":limit.is_some() && config.verify,"work_fingerprint":work_fingerprint,"fingerprint":fingerprint,"samples_wall_ns":wall,"samples_cpu_ns":cpu,"samples_thread_cpu_ns":thread_cpu,"repeats":repeats,"calibration_ns":calibration_ns,"setup_ns":setup_ns,"first_execution_ns":first_ns,"open_ns":open_ns,"text_index":text_index,"retained_index_bytes":library.text_index_bytes(),"process":memory,"threads":config.threads,"executor_setup_ns":executor_setup_ns,"read_advice":config.read_advice,"build_features":build_features(),"storage_stats":storage_stats(library.source())}),
        ).map_err(std::io::Error::other)?;
        writeln!(output)?;
        output.flush()?;
    }
    Ok(())
}
pub(super) type MainResult<T = ()> = std::result::Result<T, Box<dyn std::error::Error>>;

pub(super) struct Benchmark {
    pub(super) path: String,
    pub(super) specs: Vec<Value>,
    pub(super) config: Config,
    pub(super) loading: regol::DictionaryLoading,
}

pub(super) fn benchmark_arguments() -> MainResult<Benchmark> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() < 4 {
        return Err("usage: query_benchmark FILE SPEC paged|owned|file|windowed|cached|mmap-immutable [SAMPLES] [TARGET_MS] [ONLY|-] [REPEATS|0] [verify|skip] [THREADS|0] [ids-json|ids-json-stats]".into());
    }
    if args
        .get(10)
        .is_some_and(|arg| !matches!(arg.as_str(), "ids-json" | "ids-json-stats"))
        || args.len() > 11
    {
        return Err("unknown output mode".into());
    }
    let specs: Vec<Value> = serde_json::from_slice(&std::fs::read(&args[2])?)?;
    let read_advice = match std::env::var("GOL_READER_BENCH_READ_ADVICE") {
        Ok(value) => value,
        Err(std::env::VarError::NotPresent) => "default".to_owned(),
        Err(error) => return Err(error.into()),
    };
    if !matches!(read_advice.as_str(), "default" | "normal" | "random") {
        return Err("unknown GOL_READER_BENCH_READ_ADVICE".into());
    }
    if read_advice != "default"
        && !matches!(args[3].as_str(), "paged" | "file" | "windowed" | "cached")
    {
        return Err("read advice requires paged, file, windowed or cached storage".into());
    }
    let config = Config {
        read_advice,
        samples: args.get(4).map(|v| v.parse()).transpose()?.unwrap_or(7),
        target_ns: args
            .get(5)
            .map(|v| v.parse::<u128>())
            .transpose()?
            .unwrap_or(100)
            * 1_000_000,
        only: args.get(6).filter(|v| *v != "-").cloned(),
        repeats: args
            .get(7)
            .map(|v| v.parse::<u32>())
            .transpose()?
            .filter(|v| *v != 0),
        verify: args.get(8).is_none_or(|v| v == "verify"),
        mode: args[3].clone(),
        threads: args.get(9).map(|v| v.parse()).transpose()?.unwrap_or(0),
        once_json: args.get(10).is_some(),
        cache_stats: args.get(10).is_none_or(|arg| arg == "ids-json-stats"),
    };
    if config.samples == 0
        && (config.target_ns != 0 || config.repeats.is_some() || config.verify || config.once_json)
    {
        return Err("first-pass mode requires 0 samples, 0 target, 0 repeats, skip verification and ordinary output".into());
    }
    let loading = match std::env::var("GOL_READER_BENCH_DICTIONARY").as_deref() {
        Ok("on-demand") => regol::DictionaryLoading::OnDemand,
        Ok("eager") | Err(std::env::VarError::NotPresent) => regol::DictionaryLoading::Eager,
        _ => return Err("expected eager or on-demand dictionary loading".into()),
    };
    Ok(Benchmark {
        path: args[1].clone(),
        specs,
        config,
        loading,
    })
}

#[cfg(feature = "mmap")]
pub(super) fn run_mmap(benchmark: &Benchmark, start: Instant) -> Result<()> {
    // SAFETY: this mode is an explicit caller assertion that the file remains
    // immutable for this process's lifetime.
    let source = unsafe { regol::source::MappedFile::open(&benchmark.path)? };
    let library = Library::with_dictionary_loading(source, Default::default(), benchmark.loading)?;
    run(library, &benchmark.specs, &benchmark.config, start, |_| {
        Value::Null
    })
}
