from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from ._cpu_list import count_cpu_list
from ._types import (
    Interface,
    Notice,
    NoticeCode,
    RawCpu,
    RawCpuQuota,
    RawCpuSet,
    RawMemory,
    Source,
    Sources,
)

_PROC_SELF_CGROUP = Path('/proc/self/cgroup')
"""Lists the cgroup this process belongs to. One line per mounted hierarchy."""

_PROC_SELF_MOUNTINFO = Path('/proc/self/mountinfo')
"""Lists the mounted filesystems. Read to locate the hierarchies instead of assuming `/sys/fs/cgroup`."""

_PROC_MEMINFO = Path('/proc/meminfo')
"""Holds the total memory of the machine."""

_SYS_CPU_ONLINE = Path('/sys/devices/system/cpu/online')
"""Lists the cores of the machine that are online, e.g. `0-3`."""

_MICROSECONDS_PER_SECOND = 1_000_000
_NANOSECONDS_PER_SECOND = 1_000_000_000

_CONVENTIONAL_MOUNT_POINT = Path('/sys/fs/cgroup')
"""Where a hierarchy is mounted unless someone chose otherwise. Used only to break a tie."""


@dataclass(frozen=True)
class _FileNames:
    """What one cgroup interface calls the files this module reads.

    The two interfaces differ in the names far more than in the meaning, so the names are tabled here.
    """

    memory_limit: str
    """Holds the hard memory limit."""

    memory_usage: str
    """Holds the memory charged to the cgroup, page cache included."""

    memory_stat: str
    """Holds the breakdown of that memory, as `<key> <value>` lines."""

    inactive_file: str
    """The `memory_stat` key holding the inactive file cache."""

    cpu_quota: str
    """Holds the CPU bandwidth quota."""

    cpu_usage: str
    """Holds the consumed CPU time."""

    cpu_set: str
    """Holds the cores the cgroup may run on."""


_V2 = _FileNames(
    memory_limit='memory.max',
    memory_usage='memory.current',
    memory_stat='memory.stat',
    inactive_file='inactive_file',
    cpu_quota='cpu.max',
    cpu_usage='cpu.stat',
    cpu_set='cpuset.cpus.effective',
)

_V1 = _FileNames(
    memory_limit='memory.limit_in_bytes',
    memory_usage='memory.usage_in_bytes',
    memory_stat='memory.stat',
    inactive_file='total_inactive_file',
    cpu_quota='cpu.cfs_quota_us',
    cpu_usage='cpuacct.usage',
    cpu_set='cpuset.cpus',
)

_V2_UNLIMITED = 'max'
"""How cgroup v2 spells "no limit" in every file that can hold one."""

_V1_CPU_PERIOD = 'cpu.cfs_period_us'
"""The period a cgroup v1 quota is spelled against. cgroup v2 keeps both in one file."""

_V1_CPU_SET_EFFECTIVE = 'cpuset.effective_cpus'
"""What a cgroup v1 cpuset may really run on, inheritance resolved."""

_V2_CPU_CONTROLLER = 'cpu'
"""How `cgroup.controllers` names the controller that limits CPU bandwidth."""

_V2_CONTROLLERS = 'cgroup.controllers'
"""Lists the controllers available in one cgroup v2 group. Read to tell a real unified hierarchy from the
controller-less one a hybrid machine mounts."""


@dataclass(frozen=True)
class _Metric:
    """A metric this module reads, and how each interface says a level carries it."""

    v1_controller: str
    """The cgroup v1 controller carrying the metric. Several can share one mount, as `cpu,cpuacct` usually do."""

    v2_probe: str
    """The file that exists only where the cgroup v2 hierarchy carries the metric."""

    v1_probe: str
    """The file cgroup v1 is probed by. Not always the v2 one renamed: a cpuset is probed by the configured
    set under v1 and by the effective set under v2."""


_MEMORY = _Metric(v1_controller='memory', v2_probe=_V2.memory_usage, v1_probe=_V1.memory_usage)
_CPU_QUOTA = _Metric(v1_controller='cpu', v2_probe=_V2.cpu_quota, v1_probe=_V1.cpu_quota)
_CPU_SET = _Metric(v1_controller='cpuset', v2_probe=_V2.cpu_set, v1_probe=_V1.cpu_set)

_V1_CPU_ACCT = 'cpuacct'
"""The cgroup v1 controller counting consumed CPU time."""

