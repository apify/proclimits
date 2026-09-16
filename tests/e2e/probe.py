from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import proclimits

MEASUREMENT_SECONDS = 1.0
"""How long the CPU rate is measured for, and therefore how long the burner below runs."""

ALLOCATION_BYTES = 32 * 1024 * 1024
"""How much memory the probe charges before reading, so that what is charged against a limit has a floor.

Anonymous and touched, so no mechanism can count it as reclaimable file cache. Anything below this is not the
memory of this process.
"""


WRITE_BURST_BYTES = 32 * 1024 * 1024
"""How much the probe writes when a directory is named on its command line.

Measured with a 64 MiB burst: read back at once, it was still charged in full. Writeback could start within a
second, so the second reading is taken immediately. Half that size stays further below the background dirty
threshold, past which the kernel starts writing at once.
"""

WRITE_CHUNK_BYTES = 1024 * 1024
"""How much is written per call, so the burst needs no buffer the size of itself."""


def burn(seconds: float) -> None:
    """Keep one core busy for a while.

    An idle process reads a rate of zero in every cgroup, right or wrong. So the probe makes itself busy
    while it measures.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


def source(value: proclimits.Source | None) -> dict[str, Any] | None:
    """Spell one source as JSON."""
    return None if value is None else {'interface': value.interface, 'levels': list(value.levels)}


def charged() -> tuple[int | None, int | None]:
    """Read the memory charged against the limit and the pages waiting to reach the disk."""
    description = proclimits.describe()
    budget = description.memory_budget

    return budget.used if budget is not None else None, description.raw_memory_unflushed_cache


def write_burst(directory: str) -> int:
    """Write an unsynced file into `directory` and return how many bytes it holds.

    The file stays in place. Removing it would discard the cache it produces.
    """
    chunk = bytes(WRITE_CHUNK_BYTES)
    chunks = WRITE_BURST_BYTES // WRITE_CHUNK_BYTES

    with (Path(directory) / 'burst.bin').open('wb') as file:
        for _ in range(chunks):
            file.write(chunk)

    return chunks * WRITE_CHUNK_BYTES


def main() -> None:
    # Kept alive until everything has been read. `bytearray` zero-fills, so every page is really charged.
    ballast = bytearray(ALLOCATION_BYTES)

    reading = proclimits.snapshot()

    burner = threading.Thread(target=burn, args=(MEASUREMENT_SECONDS,), daemon=True)
    burner.start()
    cpu_used_ratio = proclimits.get_cpu_used_ratio(MEASUREMENT_SECONDS)
    burner.join()

    description = proclimits.describe()

    # Last of all, so no reading above sees pages this process left waiting to be written.
    used_before: int | None = None
    used_after: int | None = None
    unflushed_after: int | None = None
    burst_written: int | None = None

    if len(sys.argv) > 1:
        used_before, _ = charged()
        burst_written = write_burst(sys.argv[1])
        used_after, unflushed_after = charged()

    print(
        json.dumps(
            {
                'version': proclimits.__version__,
                'memory_limit': reading.memory_budget.limit if reading.memory_budget is not None else None,
                'used': reading.memory_budget.used if reading.memory_budget is not None else None,
                'available': reading.memory_budget.available if reading.memory_budget is not None else None,
                'cpu_limit': reading.cpu_limit,
                'cpu_usage': reading.cpu_usage,
                'cpu_used_ratio': cpu_used_ratio,
                'raw_memory_limit': description.raw_memory_limit,
                'raw_memory_used': description.raw_memory_used,
                'raw_memory_available': description.raw_memory_available,
                'raw_memory_unflushed_cache': description.raw_memory_unflushed_cache,
                'raw_cpu_quota': description.raw_cpu_quota,
                'raw_cpu_set_size': description.raw_cpu_set_size,
                'memory_limit_level': description.memory_limit_level,
                'cpu_limit_level': description.cpu_limit_level,
                'cpu_rate_level': description.cpu_rate_level,
                'machine_memory_bytes': description.machine_memory_bytes,
                'memory_limit_ceiling': description.memory_limit_ceiling,
                'machine_cpu_count': description.machine_cpu_count,
                'allocated': len(ballast),
                'used_before': used_before,
                'used_after': used_after,
                'unflushed_after': unflushed_after,
                'burst_written': burst_written,
                'notices': [notice.code for notice in description.notices],
                'sources': {
                    'memory': source(description.memory_source),
                    'cpu_quota': source(description.cpu_quota_source),
                    'cpu_set': source(description.cpu_set_source),
                    'cpu_usage': source(description.cpu_usage_source),
                },
            }
        )
    )


if __name__ == '__main__':
    main()
