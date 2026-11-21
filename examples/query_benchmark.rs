//! Reusable-query benchmark worker for mapped and checked storage backends.
#![cfg_attr(not(feature = "mmap"), forbid(unsafe_code))]
use regol::{
    Library,
    source::{CachedFile, FileSource, PagedFile, Source, WindowedFile},
};
use serde_json::{Value, json};
use std::{io, time::Instant};

#[path = "support/benchmark_worker.rs"]
mod benchmark;
#[cfg(feature = "mmap")]
use benchmark::run_mmap;
use benchmark::{Benchmark, MainResult, benchmark_arguments, run};

// Apply advice to the actual descriptor used by the storage backend. Never
// pre-read here: cold-cache preparation belongs to the external harness.
fn open_benchmark_file(path: &str, advice: &str) -> io::Result<std::fs::File> {
    let file = std::fs::File::open(path)?;
    if advice != "default" {
        #[cfg(target_os = "linux")]
        {
            let advice = match advice {
                "normal" => rustix::fs::Advice::Normal,
                "random" => rustix::fs::Advice::Random,
                _ => return Err(io::ErrorKind::InvalidInput.into()),
            };
            rustix::fs::fadvise(&file, 0, None, advice)?;
        }
        #[cfg(not(target_os = "linux"))]
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "benchmark read advice is only supported on Linux",
        ));
    }
    Ok(file)
}

fn main() -> MainResult {
    let benchmark = benchmark_arguments()?;
    #[cfg(feature = "mmap")]
    if benchmark.config.mode == "mmap-immutable" {
        run_mmap(&benchmark, Instant::now())?;
        return Ok(());
    }
    let Benchmark {
        path,
        specs,
        config,
        loading,
    } = benchmark;
    let start = Instant::now();
    match config.mode.as_str() {
        "paged" => {
            let file = open_benchmark_file(&path, &config.read_advice)?;
            let source = match loading {
                regol::DictionaryLoading::Eager => PagedFile::from_file(file)?,
                regol::DictionaryLoading::OnDemand => PagedFile::from_file_sparse(file)?,
            };
            let library = Library::with_dictionary_loading(source, Default::default(), loading)?;
            run(
                library,
                &specs,
                &config,
                start,
                |source| json!({"page_bytes":source.page_bytes(),"cached_bytes":source.cached_bytes()}),
            )?;
        }
        "owned" => {
            let library = Library::with_dictionary_loading(
                std::fs::read(&path)?,
                Default::default(),
                loading,
            )?;
            run(library, &specs, &config, start, |_| Value::Null)?;
        }
        "file" => {
            let library = Library::with_dictionary_loading(
                FileSource::from_file(open_benchmark_file(&path, &config.read_advice)?)?,
                Default::default(),
                loading,
            )?;
            run(library, &specs, &config, start, |_| Value::Null)?;
        }
        "cached" => {
            let cache_bytes: usize = match std::env::var("GOL_READER_BENCH_CACHE_BYTES") {
                Ok(value) => value.parse()?,
                Err(std::env::VarError::NotPresent) => 16 * 1024 * 1024,
                Err(error) => return Err(error.into()),
            };
            let library = Library::with_dictionary_loading(
                CachedFile::from_file(
                    open_benchmark_file(&path, &config.read_advice)?,
                    cache_bytes,
                )?,
                Default::default(),
                loading,
            )?;
            run(library, &specs, &config, start, |source| {
                match source.allocated_bytes() {
                    Ok(allocated) => {
                        json!({"read_buffer_bytes":source.read_buffer_size(), "cache_capacity_bytes":source.capacity_bytes(), "allocated_cache_bytes":allocated})
                    }
                    Err(error) => json!({"cache_error":error.to_string()}),
                }
            })?;
        }
        "windowed" => {
            let library = Library::with_dictionary_loading(
                WindowedFile::from_file(open_benchmark_file(&path, &config.read_advice)?)?,
                Default::default(),
                loading,
            )?;
            run(
                library,
                &specs,
                &config,
                start,
                |source| json!({"read_buffer_bytes":source.read_buffer_size()}),
            )?;
        }

        _ => return Err("unknown storage mode".into()),
    }
    Ok(())
}
