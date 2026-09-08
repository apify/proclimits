from __future__ import annotations

import asyncio
import json
import os
import threading
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import cgroups_sensor
from cgroups_sensor import _backend, _cgroup, _sensor, _types

from .conftest import (
    HYBRID_MOUNTINFO,
    HYBRID_SELF_CGROUP,
    V1_CPU_SPLIT_MOUNTINFO,
    V1_MOUNTINFO,
    V1_SELF_CGROUP,
    V2_MOUNTINFO,
    V2_SELF_CGROUP,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

MACHINE_TOTAL_BYTES = 8 * 1024**3
MACHINE_CORES = 8

HYBRID_STRAY_FILES = {
    # A controller the cgroup v1 hierarchies did not claim rides the cgroup2 mount, so its list is not empty.
    'unified/cgroup.controllers': 'hugetlb\n',
    # systemd tracks this process as the docker service there, so that counter is the daemon's whole scope.
    'unified/system.slice/docker.service/cpu.stat': 'usage_usec 999000000\n',
    'unified/cpu.stat': 'usage_usec 999000000\n',
    # The quota and the time of the container itself, both on cgroup v1 where the runtime wrote them.
    'cpu,cpuacct/docker/abc/cpu.cfs_quota_us': '50000\n',
    'cpu,cpuacct/docker/abc/cpu.cfs_period_us': '100000\n',
    'cpu,cpuacct/docker/abc/cpuacct.usage': '3000000000\n',
}
"""A hybrid layout with `hugetlb` on the unified mount. The CPU quota and the counter that pairs with it sit
on cgroup v1, and the unified mount carries counters of another scope."""

NOTICE_PREFIXES = {'memory': ('memory', 'machine-memory'), 'cpu': ('cpu', 'machine-cpu')}
"""How a notice code names the metric it is about. `machine-memory-unknown` is a memory notice."""

# The autouse fixture below replaces these module attributes, so the tests of the real implementations go
# through references captured before any fixture runs.
real_machine_memory_bytes = _sensor.get_machine_memory_bytes
real_machine_cpu_count = _sensor.get_machine_cpu_count


@pytest.fixture(autouse=True)
def _cgroup_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read through the cgroup backend whatever platform these tests run on.

    What they cover is the layer above a backend, with the cgroup one standing in for any. On Windows the
    package wires the job object backend instead, and every fixture here lays out cgroup files.
    """
    for name in _backend.__all__:
        monkeypatch.setattr(_backend, name, getattr(_cgroup, name))


@pytest.fixture(autouse=True)
def _fixed_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine facts the filters compare against, so the expected values do not move with the machine.

    The ceiling a memory limit is judged against is pinned separately, because it is a separate reading: the
    cgroup backend answers both from `/proc/meminfo`, and a job object answers them from different fields.
    """
    monkeypatch.setattr(_sensor, 'get_machine_memory_bytes', lambda: MACHINE_TOTAL_BYTES)
    monkeypatch.setattr(_sensor, 'get_machine_cpu_count', lambda: MACHINE_CORES)
    monkeypatch.setattr(_backend, 'memory_limit_ceiling', lambda: MACHINE_TOTAL_BYTES)


def fake_time(monkeypatch: pytest.MonkeyPatch, *, sleep: Callable[[float], object]) -> None:
    """Advance the clock the sensor reads by one second per reading, running `sleep` in place of waiting."""
    clock = count(start=100.0, step=1.0)
    monkeypatch.setattr(_sensor, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=sleep))


def notice_codes(metric: str | None = None) -> tuple[str, ...]:
    """The notices of the current description, or only those about one metric.

    A fixture that lays out one metric can leave the other with notices of its own. Tests of one metric
    therefore ask for that metric.
    """
    codes = tuple(str(notice.code) for notice in cgroups_sensor.describe().notices)
    if metric is None:
        return codes

    return tuple(code for code in codes if code.startswith(NOTICE_PREFIXES[metric]))


def test_get_memory_budget_restricted(fake_cgroup: Callable[..., Path]) -> None:
    """Reports a limit below the memory of the machine, with the memory in use measured against it."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '536870912\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
        },
    )

    assert cgroups_sensor.get_memory_budget() == cgroups_sensor.MemoryBudget(
        limit=536870912, used=100000000, available=436870912
    )
    assert notice_codes('memory') == ()


def test_get_memory_budget_above_the_machine_below_the_ceiling(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps a limit above the memory of the machine where the mechanism can still hand out that much."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': f'{MACHINE_TOTAL_BYTES + 4 * 1024**3}\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
        },
    )
    # A Windows job limits commit, which the page file lifts above the memory of the machine.
    monkeypatch.setattr(_backend, 'memory_limit_ceiling', lambda: MACHINE_TOTAL_BYTES * 2)

    budget = cgroups_sensor.get_memory_budget()

    assert budget is not None
    assert budget.limit == MACHINE_TOTAL_BYTES + 4 * 1024**3
    assert notice_codes('memory') == ()


def test_get_memory_budget_covers_machine(fake_cgroup: Callable[..., Path]) -> None:
    """Drops a limit at or above the memory of the machine, which is how an unrestricted group spells it."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': f'{MACHINE_TOTAL_BYTES * 2}\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
        },
    )

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('memory-limit-covers-machine',)


def test_get_memory_budget_v1_sentinel(fake_cgroup: Callable[..., Path]) -> None:
    """Drops the sentinel a cgroup v1 hierarchy spells an absent limit as."""
    fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'memory/memory.limit_in_bytes': '9223372036854771712\n',
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 400\n',
        },
    )

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('memory-limit-covers-machine',)
    # The raw sentinel stays visible next to the machine memory it lost to.
    description = cgroups_sensor.describe()
    assert description.raw_memory_limit == 9223372036854771712
    assert description.raw_memory_used == 600


