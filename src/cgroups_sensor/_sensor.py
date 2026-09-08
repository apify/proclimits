from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from . import _backend
from ._types import Notice, NoticeCode, RawMemory, Source

_SHORTEST_WINDOW_SECONDS = 0.01
"""Below this a rate says more about the counter's own resolution than about the load."""


@dataclass(frozen=True)
class MemoryBudget:
    """A memory limit that actually restricts this process, with the usage charged against it.

    `available` is what this is for: the memory this process can still allocate, and the number to size a
    budget from.

    The limit can belong to a level above this process - a slice, a pod, an enclosing job. Everything under
    that level is charged against it. `used` and `used_ratio` then answer for that level, not for this process.
    `describe().memory_limit_level` names the level.
    """

    limit: int
    """The tightest limit in bytes. Always below `describe().memory_limit_ceiling`.

    A Windows job limits commit, so that ceiling - and this limit with it - can stand above the memory of the
    machine. Taking the smaller of this and `machine_memory_bytes` there sizes from a limit that does not
    apply.
    """

    used: int
    """The memory charged against the limit, in bytes. What can still be allocated is `available`.

    Which quantity that is belongs to the mechanism. A cgroup counts the memory of the level, inactive file
    cache off - the figure `docker stats` reports. A job object counts the commit charge of the job.

    Where several cgroup levels hold a limit it is derived, so it matches no single file.
    `describe().raw_memory_used` says how. Size from `available`.
    """

    available: int
    """The memory this process can still allocate before something kills it, in bytes.

    Read from the mechanism, never more than the distance to the limit.
    `describe().raw_memory_available` keeps the number that was read.

    It answers for the limit rather than for the machine: a host that is itself out of memory can still refuse
    an allocation this leaves room for.
    """

    @property
    def used_ratio(self) -> float:
        """The share of the limit in use, between 0 and 1.

        Of the limit, not of this process: where the limit belongs to a level above, everything else under
        that level is charged against it too.
        """
        return self.used / self.limit if self.limit > 0 else 1.0


@dataclass(frozen=True)
class Snapshot:
    """Every reading the sensor takes, taken at once.

    Each reading answers on its own. The CPU counter belongs to the closest level that counts any CPU time,
    and the CPU limit can belong to a level above it, so the three are not a set of numbers to combine.
    """

    memory_budget: MemoryBudget | None
    """The memory budget, or `None` where none is reported. `describe()` says why."""

    cpu_limit: float | None
    """The number of usable CPU cores, or `None` where none is reported. `describe()` says why."""

    cpu_usage: float | None
    """The consumed CPU time in seconds, or `None` when it cannot be read.

    Counted at the closest level that counts any. `CpuLoad` and `get_cpu_used_ratio()` are what a rate comes
    from.
    """


