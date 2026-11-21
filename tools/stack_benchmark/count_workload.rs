//! Scalar-count consumer; every decoding/query error is still propagated.
#[path = "../../examples/support/workload.rs"]
mod original;
pub use original::Work;
use regol::{Feature, Result, source::Source};

pub fn consume_limited<'a, S: Source + 'a>(
    mut iter: impl Iterator<Item = Result<Feature<'a, S>>>,
    operation: &str,
    limit: Option<usize>,
) -> Result<Work> {
    if operation == "iterate" && limit.is_none() {
        let features = iter.try_fold(0u64, |n, f| f.map(|_| n + 1))?;
        return Ok(Work { features, items: features, ..Work::default() });
    }
    original::consume_limited(iter, operation, limit)
}

pub fn consume<'a, S: Source + 'a, const VERIFY: bool>(
    iter: impl Iterator<Item = Result<Feature<'a, S>>>,
    operation: &str,
) -> Result<Work> {
    original::consume::<S, VERIFY>(iter, operation)
}
