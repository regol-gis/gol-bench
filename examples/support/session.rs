//! Ordered mixed workloads: time a complete session, validate each step separately.
#![forbid(unsafe_code)]
use super::{Config, cpu_ns, fingerprint, thread_cpu_ns, workload};
use regol::{Bounds, Library, Query, Result, source::Source};
use serde_json::{Value, json};
use std::{
    collections::{BTreeSet, HashSet},
    io,
    time::Instant,
};

#[cfg(feature = "parallel")]
pub type Executor = regol::QueryExecutor;
#[cfg(not(feature = "parallel"))]
pub type Executor = ();

// Each branch retains a concrete iterator type in the timed consumption loop.
macro_rules! consume_query {
    ($executor:expr, $query:expr, $bounds:expr, |$results:ident| $body:expr) => {{
        #[cfg(feature = "parallel")]
        let parallel = $executor;
        #[cfg(not(feature = "parallel"))]
        let parallel: Option<&Executor> = None;
        match parallel {
            #[cfg(feature = "parallel")]
            Some(executor) => executor.with_query($query, $bounds, |$results| $body)?,
            _ => match $bounds {
                Some(bounds) => {
                    let $results = $query.in_bounds(bounds);
                    $body
                }
                None => {
                    let $results = $query.iter();
                    $body
                }
            },
        }
    }};
}

/// Run a fresh service request sequence once. Elapsed times include library
/// opening, query preparation, worker startup and any index construction.
pub fn replay<S: Source + Sync>(
    library: &Library<S>,
    executor: Option<&Executor>,
    schedule: &Value,
    opened_at: Instant,
) -> Result<Vec<(u64, u128)>> {
    #[cfg(not(feature = "parallel"))]
    let _ = executor;
    let sequence: Vec<String> =
        serde_json::from_value(schedule.clone()).map_err(io::Error::other)?;
    let unique: Vec<_> = sequence
        .iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let queries = unique
        .iter()
        .map(|s| Query::parse(s))
        .collect::<Result<Vec<_>>>()?;
    let prepared = queries
        .iter()
        .map(|q| q.prepare(library))
        .collect::<Result<Vec<_>>>()?;
    let mut records = Vec::with_capacity(sequence.len());
    for text in &sequence {
        let index = unique.binary_search(&text).expect("prepared every request");
        let q = &prepared[index];
        let work = consume_query!(executor, q, None, |results| workload::consume::<_, false>(
            results, "iterate"
        ))?;
        records.push((work.features, opened_at.elapsed().as_nanos()));
    }
    Ok(records)
}

pub fn run<S: Source + Sync>(
    library: &Library<S>,
    executor: Option<&Executor>,
    spec: &Value,
    config: &Config,
) -> Result<Value> {
    #[cfg(not(feature = "parallel"))]
    let _ = executor;
    let start = Instant::now();
    let invalid = || io::Error::new(io::ErrorKind::InvalidInput, "invalid session step");
    let steps = spec["steps"]
        .as_array()
        .filter(|s| !s.is_empty())
        .ok_or_else(invalid)?;
    let queries: Vec<_> = steps
        .iter()
        .map(|step| Query::parse(step["query"].as_str().ok_or_else(invalid)?))
        .collect::<Result<_>>()?;
    let prepared: Vec<_> = queries
        .iter()
        .map(|query| query.prepare(library))
        .collect::<Result<_>>()?;
    let mut settings = Vec::new();
    for step in steps {
        let bounds = step
            .get("bounds")
            .map(|v| {
                let values: Vec<i32> = v
                    .as_array()
                    .filter(|a| a.len() == 4)
                    .ok_or_else(invalid)?
                    .iter()
                    .map(|n| {
                        n.as_i64()
                            .and_then(|n| i32::try_from(n).ok())
                            .ok_or_else(invalid)
                    })
                    .collect::<std::result::Result<_, _>>()?;
                Bounds::new(values[0], values[1], values[2], values[3])
            })
            .transpose()?;
        let operation = step
            .get("operation")
            .and_then(Value::as_str)
            .unwrap_or("iterate");
        let limit = step
            .get("limit")
            .map(|v| {
                v.as_u64()
                    .and_then(|n| usize::try_from(n).ok())
                    .ok_or_else(invalid)
            })
            .transpose()?;
        if limit.is_some() && operation != "iterate" {
            return Err(invalid().into());
        }
        settings.push((bounds, operation, limit));
    }
    let setup_ns = start.elapsed().as_nanos();
    let execute = || -> Result<Vec<workload::Work>> {
        prepared
            .iter()
            .zip(&settings)
            .map(|(query, &(bounds, operation, limit))| {
                consume_query!(
                    executor,
                    query,
                    bounds,
                    |results| workload::consume_limited(results, operation, limit)
                )
            })
            .collect()
    };
    let start = Instant::now();
    let expected = execute()?;
    let first_ns = start.elapsed().as_nanos();
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
    let mut digests = Vec::new();
    let mut work_digests = Vec::new();
    let mut step_results = Vec::new();
    for (index, (query, &(bounds, operation, limit))) in prepared.iter().zip(&settings).enumerate()
    {
        let mut wanted = HashSet::new();
        if let Some(limit) = limit {
            consume_query!(executor, query, bounds, |results| {
                results.take(limit).try_for_each(|feature| -> Result<()> {
                    assert!(wanted.insert(feature?.id()), "duplicate limited result");
                    Ok(())
                })
            })?;
            assert_eq!(wanted.len() as u64, expected[index].features);
        }
        let digest = if config.verify {
            Some(consume_query!(executor, query, bounds, |results| {
                fingerprint(results, &mut wanted)
            })?)
        } else {
            None
        };
        if let (Some(limit), Some(digest)) = (limit, &digest) {
            assert!(wanted.is_empty(), "limited results absent from full query");
            assert_eq!(
                expected[index].features,
                digest["count"].as_u64().unwrap().min(limit as u64)
            );
        }
        let work_digest = if config.verify && operation != "iterate" {
            let work = consume_query!(executor, query, bounds, |results| workload::consume::<
                _,
                true,
            >(
                results, operation
            ))?;
            Some(
                json!({"features":work.features,"items":work.items,"sum":work.sum.to_string(),"xor":work.xor.to_string()}),
            )
        } else {
            None
        };
        step_results.push(json!({"name":steps[index]["name"],"count":expected[index].features,"work_items":expected[index].items,"membership_verified":limit.is_some() && config.verify}));
        digests.push(digest);
        work_digests.push(work_digest);
    }
    let total_full: u64 = digests
        .iter()
        .filter_map(|d| d.as_ref().and_then(|d| d["count"].as_u64()))
        .sum();
    let total_features: u64 = expected.iter().map(|w| w.features).sum();
    let total_items: u64 = expected.iter().map(|w| w.items).sum();
    Ok(
        json!({"name":spec["name"],"query":null,"bounds":null,"engine":config.mode,
        "count":total_features,"work_items":total_items,"operation":"session","limit":null,
        "first_item_ns":null,"release_total_ns":null,"membership_verified":false,
        "step_results":step_results,
        "fingerprint":config.verify.then(|| json!({"count":total_full,"steps":digests})),
        "work_fingerprint":config.verify.then(|| json!({"steps":work_digests})),
        "samples_wall_ns":wall,"samples_cpu_ns":cpu,"samples_thread_cpu_ns":thread_cpu,
        "repeats":repeats,"calibration_ns":calibration_ns,"setup_ns":setup_ns,"first_execution_ns":first_ns}),
    )
}
