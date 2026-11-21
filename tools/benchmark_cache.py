"""Verified Linux input-file residency for fresh-process benchmarks.

Eviction is advisory. A sample may start only after its requested state has been
observed. This does not flush storage-device caches or lock the observed pages.
"""

from contextlib import contextmanager, nullcontext
import ctypes
import json
import mmap
import os
import pathlib
import stat
import subprocess
import time


def parse_snapshot(document, path, size, page_size):
    rows = document.get("fincore") if isinstance(document, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("fincore must report exactly one input file")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("file"), str)
        or pathlib.Path(row["file"]).resolve() != pathlib.Path(path).resolve()
        or type(row.get("size")) is not int
        or row["size"] != size
    ):
        raise ValueError("fincore reported a different file or size")
    pages, resident = row.get("pages"), row.get("res")
    total_pages = (size + page_size - 1) // page_size
    if (
        type(pages) is not int
        or type(resident) is not int
        or not 0 <= pages <= total_pages
        or resident != pages * page_size
    ):
        raise ValueError("invalid or inconsistent fincore residency counts")
    return {
        "file_bytes": size,
        "page_bytes": page_size,
        "total_pages": total_pages,
        "resident_pages": pages,
        "resident_bytes": resident,
    }


def require_state(snapshot, state):
    if state not in {"warm", "cold"}:
        raise ValueError("unknown cache state")
    wanted = 0 if state == "cold" else snapshot["total_pages"]
    if snapshot["resident_pages"] != wanted:
        raise ValueError(
            f"cache state {state!r} not achieved: {snapshot['resident_pages']} resident pages; expected {wanted}"
        )


class CachePreparationError(ValueError):
    def __init__(self, state, attempts):
        self.attempts = attempts
        super().__init__(
            f"cache state {state!r} not achieved after {len(attempts)} attempts: "
            f"{attempts[-1]['resident_pages']} pages remain resident"
        )


class FileCache:
    MAX_ATTEMPTS = 5
    RETRY_SECONDS = 0.25

    def __init__(self, path, fincore="fincore"):
        self.path = pathlib.Path(path)
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("cache verification requires a regular input file")
        # mincore can return fabricated all-resident data for an unprivileged
        # non-owner without write permission. Do not accept that as warm proof.
        if info.st_uid != os.geteuid() and not os.access(self.path, os.W_OK):
            raise ValueError(
                "cache verification requires input ownership or write permission"
            )
        self.identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        self.size = info.st_size
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.command = [
            fincore,
            "--json",
            "--bytes",
            "--output",
            "FILE,SIZE,PAGES,RES",
            str(self.path),
        ]
        self.metadata = {
            "command": self.command,
            "version": subprocess.check_output(
                [fincore, "--version"], text=True
            ).strip(),
            "owner_uid": info.st_uid,
            "effective_uid": os.geteuid(),
            "page_bytes": self.page_size,
            "max_cold_attempts": self.MAX_ATTEMPTS,
            "retry_seconds": self.RETRY_SECONDS,
            "note": "fincore default backend (cachestat or mincore fallback); ownership/write access checked. Snapshots are not locks. No storage-device cache flush.",
        }

    def check_identity(self, info):
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != self.identity:
            raise ValueError("input file identity/size/mtime changed")

    @contextmanager
    def faulted_mapping(self):
        with self.path.open("rb") as source:
            self.check_identity(os.fstat(source.fileno()))
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_COPY) as mapping:
                # ZFS buffered reads can populate ARC without populating mapped
                # pages. Fault each file-backed page and hold the mapping through
                # the worker. ACCESS_COPY permits a buffer address without writes.
                for base in range(0, self.size, self.page_size * 4096):
                    mapping[base : base + self.page_size * 4096 : self.page_size]
                yield mapping

    def mapping_snapshot(self, mapping):
        total = (self.size + self.page_size - 1) // self.page_size
        address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
        mincore = ctypes.CDLL(None, use_errno=True).mincore
        mincore.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte))
        mincore.restype = ctypes.c_int
        resident = 0
        for page in range(0, total, 65536):
            count = min(65536, total - page)
            vec = (ctypes.c_ubyte * count)()
            offset = page * self.page_size
            length = min(self.size - offset, count * self.page_size)
            if mincore(address + offset, length, vec) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), self.path)
            resident += sum(byte & 1 for byte in vec)
        return dict(file_bytes=self.size, page_bytes=self.page_size,
                           total_pages=total, resident_pages=resident,
                           resident_bytes=resident * self.page_size,
                           method="mincore on a faulted file mapping")

    @contextmanager
    def warm_mapping(self):
        start = time.perf_counter_ns()
        with self.faulted_mapping() as mapping:
            observation = self.mapping_snapshot(mapping)
            if observation['resident_pages'] != observation['total_pages']:
                raise CachePreparationError("warm", [observation])
            yield dict(observation, preparation_attempts=[observation],
                       preparation_wall_ns=time.perf_counter_ns() - start)

    def sample(self, state):
        return self.warm_mapping() if state == "warm" else nullcontext(self.prepare(state))

    def snapshot(self):
        self.check_identity(self.path.stat())
        start = time.perf_counter_ns()
        process = subprocess.run(
            self.command, capture_output=True, text=True, check=True
        )
        if process.stderr:
            raise ValueError("fincore emitted diagnostics: " + process.stderr)
        snapshot = parse_snapshot(
            json.loads(process.stdout), self.path, self.size, self.page_size
        )
        snapshot["check_wall_ns"] = time.perf_counter_ns() - start
        return snapshot

    def prepare(self, state):
        if state not in {"warm", "cold"}:
            raise ValueError("unknown cache state")
        start = time.perf_counter_ns()
        attempts = []
        limit = self.MAX_ATTEMPTS if state == "cold" else 1
        for attempt in range(limit):
            if state == "cold":
                with self.path.open("rb") as source:
                    os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            snapshot = self.snapshot()
            attempts.append(snapshot)
            try:
                require_state(snapshot, state)
            except ValueError:
                if attempt + 1 == limit:
                    raise CachePreparationError(state, attempts) from None
                time.sleep(self.RETRY_SECONDS)
            else:
                return dict(
                    snapshot,
                    preparation_attempts=attempts,
                    preparation_wall_ns=time.perf_counter_ns() - start,
                )
        raise AssertionError("cache preparation loop must return or raise")