def test_get_memory_budget_no_used(fake_cgroup: Callable[..., Path]) -> None:
    """Drops a limit whose usage did not answer, and calls that a failed read rather than a shape."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'anon 600\n'},
    )

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('memory-usage-unreadable',)
    # The pair that explains the rejection stays visible: a limit next to no usable usage.
    description = cgroups_sensor.describe()
    assert description.raw_memory_limit == 536870912
    assert description.raw_memory_used is None
    # The level that went quiet is named, which is what makes this worth investigating.
    assert str(root) in description.notices[0].message


def test_get_memory_budget_usage_the_mechanism_does_not_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Separates a limit no usage figure pairs with from one whose usage failed to read."""
    raw = _types.RawMemory(
        limit=256 * 1024 * 1024,
        used=None,
        available=None,
        limit_level='job',
        unreadable_level=None,
        usage_unreadable_level=None,
    )
    monkeypatch.setattr(_backend, 'read_memory', lambda: raw)

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('memory-usage-unavailable',)


def test_get_memory_budget_brings_a_disagreeing_triple_into_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamps a usage above the limit and a room above what is left, which three separate calls can report."""
    raw = _types.RawMemory(
        limit=1000,
        used=1200,
        available=900,
        limit_level='job',
        unreadable_level=None,
        usage_unreadable_level=None,
    )
    monkeypatch.setattr(_backend, 'read_memory', lambda: raw)

    budget = cgroups_sensor.get_memory_budget()

    assert budget == cgroups_sensor.MemoryBudget(limit=1000, used=1000, available=0)
    # What the mechanism said is kept, so a consumer can see the calls disagreed.
    assert cgroups_sensor.describe().raw_memory_used == 1200


def test_get_memory_budget_keeps_a_room_the_mechanism_made_smaller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leaves a room below the distance alone, a mechanism knowing of pressure the subtraction does not."""
    raw = _types.RawMemory(
        limit=1000,
        used=250,
        available=100,
        limit_level='job',
        unreadable_level=None,
        usage_unreadable_level=None,
    )
    monkeypatch.setattr(_backend, 'read_memory', lambda: raw)

    assert cgroups_sensor.get_memory_budget() == cgroups_sensor.MemoryBudget(limit=1000, used=250, available=100)


def test_get_memory_budget_fake_limit_without_usage(fake_cgroup: Callable[..., Path]) -> None:
    """Does not complain about a missing usage next to a limit that restricts nothing anyway."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': f'{MACHINE_TOTAL_BYTES * 2}\n', 'memory.current': '1000\n', 'memory.stat': 'anon 600\n'},
    )

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('memory-limit-covers-machine',)


@pytest.mark.usefixtures('_no_cgroup')
def test_get_memory_budget_no_mechanism() -> None:
    """Reports nothing when no mechanism carries a limit, and says that is what happened."""
    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes() == ('memory-metrics-unavailable', 'cpu-metrics-unavailable')


@pytest.mark.parametrize(
    'bound',
    [
        pytest.param({'cgroup.controllers': 'cpu\n'}, id='the hierarchy lists what it binds'),
        pytest.param({}, id='the hierarchy does not say'),
    ],
)
def test_an_unbound_memory_controller_is_not_a_missing_mechanism(
    fake_cgroup: Callable[..., Path], bound: dict[str, str]
) -> None:
    """Says nothing where a mounted hierarchy binds no memory controller: unbound enforces nothing."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '50000 100000\n', 'cpu.stat': 'usage_usec 0\n', **bound},
    )

    description = cgroups_sensor.describe()

    assert description.memory_budget is None
    assert description.memory_source == cgroups_sensor.Source(interface=cgroups_sensor.Interface.CGROUP_V2, levels=())
    # `memory-metrics-unavailable` would say this machine has no cgroups, which is what it is mounting.
    assert description.notices == ()


@pytest.mark.parametrize(
    'bound',
    [
        pytest.param({'cgroup.controllers': 'memory\n'}, id='the hierarchy lists what it binds'),
        pytest.param({}, id='the hierarchy does not say'),
    ],
)
def test_an_unbound_controller_is_not_a_missing_mechanism(
    fake_cgroup: Callable[..., Path], bound: dict[str, str]
) -> None:
    """Says nothing where a mounted hierarchy binds no CPU controller: unbound enforces nothing."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '536870912\n',
            'memory.current': '1000\n',
            'memory.stat': 'inactive_file 400\n',
            **bound,
        },
    )

    description = cgroups_sensor.describe()

    assert description.cpu_limit is None
    assert description.cpu_quota_source == cgroups_sensor.Source(
        interface=cgroups_sensor.Interface.CGROUP_V2, levels=()
    )
    # `cpu-metrics-unavailable` would say this machine has no cgroups, which is what it is mounting.
    assert description.notices == ()


def test_get_memory_budget_exactly_the_machine(fake_cgroup: Callable[..., Path]) -> None:
    """Drops a limit exactly equal to the memory of the machine, which restricts nothing either."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': f'{MACHINE_TOTAL_BYTES}\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
        },
    )

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('memory-limit-covers-machine',)


@pytest.mark.parametrize(
    'limit',
    [
        pytest.param(536870912, id='a plausible limit'),
        pytest.param(9223372036854771712, id='the v1 unlimited sentinel'),
    ],
)
def test_get_memory_budget_unknown_machine_memory(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    """Drops any limit when the size it is judged against is unknown, because the sentinel cannot be told apart."""
    fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'memory/memory.limit_in_bytes': f'{limit}\n',
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 400\n',
        },
    )
    monkeypatch.setattr(_backend, 'memory_limit_ceiling', lambda: None)

    assert cgroups_sensor.get_memory_budget() is None
    assert notice_codes('memory') == ('machine-memory-unknown',)
    # The raw reading stays visible, so the rejection is attributable.
    assert cgroups_sensor.describe().raw_memory_limit == limit


