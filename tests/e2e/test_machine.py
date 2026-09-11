from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from .harness import (
    CANNOT_SET_UP,
    MEMORY_LIMIT,
    PYTHON_VERSIONS,
    QUOTA_CORES,
    check_invariants,
    is_unified,
    machine_cpu_count,
    machine_memory_bytes,
    notices_about,
    probe_here,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_unrestricted() -> None:
    """Reads this machine as it is. Whatever it reports has to be coherent."""
    reading = probe_here()

    check_invariants(reading)
    # A Linux machine always has a cgroup, even when nothing is limited there.
    assert reading.sources['memory'] is not None
    assert reading.machine_cpu_count == machine_cpu_count()
    assert reading.machine_memory_bytes == machine_memory_bytes()


@pytest.mark.parametrize('python_version', PYTHON_VERSIONS)
def test_python_versions(python_version: str) -> None:
    """Reads the same machine facts on every supported interpreter."""
    reading = probe_here(python_version=python_version)

    check_invariants(reading)
    assert reading.machine_cpu_count == machine_cpu_count()
    assert reading.machine_memory_bytes == machine_memory_bytes()


@pytest.mark.unified
def test_memory_limit(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads a memory limit set on the scope this process runs in."""
    reading = probe_here(systemd_scope(f'MemoryMax={MEMORY_LIMIT}'))

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.used is not None
    assert reading.used < reading.memory_limit
    # The machine can carry unrelated notices, e.g. about a CPU set covering every core.
    assert not notices_about(reading, 'memory')


@pytest.mark.unified
def test_cpu_quota(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads a CPU quota set on the scope this process runs in."""
    reading = probe_here(systemd_scope(f'CPUQuota={int(QUOTA_CORES * 100)}%'))

    check_invariants(reading)
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.raw_cpu_quota == QUOTA_CORES


@pytest.mark.unified
def test_memory_limit_above_the_machine(systemd_scope: Callable[..., list[str]]) -> None:
    """Drops a memory limit larger than the machine, and says so."""
    limit = machine_memory_bytes() + 1024**3
    reading = probe_here(systemd_scope(f'MemoryMax={limit}'))

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.raw_memory_limit == limit
    assert 'memory-limit-covers-machine' in reading.notices


@pytest.mark.unified
def test_cpu_quota_above_the_machine(systemd_scope: Callable[..., list[str]]) -> None:
    """Drops a CPU quota larger than the machine, and says so."""
    quota_cores = machine_cpu_count() + 1
    reading = probe_here(systemd_scope(f'CPUQuota={quota_cores * 100}%'))

    check_invariants(reading)
    assert reading.cpu_limit is None
    assert reading.raw_cpu_quota == quota_cores
    assert 'cpu-quota-covers-machine' in reading.notices


def test_every_axis_at_once(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads every limit this hierarchy allows at once. Where both CPU axes are set, the tighter one wins."""
    properties = [f'MemoryMax={MEMORY_LIMIT}', f'CPUQuota={int(QUOTA_CORES * 100)}%']
    if is_unified():
        # `AllowedCPUs=` needs a delegated cpuset, which only the unified hierarchy has.
        properties.append('AllowedCPUs=0')

    # A system scope, not a user one: under cgroup v1 a user scope gets no resource cgroup.
    reading = probe_here(systemd_scope(*properties, system=True))

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    # The quota is half a core, the set is a whole one.
    assert reading.cpu_limit == QUOTA_CORES
    if is_unified():
        assert reading.raw_cpu_set_size == 1


def test_limit_on_an_ancestor(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads a limit from an ancestor when the own cgroup carries no limit of its own."""
    properties = [f'MemoryMax={MEMORY_LIMIT}']
    if is_unified():
        # Nothing can be created below a scope that is not delegated. cgroup v1 has no such rule.
        properties.append('Delegate=yes')

    # Under cgroup v1 the controller is mounted at its own point, so the chain hangs off another path. `0` is
    # how a process names itself to cgroupfs; an explicit PID takes the path for moving somebody else, which
    # the kernel of Fedora 43 refuses with EINVAL.
    move_into_leaf = f"""
    set -e
    if [ -e /sys/fs/cgroup/cgroup.controllers ]; then
        leaf="/sys/fs/cgroup$(grep "^0::" /proc/self/cgroup | cut -d: -f3)/leaf"
    else
        leaf="/sys/fs/cgroup/memory$(grep ":memory:" /proc/self/cgroup | cut -d: -f3)/leaf"
    fi

    if ! {{ mkdir -p "$leaf" && echo 0 > "$leaf/cgroup.procs"; }}; then
        # Who we are and who owns the file. That tells a missing delegation from a refused move.
        echo "leaf=$leaf as=$(id -u):$(id -g) owner=$(stat -c %U:%G:%a "$leaf/cgroup.procs" 2>&1)" >&2
        exit {CANNOT_SET_UP}
    fi

    exec "$@"
    """
    # A system scope, because under cgroup v1 a user scope gets no resource cgroup at all.
    reading = probe_here([*systemd_scope(*properties, system=True), 'bash', '-c', move_into_leaf, '--'])

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT

    # The walk starts at the leaf and finds the limit above it.
    levels = reading.sources['memory']['levels']
    assert levels[0].endswith('/leaf')
    assert len(levels) > 1
    # The chain says where it looked, this says where the limit came from.
    assert reading.memory_limit_level in levels[1:]


def test_expected_interface() -> None:
    """Fails a run that reached another cgroup interface than the one it was started for."""
    expected = os.environ.get('E2E_INTERFACE') or ('cgroup-v2' if is_unified() else 'cgroup-v1')

    reading = probe_here()

    assert reading.limit_interfaces, 'no mechanism carries any limit here'
    assert reading.limit_interfaces == {expected}
