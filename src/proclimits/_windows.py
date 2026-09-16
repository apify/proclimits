from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import TypeVar

from ._types import (
    Interface,
    Notice,
    RawCpu,
    RawCpuQuota,
    RawCpuSet,
    RawMemory,
    Source,
    Sources,
)

_LEVEL = 'job'
"""The one level a job object provides.

A job has no path and usually no name, so there is nothing to spell it with.
"""

_HUNDRED_NANOSECONDS_PER_SECOND = 10_000_000
"""Windows counts consumed CPU time in units of 100 nanoseconds."""

_RATE_SCALE = 10_000
"""A CPU rate is a share, in hundredths of a percent. So 750 is 7.5%.

Of the whole machine, unless a job above this one also caps the rate - then it is a share of that job's
share, and the two rates multiply. Nothing reports that there is a job above, so a rate read inside a nested
job is larger than what is enforced. Measured: 50% inside a job capped at 10% yields a twentieth of the
machine, and this reads it as a half.
"""

_ALL_PROCESSOR_GROUPS = 0xFFFF
"""Asks for the cores of every processor group, not only the one this process runs in.

A machine of more than 64 cores has several groups, and a call that names one sees a fraction of the machine.
"""

_JOB_BASIC_ACCOUNTING = 1
"""`JobObjectBasicAccountingInformation`: the consumed CPU time of every process in the job."""

_JOB_EXTENDED_LIMIT = 9
"""`JobObjectExtendedLimitInformation`: the memory limits, the affinity mask, and which of them are set."""

_JOB_LIMIT_VIOLATION = 13
"""`JobObjectLimitViolationInformation`: the memory in use right now."""

_JOB_CPU_RATE_CONTROL = 15
"""`JobObjectCpuRateControlInformation`: the CPU rate, in whichever of four forms was set."""

_JOB_GROUP_AFFINITY = 14
"""`JobObjectGroupInformationEx`: the affinity of the job, as an array of masks."""

_PROCESS_APP_MEMORY_INFO = 2
"""`ProcessAppMemoryInfo`: what this process may still commit."""

_ERROR_NO_JOB = 5
"""`ERROR_ACCESS_DENIED`, which is how a query answers where this process is in no job.

Measured: outside a job every information class fails with it, and inside one every class answers. The handle
passed is null, which makes the query one about the job of the caller, and leaves no access rights to be
refused - nothing else for the code to mean.
"""

_LIMIT_AFFINITY = 0x10
"""`JOB_OBJECT_LIMIT_AFFINITY`: the job restricts which cores its processes may run on."""

_LIMIT_JOB_MEMORY = 0x200
"""`JOB_OBJECT_LIMIT_JOB_MEMORY`: the whole job may commit no more than `JobMemoryLimit`."""

_LIMIT_PROCESS_MEMORY = 0x100
"""`JOB_OBJECT_LIMIT_PROCESS_MEMORY`: each process in the job may commit no more than `ProcessMemoryLimit`."""

_RATE_CONTROL_ENABLE = 0x1
"""`JOB_OBJECT_CPU_RATE_CONTROL_ENABLE`: rate control is on. On its own it is a share, not a cap."""

_RATE_CONTROL_HARD_CAP = 0x4
"""`JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP`: the rate is a ceiling, and `CpuRate` holds it."""

_RATE_CONTROL_MIN_MAX_RATE = 0x10
"""`JOB_OBJECT_CPU_RATE_CONTROL_MIN_MAX_RATE`: the rate is a range, and `MaxRate` holds the ceiling."""


class _IoCounters(ctypes.Structure):
    """`IO_COUNTERS`: padding, as far as this module is concerned.

    It sits between the fields that are read.
    """

    _fields_ = tuple(
        (name, ctypes.c_ulonglong)
        for name in (
            'ReadOperationCount',
            'WriteOperationCount',
            'OtherOperationCount',
            'ReadTransferCount',
            'WriteTransferCount',
            'OtherTransferCount',
        )
    )


