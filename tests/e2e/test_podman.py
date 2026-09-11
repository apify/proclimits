from __future__ import annotations

import pytest

from .harness import (
    MEMORY_LIMIT,
    PODMAN,
    QUOTA_CORES,
    ROOTFUL_PODMAN,
    attempt,
    check_invariants,
    container_command,
    delegated_controllers,
    parse,
    probe_in_container,
)

pytestmark = pytest.mark.podman


def test_expected_delegation() -> None:
    """Fails the lane where a rootless container cannot be limited at all, instead of every test below it."""
    # Nothing but the delegation limits a rootless container, so without it the failures below would say
    # nothing about the sensor.
    assert {'cpu', 'memory'} <= delegated_controllers()


@pytest.mark.usefixtures('_podman')
def test_rootless_no_limits() -> None:
    """Reports no restriction for a rootless container that asks for none, several levels below the user manager."""
    reading = probe_in_container(engine=PODMAN)

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.cpu_limit is None


@pytest.mark.usefixtures('_podman')
def test_rootless_memory_limit() -> None:
    """Reads a memory limit the user manager applied, not root."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT), engine=PODMAN)

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.used is not None
    assert reading.used < reading.memory_limit
    assert reading.cpu_limit is None


@pytest.mark.usefixtures('_podman')
def test_rootless_cpu_quota() -> None:
    """Reads a fractional CPU quota, which needs the CPU controller delegated to the user manager."""
    reading = probe_in_container('--cpus', str(QUOTA_CORES), engine=PODMAN)

    check_invariants(reading)
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.memory_limit is None


@pytest.mark.usefixtures('_podman')
def test_rootless_every_axis_at_once() -> None:
    """Reads both delegated limits at once. The set of cores is left out, for the reason the next test states."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT), '--cpus', str(QUOTA_CORES), engine=PODMAN)

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES


@pytest.mark.usefixtures('_podman')
def test_rootless_cpu_set_follows_delegation() -> None:
    """Asks a rootless container for a set of cores, which only a delegated `cpuset` can honour."""
    result = attempt(container_command('--cpuset-cpus', '0', engine=PODMAN))

    if 'cpuset' in delegated_controllers():
        assert result.returncode == 0, result.stderr

        reading = parse(result.stdout)
        check_invariants(reading)
        assert reading.cpu_limit == 1.0
        assert reading.raw_cpu_set_size == 1
    else:
        # The runtime refuses to start rather than accept a set it cannot apply, so there is nothing for the
        # sensor to read - and that is the right outcome.
        assert result.returncode != 0, 'a set of cores was applied although the user manager delegates no cpuset'
        assert 'cpuset' in result.stderr


@pytest.mark.usefixtures('_podman')
def test_rootless_host_cgroup_namespace() -> None:
    """Walks the whole rootless chain, the deepest hierarchy the suite produces."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT), '--cgroupns=host', engine=PODMAN)

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    levels = reading.sources['memory']['levels']
    assert any('user@' in level for level in levels), levels


@pytest.mark.systemd_slices
@pytest.mark.usefixtures('_podman')
def test_rootless_limit_on_a_parent_slice(user_parent_slice: str) -> None:
    """Reads a limit set outside the container, on a slice of this user's own manager."""
    # The docker test of this shape needs root for the slice. Here nothing is root's. The marker is what the
    # docker one carries: rootless podman falls back to driving cgroups itself where the invoking process has
    # no logind session, and a slice name is then not something it accepts.
    reading = probe_in_container(
        '--cgroupns=host',
        f'--cgroup-parent={user_parent_slice}',
        engine=PODMAN,
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    # The limit sits above the container's own cgroup.
    assert len(reading.sources['memory']['levels']) > 1


@pytest.mark.usefixtures('_rootful_podman')
def test_rootful_no_limits() -> None:
    """Reports no restriction for a container root's podman started, whose cgroup hangs off `machine.slice`."""
    reading = probe_in_container(engine=ROOTFUL_PODMAN)

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.cpu_limit is None


@pytest.mark.usefixtures('_rootful_podman')
def test_rootful_every_axis_at_once() -> None:
    """Reads all three limits from a rootful container. The tighter of the two CPU axes wins, as under docker."""
    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        '--cpuset-cpus',
        '0',
        engine=ROOTFUL_PODMAN,
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.raw_cpu_set_size == 1
