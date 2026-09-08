from __future__ import annotations

import ast
import ctypes
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='job objects are a Windows mechanism')

if sys.platform == 'win32':
    from cgroups_sensor import _windows

if TYPE_CHECKING:
    from collections.abc import Callable

MIB = 1024 * 1024

QUERY_FAILED = 1450
"""`ERROR_NO_SYSTEM_RESOURCES`: a failure that is not the kernel saying this process is in no job."""

ERROR_BAD_LENGTH = 24
"""What the kernel refuses a buffer of the wrong size with, before it looks at whether there is a job."""

# Spelled again here rather than imported, so that a typo in the module is a failure rather than agreement.
ENABLE = 0x1
WEIGHT_BASED = 0x2
HARD_CAP = 0x4
MIN_MAX_RATE = 0x10
LIMIT_AFFINITY = 0x10
LIMIT_JOB_MEMORY = 0x200
LIMIT_PROCESS_MEMORY = 0x100


def rate_control(flags: int, *, cpu_rate: int = 0, min_rate: int = 0, max_rate: int = 0) -> Any:
    """One CPU rate control structure, filled the way the flags say it should be read."""
    control = _windows._RateControl()
    control.ControlFlags = flags

    if flags & _windows._RATE_CONTROL_MIN_MAX_RATE:
        control.Rate.MinRate = min_rate
        control.Rate.MaxRate = max_rate
    else:
        control.CpuRate = cpu_rate

    return control


def extended_limits(*, flags: int = 0, memory: int = 0, affinity: int = 0, process_memory: int = 0) -> Any:
    """One extended limit structure, with the fields this package reads and the affinity mask beside them."""
    limits = _windows._ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = flags
    limits.BasicLimitInformation.Affinity = affinity
    limits.JobMemoryLimit = memory
    limits.ProcessMemoryLimit = process_memory

    return limits


def answering(**by_class: Any) -> Callable[..., Any]:
    """A stand-in for the job query that answers the named information classes, and fails on the rest."""
    answers = {getattr(_windows, name): value for name, value in by_class.items()}

    def query(info_class: int, structure: Any) -> tuple[Any, int]:
        if info_class in answers:
            # The answer a test writes is of the structure that class carries, so this catches a class paired
            # with the wrong one. The kernel refuses that pairing by length; nothing else here would.
            assert isinstance(answers[info_class], structure)

            return answers[info_class], 0

        return None, QUERY_FAILED

    return query


def without_a_job() -> Callable[..., Any]:
    """A stand-in for the job query that answers every class the way the kernel does with no job."""
    return lambda _info_class, _structure: (None, _windows._ERROR_NO_JOB)


@pytest.mark.parametrize(
    ('flags', 'rates', 'expected'),
    [
        pytest.param(ENABLE | HARD_CAP, {'cpu_rate': 750}, 0.075, id='a hard cap is a cap'),
        pytest.param(ENABLE | MIN_MAX_RATE, {'min_rate': 1, 'max_rate': 5000}, 0.5, id='a maximum is a cap'),
        pytest.param(ENABLE | WEIGHT_BASED, {'cpu_rate': 5}, None, id='a weight is not a cap'),
        pytest.param(ENABLE, {'cpu_rate': 750}, None, id='a share on its own is not a cap'),
        pytest.param(0, {}, None, id='rate control is off'),
        pytest.param(ENABLE | HARD_CAP, {'cpu_rate': 0}, None, id='a cap of nothing is not a cap'),
    ],
)
def test_rate_cap(flags: int, rates: dict[str, int], expected: float | None) -> None:
    """Reads a share of the machine only from the two forms that are ceilings."""
    assert _windows._rate_cap(rate_control(flags, **rates)) == expected


def test_rate_cap_reads_the_flags_before_the_union() -> None:
    """Reads the member the flags name, because the wrong one answers with a number rather than an error."""
    control = rate_control(ENABLE | MIN_MAX_RATE, min_rate=1, max_rate=5000)

    # The same bytes read as a `CpuRate`, which is what makes reading the flags first the whole point.
    assert control.CpuRate == 5000 << 16 | 1
    assert _windows._rate_cap(control) == 0.5


