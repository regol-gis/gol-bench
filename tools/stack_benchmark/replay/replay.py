"""Persistent service replay using GeoDesk Python's public iterator."""
import sys
from pathlib import Path
from time import perf_counter_ns

from geodesk import Features

sequence = Path(sys.argv[2]).read_text().splitlines()
start = perf_counter_ns()
library = Features(sys.argv[1])
queries = {text: library(text) for text in sorted(set(sequence))}
records = []
for text in sequence:
    count = sum(1 for feature in queries[text])
    records.append((count, perf_counter_ns() - start))
for count, elapsed in records:
    print(count, elapsed)