class _BasicLimits(ctypes.Structure):
    """`JOBOBJECT_BASIC_LIMIT_INFORMATION`: the limits every job carries, and the flags saying which are set."""

    _fields_ = (
        ('PerProcessUserTimeLimit', wintypes.LARGE_INTEGER),
        ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    )


class _ExtendedLimits(ctypes.Structure):
    """`JOBOBJECT_EXTENDED_LIMIT_INFORMATION`: the basic limits, plus the memory limits and the peaks."""

    _fields_ = (
        ('BasicLimitInformation', _BasicLimits),
        ('IoInfo', _IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    )


class _GroupAffinity(ctypes.Structure):
    """`GROUP_AFFINITY`: the processors one group allows, and which group that is.

    A job carries an array of these, one entry per group in the range it spans.
    """

    _fields_ = (
        ('Mask', ctypes.c_size_t),
        ('Group', wintypes.WORD),
        ('Reserved', wintypes.WORD * 3),
    )


class _Accounting(ctypes.Structure):
    """`JOBOBJECT_BASIC_ACCOUNTING_INFORMATION`: what the job has consumed.

    The times sum every process in it, running or finished.
    """

    _fields_ = (
        ('TotalUserTime', wintypes.LARGE_INTEGER),
        ('TotalKernelTime', wintypes.LARGE_INTEGER),
        ('ThisPeriodTotalUserTime', wintypes.LARGE_INTEGER),
        ('ThisPeriodTotalKernelTime', wintypes.LARGE_INTEGER),
        ('TotalPageFaultCount', wintypes.DWORD),
        ('TotalProcesses', wintypes.DWORD),
        ('ActiveProcesses', wintypes.DWORD),
        ('TotalTerminatedProcesses', wintypes.DWORD),
    )


class _LimitViolation(ctypes.Structure):
    """`JOBOBJECT_LIMIT_VIOLATION_INFORMATION`: what each limit stands at against what it allows.

    Only `JobMemory` is read.
    """

    _fields_ = (
        ('LimitFlags', wintypes.DWORD),
        ('ViolationLimitFlags', wintypes.DWORD),
        ('IoReadBytes', ctypes.c_ulonglong),
        ('IoReadBytesLimit', ctypes.c_ulonglong),
        ('IoWriteBytes', ctypes.c_ulonglong),
        ('IoWriteBytesLimit', ctypes.c_ulonglong),
        ('PerJobUserTime', wintypes.LARGE_INTEGER),
        ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER),
        ('JobMemory', ctypes.c_ulonglong),
        ('JobMemoryLimit', ctypes.c_ulonglong),
        ('RateControlTolerance', wintypes.DWORD),
        ('RateControlToleranceLimit', wintypes.DWORD),
    )


class _Rate(ctypes.Structure):
    """The two ends of a rate range. `DUMMYSTRUCTNAME` in Win32."""

    _fields_ = (('MinRate', wintypes.WORD), ('MaxRate', wintypes.WORD))


class _RateUnion(ctypes.Union):
    """The three ways one field is read, according to the flags beside it. `DUMMYUNIONNAME` in Win32.

    Reading the wrong member returns a plausible number rather than an error.
    """

    _fields_ = (('CpuRate', wintypes.DWORD), ('Weight', wintypes.DWORD), ('Rate', _Rate))


class _RateControl(ctypes.Structure):
    """`JOBOBJECT_CPU_RATE_CONTROL_INFORMATION`: how the CPU of a job is capped, weighted or shared."""

    _anonymous_ = ('u',)
    _fields_ = (('ControlFlags', wintypes.DWORD), ('u', _RateUnion))


class _AppMemory(ctypes.Structure):
    """`APP_MEMORY_INFORMATION`: what this process may still commit, and what it has committed."""

    _fields_ = (
        ('AvailableCommit', ctypes.c_ulonglong),
        ('PrivateCommitUsage', ctypes.c_ulonglong),
        ('PeakPrivateCommitUsage', ctypes.c_ulonglong),
        ('TotalCommitUsage', ctypes.c_ulonglong),
    )


