from __future__ import annotations

import pytest

from .harness import (
    DEBIAN_IMAGE,
    DISTRO_IMAGES,
    MEMORY_LIMIT,
    PYTHON_VERSIONS,
    QUOTA_CORES,
    TIGHTER_MEMORY_LIMIT,
    attempt,
    check_invariants,
    container_command,
    parse,
    probe_command,
    probe_in_container,
    pull_image,
)

pytestmark = pytest.mark.usefixtures('_docker')


def test_no_limits() -> None:
    """Reports no restriction for a container that sets none, spelled `max` under cgroup v2 and a sentinel under v1."""
    reading = probe_in_container()

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.cpu_limit is None
    if reading.raw_memory_limit is not None:
        assert 'memory-limit-covers-machine' in reading.notices


def test_memory_only() -> None:
    """Reads a memory limit. Nothing restricts the CPU, so no CPU limit is reported."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT))

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.used is not None
    assert reading.used < reading.memory_limit
    assert reading.cpu_limit is None


def test_cpu_quota() -> None:
    """Reads a fractional CPU quota."""
    reading = probe_in_container('--cpus', str(QUOTA_CORES))

    check_invariants(reading)
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.memory_limit is None


def test_cpu_set_only() -> None:
    """Reads a set of allowed cores, which restricts the CPU without any quota."""
    reading = probe_in_container('--cpuset-cpus', '0')

    check_invariants(reading)
    assert reading.cpu_limit == 1.0
    assert reading.raw_cpu_set_size == 1
    assert reading.raw_cpu_quota is None


def test_every_axis_at_once() -> None:
    """Takes the tighter of the two CPU axes. Here the quota is tighter than the set."""
    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        '--cpuset-cpus',
        '0',
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.raw_cpu_set_size == 1


def test_host_cgroup_namespace() -> None:
    """Reads the limit with the cgroup namespace of the machine, not one of its own."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT), '--cgroupns=host')

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT

    # Under cgroup v2 the whole hierarchy is mounted, so the container sees its ancestry. Under cgroup v1
    # each controller is mounted at the container's own cgroup, which hides it whatever the namespace.
    if reading.sources['memory']['interface'] == 'cgroup-v2':
        assert len(reading.sources['memory']['levels']) > 1


def test_tighter_limit_inside_the_container() -> None:
    """Takes the tightest limit when two levels carry different ones, which is the shape kubelet produces."""
    # The kernel forbids a cgroup that both holds processes and delegates controllers, so the shell vacates
    # the root first, enables the controller, then moves in. `0` is how a process names itself to cgroupfs.
    setup = f"""
    set -e
    if [ -e /sys/fs/cgroup/cgroup.controllers ]; then
        mkdir -p /sys/fs/cgroup/init /sys/fs/cgroup/inner
        echo 0 > /sys/fs/cgroup/init/cgroup.procs
        echo +memory > /sys/fs/cgroup/cgroup.subtree_control
        echo {TIGHTER_MEMORY_LIMIT} > /sys/fs/cgroup/inner/memory.max
        echo 0 > /sys/fs/cgroup/inner/cgroup.procs
    else
        mkdir -p /sys/fs/cgroup/memory/inner
        echo {TIGHTER_MEMORY_LIMIT} > /sys/fs/cgroup/memory/inner/memory.limit_in_bytes
        echo 0 > /sys/fs/cgroup/memory/inner/cgroup.procs
    fi
    exec {probe_command()}
    """

    reading = probe_in_container(
        '--privileged',
        '--memory',
        str(MEMORY_LIMIT),
        command=['sh', '-c', setup],
    )

    check_invariants(reading)
    assert reading.memory_limit == TIGHTER_MEMORY_LIMIT


@pytest.mark.parametrize('image', DISTRO_IMAGES)
def test_distributions(image: str) -> None:
    """Reads the same limits on every distribution, whichever libc the image carries."""
    pull_image(image)

    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        image=image,
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.cpu_usage is not None


@pytest.mark.parametrize('python_version', PYTHON_VERSIONS)
def test_python_versions(python_version: str) -> None:
    """Reads the same limits on every supported interpreter, in a container this time."""
    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        python_version=python_version,
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES


def test_cpu_shares_are_not_a_limit() -> None:
    """Reports no CPU limit for a container given shares, which weigh it against others but cap nothing."""
    # With a tenth of the shares it still gets the whole machine when nothing else wants it. Reporting them as
    # a limit is a mistake other runtimes have shipped.
    reading = probe_in_container('--cpu-shares', '512')

    check_invariants(reading)
    assert reading.cpu_limit is None
    assert reading.raw_cpu_quota is None