@pytest.mark.parametrize(
    ('masks', 'expected'),
    [
        pytest.param([0b111], 3, id='three cores in one group'),
        pytest.param([0b1001], 2, id='a mask with a gap'),
        pytest.param([0b11, 0b11], 4, id='two cores in each of two groups'),
        pytest.param([0, 0, 0b111], 3, id='groups the job uses none of still answer'),
        pytest.param([0xFF, 0xFF, 0xF], 20, id='every core of every group'),
        pytest.param([0, 0], None, id='an affinity of no cores at all'),
        pytest.param([], None, id='no groups answered'),
    ],
)
def test_affinity_cores(monkeypatch: pytest.MonkeyPatch, masks: list[int], expected: int | None) -> None:
    """Counts the cores a job's affinity allows by summing its groups, which one mask cannot do."""
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: masks)

    assert _windows._affinity_cores() == expected


def test_affinity_cores_when_the_groups_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers nothing where the call failed, which the caller reports as a restriction of unknown size."""
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: None)

    assert _windows._affinity_cores() is None


def test_read_memory_without_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports no limit, and no failure, where nothing put this process in a job."""
    monkeypatch.setattr(_windows, '_query', without_a_job())

    assert _windows.read_memory() == _windows._NO_MEMORY


def test_read_memory_when_the_job_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls a job that will not answer unreadable, rather than unlimited."""
    monkeypatch.setattr(_windows, '_query', answering())

    assert _windows.read_memory().unreadable_level == 'job'


def test_read_memory_without_a_memory_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports nothing where the job answers and carries no memory limit."""
    monkeypatch.setattr(_windows, '_query', answering(_JOB_EXTENDED_LIMIT=extended_limits()))

    assert _windows.read_memory() == _windows._NO_MEMORY


def test_read_memory_falls_back_to_the_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers with the distance to the limit where the commit figure cannot be read."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(_JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_JOB_MEMORY, memory=512 * MIB)),
    )
    monkeypatch.setattr(_windows, '_job_memory_in_use', lambda: 12 * MIB)
    monkeypatch.setattr(_windows, '_available_commit', lambda: None)

    raw = _windows.read_memory()

    assert (raw.limit, raw.used, raw.available) == (512 * MIB, 12 * MIB, 500 * MIB)


def test_read_memory_keeps_what_the_mechanism_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports the commit figure as it was read, rather than the distance the usage figure implies."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(_JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_JOB_MEMORY, memory=512 * MIB)),
    )
    monkeypatch.setattr(_windows, '_job_memory_in_use', lambda: 12 * MIB)
    monkeypatch.setattr(_windows, '_available_commit', lambda: 8 * MIB)

    assert _windows.read_memory().available == 8 * MIB


def test_read_memory_derives_the_usage_from_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers with a whole budget where only the undocumented usage class failed."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(_JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_JOB_MEMORY, memory=512 * MIB)),
    )
    monkeypatch.setattr(_windows, '_job_memory_in_use', lambda: None)
    monkeypatch.setattr(_windows, '_available_commit', lambda: 500 * MIB)

    raw = _windows.read_memory()

    assert (raw.limit, raw.used, raw.available) == (512 * MIB, 12 * MIB, 500 * MIB)


def test_read_memory_without_a_usage_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps the limit and drops the pair where neither call answers with a figure to pair it with."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(_JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_JOB_MEMORY, memory=512 * MIB)),
    )
    monkeypatch.setattr(_windows, '_job_memory_in_use', lambda: None)
    monkeypatch.setattr(_windows, '_available_commit', lambda: None)

    raw = _windows.read_memory()

    assert (raw.limit, raw.used, raw.available) == (512 * MIB, None, None)
    # Two calls failed, so this is a fault to look into rather than the shape of the job.
    assert raw.usage_unreadable_level == 'job'


def test_read_memory_with_only_a_per_process_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Carries a per-process limit as a limit with no usage, which drops the reading and says why."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(_JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_PROCESS_MEMORY, process_memory=256 * MIB)),
    )

    raw = _windows.read_memory()

    assert (raw.limit, raw.used, raw.available) == (256 * MIB, None, None)
    assert raw.limit_level == 'job'
    # No call failed: a per-process limit is a shape the job pairs no usage figure with.
    assert raw.usage_unreadable_level is None