@dataclass(frozen=True)
class Description:
    """How the sensor arrived at its readings.

    This is the diagnostic counterpart of `snapshot()`, and it carries the readings themselves, so that one
    dump answers what was reported as well as why.

    A limit of `None` next to no notice about it means the mechanism was there and nothing limited this
    process in a way that kills it. Only hard limits are read.
    """

    memory_budget: MemoryBudget | None
    """What `get_memory_budget()` reports, taken at the same moment as everything below."""

    cpu_limit: float | None
    """What `get_cpu_limit()` reports, taken at the same moment as everything below."""

    cpu_usage: float | None
    """What `get_cpu_usage()` reports, taken at the same moment as everything below.

    Counted at the closest level that counts any. A rate is measured at `cpu_rate_level` instead, so where
    that is another level, this counter and a rate that looks wrong do not describe the same scope. That is
    the first thing to check when they disagree.
    """

    memory_source: Source | None
    """Where the memory limit and usage are read from. `None` where no mechanism is present."""

    cpu_quota_source: Source | None
    """Where the CPU bandwidth quota is read from. `None` where no mechanism is present."""

    cpu_set_source: Source | None
    """Where the set of allowed cores is read from. `None` where no mechanism is present."""

    cpu_usage_source: Source | None
    """Where the consumed CPU time is read from, as a mechanism and not as a level. Which level is counted is
    under `cpu_usage`, and `cpu_rate_level` names where a rate is measured. `None` where no mechanism is
    present."""

    raw_memory_limit: int | None
    """The tightest memory limit before filtering. Sentinels included."""

    raw_memory_used: int | None
    """The usage paired with the raw limit, in bytes.

    A cgroup derives it, so it matches no single file: `raw_memory_limit - raw_memory_used` is the
    smallest distance to a limit along the chain, moved onto that limit, with the inactive file cache
    already off `memory.current`. A job object reports the memory committed by the job outright. `None` where
    any level holding a limit did not answer with a usage, or where the mechanism pairs none with a limit of
    that kind.
    """

    raw_memory_available: int | None
    """The room the mechanism reported before filtering, in bytes.

    `MemoryBudget.available` is this number brought within the distance to the limit. Where the two differ,
    the mechanism answered the three fields from calls taken a moment apart and they disagreed.
    """

    raw_cpu_quota: float | None
    """The CPU quota in cores before filtering. It can exceed the machine."""

    raw_cpu_set_size: int | None
    """The number of allowed cores before filtering. It can cover the whole machine."""

    memory_limit_level: str | None
    """The level holding `raw_memory_limit`. `None` when no level holds one.

    The tightest limit of the chain wins, which is often not the level of this process. Under Kubernetes it
    is regularly the pod, or the `kubepods` slice holding what the node may hand out. The level is named even
    where the filters drop that limit, and a notice then says why.
    """

    cpu_limit_level: str | None
    """The level the reported CPU limit was read at. `None` where no limit is reported, a rejected reading
    included - unlike `memory_limit_level`, which names its level either way.

    Often not the level of this process: a systemd scope carries no quota of its own and the slice above it
    does, and a CPU set on an ancestor restricts everything below it.
    """

    cpu_rate_level: str | None
    """The level whose consumed CPU time belongs to that limit, which is where a rate is measured.

    Under cgroup v2, and on a job object, it is `cpu_limit_level` itself. Under cgroup v1 the accounting is a
    controller of its own and can be mounted elsewhere, so it is the same cgroup under the other mount point.
    `None` where no CPU limit is reported. `None` beside a reported limit means no level counts that time, and
    a notice then says so. No rate can be measured either way.
    """

    machine_memory_bytes: int | None
    """The memory of the machine, which is what a pool is sized from where nothing restricts this process.
    `None` when it cannot be read."""

    memory_limit_ceiling: int | None
    """The size the memory filter compared the limit against. `None` when it cannot be read.

    A limit at or above this restricts nothing and is dropped. It equals `machine_memory_bytes` where the
    mechanism limits the memory of the machine, as a cgroup does, and can stand above it where the mechanism
    limits something the page file enlarges, as a Windows job does.
    """

    machine_cpu_count: int | None
    """The machine core count the filters compared against. `None` when it cannot be read."""

    notices: tuple[Notice, ...]
    """What is worth knowing about a reading, or about the mechanism. Empty when there is nothing to say."""


def get_memory_budget() -> MemoryBudget | None:
    """Get the memory budget that actually restricts this process.

    A limit that reaches the ceiling the mechanism judges it against is not reported. That is how an
    unrestricted group spells "no limit". A limit is also not reported when that ceiling cannot be read, or
    when no usage metric can be paired with it. `describe()` explains every rejection, and carries the ceiling
    as `memory_limit_ceiling`.

    Returns:
        The budget, or `None` where none is reported.
    """
    return _evaluate_memory(_backend.memory_limit_ceiling()).effective


def get_cpu_limit() -> float | None:
    """Get the number of CPU cores this process may actually use.

    A bandwidth quota and a CPU set restrict the CPU independently. The tighter one wins. A reading that covers
    the whole machine is ignored. Process affinity (`taskset`, `SetProcessAffinityMask`) is out of scope - it
    narrows one process, not its group. `describe()` shows the readings separately.

    Returns:
        The number of cores, possibly fractional. `None` where no limit is reported.
    """
    return _evaluate_cpu_limit(get_machine_cpu_count()).effective