_V1_CONTROLLER_NAMES = frozenset({_V1_CPU_ACCT, *(metric.v1_controller for metric in (_MEMORY, _CPU_QUOTA, _CPU_SET))})
"""The controllers worth recording: the metrics above, plus the one counting consumed CPU time."""


class _UnreadableFileError(Exception):
    """A control file is there, and says nothing this module can use.

    The reader turns it into a reading of nothing plus the name of that level, so a looser ancestor limit is
    never reported in its place.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(f'{directory} holds a control file that says nothing usable')
        self.directory = directory
        """The level that could not be read."""


@dataclass(frozen=True)
class _Mount:
    """One line of the mount table, as far as this module cares."""

    root: str
    """The subtree of the filesystem this mount exposes."""

    point: Path
    """The directory it is mounted at."""

    filesystem: str
    """The kind of filesystem, e.g. `cgroup2`."""

    options: str
    """The options of the filesystem itself. A cgroup v1 mount names its controllers among them."""


@dataclass(frozen=True)
class _Hierarchy:
    """A mounted cgroup hierarchy and the cgroup this process belongs to in it."""

    mount_point: Path
    """The directory the hierarchy is mounted at."""

    mount_root: str
    """The subtree of the hierarchy the mount exposes. Spelled the same way as `own_path`."""

    own_path: str
    """The cgroup this process belongs to, as `/proc/self/cgroup` spells it."""


@dataclass(frozen=True)
class _Hierarchies:
    """The cgroup hierarchies this process belongs to, kept apart by interface.

    Each interface spells its control files differently, so where a controller was found decides how it is
    read.
    """

    unified: _Hierarchy | None
    """The cgroup v2 hierarchy, where one is mounted and covers this process. A single hierarchy serves
    whichever controllers are bound to it."""

    v1: dict[str, _Hierarchy]
    """The cgroup v1 hierarchies, keyed by controller name. Controllers sharing a mount get an entry each."""


@dataclass(frozen=True)
class Controller:
    """A located controller: its interface and the directories holding its control files."""

    is_v2: bool
    """Whether the controller provides the cgroup v2 interface. It spells the control files differently."""

    mount_root: str
    """The subtree of the hierarchy its mount exposes, read from `/proc/self/mountinfo`."""

    dirs: tuple[Path, ...]
    """The cgroup of this process first, then its ancestors up to the mount point.

    A limit on an ancestor caps everything below it.
    """

    @property
    def names(self) -> _FileNames:
        """How this controller's interface spells its files."""
        return _V2 if self.is_v2 else _V1


@dataclass(frozen=True)
class Controllers:
    """The controllers that carry resource metrics, as located for this process."""

    interface: Interface | None
    """The interface holding this process, whatever it carries. `None` where no hierarchy holds it at all.

    Named once for the machine rather than per metric, so that a metric no hierarchy carries is still
    reported against the mechanism that would have carried it. A located controller names its own interface.
    """

    memory: Controller | None
    """Carries the memory limit and the memory charged against it."""

    cpu_quota: Controller | None
    """Carries the CPU bandwidth quota."""

    cpu_usage: Controller | None
    """Carries the consumed CPU time. Under cgroup v1 that is `cpuacct`, a controller of its own."""

    cpu_set: Controller | None
    """Carries the set of cores the cgroup may run on."""


@dataclass(frozen=True)
class _MemoryLevel:
    """One level of the chain that holds a memory limit."""

    limit: int
    """The limit that level holds, in bytes."""

    usage: int | None
    """The working set of that level, in bytes, or `None` when it cannot be read."""

    directory: Path
    """The cgroup the two were read from."""


_NO_MEMORY = RawMemory(
    limit=None,
    used=None,
    available=None,
    limit_level=None,
    unreadable_level=None,
    usage_unreadable_level=None,
)
"""What is reported where no level holds a memory limit at all."""