def test_read_memory_prefers_a_tighter_per_process_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports the per-process limit over the job's where it is the tighter of the two."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            _JOB_EXTENDED_LIMIT=extended_limits(
                flags=LIMIT_JOB_MEMORY | LIMIT_PROCESS_MEMORY, memory=512 * MIB, process_memory=256 * MIB
            )
        ),
    )
    monkeypatch.setattr(_windows, '_job_memory_in_use', lambda: 12 * MIB)
    monkeypatch.setattr(_windows, '_available_commit', lambda: 500 * MIB)

    raw = _windows.read_memory()

    # Measured: the commit figure answers for the job-wide limit and is blind to this one, so pairing them
    # would hand out room the kernel refuses.
    assert (raw.limit, raw.used, raw.available) == (256 * MIB, None, None)


def test_read_memory_keeps_the_job_limit_when_it_is_tighter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignores a per-process limit that is looser than the job's, which restricts nothing further."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            _JOB_EXTENDED_LIMIT=extended_limits(
                flags=LIMIT_JOB_MEMORY | LIMIT_PROCESS_MEMORY, memory=256 * MIB, process_memory=512 * MIB
            )
        ),
    )
    monkeypatch.setattr(_windows, '_job_memory_in_use', lambda: 12 * MIB)
    monkeypatch.setattr(_windows, '_available_commit', lambda: 200 * MIB)

    raw = _windows.read_memory()

    assert (raw.limit, raw.used, raw.available) == (256 * MIB, 12 * MIB, 200 * MIB)


def test_read_cpu_without_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports no restriction, and no failure, where nothing put this process in a job."""
    monkeypatch.setattr(_windows, '_query', without_a_job())

    raw = _windows.read_cpu()

    assert (raw.quota, raw.cpu_set, raw.unreadable_level) == (None, None, None)


def test_read_cpu_when_the_job_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls a job that will not answer unreadable here too, so no looser answer is invented."""
    monkeypatch.setattr(_windows, '_query', answering())

    assert _windows.read_cpu().unreadable_level == 'job'


def test_read_cpu_takes_both_restrictions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads the rate and the affinity mask separately, each in cores."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            _JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_AFFINITY, affinity=0b111),
            _JOB_CPU_RATE_CONTROL=rate_control(ENABLE | HARD_CAP, cpu_rate=750),
        ),
    )
    monkeypatch.setattr(_windows, 'machine_cpu_count', lambda: 20)
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: [0b111])

    raw = _windows.read_cpu()

    assert raw.quota is not None
    assert raw.quota.cores == 1.5
    assert raw.cpu_set is not None
    assert raw.cpu_set.cores == 3


def test_read_cpu_without_the_machine_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drops both readings where a rate cannot be sized, because the share may be the tighter one."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            _JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_AFFINITY, affinity=0b111),
            _JOB_CPU_RATE_CONTROL=rate_control(ENABLE | HARD_CAP, cpu_rate=750),
        ),
    )
    monkeypatch.setattr(_windows, 'machine_cpu_count', lambda: None)
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: [0b111])

    raw = _windows.read_cpu()

    assert raw.unconvertible_level == 'job'
    assert (raw.quota, raw.cpu_set, raw.unreadable_level) == (None, None, None)


def test_read_cpu_keeps_an_affinity_without_the_machine_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers with a mask alone where no rate is set, because counting a mask needs no machine."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            _JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_AFFINITY, affinity=0b111),
            _JOB_CPU_RATE_CONTROL=rate_control(0),
        ),
    )
    monkeypatch.setattr(_windows, 'machine_cpu_count', lambda: None)
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: [0b111])

    raw = _windows.read_cpu()

    assert raw.unconvertible_level is None
    assert raw.cpu_set is not None
    assert raw.cpu_set.cores == 3


def test_read_cpu_when_only_the_rate_control_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drops both restrictions where the limits answered and the rate control did not."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(_JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_AFFINITY, affinity=0b111)),
    )

    raw = _windows.read_cpu()

    assert (raw.quota, raw.cpu_set, raw.unreadable_level) == (None, None, 'job')