def get_cpu_usage() -> float | None:
    """Get the CPU time consumed at the level this process is counted in, in seconds.

    The value is a counter that only grows, read at the closest level that counts any. Use it to see how much
    CPU time has been spent - not to compute a load: `get_cpu_limit()` can come from a level above this
    process, and dividing this counter by that limit compares two different scopes. `CpuLoad` and
    `get_cpu_used_ratio()` pair both correctly, and they are what a rate should come from.

    Returns:
        The cumulative CPU time. `None` when it cannot be read.
    """
    return _backend.read_cpu_usage()


def get_cpu_used_ratio(interval: float = 1.0) -> float | None:
    """Measure the CPU usage relative to the cores this process may use.

    Blocks the calling thread while measuring, but only while measuring: where there is nothing to measure it
    returns at once, so a loop around it has to pace itself. In asyncio code use `get_cpu_used_ratio_async()`.

    A short window is noisy, and the default is long enough for that to average out. When sampling repeatedly,
    use `CpuLoad` instead - it measures across the time between calls and waits for nothing.

    Args:
        interval: How long to measure, in seconds. Below 0.01 it is refused.

    Returns:
        The ratio between 0 and 1. `None` where no limit is reported, when the counter cannot be read, or
        when the limit changed while measuring, in value or in level.

    Raises:
        ValueError: If `interval` is shorter than a measurable window.
    """
    _check_interval(interval)

    start = _read_cpu()
    if start is None:
        return None

    time.sleep(interval)

    end = _read_cpu()

    return _used_ratio(start, end) if end is not None else None


async def get_cpu_used_ratio_async(interval: float = 1.0) -> float | None:
    """Measure the CPU usage relative to the cores this process may use.

    The asyncio variant of `get_cpu_used_ratio()`. Waits with `asyncio.sleep`, so the event loop stays free.
    Everything `get_cpu_used_ratio()` says about pacing a loop and about accuracy holds here.

    Args:
        interval: How long to measure, in seconds. The same floor of 0.01 applies.

    Returns:
        The ratio between 0 and 1. `None` where no limit is reported, when the counter cannot be read, or
        when the limit changed while measuring, in value or in level.

    Raises:
        ValueError: If `interval` is shorter than a measurable window.
    """
    _check_interval(interval)

    # Imported here: it costs about as much as the rest of this package, and only this one call needs it.
    import asyncio  # noqa: PLC0415

    start = _read_cpu()
    if start is None:
        return None

    await asyncio.sleep(interval)

    end = _read_cpu()

    return _used_ratio(start, end) if end is not None else None


def snapshot() -> Snapshot:
    """Take every reading at once.

    Returns:
        The memory budget, the CPU limit and the CPU usage counter. The rate is not included - it needs a
        measurement window.
    """
    return Snapshot(
        memory_budget=get_memory_budget(),
        cpu_limit=get_cpu_limit(),
        cpu_usage=get_cpu_usage(),
    )