def read_memory() -> RawMemory:
    """Read the tightest memory limit along the chain, with a usage that keeps the distance to it.

    An out-of-memory kill follows from how much memory can still be allocated, not from a ratio. The smallest
    distance to a limit anywhere along the chain is therefore the quantity carried over, expressed against the
    tightest limit so that a consumer sees one comparable pair.
    """
    controller = locate_controllers().memory
    if controller is None:
        return _NO_MEMORY

    try:
        levels = _read_memory_levels(controller)
    except _UnreadableFileError as unreadable:
        return RawMemory(
            limit=None,
            used=None,
            available=None,
            limit_level=None,
            unreadable_level=str(unreadable.directory),
            usage_unreadable_level=None,
        )

    if not levels:
        return _NO_MEMORY

    tightest = min(levels, key=lambda level: level.limit)

    # An unknown distance may be the smallest one, so the minimum of the rest would promise memory the kernel
    # will not give. Never the other kind of empty pair: a controller is located by its usage file, so a
    # hierarchy without one is never found at all.
    silent = next((level for level in levels if level.usage is None), None)
    if silent is not None:
        return RawMemory(
            limit=tightest.limit,
            used=None,
            available=None,
            limit_level=str(tightest.directory),
            unreadable_level=None,
            usage_unreadable_level=str(silent.directory),
        )

    # Every level, not only the tightest: memory is charged up the whole chain, so an ancestor counts what its
    # other children use, and its distance can be the smaller one.
    distances = [level.limit - level.usage for level in levels if level.usage is not None]

    # A cgroup sits above its limit while the kernel reclaims, and that level's distance is then negative.
    # Nothing can be allocated there, which is what a distance of zero says.
    available = max(min(distances), 0)

    return RawMemory(
        limit=tightest.limit,
        used=tightest.limit - available,
        available=available,
        limit_level=str(tightest.directory),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def _read_memory_levels(controller: Controller) -> list[_MemoryLevel]:
    """Read the limit and the usage of every level along the chain that holds a limit.

    A level with no readable usage is kept: its limit still caps everything below it.

    Raises:
        _UnreadableFileError: If a level holds a limit file that says nothing usable.
    """
    levels = []

    for directory in controller.dirs:
        # Only the hard limit counts. `memory.high` throttles reclaim instead of killing, so a cgroup can sit
        # above it indefinitely.
        limit = _read_limit(directory / controller.names.memory_limit)
        if limit is None:
            continue

        # Zero is a limit, and the tightest one a cgroup can hold. A negative number of bytes is not a limit
        # at all, and no kernel writes one - so it is a file this module cannot use rather than an absent one.
        if limit < 0:
            raise _UnreadableFileError(directory)

        levels.append(_MemoryLevel(limit=limit, usage=_read_working_set(controller, directory), directory=directory))

    return levels


def read_cpu() -> RawCpu:
    """Read both CPU restrictions, and the level that said nothing usable if there was one."""
    try:
        return RawCpu(
            quota=read_cpu_quota(),
            cpu_set=read_cpu_set_size(),
            unreadable_level=None,
            unconvertible_level=None,
        )
    except _UnreadableFileError as unreadable:
        return RawCpu(quota=None, cpu_set=None, unreadable_level=str(unreadable.directory), unconvertible_level=None)


def read_cpu_quota() -> RawCpuQuota | None:
    """Read the tightest CPU bandwidth quota along the chain, and the level it binds at.

    Returns:
        The quota, or `None` when no level sets one. Taken as read: a quota can exceed the cores of the
        machine.

    Raises:
        _UnreadableFileError: If a level holds a quota file that says nothing usable. `read_cpu` turns that
            into a reading of nothing.
    """
    controller = locate_controllers().cpu_quota
    if controller is None:
        return None

    read_quota = _read_cpu_quota_v2 if controller.is_v2 else _read_cpu_quota_v1

    # Levels that set no quota drop out. An unlimited ancestor must not hide a quota set below it.
    quotas = [(quota, directory) for directory in controller.dirs if (quota := read_quota(directory)) is not None]
    if not quotas:
        return None

    cores, directory = min(quotas, key=lambda found: found[0])

    return RawCpuQuota(
        cores=cores,
        limit_level=str(directory),
        usage_level=_cpu_usage_level(directory, controller),
    )


def read_cpu_set_size() -> RawCpuSet | None:
    """Read the set of CPU cores the cgroup of this process may run on.

    Returns:
        The set, or `None` when no level sets one. Taken as read: a set can cover every core of the machine.

    Raises:
        _UnreadableFileError: If the level holds a set that says nothing usable. `read_cpu` turns that into a
            reading of nothing.
    """
    controller = locate_controllers().cpu_set
    if controller is None:
        return None

    # Only the closest level carrying a cpuset file is read. Under cgroup v2 the effective set already
    # accounts for the ancestors, and under cgroup v1 the effective file below does the same.
    directory = _first_with(controller.dirs, controller.names.cpu_set)
    if directory is None:
        return None

    cores = _read_cpu_set(directory, controller)
    if cores is None:
        return None

    # The set restricts the level it was read at, so that is the level whose time it has to be compared
    # against.
    return RawCpuSet(
        cores=cores,
        limit_level=str(directory),
        usage_level=_cpu_usage_level(directory, controller),
    )


def _read_cpu_set(directory: Path, controller: Controller) -> int | None:
    """Count the cores one level allows. `None` when it sets none.

    Raises:
        _UnreadableFileError: If a file is there and holds no list of cores.
    """
    # The effective file first: under cgroup v1 the configured one is empty where the set is inherited, and old
    # kernels have only the configured one.
    names = (controller.names.cpu_set,) if controller.is_v2 else (_V1_CPU_SET_EFFECTIVE, controller.names.cpu_set)

    for name in names:
        cpu_list = _read_control_file(directory / name)
        if cpu_list is None:
            continue

        # An empty file is how cgroup v1 spells an inherited set. The effective file above answers for it.
        if not cpu_list:
            continue

        cores = count_cpu_list(cpu_list)
        if cores is None:
            raise _UnreadableFileError(directory)

        return cores

    return None


def read_cpu_usage(level: str | None = None) -> float | None:
    """Read the CPU time consumed since the cgroup was created, in seconds.

    Args:
        level: The level to read. Defaults to the closest level along the chain that counts any time. A
            level outside that chain is refused.

    Returns:
        The cumulative CPU time. `None` when it cannot be read.
    """
    controller = locate_controllers().cpu_usage
    if controller is None:
        return None

    directory = Path(level) if level is not None else _first_with(controller.dirs, controller.names.cpu_usage)
    if directory is None:
        return None

    # A level outside the located chain belongs to somebody else, and its counter read against a limit found
    # here would be the time of one scope over the limit of another.
    if directory not in controller.dirs:
        return None

    path = directory / controller.names.cpu_usage

    # cgroup v2 keeps the time among other counters, in microseconds. cgroup v1 gives it a file of its own,
    # in nanoseconds.
    if controller.is_v2:
        microseconds = _read_stat_value(path, 'usage_usec')
        return microseconds / _MICROSECONDS_PER_SECOND if microseconds is not None else None

    nanoseconds = _read_counter(path)
    return nanoseconds / _NANOSECONDS_PER_SECOND if nanoseconds is not None else None


@lru_cache(maxsize=1)
def locate_controllers() -> Controllers:
    """Locate the control files carrying the resource metrics of this process.

    Discovery walks `/proc` and is cached until `clear_cache()` forgets it. The control files themselves are
    read again on every sample.
    """
    # Each metric is located on its own, because a machine can serve them through different interfaces.
    try:
        hierarchies = _read_hierarchies()
    except (OSError, ValueError):
        # Not Linux, or `/proc` is not mounted. Nothing to read either way.
        return Controllers(interface=None, memory=None, cpu_quota=None, cpu_usage=None, cpu_set=None)

    return Controllers(
        interface=_interface(hierarchies),
        memory=_locate_controller(hierarchies, _MEMORY),
        cpu_quota=_locate_controller(hierarchies, _CPU_QUOTA),
        cpu_usage=_locate_cpu_usage(hierarchies),
        cpu_set=_locate_controller(hierarchies, _CPU_SET),
    )


def _interface(hierarchies: _Hierarchies) -> Interface | None:
    """Say which interface a metric of this process would come through, bound or not.

    The kernel binds a controller to one hierarchy at a time, so a unified mount holding any this module
    reads is where its metrics live. One holding only controllers it never reads gives way to cgroup v1: a
    hybrid machine parks `hugetlb` there and serves everything else from cgroup v1. With no cgroup v1
    hierarchy holding this process, that mount answers anyway. The names are compared as cgroup v1 spells
    them, which the three that carry metrics share with cgroup v2 - only `cpuacct` is v1's alone, and it
    matches nothing here.
    """
    unified = hierarchies.unified

    if unified is not None and not _V1_CONTROLLER_NAMES.isdisjoint(_bound_controllers(unified)):
        return Interface.CGROUP_V2

    if hierarchies.v1:
        return Interface.CGROUP_V1

    return Interface.CGROUP_V2 if unified is not None else None


def sources() -> Sources:
    """Say which interface serves each metric, and the levels it is looked for in."""
    controllers = locate_controllers()

    return Sources(
        memory=_source(controllers.memory, controllers.interface),
        cpu_quota=_source(controllers.cpu_quota, controllers.interface),
        cpu_set=_source(controllers.cpu_set, controllers.interface),
        cpu_usage=_source(controllers.cpu_usage, controllers.interface),
    )


def _source(controller: Controller | None, interface: Interface | None) -> Source | None:
    """Spell one metric as a source."""
    if controller is None:
        # A hierarchy that carries no such controller searched no levels, and `None` is kept for the machine
        # that has no hierarchy at all - the sensor layer reads that as "nothing here can say".
        return Source(interface=interface, levels=()) if interface is not None else None

    return Source(
        interface=Interface.CGROUP_V2 if controller.is_v2 else Interface.CGROUP_V1,
        levels=tuple(str(directory) for directory in controller.dirs),
    )


def mechanism_notices() -> tuple[Notice, ...]:
    """Name the mounts that expose only a subtree, for memory and for CPU separately.

    A limit above such a mount is enforced by the kernel and cannot be read through it, so what this process
    sees may be looser than what applies. The two are named apart because they can be read through different
    mounts, and only one of them may be truncated.
    """
    controllers = locate_controllers()
    cpu = (controllers.cpu_quota, controllers.cpu_set, controllers.cpu_usage)

    return _truncated_mounts(NoticeCode.MEMORY_ANCESTORS_HIDDEN, (controllers.memory,)) + _truncated_mounts(
        NoticeCode.CPU_ANCESTORS_HIDDEN, cpu
    )


def _truncated_mounts(code: NoticeCode, located: tuple[Controller | None, ...]) -> tuple[Notice, ...]:
    """Name each mount among these controllers that covers only part of its hierarchy."""
    # The last directory is the mount point, where the walk had to stop. The root says how much it covers.
    truncated = {(str(c.dirs[-1]), c.mount_root) for c in located if c is not None and c.mount_root != '/'}

    return tuple(
        Notice(code=code, message=f'{point} exposes only {root}, so a limit above it is enforced but unreadable')
        for point, root in sorted(truncated)
    )


def machine_memory_bytes() -> int | None:
    """Read the total memory of the machine, in bytes.

    Returns:
        The total memory, or `None` when it cannot be read.
    """
    try:
        for line in _PROC_MEMINFO.read_text().splitlines():
            key, _separator, value = line.partition(':')
            if key == 'MemTotal':
                # The value carries a unit, e.g. `8054932 kB`.
                return int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None

    return None


def memory_limit_ceiling() -> int | None:
    """Read the size a memory limit has to reach before it restricts nothing, in bytes.

    The memory of the machine, which is also what `machine_memory_bytes()` answers. A cgroup memory limit caps
    anonymous and file memory, and swap is a limit of its own, so a limit above what the machine holds cannot
    bind.

    Returns:
        The ceiling, or `None` when it cannot be read.
    """
    return machine_memory_bytes()


def machine_cpu_count() -> int | None:
    """Count the online cores of the machine.

    The kernel is asked first, then `sysconf`, then `os.cpu_count()`. The last two can describe the process
    rather than the machine, and are used anyway.

    Returns:
        The online cores, or `None` when no source answers.
    """
    try:
        online = _SYS_CPU_ONLINE.read_text().strip()
    except (OSError, ValueError):
        return _machine_cpu_count_fallback()

    return count_cpu_list(online) or _machine_cpu_count_fallback()


def _machine_cpu_count_fallback() -> int | None:
    """Count the cores where the kernel does not list them, accepting a count that may describe the process.

    An approximate count is taken over none: the limit filter skips a comparison it cannot make, but a
    consumer sizing a pool has nothing to use instead.
    """
    # Under musl `sysconf` reports the affinity of the process, and `os.cpu_count()` does the same there. From
    # Python 3.13 it also honors the `PYTHON_CPU_COUNT` override.
    try:
        count = os.sysconf('SC_NPROCESSORS_ONLN')
    except (AttributeError, OSError, ValueError):
        return os.cpu_count()

    return count if count > 0 else os.cpu_count()


def clear_cache() -> None:
    """Forget the located controllers. The next reading discovers them again."""
    locate_controllers.cache_clear()


# A child of `fork` inherits what its parent discovered. A pre-fork server whose supervisor puts each worker
# into a cgroup of its own would then have every child reading the parent's levels. Discovery is three files.
if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=clear_cache)