class _MemoryStatus(ctypes.Structure):
    """`MEMORYSTATUSEX`: the memory of the machine.

    `dwLength` has to be filled in before the call.
    """

    _fields_ = (
        ('dwLength', wintypes.DWORD),
        ('dwMemoryLoad', wintypes.DWORD),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
    )


class _SystemInfo(ctypes.Structure):
    """`SYSTEM_INFO`: what the machine is.

    Only the core count is read, and only as a fallback.
    """

    _fields_ = (
        ('wProcessorArchitecture', wintypes.WORD),
        ('wReserved', wintypes.WORD),
        ('dwPageSize', wintypes.DWORD),
        ('lpMinimumApplicationAddress', wintypes.LPVOID),
        ('lpMaximumApplicationAddress', wintypes.LPVOID),
        ('dwActiveProcessorMask', ctypes.c_size_t),
        ('dwNumberOfProcessors', wintypes.DWORD),
        ('dwProcessorType', wintypes.DWORD),
        ('dwAllocationGranularity', wintypes.DWORD),
        ('wProcessorLevel', wintypes.WORD),
        ('wProcessorRevision', wintypes.WORD),
    )


_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

# Every function bound here has been in kernel32 for longer than any Windows release a supported CPython
# starts on.
_kernel32.GetCurrentProcess.argtypes = ()
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.QueryInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
)
_kernel32.QueryInformationJobObject.restype = wintypes.BOOL
_kernel32.GetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_kernel32.GetProcessInformation.restype = wintypes.BOOL
_kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(_MemoryStatus),)
_kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
_kernel32.GetActiveProcessorCount.argtypes = (wintypes.WORD,)
_kernel32.GetActiveProcessorCount.restype = wintypes.DWORD
_kernel32.GetActiveProcessorGroupCount.argtypes = ()
_kernel32.GetActiveProcessorGroupCount.restype = wintypes.WORD
_kernel32.GetMaximumProcessorGroupCount.argtypes = ()
_kernel32.GetMaximumProcessorGroupCount.restype = wintypes.WORD
_kernel32.GetSystemInfo.argtypes = (ctypes.POINTER(_SystemInfo),)
_kernel32.GetSystemInfo.restype = None

_NO_MEMORY = RawMemory(
    limit=None,
    used=None,
    available=None,
    unflushed_cache=None,
    limit_level=None,
    unreadable_level=None,
    usage_unreadable_level=None,
)
"""What is reported where no job limits the memory."""

_UNREADABLE_MEMORY = RawMemory(
    limit=None,
    used=None,
    available=None,
    unflushed_cache=None,
    limit_level=None,
    unreadable_level=_LEVEL,
    usage_unreadable_level=None,
)
"""What is reported where this process is in a job that will not say what it enforces."""

_Structure = TypeVar('_Structure', bound=ctypes.Structure)


def read_memory() -> RawMemory:
    """Read the memory limit of the job holding this process, with the memory charged against it.

    A job can limit the whole job and each of its processes separately. The job-wide limit is reported with
    its usage. A per-process limit that is tighter, or the only one, is reported without usage: the job's
    usage sums every process and does not pair with it.
    """
    limits, error = _query(_JOB_EXTENDED_LIMIT, _ExtendedLimits)
    if limits is None:
        # A job that did not answer enforces something unknown; no job enforces nothing.
        return _NO_MEMORY if error == _ERROR_NO_JOB else _UNREADABLE_MEMORY

    flags = limits.BasicLimitInformation.LimitFlags
    job_limit = limits.JobMemoryLimit if flags & _LIMIT_JOB_MEMORY else None
    process_limit = limits.ProcessMemoryLimit if flags & _LIMIT_PROCESS_MEMORY else None

    if process_limit is not None and (job_limit is None or process_limit < job_limit):
        # `AvailableCommit` is blind to a per-process limit, so pairing it with this one would hand out room
        # the kernel refuses.
        return RawMemory(
            limit=process_limit,
            used=None,
            available=None,
            unflushed_cache=None,
            limit_level=_LEVEL,
            unreadable_level=None,
            usage_unreadable_level=None,
        )

    if job_limit is None:
        return _NO_MEMORY

    return _job_memory(job_limit)


