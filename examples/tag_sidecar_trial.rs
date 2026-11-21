#[cfg(unix)]
use regol::{
    Bounds, Feature, Query, Result,
    source::MappedFile,
    tag_sidecar::{IndexedLibrary, Kind, build},
};
#[cfg(unix)]
use serde_json::{Value, json};
#[cfg(unix)]
use std::{error::Error, fs, path::Path, time::Instant};

#[cfg(unix)]
#[path = "support/workload.rs"]
mod workload;

#[cfg(unix)]
struct Case<'a> {
    name: &'a str,
    query: &'a str,
    operation: &'a str,
    bounds: Option<Bounds>,
}

#[cfg(unix)]
fn cases(input: &str) -> std::result::Result<Vec<Value>, Box<dyn Error>> {
    Ok(serde_json::from_slice(&fs::read(input)?)?)
}

#[cfg(unix)]
fn parse_case(value: &Value) -> std::result::Result<Case<'_>, Box<dyn Error>> {
    let field = |key: &str| value[key].as_str().ok_or("missing case field");
    let bounds = value
        .get("bounds")
        .map(|bounds| {
            let [x1, y1, x2, y2] = serde_json::from_value::<[i32; 4]>(bounds.clone())?;
            Ok::<_, Box<dyn Error>>(Bounds::new(x1, y1, x2, y2)?)
        })
        .transpose()?;
    Ok(Case {
        name: field("name")?,
        query: field("query")?,
        operation: field("operation")?,
        bounds,
    })
}

#[cfg(unix)]
fn compare<'a, 'b>(
    case: &Case<'_>,
    mut baseline: impl Iterator<Item = Result<Feature<'a, MappedFile>>>,
    mut trial: impl Iterator<Item = Result<Feature<'b, MappedFile>>>,
) -> std::result::Result<(u64, u64, u64), Box<dyn Error>> {
    let mut features = 0;
    let mut items = 0;
    let mut payload_errors = 0;
    loop {
        match (baseline.next(), trial.next()) {
            (None, None) => return Ok((features, items, payload_errors)),
            (Some(Err(a)), Some(Err(b))) if a.to_string() == b.to_string() => {
                payload_errors += 1;
            }
            (Some(Ok(a)), Some(Ok(b))) if a.id() == b.id() => {
                features += 1;
                match case.operation {
                    "iterate" => items += 1,
                    "tags" => {
                        let a = a.tags().and_then(|tags| tags.collect::<Result<Vec<_>>>());
                        let b = b.tags().and_then(|tags| tags.collect::<Result<Vec<_>>>());
                        match (a, b) {
                            (Ok(a), Ok(b)) if a == b => items += a.len() as u64,
                            (Err(a), Err(b)) if a.to_string() == b.to_string() => {
                                payload_errors += 1;
                            }
                            _ => {
                                return Err(format!(
                                    "{} tag difference at {}",
                                    case.name, features
                                )
                                .into());
                            }
                        }
                    }
                    "geometry" => match (a.geometry(), b.geometry()) {
                        (Ok(a), Ok(b)) if a == b => items += 1,
                        (Err(a), Err(b)) if a.to_string() == b.to_string() => {
                            payload_errors += 1;
                        }
                        _ => {
                            return Err(format!(
                                "{} geometry difference at {}",
                                case.name, features
                            )
                            .into());
                        }
                    },
                    _ => return Err("expected iterate, tags or geometry".into()),
                }
            }
            (a, b) => {
                return Err(format!(
                    "{} stream differs after {features} features: {a:?}, {b:?}",
                    case.name
                )
                .into());
            }
        }
    }
}

#[cfg(unix)]
fn faults() -> (i64, i64, i64) {
    // The Linux harness measures these process counters externally through
    // getrusage when available, with zeroes on other Unix hosts.
    #[cfg(target_os = "linux")]
    if let Ok(data) = fs::read_to_string("/proc/self/stat") {
        if let Some((_, counters)) = data.rsplit_once(") ") {
            let fields = counters.split_ascii_whitespace().collect::<Vec<_>>();
            let parse = |index: usize| {
                fields
                    .get(index)
                    .and_then(|value| value.parse().ok())
                    .unwrap_or(0)
            };
            return (parse(7), parse(9), parse(21));
        }
    }
    (0, 0, 0)
}