@pytest.mark.systemd_slices
def test_limit_on_a_parent_cgroup(parent_slice: str) -> None:
    """Reads a limit set outside the container, on the slice it was put into, when it carries none of its own."""
    reading = probe_in_container('--cgroupns=host', f'--cgroup-parent={parent_slice}')

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    # The limit sits above the container's own cgroup.
    assert len(reading.sources['memory']['levels']) > 1


@pytest.mark.systemd_slices
def test_cpu_rate_is_measured_where_the_quota_binds(quota_slice: str) -> None:
    """Measures the rate at the level the quota throttles, which is the slice and not the container."""
    reading = probe_in_container('--cgroupns=host', f'--cgroup-parent={quota_slice}')

    check_invariants(reading)
    assert reading.cpu_limit == QUOTA_CORES
    # The kernel throttles the slice as a whole, so this container's own time would read as idle whenever a
    # neighbour is what fills the quota up.
    assert reading.cpu_limit_level == reading.cpu_rate_level
    # Both name the slice, which is above the cgroup of this container - the first level of the chain.
    assert reading.cpu_limit_level != reading.sources['cpu_quota']['levels'][0]


@pytest.mark.systemd_slices
def test_limit_on_a_parent_no_one_inside_can_see(parent_slice: str) -> None:
    """Reports nothing for a limit that binds this container and that nothing inside it can read."""
    # The same slice as the test above, with the cgroup namespace a container gets by default: its own cgroup
    # is the root of the tree it sees, so the slice above is not in that tree at all. Pinned here so that no
    # later change starts guessing at paths above the root to produce a number.
    reading = probe_in_container(f'--cgroup-parent={parent_slice}')

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.raw_memory_limit is None


@pytest.mark.unified
def test_cgroup_above_the_namespace_root() -> None:
    """Reads the limit of a process whose cgroup sits above the root of its own cgroup namespace."""
    # It takes two processes to make: the kernel refuses to let one leave its own namespace upwards, so the
    # shell unshares a child inside a leaf and moves it back up from outside that namespace.
    setup = f"""
    set -e
    mkdir -p /sys/fs/cgroup/leaf
    echo 0 > /sys/fs/cgroup/leaf/cgroup.procs

    unshare -C sh -c 'touch /tmp/unshared
      while [ ! -e /tmp/moved ]; do sleep 0.1; done
      grep "^0::" /proc/self/cgroup
      exec {probe_command()}' &
    child=$!

    while [ ! -e /tmp/unshared ]; do sleep 0.1; done
    echo $child > /sys/fs/cgroup/cgroup.procs
    touch /tmp/moved
    wait $child
    """

    # This image for its `unshare`, which the busybox of the alpine one does not carry.
    pull_image(DEBIAN_IMAGE)

    result = attempt(
        container_command(
            '--privileged',
            '--memory',
            str(MEMORY_LIMIT),
            image=DEBIAN_IMAGE,
            command=['sh', '-c', setup],
        )
    )
    assert result.returncode == 0, result.stderr

    # `/..` is the one path the walk cannot descend into from the mount point, and what it must not do is leave
    # the hierarchy: above a mount point is an ordinary directory of `/sys`. The child says where it ended up,
    # or a kernel that refused the move would pass this on an ordinary chain.
    own = next(line for line in result.stdout.splitlines() if line.startswith('0::'))
    assert own.startswith('0::/..'), own

    reading = parse(result.stdout)
    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT


@pytest.mark.unified
def test_second_hierarchy_mounted_elsewhere() -> None:
    """Reads the limit of this container, not the one of a cgroup somebody else mounted a second time."""
    # A second mount can expose any subtree of the same hierarchy, and only one covering the cgroup of this
    # process says anything about it - so the tighter limit below is ignored although it is the closest on
    # offer. As in the test above, the root of the namespace is vacated before it may delegate a controller.
    setup = f"""
    set -e
    mkdir -p /sys/fs/cgroup/init /sys/fs/cgroup/other /mnt/foreign
    echo 0 > /sys/fs/cgroup/init/cgroup.procs
    echo +memory > /sys/fs/cgroup/cgroup.subtree_control
    echo {TIGHTER_MEMORY_LIMIT} > /sys/fs/cgroup/other/memory.max
    mount -o bind /sys/fs/cgroup/other /mnt/foreign
    exec {probe_command()}
    """

    reading = probe_in_container(
        '--privileged',
        '--memory',
        str(MEMORY_LIMIT),
        command=['sh', '-c', setup],
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
