from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from . import harness

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='job objects are a Windows mechanism')

JOB = Path(__file__).parent / 'scripts' / 'job.py'
"""The wrapper that builds a job object and runs the probe inside it."""

BASE_PYTHON = Path(sys.base_prefix) / 'python.exe'
"""The interpreter behind any virtual environment, which is what the probe has to run on.

A virtual environment spells `python.exe` as a launcher on Windows, and a process started through it leaves
the job object of its parent: it comes up in a job of its own, carrying none of the limits the test set. The
package has no dependencies, so the base interpreter runs the probe with `PYTHONPATH` and nothing else.
"""

MACHINE_CORES = harness.machine_cpu_count()

HALF_A_CORE_RATE = round(0.5 / MACHINE_CORES * 10_000) if MACHINE_CORES else 0
"""A rate that allows half a core here. Below any machine, so it is always a real restriction."""

LOOSER_RATE = round(1.5 / MACHINE_CORES * 10_000) if MACHINE_CORES else 0
"""A rate that allows one and a half cores here.

Looser than the single-core mask the tests pair it with, and still below the machine on the smallest runner
there is - a private repository gets two cores, where a rate allowing two would restrict nothing and be
dropped.
"""

WHOLE_MACHINE_MASK = (1 << MACHINE_CORES) - 1
"""An affinity mask covering every core of this machine, which restricts nothing."""


def probe_in_job(*, nest: tuple[int, ...] = (), **limits: int) -> harness.Reading:
    """Run the probe inside a job carrying the named limits, and read what it saw.

    The limits are spelled as the wrapper's own options, so a test names the mechanism rather than a shape
    invented here. `nest` wraps the job in outer ones of those memory limits, outermost first.
    """
    options = [f'--nest={outer}' for outer in nest]
    options += [f'--{name.replace("_", "-")}={value}' for name, value in limits.items()]

    command = [sys.executable, str(JOB), *options, '--', str(BASE_PYTHON), str(harness.PROBE)]

    return harness.run(command, env={**os.environ, 'PYTHONPATH': str(harness.SRC_DIR)})


def test_expected_interface() -> None:
    """Fails the lane rather than testing less, where the run did not reach a job object at all."""
    reading = probe_in_job(memory=harness.MEMORY_LIMIT)

    assert reading.interfaces == {'windows-job-object'}
    assert reading.sources['memory']['levels'] == ['job']
    # The harness makes the same kernel call the package makes, so this checks the package's own ctypes
    # wrapper around it rather than the number.
    assert reading.machine_cpu_count == MACHINE_CORES


def test_without_a_job_of_its_own() -> None:
    """Reports nothing, and explains nothing, where no test put a limit on this process."""
    reading = harness.run(
        [str(BASE_PYTHON), str(harness.PROBE)], env={**os.environ, 'PYTHONPATH': str(harness.SRC_DIR)}
    )

    harness.check_invariants(reading)
    assert (reading.memory_limit, reading.cpu_limit) == (None, None)
    assert reading.notices == ()
    # The environment decides whether there is a job, so the expected levels are computed rather than fixed.
    # The mechanism is reported either way, so only the levels vary. The two sides ask different calls -
    # `IsProcessInJob` here, the error code of a query in the package.
    assert reading.sources['memory']['levels'] == (['job'] if harness.in_a_job(str(BASE_PYTHON)) else [])


def test_memory_limit() -> None:
    """Reports a job memory limit, with the memory charged to the job measured against it."""
    reading = probe_in_job(memory=harness.MEMORY_LIMIT)

    harness.check_invariants(reading)
    assert reading.memory_limit == harness.MEMORY_LIMIT
    assert reading.memory_limit_level == 'job'
    assert reading.notices == ()


def test_memory_limit_above_the_machine() -> None:
    """Drops a limit at least as large as the commit limit it is judged against, and says why."""
    reading = probe_in_job(memory=1 << 46)

    harness.check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.raw_memory_limit == 1 << 46
    assert harness.notices_about(reading, 'memory') == ['memory-limit-covers-machine']


def test_a_limit_above_the_machine_but_below_the_ceiling() -> None:
    """Reports a limit physical memory alone would call unlimited, a job capping commit rather than memory."""
    facts = harness.windows_machine_facts()
    if facts.commit_limit <= facts.memory_bytes:
        pytest.fail(
            f'this machine has a commit limit of {facts.commit_limit} bytes and {facts.memory_bytes} bytes of '
            'memory, so there is no gap between the two to place a limit in - the page file is off, and this '
            'lane cannot check it'
        )

    limit = (facts.memory_bytes + facts.commit_limit) // 2
    reading = probe_in_job(memory=limit)

    harness.check_invariants(reading)
    # Enforced, so reported: a filter comparing against physical memory would have dropped it.
    assert reading.memory_limit == limit
    assert reading.memory_limit > (reading.machine_memory_bytes or 0)
    assert reading.notices == ()


