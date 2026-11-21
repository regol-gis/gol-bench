"""Paths shared by benchmark processes. SDK installations may be reused across checkouts."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get("GOL_BENCH_SDK_DIR", ROOT / ".reference/corpus")).expanduser().absolute()
WORKER = Path(os.environ.get("GOL_BENCH_RUST_WORKER", ROOT / "target/release/examples/query_benchmark")).absolute()


def dotnet_runtime():
    local=SDK/'dotnet/dotnet'
    return os.environ.get('GOL_BENCH_DOTNET') or (str(local) if local.is_file() else 'dotnet')