def _locate_cpu_usage(hierarchies: _Hierarchies) -> Controller | None:
    """Locate the counter of consumed CPU time. The hierarchy carrying the limits wins.

    A cgroup v2 group has `cpu.stat` whether or not the CPU controller is enabled for it, so its presence says
    nothing about where the metrics live. A hybrid machine mounts a controller-less cgroup2 beside the cgroup
    v1 controllers, naming this process as belonging to another cgroup there. So the unified hierarchy answers
    only where the CPU controller is bound to it, or where nothing else counts anything.
    """
    unified = hierarchies.unified
    with_controllers = unified if unified is not None and _carries_controller(unified, _V2_CPU_CONTROLLER) else None

    counter = _controller_in(with_controllers, probe=_V2.cpu_usage, is_v2=True) or _controller_in(
        hierarchies.v1.get(_V1_CPU_ACCT), probe=_V1.cpu_usage, is_v2=False
    )

    return counter or _controller_in(unified, probe=_V2.cpu_usage, is_v2=True)


def _carries_controller(hierarchy: _Hierarchy, name: str) -> bool:
    """Whether one named controller is available through this cgroup2 mount."""
    return name in _bound_controllers(hierarchy)


def _bound_controllers(hierarchy: _Hierarchy) -> list[str]:
    """Read the controllers available at the top of this cgroup2 mount, which it lists there by name.

    A mount exposing a subtree lists what that cgroup was given rather than what the hierarchy has, so this
    answers for what is readable through this mount. Empty where the file says nothing or cannot be read,
    which is a shape to expect rather than a failure.
    """
    try:
        return (hierarchy.mount_point / _V2_CONTROLLERS).read_text().split()
    except (OSError, ValueError):
        return []


