from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class NoticeCode(str, Enum):
    """Why a reading was dropped, why a rate cannot be measured, or what the mechanism cannot see.

    Most accompany a reading that was dropped. The `*_ANCESTORS_HIDDEN` pair reports a hierarchy this process
    can read only part of. It follows from what the mechanism exposes rather than from a reading, so it
    appears whether or not one was made, and what was read may be looser than what applies.

    Compare against these rather than against the strings they carry: the strings are what a log line shows,
    the members are what code should branch on.
    """

    # Without this a member prints as `NoticeCode.MEMORY_LIMIT_COVERS_MACHINE`, and an f-string of it differs
    # between the supported Python versions. `StrEnum` would do the same, and arrives only in 3.11.
    __str__ = str.__str__

    MEMORY_LIMIT_COVERS_MACHINE = 'memory-limit-covers-machine'
    """The limit is at least everything the machine can hand out, which is how an unrestricted group spells
    "no limit".

    What that size is belongs to the mechanism; `Description.memory_limit_ceiling` carries the number used.
    """

    MEMORY_USAGE_UNAVAILABLE = 'memory-usage-unavailable'
    """A limit was found, and the mechanism pairs no usage figure with a limit of that kind.

    A property of the shape, steady for as long as it is configured that way. A Windows job's per-process
    limit is the case: it is charged per process, and the job's usage figure counts them all together.
    """

    MEMORY_USAGE_UNREADABLE = 'memory-usage-unreadable'
    """A limit was found, and the level that carries the usage to pair with it did not answer.

    A read that failed, so it can come and go, and something on this machine is worth looking into. An
    ancestor cgroup that holds a limit and no readable `memory.current` raises it.
    """

    MACHINE_MEMORY_UNKNOWN = 'machine-memory-unknown'
    """What the machine can hand out cannot be read, so a limit cannot be told apart from a sentinel."""

    MACHINE_CPU_COUNT_UNKNOWN = 'machine-cpu-count-unknown'
    """A CPU limit is a share of the machine here, and the cores of the machine cannot be read.

    A share cannot be turned into cores without them, so the limit is enforced but its size is unknown. Only a
    mechanism that expresses a limit as a share raises this - a cgroup states cores outright.
    """

    CPU_QUOTA_COVERS_MACHINE = 'cpu-quota-covers-machine'
    """The quota is at least the cores of the machine, so it restricts nothing."""

    CPU_SET_COVERS_MACHINE = 'cpu-set-covers-machine'
    """The set of allowed cores covers every core of the machine, so it restricts nothing."""

    CPU_USAGE_SCOPE_MISMATCH = 'cpu-usage-scope-mismatch'
    """The level the CPU limit applies to counts no CPU time, so no rate can be measured against it."""

    MEMORY_METRICS_UNAVAILABLE = 'memory-metrics-unavailable'
    """No mechanism here is present to carry a memory limit: an unsupported platform, or none is mounted."""

    CPU_METRICS_UNAVAILABLE = 'cpu-metrics-unavailable'
    """No mechanism here is present to carry a CPU limit: an unsupported platform, or none is mounted."""

    MEMORY_LIMIT_UNREADABLE = 'memory-limit-unreadable'
    """A level holds a memory limit that says nothing usable, so what it enforces is unknown."""

    CPU_LIMIT_UNREADABLE = 'cpu-limit-unreadable'
    """A level holds a CPU limit that says nothing usable, so what it enforces is unknown."""

    MEMORY_ANCESTORS_HIDDEN = 'memory-ancestors-hidden'
    """Only part of the memory hierarchy is visible, so a limit above it is enforced but never read."""

    CPU_ANCESTORS_HIDDEN = 'cpu-ancestors-hidden'
    """Only part of a CPU hierarchy is visible, so a limit above it is enforced but never read."""


@dataclass(frozen=True)
class Notice:
    """One thing worth knowing about a reading, or about what the mechanism could not see."""

    code: NoticeCode
    """What happened, as a member of `NoticeCode`. It carries its own string, so an f-string of it, a `%s` in
    a log line and `json.dumps` all show that string rather than the member."""

    message: str
    """A human-readable explanation with the values involved."""


class Interface(str, Enum):
    """The mechanism a reading comes from.

    A machine can serve different metrics through different interfaces, so this is reported per metric rather
    than once. Compare against these members rather than against the strings they carry.
    """

    # As in `NoticeCode`, so that a member prints as its string on every supported Python version.
    __str__ = str.__str__

    CGROUP_V2 = 'cgroup-v2'
    """The unified hierarchy, which serves whichever controllers are bound to it."""

    CGROUP_V1 = 'cgroup-v1'
    """The older interface, where each controller is a hierarchy of its own."""

    WINDOWS_JOB_OBJECT = 'windows-job-object'
    """A Windows job object, which carries the limits of every process assigned to it."""


