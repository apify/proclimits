from __future__ import annotations

import sys

import pytest

from . import harness

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='these are the machine facts of macOS')


def test_unrestricted() -> None:
    """Reports no limit, says that nothing here can carry one, and still reads the machine."""
    reading = harness.probe_here()

    harness.check_invariants(reading)
    assert (reading.memory_limit, reading.cpu_limit) == (None, None)
    assert sorted(reading.notices) == ['cpu-metrics-unavailable', 'memory-metrics-unavailable']
    assert reading.sources == {'memory': None, 'cpu_quota': None, 'cpu_set': None, 'cpu_usage': None}
    assert (reading.cpu_usage, reading.cpu_used_ratio) == (None, None)
    assert reading.machine_memory_bytes == harness.machine_memory_bytes()
    # The ceiling is the cgroup one, which reads `/proc/meminfo` and so reports nothing on macOS.
    assert reading.memory_limit_ceiling is None
    assert reading.machine_cpu_count == harness.machine_cpu_count()


@pytest.mark.parametrize('python_version', harness.PYTHON_VERSIONS)
def test_python_versions(python_version: str) -> None:
    """Reads the same machine facts on every supported interpreter, whose `sysconf` tables can differ."""
    reading = harness.probe_here(python_version=python_version)

    harness.check_invariants(reading)
    # Checked on every version too, so a failure on one of them shows its cause.
    assert sorted(reading.notices) == ['cpu-metrics-unavailable', 'memory-metrics-unavailable']
    assert reading.memory_limit_ceiling is None
    assert reading.machine_cpu_count == harness.machine_cpu_count()
    assert reading.machine_memory_bytes == harness.machine_memory_bytes()