def test_a_per_process_limit_is_not_a_budget() -> None:
    """Drops the reading where the tighter limit binds each process, which the job's usage does not belong to."""
    reading = probe_in_job(memory=harness.MEMORY_LIMIT, process_memory=harness.TIGHTER_MEMORY_LIMIT)

    harness.check_invariants(reading)
    assert reading.memory_limit is None
    # Named all the same, so a consumer reading the diagnostics sees which number the kernel will enforce.
    assert reading.raw_memory_limit == harness.TIGHTER_MEMORY_LIMIT
    assert harness.notices_about(reading, 'memory') == ['memory-usage-unavailable']


def test_hard_cap() -> None:
    """Turns a hard cap into cores, a rate being a share of the whole machine."""
    reading = probe_in_job(hard_cap=HALF_A_CORE_RATE)

    harness.check_invariants(reading)
    assert reading.cpu_limit == pytest.approx(0.5, abs=0.05)
    assert reading.cpu_limit_level == 'job'
    assert reading.cpu_rate_level == 'job'


def test_min_max_rate() -> None:
    """Reads the ceiling of a rate range, which is the other form that caps."""
    reading = probe_in_job(max_rate=HALF_A_CORE_RATE)

    harness.check_invariants(reading)
    assert reading.cpu_limit == pytest.approx(0.5, abs=0.05)


def test_a_weight_is_not_a_limit() -> None:
    """Reports nothing for a weight, which shares the CPU out rather than capping it."""
    reading = probe_in_job(weight=5)

    harness.check_invariants(reading)
    assert (reading.cpu_limit, reading.raw_cpu_quota) == (None, None)
    assert harness.notices_about(reading, 'cpu') == []


def test_a_share_is_not_a_limit() -> None:
    """Reports nothing for rate control that is merely enabled: an idle machine lets it be exceeded."""
    reading = probe_in_job(share=HALF_A_CORE_RATE)

    harness.check_invariants(reading)
    assert (reading.cpu_limit, reading.raw_cpu_quota) == (None, None)


def test_affinity_mask() -> None:
    """Counts an affinity mask on the job as a CPU limit, as a cpuset is counted on Linux."""
    reading = probe_in_job(affinity=0b1)

    harness.check_invariants(reading)
    assert reading.cpu_limit == 1.0
    assert reading.raw_cpu_set_size == 1


def test_affinity_covering_the_machine() -> None:
    """Drops a mask that allows every core, and says why."""
    reading = probe_in_job(affinity=WHOLE_MACHINE_MASK)

    harness.check_invariants(reading)
    assert reading.cpu_limit is None
    assert reading.raw_cpu_set_size == MACHINE_CORES
    assert harness.notices_about(reading, 'cpu') == ['cpu-set-covers-machine']


def test_the_tighter_cpu_restriction_wins() -> None:
    """Takes the mask over the rate where the mask allows less, the two restricting independently."""
    reading = probe_in_job(hard_cap=LOOSER_RATE, affinity=0b1)

    harness.check_invariants(reading)
    assert reading.cpu_limit == 1.0
    assert reading.raw_cpu_set_size == 1
    assert reading.raw_cpu_quota == pytest.approx(1.5, abs=0.05)


def test_a_nested_job_reads_the_inner_limit() -> None:
    """Reads the immediate job and not the one around it, which is the documented blind spot."""
    # The outer limit is the tighter of the two, so a reading of the wrong job would differ.
    reading = probe_in_job(nest=(harness.TIGHTER_MEMORY_LIMIT,), memory=harness.MEMORY_LIMIT)

    # The invariants check the room against the inner limit, so the room answers for the immediate job too.
    harness.check_invariants(reading)
    assert reading.memory_limit == harness.MEMORY_LIMIT
    assert reading.notices == ()


def test_every_axis_at_once() -> None:
    """Reads memory and both CPU restrictions from one job, each answering on its own."""
    reading = probe_in_job(memory=harness.MEMORY_LIMIT, hard_cap=LOOSER_RATE, affinity=0b1)

    harness.check_invariants(reading)
    assert reading.memory_limit == harness.MEMORY_LIMIT
    assert reading.cpu_limit == 1.0
    assert reading.cpu_usage is not None
    assert reading.notices == ()
