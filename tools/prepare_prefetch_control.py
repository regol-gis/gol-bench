"""Add benchmark-only prefetch switches to a private, pinned Regol source tree."""
import argparse
from pathlib import Path

KEY='GOL_BENCH_PREFETCH_CONTROL_V3'

def prepare(root):
    if not (root/'BENCHMARK-READER-REVISION').is_file():
        raise ValueError('expected a private source tree produced by prepare_regol.py')
    path = root/'src/source/mmap_read_ahead.rs'
    code = path.read_text()
    changes = {
        "impl<'a> MappedScan<'a> {": '''fn control() -> &'static str {
    static MODE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    MODE.get_or_init(|| std::env::var("@CONTROL_KEY@").unwrap_or_default())
}

impl<'a> MappedScan<'a> {''',
        '            allow_sequential,': '            allow_sequential: allow_sequential && !matches!(control(), "no-sequential" | "neither"),',
        # Removing the partition probe also removes the unordered executor's
        # independent root-span hints, including those initiated in workers.
        'pub(crate) fn now() -> Option<Self> {': '''pub(crate) fn now() -> Option<Self> {
        if control() == "neither" {
            return None;
        }''',
        # Test both admission decisions with the same per-thread sample. Some
        # filesystems report major faults with no accounted input blocks.
        'let blocks = current.input_blocks.saturating_sub(before.input_blocks);':
            '''let blocks = current.input_blocks.saturating_sub(before.input_blocks);
        if control() == "charged-io" && blocks == 0 {
            self.sequential = false;
            self.next_sample = steps.saturating_add(SAMPLE_INTERVAL);
            return;
        }''',
        'self.helpers_enabled = true;': 'self.helpers_enabled = !matches!(control(), "no-helpers" | "neither");',
    }
    for old,new in changes.items():
        if code.count(old)!=1:
            raise ValueError(f'expected exactly one {old!r} in pinned reader')
        code=code.replace(old,new.replace("@CONTROL_KEY@", KEY))
    path.write_text(code)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('private_source_tree',type=Path)
    prepare(p.parse_args().private_source_tree)