def _cpu_usage_level(directory: Path, source: Controller) -> str | None:
    """Find where the CPU time of one level is counted, given that level in another hierarchy.

    Under cgroup v1 the quota and the accounting can be two hierarchies with two mount points, so the level
    is translated by its path below the mount point. Every chain ends at its own mount point, which is what
    makes that path comparable.
    """
    usage = locate_controllers().cpu_usage
    if usage is None:
        return None

    try:
        relative = directory.relative_to(source.dirs[-1])
    except ValueError:
        return None

    translated = usage.dirs[-1] / relative
    # The translation is textual, so the result has to be a level this process belongs to: two hierarchies can
    # name different cgroups for one process, and a group of the same name there belongs to somebody else.
    if translated not in usage.dirs:
        return None

    return str(translated) if _exists(translated / usage.names.cpu_usage) else None


def _read_working_set(controller: Controller, directory: Path) -> int | None:
    """Read the memory charged to one cgroup, in bytes. Excludes the inactive file cache.

    Subtracting it gives the working set - the figure `docker stats` and `kubectl top` report. The active file
    cache is reclaimable too and stays counted, as it does there.
    """
    current = _read_counter(directory / controller.names.memory_usage)
    if current is None:
        return None

    # No fallback to the raw usage. That would count the inactive file cache as usage.
    inactive_file = _read_stat_value(directory / controller.names.memory_stat, controller.names.inactive_file)
    if inactive_file is None:
        return None

    return max(current - inactive_file, 0)