def describe() -> Description:
    """Explain how the sensor arrives at its readings.

    Every reading is taken again, so a limit changed between two calls shows up here. Where the readings come
    from was discovered once - `clear_cache()` is what forgets that.

    Returns:
        The readings, the source of each metric, the raw values before filtering, the machine facts, and a
        notice for whatever is worth knowing about them.
    """
    sources = _backend.sources()

    # One read of each filtering fact, here and in the report, so what decided a rejection is the number
    # reported beside it.
    machine_memory = get_machine_memory_bytes()
    machine_cpus = get_machine_cpu_count()
    ceiling = _backend.memory_limit_ceiling()
    memory = _evaluate_memory(ceiling)
    cpu = _evaluate_cpu_limit(machine_cpus)

    return Description(
        memory_budget=memory.effective,
        cpu_limit=cpu.effective,
        cpu_usage=get_cpu_usage(),
        memory_source=sources.memory,
        cpu_quota_source=sources.cpu_quota,
        cpu_set_source=sources.cpu_set,
        cpu_usage_source=sources.cpu_usage,
        raw_memory_limit=memory.raw.limit,
        raw_memory_used=memory.raw.used,
        raw_memory_available=memory.raw.available,
        raw_cpu_quota=cpu.raw_quota,
        raw_cpu_set_size=cpu.raw_set_size,
        memory_limit_level=memory.raw.limit_level,
        cpu_limit_level=cpu.limit_level,
        cpu_rate_level=cpu.usage_level,
        machine_memory_bytes=machine_memory,
        memory_limit_ceiling=ceiling,
        machine_cpu_count=machine_cpus,
        notices=memory.notices + cpu.notices + _backend.mechanism_notices(),
    )


def clear_cache() -> None:
    """Forget the discovered metric sources.

    Where a mechanism has anything to discover, it is discovered once and kept. Call this after the process
    was moved into another group, and the next reading locates the sources again.

    A child of `fork` clears it on its own, so a pre-fork server whose supervisor puts each worker into a
    group of its own needs no call here.
    """
    _backend.clear_cache()


@dataclass(frozen=True)
class _CpuReading:
    """One reading of the CPU counter, with what it has to be compared against."""

    limit: float
    """The cores allowed when the counter was read. A limit that moves between two readings voids the pair."""

    usage: float
    """The counter as it stood, in seconds."""

    taken_at: float
    """When it was read, on the monotonic clock."""

    usage_level: str | None
    """The level the counter was read at, so a later reading can tell that the limit moved."""


def _check_interval(interval: float) -> None:
    """Reject a measurement window too short to report a load.

    Raises:
        ValueError: If `interval` is shorter than a measurable window.
    """
    if interval < _SHORTEST_WINDOW_SECONDS:
        raise ValueError(
            f'interval must be at least {_SHORTEST_WINDOW_SECONDS} seconds, got {interval}. '
            'A window that short reports noise rather than a load.'
        )


def _read_cpu() -> _CpuReading | None:
    """Read the CPU limit and the consumed time at the level that limit binds at."""
    evaluation = _evaluate_cpu_limit(get_machine_cpu_count())
    if evaluation.effective is None or evaluation.usage_level is None:
        return None

    usage = _backend.read_cpu_usage(evaluation.usage_level)
    if usage is None:
        return None

    return _CpuReading(
        limit=evaluation.effective,
        usage=usage,
        taken_at=time.monotonic(),
        usage_level=evaluation.usage_level,
    )


def _used_ratio(start: _CpuReading, end: _CpuReading) -> float | None:
    """Turn two readings into the share of the allowed cores that was used between them."""
    # A limit that moved level, or changed in place, makes the two readings incomparable: the counters then
    # belong to two scopes, or the same consumption divides by two different numbers.
    if end.usage_level != start.usage_level or end.limit != start.limit:
        return None

    elapsed = end.taken_at - start.taken_at
    if elapsed < _SHORTEST_WINDOW_SECONDS or end.limit <= 0:
        return None

    used_ratio = (end.usage - start.usage) / (elapsed * end.limit)

    # A counter restart makes the difference negative. Clamp both ends.
    return min(max(used_ratio, 0.0), 1.0)


class CpuLoad:
    """Measures the CPU load between calls, without blocking.

    Consumed CPU time is reported as a counter, so a rate needs two readings. This keeps the previous one and
    measures against it, so the window is as long as the interval between calls. Pacing those calls is the
    caller's job.

    Give each caller a sampler of its own. Two callers sharing one measure each other's windows, and a window
    of nearly no time reports nothing at all.

    Safe to call from several threads.
    """

    def __init__(self) -> None:
        self._previous: _CpuReading | None = None
        self._lock = threading.Lock()

    def sample(self) -> float | None:
        """Measure the load since the previous call.

        Returns:
            The ratio between 0 and 1. `None` on the first call, where no limit is reported, when the counter
            cannot be read, when the limit changed since the previous call, or when that call was too recent
            for the counter to have moved.
        """
        reading = _read_cpu()

        with self._lock:
            previous, self._previous = self._previous, reading if reading is not None else self._previous

        if reading is None or previous is None:
            return None

        return _used_ratio(previous, reading)