def test_machine_cpu_count_falls_back_on_one_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts through the older call where the group-aware one answers nothing and there is one group."""
    if _windows._kernel32.GetActiveProcessorGroupCount() != 1:
        pytest.skip('the fallback is only claimed to count the machine where the machine is one group')

    # Taken through the path that works before that path is broken, so the two are compared rather than one
    # of them restated. `os.cpu_count()` would not do: it honours an override this module exists to avoid.
    expected = _windows.machine_cpu_count()

    monkeypatch.setattr(_windows._kernel32, 'GetActiveProcessorCount', lambda _groups: 0)
    monkeypatch.setattr(_windows._kernel32, 'GetActiveProcessorGroupCount', lambda: 1)

    assert _windows.machine_cpu_count() == expected


def test_machine_cpu_count_refuses_the_fallback_across_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers nothing rather than one group's worth, measured at 8 and at 4 on a machine of 20."""
    monkeypatch.setattr(_windows._kernel32, 'GetActiveProcessorCount', lambda _groups: 0)
    monkeypatch.setattr(_windows._kernel32, 'GetActiveProcessorGroupCount', lambda: 3)

    assert _windows.machine_cpu_count() is None


def test_read_cpu_when_the_affinity_cannot_be_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls a job holding an affinity it will not detail unreadable, rather than unrestricted."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            _JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_AFFINITY, affinity=0b111),
            _JOB_CPU_RATE_CONTROL=rate_control(0),
        ),
    )
    monkeypatch.setattr(_windows, 'machine_cpu_count', lambda: 20)
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: None)

    raw = _windows.read_cpu()

    assert (raw.quota, raw.cpu_set, raw.unreadable_level) == (None, None, 'job')


def test_read_cpu_ignores_the_single_group_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts the groups rather than the mask beside the other limits, which spans none of them."""
    monkeypatch.setattr(
        _windows,
        '_query',
        answering(
            # Measured on a machine of several groups: an affinity spanning two sets the flag and zeroes this
            # mask, so counting its bits would report no restriction where four cores are enforced.
            _JOB_EXTENDED_LIMIT=extended_limits(flags=LIMIT_AFFINITY, affinity=0),
            _JOB_CPU_RATE_CONTROL=rate_control(0),
        ),
    )
    monkeypatch.setattr(_windows, 'machine_cpu_count', lambda: 20)
    monkeypatch.setattr(_windows, '_group_affinity_masks', lambda: [0b11, 0b11])

    raw = _windows.read_cpu()

    assert raw.cpu_set is not None
    assert raw.cpu_set.cores == 4


def test_read_cpu_usage_converts_the_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns the two halves of the job's consumed time into seconds."""
    accounting = _windows._Accounting()
    accounting.TotalUserTime = 15_000_000
    accounting.TotalKernelTime = 10_000_000
    monkeypatch.setattr(_windows, '_query', answering(_JOB_BASIC_ACCOUNTING=accounting))

    assert _windows.read_cpu_usage() == 2.5
    assert _windows.read_cpu_usage('job') == 2.5


def test_read_cpu_usage_refuses_another_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers nothing for a level that is not the job, rather than the time of the job."""
    monkeypatch.setattr(_windows, '_query', answering(_JOB_BASIC_ACCOUNTING=object()))

    assert _windows.read_cpu_usage('/sys/fs/cgroup') is None


def test_sources_name_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names one level for every metric while this process is in a job."""
    monkeypatch.setattr(_windows, '_in_job', lambda: True)

    sources = _windows.sources()

    assert sources.memory is not None
    assert str(sources.memory.interface) == 'windows-job-object'
    assert sources.memory.levels == ('job',)
    assert all(source == sources.memory for source in (sources.cpu_quota, sources.cpu_set, sources.cpu_usage))


def test_sources_without_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps the mechanism and names no level, which is not the same as having no mechanism."""
    monkeypatch.setattr(_windows, '_in_job', lambda: False)

    sources = _windows.sources()

    assert sources.memory is not None
    assert sources.memory.levels == ()


def test_in_job_without_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads the one error code that means this process is in no job."""
    monkeypatch.setattr(_windows, '_query', without_a_job())

    assert _windows._in_job() is False