#[cfg(unix)]
fn main() -> std::result::Result<(), Box<dyn Error>> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    match arguments.as_slice() {
        [command, gol, kind, pairs, output] if command == "build" => {
            let kind = match kind.as_str() {
                "exact" => Kind::Exact,
                "words" => Kind::Words,
                _ => return Err("expected exact or words".into()),
            };
            let pairs: Vec<(String, String)> = serde_json::from_slice(&fs::read(pairs)?)?;
            let start = Instant::now();
            // SAFETY: The trial uses private, immutable input files.
            let stats = unsafe { build(Path::new(gol), Path::new(output), kind, &pairs)? };
            let matches = pairs.iter().zip(stats.pair_matches).map(|((key, value), count)| {
                json!({"key": key,"value": value,"physical_matches": count})
            }).collect::<Vec<_>>();
            println!(
                "{}",
                json!({
                    "kind": format!("{kind:?}"),
                    "elapsed_ns": start.elapsed().as_nanos(),
                    "bytes": stats.bytes,
                    "leaves": stats.leaves,
                    "represented_leaves": stats.represented_leaves,
                    "records": stats.records,
                    "pairs": matches,
                    "faults": faults(),
                })
            );
        }
        [command, gol, sidecar, spec]
            if matches!(
                command.as_str(),
                "run" | "verify" | "scalar" | "leaves" | "verify-scalar" | "verify-leaves"
            ) =>
        {
            let cases = cases(spec)?;
            let verify = command.starts_with("verify");
            let start = Instant::now();
            let sidecar = (sidecar != "-").then(|| Path::new(sidecar));
            // SAFETY: The trial uses private, immutable files generated by build.
            let mut library = unsafe { IndexedLibrary::open(Path::new(gol), sidecar)? };
            if matches!(command.as_str(), "scalar" | "verify-scalar") {
                library.use_scalar_filter();
            }
            if matches!(command.as_str(), "leaves" | "verify-leaves") {
                library.use_leaf_postings_control();
            }
            let open_ns = start.elapsed().as_nanos();
            println!("{}", json!({"open_ns":open_ns}));
            for case in &cases {
                let case = parse_case(case)?;
                let query = Query::parse(case.query)?;
                let start = Instant::now();
                let prepared = library.prepare(&query)?;
                if verify {
                    let ordinary = prepared.ordinary_iter(case.bounds);
                    let (count, items, errors) =
                        if let Some(results) = prepared.indexed_iter(case.bounds) {
                            compare(&case, ordinary, results)?
                        } else {
                            compare(&case, ordinary, prepared.ordinary_iter(case.bounds))?
                        };
                    println!(
                        "{}",
                        json!({
                            "name":case.name, "count":count, "items":items, "payload_errors":errors,
                            "plan":prepared.plan(case.bounds),
                        })
                    );
                } else {
                    let result = if let Some(results) = prepared.indexed_iter(case.bounds) {
                        workload::consume_limited(results, case.operation, None)
                    } else {
                        workload::consume_limited(
                            prepared.ordinary_iter(case.bounds),
                            case.operation,
                            None,
                        )
                    };
                    let elapsed_ns = start.elapsed().as_nanos();
                    let mut row = json!({
                        "name": case.name, "operation":case.operation,
                        "elapsed_ns": elapsed_ns, "plan": prepared.plan(case.bounds),
                    });
                    match result {
                        Ok(work) => {
                            row["count"] = json!(work.features);
                            row["items"] = json!(work.items);
                        }
                        Err(error) => row["error"] = json!(error.to_string()),
                    }
                    println!("{row}");
                }
            }
            println!("{}", json!({"faults": faults()}));
        }
        _ => {
            return Err(
                "expected build GOL KIND PAIRS OUTPUT or run|verify|scalar|leaves|verify-scalar|verify-leaves GOL SIDECAR|- CASES"
                    .into(),
            );
        }
    }
    Ok(())
}

#[cfg(not(unix))]
fn main() {
    eprintln!("The tag sidecar trial requires Unix.");
}