def _locate_controller(hierarchies: _Hierarchies, metric: _Metric) -> Controller | None:
    """Locate the directories carrying one metric. The cgroup v2 unified hierarchy wins.

    A hybrid system can mount both interfaces with only some controllers on the unified hierarchy, so each
    candidate counts only where the file it is probed by exists.
    """
    return _controller_in(hierarchies.unified, probe=metric.v2_probe, is_v2=True) or _controller_in(
        hierarchies.v1.get(metric.v1_controller), probe=metric.v1_probe, is_v2=False
    )


def _controller_in(hierarchy: _Hierarchy | None, *, probe: str, is_v2: bool) -> Controller | None:
    """Locate one controller in one hierarchy, if that hierarchy carries it at all."""
    if hierarchy is None:
        return None

    # The whole chain, not only the levels carrying the controller today: `systemctl set-property` can enable a
    # controller on a parent at runtime, and discovery happens only once per process.
    dirs = _candidate_dirs(hierarchy)

    if _first_with(dirs, probe) is None:
        return None

    return Controller(is_v2=is_v2, mount_root=hierarchy.mount_root, dirs=dirs)


def _first_with(dirs: tuple[Path, ...], file_name: str) -> Path | None:
    """Find the closest level that carries one file, or `None` when no level does."""
    return next((directory for directory in dirs if _exists(directory / file_name)), None)


def _exists(path: Path) -> bool:
    """Whether a path exists.

    `Path.exists()` raises on a directory this process may not traverse. Only Python 3.14 turns that into
    `False`, and a cgroup chain can hold such a directory.
    """
    try:
        return path.exists()
    except OSError:
        return False