def test_in_job_when_the_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps the job where a call failed for any other reason, rather than reporting it away."""
    monkeypatch.setattr(_windows, '_query', answering())

    assert _windows._in_job() is True


def test_mechanism_notices_are_silent() -> None:
    """Says nothing about the blind spot, which cannot be detected and would therefore never vary."""
    assert _windows.mechanism_notices() == ()


def test_machine_facts_answer() -> None:
    """Reads the memory, the ceiling and the cores of the machine this test runs on."""
    assert (_windows.machine_memory_bytes() or 0) > 0
    assert (_windows.memory_limit_ceiling() or 0) > 0
    assert (_windows.machine_cpu_count() or 0) > 0


def memory_status(*, total_phys: int, total_page_file: int) -> Callable[[Any], int]:
    """A `GlobalMemoryStatusEx` that fills the two totals and answers success."""

    def fake(pointer: Any) -> int:
        status = pointer._obj
        status.ullTotalPhys = total_phys
        status.ullTotalPageFile = total_page_file

        return 1

    return fake


@pytest.mark.parametrize(
    'total_page_file',
    [
        pytest.param(12288 * MIB, id='a page file lifts the ceiling above physical memory'),
        pytest.param(7168 * MIB, id='no page file leaves it below what the machine holds'),
    ],
)
def test_the_ceiling_and_the_machine_read_different_fields(
    monkeypatch: pytest.MonkeyPatch, total_page_file: int
) -> None:
    """Judges a limit against the commit limit and sizes a pool from physical memory, in either order."""
    monkeypatch.setattr(
        _windows._kernel32,
        'GlobalMemoryStatusEx',
        memory_status(total_phys=8192 * MIB, total_page_file=total_page_file),
    )

    # Never asserted against each other: which of the two is larger is a property of the page file, not of
    # this package, and a machine without one puts the ceiling below the memory it holds.
    assert _windows.machine_memory_bytes() == 8192 * MIB
    assert _windows.memory_limit_ceiling() == total_page_file


def test_clear_cache_does_nothing() -> None:
    """Forgets nothing, because nothing is discovered: every reading asks the job directly."""
    assert _windows.clear_cache() is None


@pytest.mark.parametrize(
    ('info_class', 'structure'),
    [
        pytest.param('_JOB_BASIC_ACCOUNTING', '_Accounting', id='accounting'),
        pytest.param('_JOB_EXTENDED_LIMIT', '_ExtendedLimits', id='extended limits'),
        pytest.param('_JOB_LIMIT_VIOLATION', '_LimitViolation', id='limit violation'),
        pytest.param('_JOB_CPU_RATE_CONTROL', '_RateControl', id='cpu rate control'),
    ],
)
def test_the_kernel_accepts_the_layout(info_class: str, structure: str) -> None:
    """Asks the running kernel whether each buffer is the size it expects, for the architecture in use."""
    _, error = _windows._query(getattr(_windows, info_class), getattr(_windows, structure))

    # Any other code answers the question that was asked, which is only about the length: outside a job every
    # class is refused with `_ERROR_NO_JOB`, and inside one every class answers. Not a complete guard on its
    # own - `_JOB_LIMIT_VIOLATION` accepts a later 88-byte form of its structure as well.
    assert error != ERROR_BAD_LENGTH


def test_the_kernel_accepts_the_process_layouts() -> None:
    """Reads through the two calls that check a length of their own, which a wrong size would refuse."""
    assert _windows._available_commit() is not None
    assert _windows.machine_memory_bytes() is not None


def declared_structures() -> set[str]:
    """Every ctypes structure and union the module declares."""
    return {
        name
        for name, value in vars(_windows).items()
        if isinstance(value, type) and issubclass(value, (ctypes.Structure, ctypes.Union))
    }


# The size of every structure the module declares, measured on this architecture. The kernel checks the length
# of every query above, which covers any machine the tests run on; these pin the same thing at the point of the
# edit, and name the structure that moved.
STRUCTURE_SIZES = {
    '_IoCounters': 48,
    '_BasicLimits': 64,
    '_ExtendedLimits': 144,
    '_GroupAffinity': 16,
    '_Accounting': 48,
    '_LimitViolation': 80,
    '_Rate': 4,
    '_RateUnion': 4,
    '_RateControl': 8,
    '_AppMemory': 32,
    '_MemoryStatus': 64,
    '_SystemInfo': 48,
}

# Where every field the module reads sits, measured on this architecture. The buffer is the length the kernel
# wants whichever order two fields of one type sit in. `JobMemory` reading where `JobMemoryLimit` sits would
# then report every job as pinned at its limit, with nothing to say so. A few fields the module never reads
# are pinned beside those it does.
FIELD_OFFSETS = {
    ('_ExtendedLimits', 'BasicLimitInformation'): 0,
    ('_ExtendedLimits', 'IoInfo'): 64,
    ('_ExtendedLimits', 'ProcessMemoryLimit'): 112,
    ('_ExtendedLimits', 'JobMemoryLimit'): 120,
    ('_BasicLimits', 'LimitFlags'): 16,
    ('_BasicLimits', 'Affinity'): 48,
    ('_GroupAffinity', 'Mask'): 0,
    ('_GroupAffinity', 'Group'): 8,
    ('_LimitViolation', 'LimitFlags'): 0,
    ('_LimitViolation', 'JobMemory'): 56,
    # Pinned though nothing reads it: `test_every_field_the_module_reads_has_a_pinned_offset` matches by
    # field name, and `JobMemoryLimit` is read on `_ExtendedLimits`. Measured by mutation - exchanging it
    # with `PerJobUserTimeLimit` fails this check alone.
    ('_LimitViolation', 'JobMemoryLimit'): 64,
    ('_Accounting', 'TotalUserTime'): 0,
    ('_Accounting', 'TotalKernelTime'): 8,
    ('_RateControl', 'ControlFlags'): 0,
    ('_RateControl', 'CpuRate'): 4,
    ('_RateControl', 'Rate'): 4,
    ('_Rate', 'MinRate'): 0,
    ('_Rate', 'MaxRate'): 2,
    ('_RateUnion', 'CpuRate'): 0,
    ('_RateUnion', 'Rate'): 0,
    ('_AppMemory', 'AvailableCommit'): 0,
    # `ullTotalPhys` is the machine memory and `ullTotalPageFile` the ceiling a memory limit is filtered
    # against. `ullAvailPhys` sits at 16 and would pass for the total without moving the size of anything.
    ('_MemoryStatus', 'dwLength'): 0,
    ('_MemoryStatus', 'ullTotalPhys'): 8,
    ('_MemoryStatus', 'ullTotalPageFile'): 24,
    ('_SystemInfo', 'dwNumberOfProcessors'): 32,
}


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason='these are the sizes of the 64-bit layout')
@pytest.mark.parametrize(
    ('name', 'expected'),
    [pytest.param(name, size, id=name) for name, size in STRUCTURE_SIZES.items()],
)
def test_structure_size(name: str, expected: int) -> None:
    """Pins the length the kernel checks a query against, which the query itself cannot report as wrong."""
    assert ctypes.sizeof(getattr(_windows, name)) == expected


def test_every_structure_has_a_pinned_size() -> None:
    """Fails where a structure was added without a size, rather than leaving it unchecked."""
    assert declared_structures() == set(STRUCTURE_SIZES)


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason='these are the offsets of the 64-bit layout')
@pytest.mark.parametrize(
    ('name', 'field', 'expected'),
    [pytest.param(name, field, offset, id=f'{name}.{field}') for (name, field), offset in FIELD_OFFSETS.items()],
)
def test_structure_field_offset(name: str, field: str, expected: int) -> None:
    """Pins where each field in the table above sits, which neither a size nor the kernel can check."""
    assert getattr(getattr(_windows, name), field).offset == expected


def test_every_field_the_module_reads_has_a_pinned_offset() -> None:
    """Fails where the module began reading a field whose place nothing above checks."""
    # Read from the source rather than listed here, so the table cannot fall behind the module.
    source = Path(_windows.__file__).read_text(encoding='utf-8')
    named = {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}

    read = {
        (name, field)
        for name in declared_structures()
        for field, _ in getattr(_windows, name)._fields_
        if field in named
    }

    assert read - set(FIELD_OFFSETS) == set()