@dataclass(frozen=True)
class _MemoryEvaluation:
    """The memory reading with the judgement applied."""

    effective: MemoryBudget | None
    """The budget to report, or `None` where there is none to report."""

    raw: RawMemory
    """What the backend said, kept whichever way the judgement went."""

    notices: tuple[Notice, ...]
    """What is worth knowing about a reading, or about the mechanism. Empty when there is nothing to say."""


_COVERS_MACHINE_MESSAGES = {
    NoticeCode.CPU_QUOTA_COVERS_MACHINE: 'The CPU quota of {cores} cores is at least the cores of the machine '
    '({machine_cpus}), so it does not restrict this process.',
    NoticeCode.CPU_SET_COVERS_MACHINE: 'The set of {cores} allowed cores covers every core of the machine '
    '({machine_cpus}), so it does not restrict this process.',
}
"""How each CPU reading explains itself away where it covers the machine."""


def _spell_cores(cores: float) -> str:
    """Spell a number of cores for a message."""
    # Not `float.is_integer()`: an `int` satisfies this annotation, and only Python 3.12 gives `int` that
    # method. The remainder answers for both, and a whole number must not print as `64.0 allowed cores`.
    return str(int(cores)) if cores % 1 == 0 else str(cores)


@dataclass(frozen=True)
class _CpuRestriction:
    """One CPU restriction as read, with the notice that explains it away where it covers the machine."""

    cores: float
    """The cores this restriction allows, possibly fractional."""

    limit_level: str
    """The level this restriction was read at."""

    usage_level: str | None
    """Where the CPU time this restriction applies to is counted. `None` when nothing counts it."""

    code: NoticeCode
    """The notice to raise where this restriction turns out to cover the machine."""


@dataclass(frozen=True)
class _CpuEvaluation:
    """The CPU readings with the judgement applied."""

    effective: float | None
    """The tighter of the two restrictions in cores. `None` where no limit is reported."""

    limit_level: str | None
    """The level `effective` was read at. `None` where no limit is reported."""

    usage_level: str | None
    """Where the consumed CPU time has to be read to match `effective`.

    `None` when no level counts the time this limit applies to, and therefore no rate can be measured.
    """

    raw_quota: float | None
    """The bandwidth quota in cores before filtering. `None` where there is none to report."""

    raw_set_size: int | None
    """The number of allowed cores before filtering. `None` where there is none to report."""

    notices: tuple[Notice, ...]
    """What is worth knowing about a reading, or about the mechanism. Empty when there is nothing to say."""


def _evaluate_memory(ceiling: int | None) -> _MemoryEvaluation:
    """Judge the raw memory reading against the size at which a limit stops restricting."""
    raw = _backend.read_memory()
    rejection = _memory_rejection(raw, ceiling)

    if rejection is not None:
        return _MemoryEvaluation(effective=None, raw=raw, notices=(rejection,))

    if raw.limit is None or raw.used is None or raw.available is None:
        # Nothing to report and nothing to explain: the mechanism is there and no level limits anything. The
        # last two tests never decide this branch, and stay only to narrow the type below.
        return _MemoryEvaluation(effective=None, raw=raw, notices=())

    return _MemoryEvaluation(effective=_reconcile(raw.limit, raw.used, raw.available), raw=raw, notices=())


def _reconcile(limit: int, used: int, available: int) -> MemoryBudget:
    """Bring three numbers a mechanism may have read separately into one pair a consumer can use."""
    # The memory in use first, so the distance below it is already a number that can be allocated. The room
    # gives way rather than the usage: one left too high promises memory the kernel will refuse.
    charged = min(max(used, 0), limit)

    return MemoryBudget(limit=limit, used=charged, available=min(max(available, 0), limit - charged))