def _candidate_dirs(hierarchy: _Hierarchy) -> tuple[Path, ...]:
    """List the directories a controller's files can be read from. The cgroup of this process first."""
    parts = _own_path_within_mount(hierarchy)

    # The walk starts at the mount point because nothing above it belongs to the hierarchy.
    chain = [hierarchy.mount_point]
    for part in parts:
        chain.append(chain[-1] / part)

    return tuple(reversed(chain))


def _own_path_within_mount(hierarchy: _Hierarchy) -> tuple[str, ...]:
    """Spell the cgroup of this process as the path components below the mount point."""
    path = PurePosixPath(hierarchy.own_path)

    # A mount can expose just a subtree. The paths in `/proc/self/cgroup` then carry the mount root as a
    # prefix, which has to come off.
    if hierarchy.mount_root != '/':
        try:
            path = PurePosixPath('/') / path.relative_to(hierarchy.mount_root)
        except ValueError:
            # The mount does not cover the cgroup of this process. Only the top of the mount is readable.
            path = PurePosixPath('/')

    return path.parts[1:] if path.is_absolute() else path.parts


def _read_hierarchies() -> _Hierarchies:
    """Locate the mounted cgroup hierarchies and the cgroup this process belongs to in each.

    Raises:
        OSError: If `/proc/self/mountinfo` or `/proc/self/cgroup` cannot be read.
    """
    unified_path, controller_paths = _read_own_paths()
    mounts = _read_mounts()

    controllers: dict[str, _Hierarchy] = {}

    for mount in mounts:
        if mount.filesystem != 'cgroup':
            continue

        # A cgroup v1 mount names its controllers among its options, e.g. `rw,cpu,cpuacct`.
        for option in mount.options.split(','):
            own_path = controller_paths.get(option)
            if option not in _V1_CONTROLLER_NAMES or own_path is None:
                continue

            # A controller can be mounted more than once, so the same rule as for the unified hierarchy: a
            # mount that does not expose the cgroup of this process belongs to somebody else. The first mount
            # that does wins, because mounts are listed in the order they were made.
            hierarchy = _Hierarchy(mount_point=mount.point, mount_root=mount.root, own_path=own_path)
            if _covers_own_cgroup(hierarchy):
                controllers.setdefault(option, hierarchy)

    unified = _pick_unified(mounts, unified_path) if unified_path is not None else None

    return _Hierarchies(unified=unified, v1=controllers)


def _pick_unified(mounts: list[_Mount], own_path: str) -> _Hierarchy | None:
    """Pick the cgroup2 mount that exposes the cgroup of this process.

    The same hierarchy can be mounted more than once - an agent watching the machine from inside a container
    bind-mounts the whole of it somewhere else - so a mount counts only where it covers the cgroup of this
    process and that cgroup exists under it.

    Where several qualify, the conventional mount point wins, then the order the mounts were made in.
    """
    hierarchies = (
        _Hierarchy(mount_point=mount.point, mount_root=mount.root, own_path=own_path)
        for mount in mounts
        if mount.filesystem == 'cgroup2'
    )

    covering = [hierarchy for hierarchy in hierarchies if _covers_own_cgroup(hierarchy)]
    if not covering:
        return None

    conventional = [hierarchy for hierarchy in covering if hierarchy.mount_point == _CONVENTIONAL_MOUNT_POINT]

    return (conventional or covering)[0]


def _covers_own_cgroup(hierarchy: _Hierarchy) -> bool:
    """Whether one mount exposes the cgroup of this process, and that cgroup exists under it."""
    mount_root = PurePosixPath(hierarchy.mount_root)

    # A mount of a subtree only covers the cgroups below that subtree.
    if mount_root != PurePosixPath('/') and not PurePosixPath(hierarchy.own_path).is_relative_to(mount_root):
        return False

    return _exists(_candidate_dirs(hierarchy)[0])


def _read_mounts() -> list[_Mount]:
    """Read the mount table, skipping the lines that are not mount entries.

    Raises:
        OSError: If `/proc/self/mountinfo` cannot be read.
    """
    # `errors='replace'` because a foreign mount point with non-UTF-8 bytes must not hide our own cgroup.
    # Such a path decodes to something that matches no file, so only that mount drops out.
    lines = _PROC_SELF_MOUNTINFO.read_text(errors='replace').splitlines()

    return [mount for line in lines if (mount := _parse_mount(line)) is not None]


