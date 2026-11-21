//! Immutable-mmap benchmark target. Only the mapped source is code-generated.
use std::time::Instant;

#[path = "support/benchmark_worker.rs"]
mod benchmark;
use benchmark::{MainResult, benchmark_arguments, run_mmap};

fn main() -> MainResult {
    let benchmark = benchmark_arguments()?;
    if benchmark.config.mode != "mmap-immutable" {
        return Err("query_benchmark_mmap requires mmap-immutable storage".into());
    }
    run_mmap(&benchmark, Instant::now())?;
    Ok(())
}