def read_cpu() -> RawCpu:
    """Read both CPU restrictions of the job holding this process.

    A rate cap and an affinity mask restrict the CPU independently, as a quota and a cpuset do on Linux. Both
    belong to the job rather than to a process, so both are read.
    """
    limits, error = _query(_JOB_EXTENDED_LIMIT, _ExtendedLimits)
    if limits is None:
        return RawCpu(
            quota=None,
            cpu_set=None,
            unreadable_level=None if error == _ERROR_NO_JOB else _LEVEL,
            unconvertible_level=None,
        )

    # One unreadable answer drops both restrictions, because the unread one may be the tighter. No machine
    # separates the two: rate control is a job information class wherever a supported CPython runs, and both
    # were measured to answer or fail together.
    control, _ = _query(_JOB_CPU_RATE_CONTROL, _RateControl)
    if control is None:
        return RawCpu(quota=None, cpu_set=None, unreadable_level=_LEVEL, unconvertible_level=None)

    rate = _rate_cap(control)
    machine_cores = machine_cpu_count()

    if rate is not None and machine_cores is None:
        # A rate is a share of the machine, so without the size of the machine it says nothing about cores.
        # The affinity mask goes with it: the share that cannot be sized may be the tighter of the two.
        return RawCpu(quota=None, cpu_set=None, unreadable_level=None, unconvertible_level=_LEVEL)

    quota = (
        RawCpuQuota(cores=rate * machine_cores, limit_level=_LEVEL, usage_level=_LEVEL)
        if rate is not None and machine_cores is not None
        else None
    )

    # The flag gates the group-aware read, and has to: that class answers for a job with no affinity as
    # well, with every core of the machine, measured. No measured way of setting one leaves the flag clear -
    # through the basic limits it is raised by construction, being a field of that structure, and the kernel
    # raises it for the group-aware class and for `JobObjectGroupInformation`, which carries no mask at all.
    cpu_set = None
    if limits.BasicLimitInformation.LimitFlags & _LIMIT_AFFINITY:
        allowed = _affinity_cores()
        if allowed is None:
            # The job restricts the cores and will not say which, so what it allows is unknown rather than
            # unrestricted.
            return RawCpu(quota=None, cpu_set=None, unreadable_level=_LEVEL, unconvertible_level=None)

        cpu_set = RawCpuSet(cores=allowed, limit_level=_LEVEL, usage_level=_LEVEL)

    return RawCpu(quota=quota, cpu_set=cpu_set, unreadable_level=None, unconvertible_level=None)


def read_cpu_usage(level: str | None = None) -> float | None:
    """Read the CPU time the job has consumed, in seconds.

    The counter sums every process in the job, finished ones included, which is what makes it comparable with
    a limit that applies to the job as a whole.

    Args:
        level: The level to read. Defaults to the one level a job has, and any other is refused.

    Returns:
        The cumulative CPU time. `None` where this process is in no job, and where the counter cannot be read.
    """
    if level is not None and level != _LEVEL:
        return None

    accounting, _ = _query(_JOB_BASIC_ACCOUNTING, _Accounting)
    if accounting is None:
        return None

    total = accounting.TotalUserTime + accounting.TotalKernelTime

    return total / _HUNDRED_NANOSECONDS_PER_SECOND


def sources() -> Sources:
    """Say that every metric comes from the job object, and name the one level it has.

    The mechanism is always there on Windows, so a source is always reported. The level list is filled only
    where this process is in a job.
    """
    source = Source(interface=Interface.WINDOWS_JOB_OBJECT, levels=(_LEVEL,) if _in_job() else ())

    return Sources(memory=source, cpu_quota=source, cpu_set=source, cpu_usage=source)


def mechanism_notices() -> tuple[Notice, ...]:
    """Report nothing about the mechanism itself, every property of it being constant.

    A job nested inside another reads only its own limits, and the outer ones are enforced and invisible.
    Nothing detects that, so a notice would be permanent. The two numbers that might have disagreed under an
    outer job are one number: `AvailableCommit` was measured to be the distance to the inner limit to the
    byte, whatever encloses it.
    """
    return ()