@pytest.mark.parametrize(
    ('quota', 'cpu_set_cores', 'machine_cores', 'expected'),
    [
        pytest.param(None, None, 8, None, id='nothing restricts the cpu'),
        pytest.param(2.0, None, 8, 2.0, id='bandwidth quota only'),
        pytest.param(None, 2, 8, 2.0, id='cpu set only'),
        pytest.param(None, 8, 8, None, id='a cpu set covering every core is not a restriction'),
        pytest.param(8.0, None, 8, None, id='a quota covering every core is not a restriction'),
        pytest.param(10.0, None, 8, None, id='a quota above the cores of the machine is not a restriction'),
        pytest.param(10.0, 2, 8, 2.0, id='a quota above the machine leaves the cpu set to bind'),
        pytest.param(4.0, 2, 8, 2.0, id='cpu set is tighter than the quota'),
        pytest.param(1.0, 2, 8, 1.0, id='quota is tighter than the cpu set'),
        pytest.param(2.0, None, None, 2.0, id='a quota counts when the cores of the machine are unknown'),
        pytest.param(None, 2, None, 2.0, id='a cpu set counts when the cores of the machine are unknown'),
    ],
)
def test_get_cpu_limit(
    monkeypatch: pytest.MonkeyPatch,
    quota: float | None,
    cpu_set_cores: int | None,
    machine_cores: int | None,
    expected: float | None,
) -> None:
    """Takes the tighter of the bandwidth quota and the CPU set, which restrict the CPU independently."""
    counter = '/sys/fs/cgroup'
    raw_quota = None if quota is None else _cgroup.RawCpuQuota(cores=quota, limit_level=counter, usage_level=counter)
    raw_set = (
        None
        if cpu_set_cores is None
        else _cgroup.RawCpuSet(cores=cpu_set_cores, limit_level=counter, usage_level=counter)
    )
    monkeypatch.setattr(_cgroup, 'read_cpu_quota', lambda: raw_quota)
    monkeypatch.setattr(_cgroup, 'read_cpu_set_size', lambda: raw_set)
    monkeypatch.setattr(_sensor, 'get_machine_cpu_count', lambda: machine_cores)

    assert cgroups_sensor.get_cpu_limit() == expected


@pytest.mark.usefixtures('_no_cgroup')
def test_get_cpu_limit_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explains each reading that covers the machine, so a missing limit is attributable."""
    counter = '/sys/fs/cgroup'
    quota = _cgroup.RawCpuQuota(cores=10.0, limit_level=counter, usage_level=counter)
    monkeypatch.setattr(_cgroup, 'read_cpu_quota', lambda: quota)
    cpu_set = _cgroup.RawCpuSet(cores=8, limit_level=counter, usage_level=counter)
    monkeypatch.setattr(_cgroup, 'read_cpu_set_size', lambda: cpu_set)

    assert cgroups_sensor.get_cpu_limit() is None
    assert notice_codes('cpu') == ('cpu-quota-covers-machine', 'cpu-set-covers-machine')


def test_cpu_limit_from_an_unsizeable_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports nothing, and names the level, where a limit is a share of a machine of unknown size."""
    raw = _cgroup.RawCpu(quota=None, cpu_set=None, unreadable_level=None, unconvertible_level='job')
    monkeypatch.setattr(_backend, 'read_cpu', lambda: raw)

    assert cgroups_sensor.get_cpu_limit() is None
    assert notice_codes('cpu') == ('machine-cpu-count-unknown',)
    assert 'job' in next(n.message for n in cgroups_sensor.describe().notices if str(n.code).startswith('machine-cpu'))


def test_an_unsizeable_share_drops_the_other_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drops a reading in cores alongside it, because the share that cannot be sized may be the tighter one."""
    counter = '/sys/fs/cgroup'
    raw = _cgroup.RawCpu(
        quota=_cgroup.RawCpuQuota(cores=4.0, limit_level=counter, usage_level=counter),
        cpu_set=_cgroup.RawCpuSet(cores=2, limit_level=counter, usage_level=counter),
        unreadable_level=None,
        unconvertible_level='job',
    )
    monkeypatch.setattr(_backend, 'read_cpu', lambda: raw)

    description = cgroups_sensor.describe()

    assert description.cpu_limit is None
    assert (description.raw_cpu_quota, description.raw_cpu_set_size) == (None, None)
    assert notice_codes('cpu') == ('machine-cpu-count-unknown',)


def test_get_cpu_used_ratio(fake_cgroup: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Measures the consumed CPU time across the interval, against the cores the process may use."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    # The counter advances by one core-second during the measurement interval.
    fake_time(monkeypatch, sleep=lambda _seconds: (root / 'cpu.stat').write_text('usage_usec 1000000\n'))

    # One core-second over one second of wall time, out of the two cores the quota allows.
    assert cgroups_sensor.get_cpu_used_ratio() == pytest.approx(0.5)


def test_get_cpu_used_ratio_counter_restart(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clamps the counter that restarts when a cgroup of this path is created anew."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 5000000\n'},
    )
    # A restarted unit takes the path of the one before it, and its counter starts at zero.
    fake_time(monkeypatch, sleep=lambda _seconds: (root / 'cpu.stat').write_text('usage_usec 0\n'))

    assert cgroups_sensor.get_cpu_used_ratio() == 0.0


def test_get_cpu_used_ratio_no_limit(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing when the CPU of this process is unrestricted - measuring the machine is not its job."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': 'max 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )

    assert cgroups_sensor.get_cpu_used_ratio() is None


def test_get_cpu_used_ratio_no_counter(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing when a limit is found but no consumed CPU time can be measured against it."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n'},
    )

    assert cgroups_sensor.get_cpu_used_ratio() is None


def test_notices_a_mount_that_hides_the_ancestors(fake_cgroup: Callable[..., Path]) -> None:
    """Says so when a mount exposes a subtree, because a limit above it is enforced and never read."""
    fake_cgroup(
        # The mount root is the cgroup of this process, so `/tenant` above it exists and cannot be reached.
        mountinfo='30 25 0:26 /tenant/app {root}/memory rw,nosuid shared:14 - cgroup cgroup rw,memory',
        self_cgroup='2:memory:/tenant/app\n',
        files={
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 0\n',
        },
    )

    assert 'memory-ancestors-hidden' in notice_codes('memory')
    assert 'cpu-ancestors-hidden' not in notice_codes('cpu')


def test_no_notice_when_the_mount_covers_the_whole_hierarchy(fake_cgroup: Callable[..., Path]) -> None:
    """Stays silent where the mount root is the hierarchy root, which is every ordinary machine."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod'),
        files={'pod/memory.max': '1000\n', 'pod/memory.current': '100\n', 'pod/memory.stat': 'inactive_file 0\n'},
    )

    assert 'memory-ancestors-hidden' not in notice_codes()