def _parse_mount(line: str) -> _Mount | None:
    """Read one line of the mount table, or `None` when it is not a mount entry."""
    # A variable number of optional fields sits before the ` - ` separator. Split on it first.
    before, separator, after = line.partition(' - ')
    if not separator:
        return None

    try:
        _mount_id, _parent_id, _device, root, point, *_ = before.split(' ')
        filesystem, _source, options, *_ = after.split(' ')
    except ValueError:
        return None

    # `/proc/self/cgroup` spells the same paths unescaped. Without this the two cannot be compared, and the
    # mount point cannot be opened either.
    return _Mount(root=_unescape(root), point=Path(_unescape(point)), filesystem=filesystem, options=options)


def _read_own_paths() -> tuple[str | None, dict[str, str]]:
    """Read the cgroup this process belongs to in the unified hierarchy and in each cgroup v1 one.

    Raises:
        OSError: If `/proc/self/cgroup` cannot be read.
    """
    unified: str | None = None
    controllers: dict[str, str] = {}

    for line in _PROC_SELF_CGROUP.read_text(errors='replace').splitlines():
        try:
            _hierarchy_id, controller_list, cgroup_path = line.split(':', 2)
        except ValueError:
            continue

        # The unified hierarchy is the entry with no controllers listed, spelled `0::<path>`.
        if not controller_list:
            unified = cgroup_path
            continue

        for controller in controller_list.split(','):
            # A cgroup v1 hierarchy without a controller carries a name instead, e.g. `name=systemd`.
            controllers[controller.removeprefix('name=')] = cgroup_path

    return unified, controllers


def _unescape(field: str) -> str:
    """Decode the octal escapes in a path field of `/proc/self/mountinfo`."""
    # The backslash goes last. Undoing it first would decode `\134040` into a space.
    return field.replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')


def _read_cpu_quota_v2(directory: Path) -> float | None:
    """Read the cores allowed by a cgroup v2 `cpu.max` file, which holds the quota and its period.

    Raises:
        _UnreadableFileError: If the file is there and describes no bandwidth this module can use.
    """
    content = _read_control_file(directory / _V2.cpu_quota)
    if content is None:
        return None

    # An unlimited cgroup spells the quota as `max`, and keeps the period next to it.
    written, _separator, period_written = content.partition(' ')
    if written == _V2_UNLIMITED:
        return None

    try:
        quota, period = int(written), int(period_written)
    except ValueError:
        raise _UnreadableFileError(directory) from None

    # Neither can be zero or negative on a real kernel, and cgroup v2 has `max` for "no quota".
    if quota <= 0 or period <= 0:
        raise _UnreadableFileError(directory)

    return quota / period


def _read_cpu_quota_v1(directory: Path) -> float | None:
    """Read the cores allowed by the cgroup v1 quota and period files.

    Raises:
        _UnreadableFileError: If a file is there and describes no bandwidth this module can use.
    """
    quota = _read_limit(directory / _V1.cpu_quota)
    period = _read_limit(directory / _V1_CPU_PERIOD)
    if quota is None or period is None:
        return None

    # Unlike cgroup v2, this interface has no word for "no quota": it writes a negative number instead.
    if quota < 0:
        return None

    if quota == 0 or period <= 0:
        raise _UnreadableFileError(directory)

    return quota / period


def _read_text(path: Path) -> str | None:
    """Read a control file. `None` when it cannot be read at all."""
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def _read_control_file(path: Path) -> str | None:
    """Read a control file, telling an absent one from an unreadable one. `None` when the level has no such file.

    Raises:
        _UnreadableFileError: If the file is there and cannot be read.
    """
    content = _read_text(path)
    if content is None and _exists(path):
        raise _UnreadableFileError(path.parent)

    return content


def _read_counter(path: Path) -> int | None:
    """Read a counter, best effort. `None` when it cannot be read or says anything but a number.

    A counter has no looser value to fall back to, which is what makes best effort right here and wrong for a
    limit.
    """
    content = _read_text(path)
    if content is None:
        return None

    try:
        return int(content)
    except ValueError:
        return None


def _read_limit(path: Path) -> int | None:
    """Read a file holding a limit. `None` when the file is absent or spells `max`.

    Raises:
        _UnreadableFileError: If the file is there and holds no number this module can use.
    """
    content = _read_control_file(path)
    if content is None or content == _V2_UNLIMITED:
        return None

    try:
        return int(content)
    except ValueError:
        raise _UnreadableFileError(path.parent) from None


def _read_stat_value(path: Path, key: str) -> int | None:
    """Read one entry of a control file holding `<key> <value>` lines."""
    try:
        with path.open() as file:
            for line in file:
                entry_key, _separator, value = line.partition(' ')
                if entry_key == key:
                    return int(value)
    except (OSError, ValueError):
        return None

    return None