def _memory_rejection(raw: RawMemory, ceiling: int | None) -> Notice | None:
    """Say why the raw memory reading cannot be reported. `None` where it can."""
    if raw.unreadable_level is not None:
        return Notice(
            code=NoticeCode.MEMORY_LIMIT_UNREADABLE,
            message=f'The memory limit of {raw.unreadable_level} cannot be read, so a tighter limit than '
            'any this process can see may apply and nothing is reported.',
        )

    if raw.limit is None:
        # No limit and no mechanism read the same way from the outside, and only one of them is a fact about
        # this machine.
        return (
            Notice(
                code=NoticeCode.MEMORY_METRICS_UNAVAILABLE,
                message='No mechanism here carries a memory limit, so nothing was read. A Linux machine with '
                'no cgroup filesystem mounted looks like this, and so does a platform with no backend here.',
            )
            if _backend.sources().memory is None
            else None
        )

    if ceiling is None:
        return Notice(
            code=NoticeCode.MACHINE_MEMORY_UNKNOWN,
            message=f'The size a memory limit is judged against cannot be read, so the limit of {raw.limit} '
            'bytes cannot be told apart from an "unlimited" sentinel and is not reported.',
        )

    if raw.limit >= ceiling:
        # The exact sentinel differs between runtimes.
        return Notice(
            code=NoticeCode.MEMORY_LIMIT_COVERS_MACHINE,
            message=f'The memory limit of {raw.limit} bytes is at least everything this machine can hand out '
            f'({ceiling} bytes), so it does not restrict this process.',
        )

    if raw.used is None or raw.available is None:
        return _unpaired_limit_notice(raw.limit, raw.usage_unreadable_level)

    return None


def _unpaired_limit_notice(limit: int, usage_unreadable_level: str | None) -> Notice:
    """Say why a limit was found with no usage to pair against it, which happens two ways."""
    if usage_unreadable_level is not None:
        return Notice(
            code=NoticeCode.MEMORY_USAGE_UNREADABLE,
            message=f'Found a memory limit of {limit} bytes, and {usage_unreadable_level} did not say how '
            'much of it is in use, so the limit is not reported.',
        )

    return Notice(
        code=NoticeCode.MEMORY_USAGE_UNAVAILABLE,
        message=f'Found a memory limit of {limit} bytes that this mechanism pairs no usage metric with, so '
        'the limit is not reported.',
    )