@dataclass(frozen=True)
class Source:
    """Where one metric is read from."""

    interface: Interface
    """The mechanism providing the metric. It carries its own string, as `Notice.code` does."""

    levels: tuple[str, ...]
    """The levels the metric is looked for in, closest to this process first.

    A cgroup mechanism names the cgroup of this process and then its ancestors. Most of a chain holds no limit,
    and the whole chain is kept regardless: a limit can be written to any level later, and discovery happens
    only once. A Windows job object is a single level named `job`.

    Empty where the mechanism is present with nothing to search: a hierarchy binding no such controller, or a
    process in no job.
    """


@dataclass(frozen=True)
class Sources:
    """Where each metric is read from, as located for this process."""

    memory: Source | None
    """Carries the memory limit and the memory charged against it."""

    cpu_quota: Source | None
    """Carries the CPU bandwidth quota."""

    cpu_set: Source | None
    """Carries the set of cores the process may run on."""

    cpu_usage: Source | None
    """Carries the consumed CPU time, which a mechanism may count somewhere other than where it limits."""


@dataclass(frozen=True)
class RawMemory:
    """The memory limit and usage as the mechanism spells them."""

    limit: int | None
    """The tightest limit along the chain, in bytes. `None` when no level holds one.

    Taken as read: cgroup v1 spells "no limit" as a sentinel near 2**63, and it passes through here.
    """

    used: int | None
    """The memory charged against `limit`, in bytes, as the mechanism derives it.

    `None` where any level holding a limit did not answer with a usage, not only the level holding `limit`.
    `None` too where the mechanism pairs no usage with a limit of that kind.
    """

    available: int | None
    """The memory that can still be allocated before the limit, in bytes, as the mechanism reports it.

    Filled with `used` and `None` with it: the two are the pair a limit is reported against.
    """

    limit_level: str | None
    """The level holding `limit`, or `None` when no level holds one.

    The whole chain is walked and the tightest limit wins, which is often not the level of this process. The
    number alone therefore does not say which level it came from.
    """

    unreadable_level: str | None
    """The level whose limit says nothing usable, or `None` when every level answered.

    The reading is dropped when this is set, rather than falling back to a level that did answer: every such
    level is looser, and the mechanism enforces the one that did not.
    """

    usage_unreadable_level: str | None
    """The level that carries a usage figure and did not answer with one, or `None`.

    Only ever set alongside a `used` of `None`, where it tells the two empty pairs apart. Set, a read failed
    and may succeed on the next call. `None`, the limit is of a kind this mechanism pairs no usage with at
    all.

    Beside a `used` that was filled it is `None` as well, and means nothing there: no read failed, so there is
    no level to name.
    """


@dataclass(frozen=True)
class RawCpuQuota:
    """A CPU bandwidth quota, the level it binds at, and where the CPU time it throttles is counted."""

    cores: float
    """The number of cores the quota allows, possibly fractional."""

    limit_level: str
    """The level holding the quota.

    That level is throttled as a whole, siblings included, and is often not the level of this process.
    """

    usage_level: str | None
    """Where the CPU time of that level is counted, or `None` when nothing counts it.

    A rate measured anywhere else would divide the time of one scope by the limit of another, so `None` means
    no rate can be measured against this quota at all.
    """


@dataclass(frozen=True)
class RawCpuSet:
    """A set of allowed cores, the level it was read at, and where the CPU time it restricts is counted."""

    cores: int
    """The number of cores the set allows."""

    limit_level: str
    """The level the set was read at, which need not be the level of this process.

    The number read there already resolves inheritance, so the set restricting it can be configured on any
    level above.
    """

    usage_level: str | None
    """Where the CPU time of the level the set applies to is counted, or `None` when nothing counts it.

    As for a quota, `None` means no rate can be measured against this set.
    """


@dataclass(frozen=True)
class RawCpu:
    """Everything the CPU controls say, read in one go.

    A level that says nothing usable concerns both restrictions, so they are read together.
    """

    quota: RawCpuQuota | None
    """The bandwidth quota, or `None` when no level sets one."""

    cpu_set: RawCpuSet | None
    """The set of allowed cores, or `None` when no level sets one."""

    unreadable_level: str | None
    """The level whose CPU control says nothing usable, or `None` when every level answered.

    Both readings are dropped when this is set. The control exists, so what it holds is unknown rather than
    absent, and an ancestor's looser number is not a substitute for it.
    """

    unconvertible_level: str | None
    """The level holding a limit expressed as a share of the machine, when the machine's cores are unknown.

    A share says nothing about cores on its own, so the reading is dropped and the level is named here. Only
    mechanisms that express a limit that way ever set this - a cgroup states cores outright, so it is always
    `None` there.
    """