def machine_memory_bytes() -> int | None:
    """Read the total physical memory of the machine, in bytes.

    A memory limit is judged against `memory_limit_ceiling()` instead.

    Returns:
        The total memory, or `None` when the call fails.
    """
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)

    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None

    return status.ullTotalPhys or None


def memory_limit_ceiling() -> int | None:
    """Read the size a memory limit has to reach before it restricts nothing, in bytes.

    The commit limit of the system, which is what every process together may commit, because a job limits
    commit rather than resident memory. A page file lifts it above the memory of the machine, and where there
    is none it settles just below, the kernel holding back what it could not page out.

    Measured to describe the system rather than the caller: `ullTotalPageFile` is documented as the smaller of
    the system commit limit and the caller's own, and one that followed the job limit would equal it, so every
    job limit would read as restricting nothing. Setting a job memory limit of 2 GiB left it unchanged.

    Returns:
        The ceiling, or `None` when the call fails.
    """
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)

    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None

    return status.ullTotalPageFile or None


def machine_cpu_count() -> int | None:
    """Count the cores of the machine, across every processor group.

    A job states its CPU limit as a share of the machine, so this is what turns that share into cores. Only the
    system is asked: `os.cpu_count()` honors the `PYTHON_CPU_COUNT` override, and a wrong count would silently
    resize every CPU limit this backend reports.

    Returns:
        The active cores, or `None` where no call this machine's shape allows has answered.
    """
    active = _kernel32.GetActiveProcessorCount(_ALL_PROCESSOR_GROUPS)
    if active:
        return active

    # The fallback counts the group this process runs in, not the machine: measured at 8, then 4 on a rerun,
    # where the machine had 20 split into groups of 8, 8 and 4 - it follows whichever group the process landed
    # in. A CPU rate is a share of the machine, so a count that small would shrink the quota read here.
    if _kernel32.GetActiveProcessorGroupCount() != 1:
        return None

    info = _SystemInfo()
    _kernel32.GetSystemInfo(ctypes.byref(info))

    return info.dwNumberOfProcessors or None


def clear_cache() -> None:
    """Discard nothing: every reading asks the job of this process directly."""


def _in_job() -> bool:
    """Whether something put this process in a job.

    The basic accounting class is asked; a job that will not answer still counts as one.
    """
    _, error = _query(_JOB_BASIC_ACCOUNTING, _Accounting)

    return error != _ERROR_NO_JOB


def _query(info_class: int, structure: type[_Structure]) -> tuple[_Structure | None, int]:
    """Ask the job of this process about itself.

    Only the immediate job answers.

    Returns:
        The filled structure and zero. Where the call failed, `None` and the code it failed with:
        `_ERROR_NO_JOB` for a process that is in no job, and anything else for a job that would not answer
        or a structure declared wrong here.
    """
    buffer = structure()

    ctypes.set_last_error(0)
    ok = _kernel32.QueryInformationJobObject(
        None,
        info_class,
        ctypes.byref(buffer),
        ctypes.sizeof(buffer),
        None,
    )

    return (buffer, 0) if ok else (None, ctypes.get_last_error())


def _job_memory(limit: int) -> RawMemory:
    """Pair a job-wide memory limit with what is charged against it.

    Two calls answer the pair, and either can stand in for the other. Where neither answers, the limit is
    reported alone.
    """
    charged = _job_memory_in_use()
    available = _available_commit()

    if charged is None and available is not None:
        charged = max(limit - available, 0)

    if available is None and charged is not None:
        available = max(limit - charged, 0)

    return RawMemory(
        limit=limit,
        used=charged,
        available=available,
        # A job limits commit, and no cache is charged against commit.
        unflushed_cache=None,
        limit_level=_LEVEL,
        unreadable_level=None,
        # Nothing charged takes two unrelated APIs refusing at once, and has never been seen on a machine.
        # A job-wide limit does have a usage figure, so this is a guard rather than a path.
        usage_unreadable_level=_LEVEL if charged is None else None,
    )


