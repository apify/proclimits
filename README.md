# proclimits

Reports the CPU and memory limits that actually apply to the running process. It reads cgroups on Linux and job objects on Windows.

## Why

Inside a container the usual answers describe the machine, not the process. `psutil.virtual_memory().total` reports the memory of the node, `os.cpu_count()` reports every core of it. A process that sizes a budget from those numbers keeps growing until the kernel kills it.

Reading the cgroup files directly is not enough either. The limit that applies is rarely written where the process is, and a cgroup that restricts nothing does not say so by leaving its limit file empty. A consumer that reads one file and takes it at face value believes it has 8 EB of memory.

This package handles both, and reports only what actually restricts the process. `None` means nothing visible to the process restricts it - the machine is then the honest answer, and `get_machine_cpu_count()` and `get_machine_memory_bytes()` below give it.

Off Linux and Windows every limit reads as `None`. macOS still reports the memory and the CPU cores of the machine. On other platforms the machine facts can read as `None`. Some examples below pair this package with `psutil`, which it does not require - `psutil` answers what the machine is using, which is not a question about limits.

## How it reads a limit

In both mechanisms a limit sits on a level above the process: on Linux any cgroup in the chain above it, on Windows the job it was assigned to. This package reads every level it can see and reports the tightest limit among them. Three things follow, and they run through everything below.

**A reported number can describe a level above this process.** Everything under that level shares the limit. The memory of sibling groups is charged against it, and its CPU quota throttles them all. `describe()` names the level each reading came from: `memory_limit_level` for the memory limit, `cpu_limit_level` for the CPU one.

**A limit that restricts nothing is dropped rather than reported.** An unrestricted group still holds a number that looks like a limit: cgroup v1 spells "no limit" as a sentinel near 2\*\*63, a CPU quota can exceed the cores of the machine, and a CPU set can cover every core of it. Every reading is therefore compared against the machine, and one that turns out to restrict nothing is dropped. The answer is then `None`, and `describe().notices` says which reading was dropped and why.