def test_get_cpu_usage_hybrid_with_a_stray_controller(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the time through the interface the limits came from, not through a stray controller's."""
    root = fake_cgroup(
        mountinfo=HYBRID_MOUNTINFO,
        self_cgroup=HYBRID_SELF_CGROUP,
        files=HYBRID_STRAY_FILES,
    )

    assert cgroups_sensor.get_cpu_usage() == pytest.approx(3.0)

    description = cgroups_sensor.describe()
    assert description.cpu_usage_source is not None
    assert description.cpu_usage_source.interface is cgroups_sensor.Interface.CGROUP_V1
    # The rate is measurable only where the quota binds, and the comount carries the counter right there.
    assert description.cpu_rate_level == str(root / 'cpu,cpuacct' / 'docker' / 'abc')


def test_get_cpu_used_ratio_hybrid_with_a_stray_controller(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measures the rate against the cgroup v1 quota instead of reporting a scope mismatch."""
    root = fake_cgroup(
        mountinfo=HYBRID_MOUNTINFO,
        self_cgroup=HYBRID_SELF_CGROUP,
        files=HYBRID_STRAY_FILES,
    )

    def advance(_seconds: float) -> None:
        # A quarter of a CPU second over one second of the clock, against half a core: half the allowance.
        (root / 'cpu,cpuacct' / 'docker' / 'abc' / 'cpuacct.usage').write_text('3250000000\n')

    fake_time(monkeypatch, sleep=advance)

    assert cgroups_sensor.get_cpu_used_ratio() == pytest.approx(0.5)
    assert notice_codes('cpu') == ()


def test_get_cpu_used_ratio_second_reading_fails(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports nothing when the counter disappears mid-measurement instead of comparing across the gap."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    fake_time(monkeypatch, sleep=lambda _seconds: (root / 'cpu.stat').unlink())

    assert cgroups_sensor.get_cpu_used_ratio() is None


def test_get_cpu_used_ratio_clamped_to_one(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clamps a burst above the quota to 1, so the promised range holds despite clock skew."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    # Three core-seconds within a one-second window exceed the two cores the quota allows.
    fake_time(monkeypatch, sleep=lambda _seconds: (root / 'cpu.stat').write_text('usage_usec 3000000\n'))

    assert cgroups_sensor.get_cpu_used_ratio() == 1.0


def test_get_cpu_used_ratio_empty_window(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports nothing when the clock does not advance across the interval, instead of dividing by zero."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    monkeypatch.setattr(_sensor, 'time', SimpleNamespace(monotonic=lambda: 100.0, sleep=lambda _seconds: None))

    assert cgroups_sensor.get_cpu_used_ratio() is None


def test_get_cpu_used_ratio_measures_the_given_interval(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waits for the interval it was given, not for a fixed one."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    slept: list[float] = []
    fake_time(monkeypatch, sleep=slept.append)

    cgroups_sensor.get_cpu_used_ratio(interval=0.25)

    assert slept == [0.25]


def test_get_cpu_used_ratio_async_measures_the_given_interval(fake_cgroup: Callable[..., Path]) -> None:
    """Waits for the interval it was given, as the blocking variant does."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio, 'sleep', sleep)
        asyncio.run(cgroups_sensor.get_cpu_used_ratio_async(interval=0.25))

    assert slept == [0.25]


@pytest.mark.usefixtures('_no_cgroup')
@pytest.mark.parametrize('interval', [0, -1.0, 0.005])
def test_get_cpu_used_ratio_invalid_interval(interval: float) -> None:
    """Rejects a window shorter than the counter can resolve, instead of sleeping and reporting nothing."""
    with pytest.raises(ValueError, match='interval must be at least'):
        cgroups_sensor.get_cpu_used_ratio(interval=interval)


def test_get_cpu_used_ratio_async(fake_cgroup: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Measures the same ratio as the blocking variant, waiting with `asyncio.sleep` instead."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )

    async def sleep(_seconds: float) -> None:
        (root / 'cpu.stat').write_text('usage_usec 1000000\n')

    clock = count(start=100.0, step=1.0)
    monkeypatch.setattr(_sensor, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(asyncio, 'sleep', sleep)

    assert asyncio.run(cgroups_sensor.get_cpu_used_ratio_async()) == pytest.approx(0.5)


def test_get_cpu_used_ratio_async_no_limit(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing when the CPU is unrestricted, same as the blocking variant."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': 'max 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )

    assert asyncio.run(cgroups_sensor.get_cpu_used_ratio_async()) is None


@pytest.mark.usefixtures('_no_cgroup')
def test_get_cpu_used_ratio_async_invalid_interval() -> None:
    """Rejects an unmeasurable window, same as the blocking variant."""
    with pytest.raises(ValueError, match='interval must be at least'):
        asyncio.run(cgroups_sensor.get_cpu_used_ratio_async(interval=0.005))


def test_snapshot(fake_cgroup: Callable[..., Path]) -> None:
    """Takes every reading at once."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '536870912\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
            'cpu.max': '200000 100000\n',
            'cpu.stat': 'usage_usec 2500000\n',
            'cpuset.cpus.effective': '0-3\n',
        },
    )

    assert cgroups_sensor.snapshot() == cgroups_sensor.Snapshot(
        memory_budget=cgroups_sensor.MemoryBudget(limit=536870912, used=100000000, available=436870912),
        cpu_limit=2.0,
        cpu_usage=2.5,
    )


@pytest.mark.usefixtures('_no_cgroup')
def test_snapshot_no_mechanism() -> None:
    """Reports an all-empty snapshot, without raising, on a system that has no cgroups."""
    assert cgroups_sensor.snapshot() == cgroups_sensor.Snapshot(memory_budget=None, cpu_limit=None, cpu_usage=None)


def test_snapshot_v1(fake_cgroup: Callable[..., Path]) -> None:
    """Reads every metric through the cgroup v1 controllers, spread over the mounts that carry them."""
    fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'memory/memory.limit_in_bytes': '536870912\n',
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 400\n',
            'cpu,cpuacct/cpu.cfs_quota_us': '150000\n',
            'cpu,cpuacct/cpu.cfs_period_us': '100000\n',
            'cpu,cpuacct/cpuacct.usage': '2500000000\n',
            'cpuset/cpuset.cpus': '0-1\n',
        },
    )

    assert cgroups_sensor.snapshot() == cgroups_sensor.Snapshot(
        memory_budget=cgroups_sensor.MemoryBudget(limit=536870912, used=600, available=536870312),
        cpu_limit=1.5,
        cpu_usage=2.5,
    )

    description = cgroups_sensor.describe()
    for source in (
        description.memory_source,
        description.cpu_quota_source,
        description.cpu_set_source,
        description.cpu_usage_source,
    ):
        assert source is not None
        assert source.interface is cgroups_sensor.Interface.CGROUP_V1


def test_describe(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the source of every metric, the raw values before filtering, and the machine facts."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '536870912\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
            'cpu.max': '1000000 100000\n',
            'cpu.stat': 'usage_usec 2500000\n',
            'cpuset.cpus.effective': '0-3\n',
        },
    )

    description = cgroups_sensor.describe()

    v2 = cgroups_sensor.Interface.CGROUP_V2

    # The readings themselves, so that one dump of this is a complete answer.
    assert description.memory_budget == cgroups_sensor.MemoryBudget(
        limit=536870912, used=100000000, available=436870912
    )
    assert description.cpu_limit == 4.0
    assert description.memory_source == cgroups_sensor.Source(interface=v2, levels=(str(root),))
    assert description.cpu_quota_source == cgroups_sensor.Source(interface=v2, levels=(str(root),))
    assert description.cpu_set_source == cgroups_sensor.Source(interface=v2, levels=(str(root),))
    assert description.cpu_usage_source == cgroups_sensor.Source(interface=v2, levels=(str(root),))
    assert description.raw_memory_limit == 536870912
    assert description.raw_memory_used == 100000000
    assert description.memory_limit_level == str(root)
    # The set of four cores is the effective limit here, and its time is counted in the own cgroup.
    assert description.cpu_limit_level == str(root)
    # The quota of ten cores exceeds the machine, so it is visible here and filtered from `get_cpu_limit`.
    assert description.raw_cpu_quota == 10.0
    assert description.raw_cpu_set_size == 4
    assert description.machine_memory_bytes == MACHINE_TOTAL_BYTES
    assert description.machine_cpu_count == MACHINE_CORES
    assert [notice.code for notice in description.notices] == ['cpu-quota-covers-machine']


def test_describe_hybrid_interfaces(fake_cgroup: Callable[..., Path]) -> None:
    """Names the interface per metric, because a hybrid layout serves different metrics from different versions."""
    fake_cgroup(
        mountinfo=f'{V2_MOUNTINFO}\n{V1_MOUNTINFO}',
        self_cgroup=f'{V2_SELF_CGROUP.format(path="/")}2:memory:/\n',
        files={
            'memory/memory.limit_in_bytes': '536870912\n',
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 400\n',
        },
    )

    description = cgroups_sensor.describe()

    assert description.memory_source is not None
    assert description.memory_source.interface is cgroups_sensor.Interface.CGROUP_V1
    # Nothing binds a CPU controller here, so the source searches no levels rather than going missing. It is
    # named cgroup v1 like the memory beside it, the unified mount of a hybrid machine carrying nothing.
    assert description.cpu_quota_source == cgroups_sensor.Source(
        interface=cgroups_sensor.Interface.CGROUP_V1, levels=()
    )
    assert [notice.code for notice in description.notices] == []


def test_describe_ignores_a_stray_controller_when_naming_an_absent_metric(
    fake_cgroup: Callable[..., Path],
) -> None:
    """Names cgroup v1 for a metric nothing carries, a stray controller on the cgroup2 mount notwithstanding."""
    fake_cgroup(mountinfo=HYBRID_MOUNTINFO, self_cgroup=HYBRID_SELF_CGROUP, files=HYBRID_STRAY_FILES)

    description = cgroups_sensor.describe()

    # `hugetlb` rides the unified mount and none of the metrics here can: a controller is bound to one
    # hierarchy at a time, so this machine would serve its memory through cgroup v1 or not at all.
    assert description.memory_source == cgroups_sensor.Source(interface=cgroups_sensor.Interface.CGROUP_V1, levels=())


def test_describe_names_cgroup_v2_for_an_absent_metric_where_it_binds_the_others(
    fake_cgroup: Callable[..., Path],
) -> None:
    """Names the unified hierarchy where it binds the controllers this package reads, cgroup v1 mounts and all."""
    fake_cgroup(
        mountinfo=HYBRID_MOUNTINFO,
        self_cgroup=HYBRID_SELF_CGROUP,
        files={
            # The memory controller moved to the unified hierarchy, so a cpuset would appear there too.
            'unified/cgroup.controllers': 'memory\n',
            'unified/system.slice/docker.service/memory.max': '536870912\n',
            'unified/system.slice/docker.service/memory.current': '1000\n',
            'unified/system.slice/docker.service/memory.stat': 'inactive_file 400\n',
            'cpu,cpuacct/docker/abc/cpu.cfs_quota_us': '50000\n',
            'cpu,cpuacct/docker/abc/cpu.cfs_period_us': '100000\n',
        },
    )

    description = cgroups_sensor.describe()

    assert description.memory_source is not None
    assert description.memory_source.interface is cgroups_sensor.Interface.CGROUP_V2
    assert description.cpu_set_source == cgroups_sensor.Source(interface=cgroups_sensor.Interface.CGROUP_V2, levels=())


def test_describe_names_the_level_the_memory_limit_came_from(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level holding the limit, which the chain of searched levels does not say."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '536870912\n',
            'pod/container/memory.current': '1000\n',
            'pod/container/memory.stat': 'inactive_file 400\n',
            # The tighter limit sits on the pod above.
            'pod/memory.max': '268435456\n',
            'pod/memory.current': '2000\n',
            'pod/memory.stat': 'inactive_file 400\n',
        },
    )

    description = cgroups_sensor.describe()

    assert description.memory_source is not None
    assert description.memory_source.levels == (
        str(root / 'pod' / 'container'),
        str(root / 'pod'),
        str(root),
    )
    assert description.memory_limit_level == str(root / 'pod')


def test_describe_names_the_level_the_cpu_limit_came_from(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level the quota binds on, which is also where a rate has to be measured."""
    root = loaded_slice(fake_cgroup, own_usec=0, slice_usec=0)

    description = cgroups_sensor.describe()

    assert description.cpu_limit_level == str(root / 'bench.slice')
    # One hierarchy carries both, so the two coincide here. The test below is where they do not.
    assert description.cpu_rate_level == str(root / 'bench.slice')


def test_describe_names_both_cpu_levels_across_split_hierarchies(fake_cgroup: Callable[..., Path]) -> None:
    """Tells the level holding the limit from the level counting its time, which cgroup v1 can keep apart."""
    root = fake_cgroup(
        mountinfo=V1_CPU_SPLIT_MOUNTINFO,
        self_cgroup='3:cpu:/slice\n4:cpuacct:/slice\n',
        files={
            'cpu/slice/cpu.cfs_quota_us': '50000\n',
            'cpu/slice/cpu.cfs_period_us': '100000\n',
            'cpuacct/slice/cpuacct.usage': '1000000000\n',
        },
    )

    description = cgroups_sensor.describe()

    assert description.cpu_limit == 0.5
    assert description.cpu_limit_level == str(root / 'cpu' / 'slice')
    assert description.cpu_rate_level == str(root / 'cpuacct' / 'slice')


@pytest.mark.parametrize(
    ('member', 'expected'),
    [
        pytest.param(cgroups_sensor.NoticeCode.MEMORY_LIMIT_COVERS_MACHINE, 'memory-limit-covers-machine', id='notice'),
        pytest.param(cgroups_sensor.Interface.CGROUP_V2, 'cgroup-v2', id='interface'),
    ],
)
def test_enum_members_print_as_their_strings(member: str, expected: str) -> None:
    """Prints as the string it carries, which is what a log line shows and what a payload carries."""
    assert str(member) == expected
    assert f'{member}' == expected
    assert member == expected
    assert json.dumps(member) == f'"{expected}"'


@pytest.mark.parametrize(
    ('cores', 'expected'),
    [
        pytest.param(64.0, '64', id='a whole number of cores'),
        pytest.param(0.5, '0.5', id='half a core'),
        pytest.param(1.5, '1.5', id='one and a half'),
    ],
)
def test_spell_cores(cores: float, expected: str) -> None:
    """Writes a whole number of cores without a fraction, and a fractional quota as it is."""
    assert _sensor._spell_cores(cores) == expected


@pytest.mark.usefixtures('_no_cgroup')
def test_covers_machine_notice_spells_the_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Says "64 allowed cores" in the message a consumer reads, not "64.0" - a set has no fractional size."""
    counter = '/sys/fs/cgroup'
    cpu_set = _cgroup.RawCpuSet(cores=64, limit_level=counter, usage_level=counter)
    monkeypatch.setattr(_cgroup, 'read_cpu_quota', lambda: None)
    monkeypatch.setattr(_cgroup, 'read_cpu_set_size', lambda: cpu_set)

    (notice,) = [n for n in cgroups_sensor.describe().notices if str(n.code) == 'cpu-set-covers-machine']

    assert 'The set of 64 allowed cores' in notice.message


def test_every_notice_code_names_its_metric() -> None:
    """Names one metric in every code, which is the only thing telling a memory notice from a CPU one."""
    metrics = {
        str(code): [metric for metric, prefixes in NOTICE_PREFIXES.items() if str(code).startswith(prefixes)]
        for code in cgroups_sensor.NoticeCode
    }

    assert all(len(found) == 1 for found in metrics.values()), metrics


@pytest.mark.usefixtures('_no_cgroup')
def test_describe_no_mechanism() -> None:
    """Reports no sources when no mechanism exists at all, and one notice per metric saying so."""
    description = cgroups_sensor.describe()

    assert description.memory_source is None
    assert description.cpu_quota_source is None
    assert description.cpu_set_source is None
    assert description.cpu_usage_source is None
    assert description.raw_memory_limit is None
    assert notice_codes() == ('memory-metrics-unavailable', 'cpu-metrics-unavailable')


def test_clear_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_cgroup: Callable[..., Path]) -> None:
    """Forgets the discovered sources, so a process moved to another group stops reading the old directories."""
    monkeypatch.setattr(_cgroup, '_PROC_SELF_MOUNTINFO', tmp_path / 'missing')
    monkeypatch.setattr(_cgroup, '_PROC_SELF_CGROUP', tmp_path / 'missing')
    assert cgroups_sensor.get_memory_budget() is None

    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '536870912\n',
            'memory.current': '150000000\n',
            'memory.stat': 'inactive_file 50000000\n',
        },
    )

    # The stale discovery still reports nothing until the cache is dropped.
    assert cgroups_sensor.get_memory_budget() is None

    cgroups_sensor.clear_cache()

    assert cgroups_sensor.get_memory_budget() == cgroups_sensor.MemoryBudget(
        limit=536870912, used=100000000, available=436870912
    )


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        pytest.param('MemFree:  100 kB\nMemTotal:       8054932 kB\n', 8054932 * 1024, id='the usual spelling'),
        pytest.param('MemFree:  100 kB\n', None, id='no MemTotal line'),
        pytest.param('MemTotal:       garbage kB\n', None, id='unparsable value'),
        pytest.param('MemTotal:\n', None, id='empty value'),
    ],
)
def test_machine_memory_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected: int | None,
) -> None:
    """Parses the machine memory out of `/proc/meminfo`, and reports nothing rather than raising on any other."""
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text(content)
    monkeypatch.setattr(_cgroup, '_PROC_MEMINFO', meminfo)

    assert real_machine_memory_bytes() == expected


def test_machine_memory_bytes_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports nothing on a system without `/proc/meminfo`."""
    monkeypatch.setattr(_cgroup, '_PROC_MEMINFO', tmp_path / 'missing')

    assert real_machine_memory_bytes() is None


@pytest.mark.parametrize(
    ('online', 'expected'),
    [
        pytest.param('0-15\n', 16, id='a range'),
        pytest.param('0-3,8-11\n', 8, id='ranges with a gap'),
        pytest.param('0\n', 1, id='a single core'),
    ],
)
def test_machine_cpu_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, online: str, expected: int) -> None:
    """Counts the cores the kernel lists as online, whatever the process is allowed to run on."""
    listing = tmp_path / 'online'
    listing.write_text(online)
    monkeypatch.setattr(_cgroup, '_SYS_CPU_ONLINE', listing)
    # Numbers no case expects, so that a count taken from a fallback instead of the listing fails here.
    # `raising=False`, because Windows has no `os.sysconf` to replace.
    monkeypatch.setattr(os, 'sysconf', lambda _name: 99, raising=False)
    monkeypatch.setattr(os, 'cpu_count', lambda: 98)

    assert real_machine_cpu_count() == expected


def test_machine_cpu_count_ignores_process_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls back to `sysconf` where the kernel lists nothing, and not to the `PYTHON_CPU_COUNT` override."""
    monkeypatch.setattr(_cgroup, '_SYS_CPU_ONLINE', tmp_path / 'missing')
    monkeypatch.setattr(os, 'sysconf', lambda _name: 16, raising=False)
    monkeypatch.setattr(os, 'cpu_count', lambda: 2)

    assert real_machine_cpu_count() == 16


@pytest.mark.parametrize(
    'sysconf_result',
    [
        pytest.param(-1, id='the machine count is unavailable'),
        pytest.param(ValueError('unknown name'), id='the name is not configured'),
    ],
)
def test_machine_cpu_count_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sysconf_result: int | Exception,
) -> None:
    """Falls back to `os.cpu_count()` where neither the kernel nor `sysconf` can answer."""

    def sysconf(_name: str) -> int:
        if isinstance(sysconf_result, Exception):
            raise sysconf_result
        return sysconf_result

    monkeypatch.setattr(_cgroup, '_SYS_CPU_ONLINE', tmp_path / 'missing')
    monkeypatch.setattr(os, 'sysconf', sysconf, raising=False)
    monkeypatch.setattr(os, 'cpu_count', lambda: 2)

    assert real_machine_cpu_count() == 2


def loaded_slice(fake_cgroup: Callable[..., Path], *, own_usec: int, slice_usec: int) -> Path:
    """Lay out a slice whose quota binds above this process, with the load sitting at that level.

    This is the shape a `CPUQuota=` slice with several busy units in it produces: the process of interest is
    nearly idle, while the level the quota throttles is saturated.

    The counter of a level counts its whole subtree, so `slice_usec` has to include `own_usec`.
    """
    return fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/bench.slice/own.service'),
        files={
            'bench.slice/own.service/cpu.max': 'max 100000\n',
            'bench.slice/own.service/cpu.stat': f'usage_usec {own_usec}\n',
            'bench.slice/cpu.max': '50000 100000\n',
            'bench.slice/cpu.stat': f'usage_usec {slice_usec}\n',
        },
    )


def test_get_cpu_used_ratio_measures_where_the_quota_binds(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measures the level the quota throttles, not the idle process inside it."""
    # The service has burned every microsecond the slice counts so far, the other units in it being idle.
    root = loaded_slice(fake_cgroup, own_usec=7100, slice_usec=7100)

    def sleep(_seconds: float) -> None:
        # The process adds another 7100 microseconds; the busy units beside it take the slice half a second on.
        (root / 'bench.slice' / 'own.service' / 'cpu.stat').write_text('usage_usec 14200\n')
        (root / 'bench.slice' / 'cpu.stat').write_text('usage_usec 507100\n')

    fake_time(monkeypatch, sleep=sleep)

    assert cgroups_sensor.get_cpu_used_ratio() == pytest.approx(1.0)


def test_cpu_load_first_sample(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing on the first call, because a rate needs an earlier reading."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )

    assert cgroups_sensor.CpuLoad().sample() is None


def test_cpu_load_measures_between_calls(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measures across the time between two calls."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    fake_time(monkeypatch, sleep=lambda _seconds: None)
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None

    # One core-second over the one second the fake clock advances, out of the two the quota allows.
    (root / 'cpu.stat').write_text('usage_usec 1000000\n')

    assert load.sample() == pytest.approx(0.5)


def test_cpu_load_counter_restart(fake_cgroup: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamps the counter that restarts when a cgroup of this path is created anew."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 5000000\n'},
    )
    fake_time(monkeypatch, sleep=lambda _seconds: None)
    load = cgroups_sensor.CpuLoad()
    load.sample()

    (root / 'cpu.stat').write_text('usage_usec 0\n')

    assert load.sample() == 0.0


def test_cpu_load_keeps_the_previous_reading(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps the earlier reading when one fails, because a counter that only grows stays comparable."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    fake_time(monkeypatch, sleep=lambda _seconds: None)
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None

    (root / 'cpu.stat').unlink()
    assert load.sample() is None

    # Four core-seconds against the two the quota allows over that window, so the ratio comes back clamped.
    # The failed read took no clock reading, so the window is the one second between the samples that worked.
    (root / 'cpu.stat').write_text('usage_usec 4000000\n')
    assert load.sample() == pytest.approx(1.0)


def test_cpu_load_when_nothing_restricts_the_cpu(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing where no limit applies, as the one-shot call does."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': 'max 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None
    assert load.sample() is None


def test_no_rate_without_a_counter_for_the_limit(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports no rate at all when the level the limit applies to counts no CPU time."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    quota = _cgroup.RawCpuQuota(cores=2.0, limit_level='/sys/fs/cgroup', usage_level=None)
    monkeypatch.setattr(_cgroup, 'read_cpu_quota', lambda: quota)
    monkeypatch.setattr(_cgroup, 'read_cpu_set_size', lambda: None)

    # The limit itself still applies and is still reported.
    assert cgroups_sensor.get_cpu_limit() == 2.0
    assert notice_codes('cpu') == ('cpu-usage-scope-mismatch',)

    assert cgroups_sensor.get_cpu_used_ratio() is None
    assert cgroups_sensor.CpuLoad().sample() is None


def test_cpu_load_when_the_limit_moves_to_another_level(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports nothing for the sample across a limit that moved to another level."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.max': '200000 100000\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpu.max': 'max 100000\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )
    fake_time(monkeypatch, sleep=lambda _seconds: None)
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None

    # A tighter quota appears on the pod, so the limit now binds there and the counter to read moves with it.
    (root / 'pod' / 'cpu.max').write_text('50000 100000\n')

    assert load.sample() is None

    # The reading taken at the pod becomes the baseline, and the next sample measures against it.
    (root / 'pod' / 'cpu.stat').write_text('usage_usec 500000\n')

    assert load.sample() == pytest.approx(1.0)


class _CountingLock:
    """Stands in for the sampler's lock and records that it was taken."""

    def __init__(self, lock: threading.Lock) -> None:
        self.lock = lock
        self.taken = 0

    def __enter__(self) -> None:
        self.taken += 1
        self.lock.acquire()

    def __exit__(self, *_exception: object) -> None:
        self.lock.release()


def test_cpu_load_swaps_the_previous_reading_under_its_lock(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaces the kept reading inside the lock, which is what the promise of thread safety rests on."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    load = cgroups_sensor.CpuLoad()
    # A race cannot be reproduced on demand, so what is counted here is the discipline that prevents it.
    counting = _CountingLock(load._lock)
    monkeypatch.setattr(load, '_lock', counting)

    load.sample()
    load.sample()

    assert counting.taken == 2


def test_cpu_load_sampled_from_several_threads(fake_cgroup: Callable[..., Path]) -> None:
    """Answers every caller without deadlocking or raising when several threads share one sampler."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    load = cgroups_sensor.CpuLoad()
    samples: list[float | None] = []

    def sample_repeatedly() -> None:
        samples.extend(load.sample() for _ in range(50))

    threads = [threading.Thread(target=sample_repeatedly) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not [thread for thread in threads if thread.is_alive()]
    assert len(samples) == 8 * 50
    assert all(sample is None or 0.0 <= sample <= 1.0 for sample in samples)


def test_cpu_load_when_the_limit_moves_without_changing_value(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports nothing for a limit that moved level while keeping its value, as kubelet's pod and container do."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.max': '50000 100000\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpu.max': 'max 100000\n',
            # The pod has been running its other containers all along.
            'pod/cpu.stat': 'usage_usec 900000000\n',
        },
    )
    fake_time(monkeypatch, sleep=lambda _seconds: None)
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None

    # The same half core, one level up. Comparing the pod's counter against the container's baseline would
    # report a saturated process out of nothing.
    (root / 'pod' / 'container' / 'cpu.max').write_text('max 100000\n')
    (root / 'pod' / 'cpu.max').write_text('50000 100000\n')

    assert load.sample() is None


def test_cpu_load_when_the_limit_changes_in_place(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports nothing for the sample across a limit that changed value."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '400000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    fake_time(monkeypatch, sleep=lambda _seconds: None)
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None

    # Four cores become one, and the counter keeps growing across the change.
    (root / 'cpu.max').write_text('100000 100000\n')
    (root / 'cpu.stat').write_text('usage_usec 1000000\n')

    assert load.sample() is None

    # The reading taken under the new limit becomes the baseline.
    (root / 'cpu.stat').write_text('usage_usec 2000000\n')

    assert load.sample() == pytest.approx(1.0)


def test_get_cpu_used_ratio_returns_at_once_without_a_limit(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waits for nothing when there is nothing to measure, which a loop around it has to expect."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': 'max 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    slept: list[float] = []
    fake_time(monkeypatch, sleep=slept.append)

    assert cgroups_sensor.get_cpu_used_ratio(interval=5.0) is None
    assert slept == []


def test_cpu_load_two_samples_too_close(fake_cgroup: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports nothing for a window shorter than the counter's own resolution."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 5000000\n'},
    )
    clock = count(start=100.0, step=0.001)
    monkeypatch.setattr(_sensor, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _s: None))
    load = cgroups_sensor.CpuLoad()

    assert load.sample() is None
    assert load.sample() is None


def test_nothing_raises_on_unreadable_files(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing rather than raising when every control file holds something unreadable, and says so."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': 'not a number\n',
            'memory.current': '\n',
            'memory.stat': 'inactive_file\n',
            'cpu.max': 'one two three\n',
            'cpu.stat': 'usage_usec not-a-number\n',
            'cpuset.cpus.effective': '9-1\n',
        },
    )

    assert cgroups_sensor.snapshot() == cgroups_sensor.Snapshot(
        memory_budget=None,
        cpu_limit=None,
        cpu_usage=None,
    )
    assert cgroups_sensor.get_cpu_used_ratio(interval=0.01) is None
    assert cgroups_sensor.CpuLoad().sample() is None
    assert notice_codes() == ('memory-limit-unreadable', 'cpu-limit-unreadable')


def test_nothing_raises_on_directories_instead_of_files(
    fake_cgroup: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reports nothing rather than raising when a `/proc` file is not a file at all."""
    fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files={})
    for name in ('_PROC_SELF_MOUNTINFO', '_PROC_SELF_CGROUP'):
        monkeypatch.setattr(_cgroup, name, tmp_path)

    assert cgroups_sensor.snapshot() == cgroups_sensor.Snapshot(
        memory_budget=None,
        cpu_limit=None,
        cpu_usage=None,
    )
    assert cgroups_sensor.describe().memory_source is None


def test_memory_budget_used_ratio() -> None:
    """Reports the share of the limit in use, which a consumer would otherwise compute."""
    budget = cgroups_sensor.MemoryBudget(limit=1000, used=250, available=750)

    assert budget.used_ratio == 0.25


def test_memory_budget_of_zero() -> None:
    """Calls a group that may hold no memory fully used, rather than dividing by its limit."""
    budget = cgroups_sensor.MemoryBudget(limit=0, used=0, available=0)

    assert budget.used_ratio == 1.0
