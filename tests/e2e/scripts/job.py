from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from ctypes import wintypes

# The layouts below are written out again rather than imported from the package. An instrument that shares a
# layout with its subject agrees with a broken one. The names are shared on purpose: a name carries no layout.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS = 15

JOB_OBJECT_LIMIT_AFFINITY = 0x10
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100

CPU_RATE_CONTROL_ENABLE = 0x1
CPU_RATE_CONTROL_WEIGHT_BASED = 0x2
CPU_RATE_CONTROL_HARD_CAP = 0x4
CPU_RATE_CONTROL_MIN_MAX_RATE = 0x10

CANNOT_SET_UP = 77
"""What this exits with when the job could not be built, so a failure is not read as a wrong reading."""

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)


class IoCounters(ctypes.Structure):
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


class BasicLimits(ctypes.Structure):
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


class ExtendedLimits(ctypes.Structure):
    _fields_ = (
        ('BasicLimitInformation', BasicLimits),
        ('IoInfo', IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    )


class Rate(ctypes.Structure):
    _fields_ = (('MinRate', wintypes.WORD), ('MaxRate', wintypes.WORD))


class RateUnion(ctypes.Union):
    _fields_ = (('CpuRate', wintypes.DWORD), ('Weight', wintypes.DWORD), ('Rate', Rate))


class RateControl(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = (('ControlFlags', wintypes.DWORD), ('u', RateUnion))


kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
kernel32.SetInformationJobObject.restype = wintypes.BOOL


def give_up(what: str) -> None:
    """Say which call failed, and exit so the caller can tell setup from a wrong reading."""
    code = ctypes.get_last_error()
    print(f'{what} failed: {code} ({ctypes.FormatError(code).strip()})', file=sys.stderr)
    raise SystemExit(CANNOT_SET_UP)


def enter_job() -> int:
    """Put this process in a new job, and hand back the handle so limits can be written to it."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        give_up('CreateJobObject')

    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        give_up('AssignProcessToJobObject')

    return job


def set_memory_and_affinity(job: int, *, memory: int, affinity: int, process_memory: int = 0) -> None:
    """Write the memory limits and the affinity mask, whichever of them were asked for."""
    if not memory and not affinity and not process_memory:
        return

    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = (
        (JOB_OBJECT_LIMIT_JOB_MEMORY if memory else 0)
        | (JOB_OBJECT_LIMIT_PROCESS_MEMORY if process_memory else 0)
        | (JOB_OBJECT_LIMIT_AFFINITY if affinity else 0)
    )
    info.JobMemoryLimit = memory
    info.ProcessMemoryLimit = process_memory
    info.BasicLimitInformation.Affinity = affinity

    ok = kernel32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        give_up('SetInformationJobObject(extended limits)')


def set_cpu_rate(job: int, *, hard_cap: int, max_rate: int, weight: int, share: int) -> None:
    """Write one of the four forms of CPU rate control, or none of them."""
    if not (hard_cap or max_rate or weight or share):
        return

    info = RateControl()

    if max_rate:
        info.ControlFlags = CPU_RATE_CONTROL_ENABLE | CPU_RATE_CONTROL_MIN_MAX_RATE
        # A minimum of nothing is refused, so the range starts at the smallest share there is.
        info.Rate.MinRate = 1
        info.Rate.MaxRate = max_rate
    elif weight:
        info.ControlFlags = CPU_RATE_CONTROL_ENABLE | CPU_RATE_CONTROL_WEIGHT_BASED
        info.Weight = weight
    elif hard_cap:
        info.ControlFlags = CPU_RATE_CONTROL_ENABLE | CPU_RATE_CONTROL_HARD_CAP
        info.CpuRate = hard_cap
    else:
        info.ControlFlags = CPU_RATE_CONTROL_ENABLE
        info.CpuRate = share

    ok = kernel32.SetInformationJobObject(
        job, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        give_up('SetInformationJobObject(cpu rate control)')


def main() -> None:
    """Build the job the test asked for, then run the command inside it."""
    parser = argparse.ArgumentParser(description='Run a command inside a job object carrying the named limits.')
    parser.add_argument('--memory', type=int, default=0, help='JobMemoryLimit in bytes')
    parser.add_argument('--process-memory', type=int, default=0, help='ProcessMemoryLimit in bytes')
    parser.add_argument('--affinity', type=int, default=0, help='affinity mask')
    parser.add_argument('--hard-cap', type=int, default=0, help='CpuRate under HARD_CAP')
    parser.add_argument('--max-rate', type=int, default=0, help='MaxRate under MIN_MAX_RATE')
    parser.add_argument('--weight', type=int, default=0, help='Weight under WEIGHT_BASED')
    parser.add_argument('--share', type=int, default=0, help='CpuRate with rate control merely enabled')
    parser.add_argument(
        '--nest',
        action='append',
        type=int,
        default=[],
        metavar='BYTES',
        help='wrap the job in an outer one of this memory limit first, repeatable',
    )
    parser.add_argument('command', nargs='+', help='the command to run inside the job, after a bare --')

    args = parser.parse_args()

    # Outermost first: each call puts this process in a job nested inside the previous one.
    for outer in args.nest:
        set_memory_and_affinity(enter_job(), memory=outer, affinity=0)

    job = enter_job()
    set_memory_and_affinity(job, memory=args.memory, affinity=args.affinity, process_memory=args.process_memory)
    set_cpu_rate(job, hard_cap=args.hard_cap, max_rate=args.max_rate, weight=args.weight, share=args.share)

    # A child of a process in a job lands in that job, so the probe reads the limits set above.
    raise SystemExit(subprocess.run(args.command, check=False).returncode)


if __name__ == '__main__':
    main()