def _evaluate_cpu_limit(machine_cpus: int | None) -> _CpuEvaluation:
    """Judge the raw CPU readings. The tighter of the quota and the set wins."""
    raw = _backend.read_cpu()

    if raw.unreadable_level is not None:
        # Both readings are already empty here, and this test is before them so that nothing has to rely on
        # that.
        return _no_cpu_limit(
            Notice(
                code=NoticeCode.CPU_LIMIT_UNREADABLE,
                message=f'The CPU limit of {raw.unreadable_level} cannot be read, so a tighter limit '
                'than any this process can see may apply and nothing is reported.',
            )
        )

    if raw.unconvertible_level is not None:
        # The other reading is dropped with it, for the same reason an unreadable level drops both: the share
        # that cannot be sized may be the tighter of the two.
        return _no_cpu_limit(
            Notice(
                code=NoticeCode.MACHINE_CPU_COUNT_UNKNOWN,
                message=f'The CPU limit of {raw.unconvertible_level} is a share of the machine, and the cores '
                'of the machine cannot be read. A share says nothing about cores on its own, so nothing is '
                'reported.',
            )
        )

    quota, cpu_set = raw.quota, raw.cpu_set

    restrictions = []

    if quota is not None:
        restrictions.append(
            _CpuRestriction(
                cores=quota.cores,
                limit_level=quota.limit_level,
                usage_level=quota.usage_level,
                code=NoticeCode.CPU_QUOTA_COVERS_MACHINE,
            )
        )

    if cpu_set is not None:
        restrictions.append(
            _CpuRestriction(
                cores=float(cpu_set.cores),
                limit_level=cpu_set.limit_level,
                usage_level=cpu_set.usage_level,
                code=NoticeCode.CPU_SET_COVERS_MACHINE,
            )
        )

    notices = []
    candidates = []

    if not restrictions and _no_cpu_mechanism():
        # As for memory: "nothing limits the CPU here" and "nothing here can say" are different answers.
        notices.append(
            Notice(
                code=NoticeCode.CPU_METRICS_UNAVAILABLE,
                message='No mechanism here carries a CPU limit, so nothing was read. A Linux machine with no '
                'cgroup filesystem mounted looks like this, and so does a platform with no backend here.',
            )
        )

    for restriction in restrictions:
        # A reading in cores is trusted where the machine core count is unknown: cores have no sentinel, so
        # whatever was read is a number a human configured.
        if machine_cpus is not None and restriction.cores >= machine_cpus:
            # Built only where a reading is dropped: `CpuLoad.sample()` evaluates the CPU limit on every call,
            # and the usual container - a cpuset covering the machine beside a real quota - drops one each time.
            message = _COVERS_MACHINE_MESSAGES[restriction.code]
            notices.append(
                Notice(
                    code=restriction.code,
                    message=message.format(cores=_spell_cores(restriction.cores), machine_cpus=machine_cpus),
                )
            )
        else:
            candidates.append(restriction)

    tightest = min(candidates, key=lambda restriction: restriction.cores) if candidates else None

    # A limit whose level counts no CPU time still limits, and is still reported. Only the rate is impossible.
    if tightest is not None and tightest.usage_level is None:
        notices.append(
            Notice(
                code=NoticeCode.CPU_USAGE_SCOPE_MISMATCH,
                message=f'The CPU limit of {_spell_cores(tightest.cores)} cores applies to a level whose '
                'consumed CPU time cannot be read, so no rate can be measured against it.',
            )
        )

    return _CpuEvaluation(
        effective=tightest.cores if tightest is not None else None,
        limit_level=tightest.limit_level if tightest is not None else None,
        usage_level=tightest.usage_level if tightest is not None else None,
        raw_quota=quota.cores if quota is not None else None,
        raw_set_size=cpu_set.cores if cpu_set is not None else None,
        notices=tuple(notices),
    )


def _no_cpu_limit(notice: Notice) -> _CpuEvaluation:
    """Report no CPU limit at all, with one notice saying why."""
    return _CpuEvaluation(
        effective=None,
        limit_level=None,
        usage_level=None,
        raw_quota=None,
        raw_set_size=None,
        notices=(notice,),
    )


def _no_cpu_mechanism() -> bool:
    """Whether no mechanism here could carry a CPU limit of either kind."""
    sources = _backend.sources()

    return sources.cpu_quota is None and sources.cpu_set is None


def get_machine_memory_bytes() -> int | None:
    """Read the total memory of the machine, in bytes.

    This is what a pool is sized from where nothing restricts this process, so a consumer that got `None` from
    `get_memory_budget()` has the other half of the answer here. A limit is compared against
    `describe().memory_limit_ceiling` instead.

    A runtime that virtualizes this number (lxcfs) makes it equal the limit, which is then reported as no
    restriction.

    Returns:
        The total memory, or `None` when it cannot be read.
    """
    return _backend.machine_memory_bytes()


def get_machine_cpu_count() -> int | None:
    """Read the number of online CPU cores of the machine.

    This is the number the filters compare a quota or a set against, and the reason it is public: the usual
    answers, `os.cpu_count()` and `psutil.cpu_count()`, can describe the process instead and make a real
    restriction look like the whole machine. The system is asked first here.

    On Windows this is more than a filter: a job object states its CPU limit as a share of the machine, so
    without this number that limit cannot be turned into cores at all.

    Returns:
        The online cores, or `None` when even the fallbacks say nothing.
    """
    return _backend.machine_cpu_count()