**A limit this process cannot see is not reported.** A container can be given a subtree as its whole cgroup filesystem, a [cgroup namespace](https://man7.org/linux/man-pages/man7/cgroup_namespaces.7.html) can hide the levels above the process, and a Windows job can [sit inside another](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs). A limit there is still enforced, and nothing here can read it. The first is reported as a `*_ANCESTORS_HIDDEN` notice. The other two leave nothing to detect, so the absence of that notice is not a guarantee.

### On Linux

A process does not sit in one cgroup. It sits in a chain of them: its own group, the group above that, and so on up to the root. A limit on [any level of that chain](https://docs.kernel.org/admin-guide/cgroup-v2.html) restricts the process, and the kernel enforces whichever level is tightest. That level is often not the group of the process. Under Kubernetes the limit usually sits on the pod cgroup rather than the container's, and the `kubepods` slice above them holds the memory the node is allowed to hand out at all. Under systemd a scope carries no CPU quota of its own while the slice above it does. This package therefore walks the whole chain.

### On Windows

Only the [immediate job](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject) of a process is read. Its memory limit, its CPU rate cap and its affinity mask are the limits reported. A job limits [commit rather than memory](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information), so its memory limit is judged against the commit limit of the machine, which the page file lifts above the memory. A limit between the two - 6 GiB on a machine of 4 - is enforced, and is reported.

A job can also carry a CPU share rather than a cap, and a memory cap on each of its processes. Neither is a limit on the job, and neither is reported as one. A share is `cpu.weight` by another name, and that is not read on Linux either. A per-process cap restricts a process rather than the job, so no usage pairs with it. Where it is tighter than the job's limit, or the job has none, the memory reading is dropped rather than answered with the looser number, and a `MEMORY_USAGE_UNAVAILABLE` notice names it.

Nesting, the case this mechanism hides, does not arise in a container: a container's own job is the immediate one. It arises where an application wraps itself in a job, as a sandbox or a test harness does. A memory limit read from the inner job is real, merely not the tightest. A CPU rate is a [share of the outer job's share](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_cpu_rate_control_information), so the number reported is higher than what applies; cap the rate in one place.

Inside a Windows container `--memory` and `--cpus` come back as the numbers they were set to. Under Hyper-V isolation only the CPU differs: `--cpus` sizes the guest rather than capping a job, so `get_cpu_limit()` is `None` and `get_machine_cpu_count()` is the number to size a pool from. The guest holds about 1 GiB whatever `--memory` says, so a memory limit above that covers the machine as the guest sees it, and is dropped.

## Use

Size a memory budget from the limit, and fall back to the machine when nothing limits you:

```python
import psutil

import proclimits


def memory_budget() -> tuple[int, int]:
    """The total and the used memory, in bytes.

    For a consumer that sizes a budget from the pair rather than from `MemoryBudget.available`.
    """
    budget = proclimits.get_memory_budget()
    if budget is not None:
        return budget.limit, budget.used

    memory = psutil.virtual_memory()
    return memory.total, memory.total - memory.available
```

Size a worker pool the same way, from the cores you may use:

```python
import proclimits

cores = proclimits.get_cpu_limit() or proclimits.get_machine_cpu_count() or 1
workers = max(1, round(cores))
```

`get_machine_cpu_count()` counts the cores of the machine. `os.cpu_count()` and `psutil.cpu_count()` can report the process instead, which turns a real restriction into the whole node.

Measure the CPU load against what you may use, rather than against the machine. `CpuLoad` is a sampler: each `sample()` returns the load since the previous `sample()` on the same object, so the measurement window is however long you leave between calls:

```python
import time

import psutil

import proclimits

load = proclimits.CpuLoad()

while True:
    used_ratio = load.sample()
    if used_ratio is None:
        used_ratio = psutil.cpu_percent() / 100
    ...
    time.sleep(5)
```

`sample()` never waits: it reads the counter and compares it against the one it kept. Pacing the loop is the caller's job, as the `sleep` above does it. A few seconds is a good gap: the kernel advances the counter in coarse steps, so over a shorter window a busy process can read as idle. Give each caller a `CpuLoad` of its own, or two of them consume each other's windows.

`None` means there is no ratio to report: the first call, nothing restricting the CPU, or two calls too close together.

The load is measured where the limit binds, which may be the slice above this process. An idle service inside a saturated slice therefore reports the slice's load, because that is what predicts its throttling. `describe().cpu_rate_level` names the level.

For a single measurement rather than a series, `get_cpu_used_ratio(interval)` waits out the window itself and `get_cpu_used_ratio_async(interval)` is its asyncio form. Both raise `ValueError` for an interval below 0.01 seconds - a window that short reports noise rather than a load. Nothing else raises: every reading answers `None` where it cannot be read.

Log what applies when a service starts, so a surprising number can be explained later:

```python
import logging

import proclimits

logger = logging.getLogger(__name__)
logger.info(f'resource limits: {proclimits.snapshot()}')

for notice in proclimits.describe().notices:
    logger.info(f'{notice.code}: {notice.message}')
```

## What it reports

Which call for which job: `get_memory_budget().available` to size a memory budget, `get_cpu_limit()` to size a pool, `CpuLoad().sample()` or `get_cpu_used_ratio()` for a load, `describe()` when a number looks wrong.

| Call | Answers |
| --- | --- |
| `get_memory_budget()` | the memory limit and the memory charged against it, or `None` |
| `get_cpu_limit()` | how many cores may be used, or `None` |
| `get_cpu_usage()` | CPU seconds consumed so far, a counter that only grows |
| `CpuLoad().sample()` | the share of the allowed cores used since the previous call |
| `get_cpu_used_ratio(interval)` | the same, measured across one window it waits out |
| `get_cpu_used_ratio_async(interval)` | the same, for asyncio |
| `snapshot()` | a `Snapshot` carrying `memory_budget`, `cpu_limit` and `cpu_usage`, taken at once |
| `describe()` | a `Description`: the readings again, where they came from, the raw values, and why any are missing |
| `get_machine_cpu_count()` | the cores the system lists as active, whatever this process may use |
| `get_machine_memory_bytes()` | the total memory of the machine |
| `clear_cache()` | forget where the readings come from, after the process was moved to another group - a forked child forgets on its own |

Do not divide `get_cpu_usage()` by `get_cpu_limit()`. The counter is read at the closest level that counts any CPU time, and the limit can come from a level above that, so the two describe different scopes. `CpuLoad` pairs them correctly.

### The memory budget

`get_memory_budget()` returns a `MemoryBudget`, which carries four numbers:

| Attribute | Answers |
| --- | --- |
| `available` | how much more memory the limit leaves room for, as the mechanism reports it |
| `limit` | the tightest limit on the chain, in bytes |
| `used` | the memory charged against that limit, as the mechanism counts it |
| `used_ratio` | `used / limit` |

`available` is the number to size a budget from, and it is read from the mechanism rather than worked out from the other two: a kill follows from a distance, not from a ratio. On Linux it is the smallest distance to any limit on the chain, and `used` is whatever keeps that distance exact against the tightest of them - so it need not match the content of any file. On Windows it is what the job may still commit, which is the distance to the job's own limit. `used_ratio` follows from the pair, and is the approximate one.

On Linux the distance counts as free the inactive file cache that is not waiting to reach the disk. The kernel has to reclaim that cache before it hands the memory out. So the number sizes a budget and does not promise the next allocation. Right after writing it reads lower. It stays lower until the data reaches the disk. On default kernel settings that can be more than half a minute after the writing stopped.

It answers for the limit, not for the machine. A host that is itself running out of memory can refuse an allocation that every number here says there is room for. A cgroup limit does not see the host swapping, and a job's commit headroom does not fall when the machine's does.

The level those numbers describe may be above this process - the pod, or the slice. `describe().memory_limit_level` names it. Everything under that level is charged there, so `used_ratio` is the share of the level rather than of this process. `available` still answers for this process: the room left at that level is the room left here.

`used` is counted differently on the two mechanisms. On Linux it leaves out the inactive file cache, less the pages waiting to be written to disk. It equals the working set that `docker stats` and `kubectl top` report while no pages wait. While pages wait, it reads above that working set on kernels that report them. Waiting pages on the active list come off the credit too, so it can read high. Those tools read one cgroup and never walk up, so they agree with this only while a single level holds a limit. On Windows it is the commit charge of the job, the "Commit size" column rather than the "Working set" one: a process that has committed a gigabyte and touched a tenth of it is charged the gigabyte, because that is what the limit counts.

## What it does not do

It reports three facts about the machine and no more: the cores the system lists as active, the total memory, and the size a memory limit is judged against. The first two are what a consumer sizes a pool from when it gets `None`. The third is `describe().memory_limit_ceiling`, and on Windows it is the commit limit rather than the memory. Everything else about the machine - free memory, load, per-process figures - is `psutil`'s job.

Process affinity stays out too. `taskset` on Linux, and `SetProcessAffinityMask` or [`SetProcessDefaultCpuSets`](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setprocessdefaultcpusets) on Windows, narrow one process without narrowing the group its CPU time is accounted to, so what they set is not a limit on the group. An affinity mask on a *job* is a property of the group, and that one is read, exactly as a cpuset is.

## Diagnostics

`describe()` answers why a number looks wrong. It repeats the readings themselves, so one dump says what was reported as well as why, and adds:

| Field | Carries |
| --- | --- |
| `raw_memory_limit`, `raw_cpu_quota`, `raw_cpu_set_size` | the values as the mechanism spells them, before anything was dropped |
| `raw_memory_used` | the usage paired with the raw limit |
| `raw_memory_available` | the room the mechanism reported, before it was brought within the distance to the limit |
| `raw_memory_unflushed_cache` | the pages waiting to reach the disk, at the level the room was measured at, which can be above `memory_limit_level` |
| `memory_limit_level`, `cpu_limit_level` | the level each limit came from - the memory one names it even where the reading was dropped |
| `cpu_rate_level` | the level a CPU rate is measured in |
| `memory_source`, `cpu_quota_source`, `cpu_set_source`, `cpu_usage_source` | the interface each metric would be read through, and the chain searched for it |
| `memory_limit_ceiling`, `machine_cpu_count` | what the readings were compared against - the ceiling is the memory of the machine for a cgroup, the commit limit for a job |
| `machine_memory_bytes` | the memory of the machine, which is what a pool is sized from |
| `notices` | what is worth knowing about a reading or the mechanism, each carrying a `code` and a `message` |

`Source.levels` is the whole chain that was searched, not the levels that answered. Most levels on a chain hold no limit, which is normal - the chain is searched once and kept, because a limit can be written to any of them later. A job object is one level, named `job`. The chain is empty where the mechanism is present with nothing to search: a hierarchy binding no such controller, or a process in no job. The level a reading did come from is `memory_limit_level` or `cpu_limit_level`.

A limit of `None` with no notice about it means nothing went wrong: the mechanism was there, and no level held a limit. Only hard limits count - [`memory.high`](https://docs.kernel.org/admin-guide/cgroup-v2.html) throttles reclaim rather than killing, so a cgroup can sit above it indefinitely and nothing here reports that.

`Source.interface` is an `Interface` member - `CGROUP_V2`, `CGROUP_V1` or `WINDOWS_JOB_OBJECT` - and a notice carries a `NoticeCode`. Branch on those rather than on the strings they print as:

| `NoticeCode` | What happened | Example |
| --- | --- | --- |
| `MEMORY_LIMIT_COVERS_MACHINE` | the limit reaches `memory_limit_ceiling`, so it restricts nothing | a cgroup v1 container started without `-m`, whose limit file holds the sentinel near 2\*\*63 |
| `CPU_QUOTA_COVERS_MACHINE` | the quota allows the cores of the machine or more | `--cpus=8` on an 8-core host |
| `CPU_SET_COVERS_MACHINE` | the allowed set covers every core of the machine | a container with no `--cpuset-cpus`, whose effective set lists every core of the host; or a job whose affinity mask lists them all |
| `MEMORY_USAGE_UNAVAILABLE` | a limit was found, and the mechanism pairs no usage figure with a limit of that kind | a Windows job's per-process memory limit, charged per process while the job counts them together |
| `MEMORY_USAGE_UNREADABLE` | a limit was found, and the level carrying the usage to pair with it did not answer | an ancestor that holds a limit and no readable `memory.current` |
| `MACHINE_MEMORY_UNKNOWN` | the size a limit is judged against cannot be read, so a limit cannot be told apart from a sentinel | a sandbox that masks `/proc/meminfo` but leaves `/sys/fs/cgroup` in place |
| `MACHINE_CPU_COUNT_UNKNOWN` | the cores of the machine cannot be read, and a limit stated as a share of it cannot be turned into cores | a Windows job carrying a CPU rate, where the system will not say how many cores it has |
| `CPU_USAGE_SCOPE_MISMATCH` | the level the CPU limit applies to counts no CPU time, so no rate can be measured | cgroup v1 with `cpu` and `cpuacct` on separate mounts, where the level holding the quota has no counterpart under the accounting one |
| `MEMORY_METRICS_UNAVAILABLE` | no mechanism here is present to carry a memory limit | a host with no cgroup filesystem mounted, or a platform this package has no backend for |
| `CPU_METRICS_UNAVAILABLE` | no mechanism here is present to carry a CPU limit | the same |
| `MEMORY_ANCESTORS_HIDDEN` | only part of the memory hierarchy is visible, so a limit above it is enforced but unreadable | a container given a subtree as its whole `/sys/fs/cgroup` |
| `CPU_ANCESTORS_HIDDEN` | only part of a CPU hierarchy is visible | the same |
| `MEMORY_LIMIT_UNREADABLE` | a level holds a memory limit that says nothing usable, so what it enforces is unknown | a hand-built or mocked cgroup tree; no kernel writes such a value |
| `CPU_LIMIT_UNREADABLE` | the same for a CPU limit | the same, plus a `cpuset.cpus` no kernel writes, such as the overlapping `0-2,1-3` |