def _job_memory_in_use() -> int | None:
    """Read the memory charged to the job right now, in bytes.

    From `JOBOBJECT_LIMIT_VIOLATION_INFORMATION.JobMemory`, which answers with no violation having happened.
    An odd door, and the only one: the documented limit structure carries the peak rather than the current
    figure.
    """
    violation, _ = _query(_JOB_LIMIT_VIOLATION, _LimitViolation)

    return violation.JobMemory if violation is not None else None


def _available_commit() -> int | None:
    """Read what this process may still commit, in bytes.

    It answers against the limit of the immediate job where one applies, and against the headroom of the
    machine where none does. That is the same scope the limits are read at.

    Under a job limit it was measured to equal `JobMemoryLimit` minus `JobMemory` to the byte, which is what
    makes it a stand-in for either. It combines nothing, and answers for the immediate job whatever encloses
    it: a job limited to 64 GiB answered 64 GiB on a machine with 10.8 GiB of commit headroom, and a job of
    1 GiB nested inside one of 256 MiB answered 1 GiB - there the outer limit is what refused a commit of
    512 MiB.
    """
    info = _AppMemory()

    ok = _kernel32.GetProcessInformation(
        _kernel32.GetCurrentProcess(),
        _PROCESS_APP_MEMORY_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )

    return info.AvailableCommit if ok else None


def _rate_cap(control: _RateControl) -> float | None:
    """Read the share of the machine a job may use, between 0 and 1.

    Two of the four accepted forms are caps: a hard cap, and the ceiling of a rate range. A weight is the
    analogue of `cpu.shares`.

    Returns:
        The share, or `None` where the job sets no cap.
    """
    flags = control.ControlFlags
    if not flags & _RATE_CONTROL_ENABLE:
        return None

    if flags & _RATE_CONTROL_MIN_MAX_RATE:
        rate = control.Rate.MaxRate
    elif flags & _RATE_CONTROL_HARD_CAP:
        rate = control.CpuRate
    else:
        return None

    # The kernel refuses to store a rate of zero, with `ERROR_INVALID_PARAMETER`, in every form that carries
    # one - so a zero here is not a cap of nothing.
    return rate / _RATE_SCALE if rate > 0 else None


def _affinity_cores() -> int | None:
    """Count the cores a job's affinity allows, across every processor group.

    Read from the group-aware class rather than from the pointer-sized mask beside the other limits. That mask
    holds one group. Measured on a machine split into groups of 8, 8 and 4: a job allowed two cores in each of
    two groups leaves the mask at zero, where the answer is four. Within one group the two agree exactly,
    whichever group it is.

    Returns:
        The number of allowed cores, or `None` where the affinity cannot be read or counts nothing.
    """
    masks = _group_affinity_masks()
    if masks is None:
        return None

    return sum(mask.bit_count() for mask in masks) or None


def _group_affinity_masks() -> list[int] | None:
    """Read the affinity of the job, as one mask per processor group.

    Unlike every other class read here this one answers with an array, so its length has to be asked for.

    Returns:
        One mask per entry, or `None` where the call failed.
    """
    # Measured: an oversized buffer is accepted, and the length written back is what was filled rather than
    # what was offered. So one call at the maximum group count is enough.
    room = max(_kernel32.GetMaximumProcessorGroupCount(), 1)
    buffer = (_GroupAffinity * room)()
    returned = wintypes.DWORD()

    ok = _kernel32.QueryInformationJobObject(
        None,
        _JOB_GROUP_AFFINITY,
        ctypes.byref(buffer),
        ctypes.sizeof(buffer),
        ctypes.byref(returned),
    )
    if not ok:
        return None

    # Measured over six shapes: the array runs from group 0 to the highest group the job is affinitized to,
    # and a group between them that it uses none of comes back with a mask of nothing - so summing the bits is
    # right whichever shape arrives, and no entry has to be matched to its group.
    return [buffer[index].Mask for index in range(returned.value // ctypes.sizeof(_GroupAffinity))]
