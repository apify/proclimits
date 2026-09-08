from __future__ import annotations

import atexit
import dataclasses
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / 'src'
PROBE = Path(__file__).parent / 'probe.py'

IMAGE = 'python:3.13-alpine'
"""The image the container tests use. A small musl image, and it ships `PYTHON_VERSION` itself."""

DOCKER_HUB = 'docker.io'
"""The registry the images above live in, for the engines that do not assume one."""


@dataclass(frozen=True)
class Engine:
    """A container engine, and how a test reaches it."""

    name: str
    """What this engine's uv cache is kept under. A rootful engine writes into it as real root, which a
    rootless one cannot then use."""

    command: tuple[str, ...]
    """How the engine is started, `sudo` included where the containers have to be root's."""

    registry: str | None = None
    """The registry to name in front of an image, or `None` where the engine assumes one. Podman refuses a
    bare name that several of them could answer."""


DOCKER = Engine(name='docker', command=('docker',))
PODMAN = Engine(name='podman', command=('podman',), registry=DOCKER_HUB)
ROOTFUL_PODMAN = Engine(name='podman-root', command=('sudo', 'podman'), registry=DOCKER_HUB)

DEBIAN_IMAGE = 'python:3.13-slim'
"""For the tests whose setup needs a tool the busybox of an alpine image does not carry, such as `unshare`."""

DISTRO_IMAGES = (
    IMAGE,
    DEBIAN_IMAGE,
    'fedora:43',
    'rockylinux:9',
)
"""Images the readings are compared across: musl, Debian glibc, and two distributions of another family."""

PYTHON_VERSION = '3.13'
"""The Python version the container lanes and the Windows stage ask uv for. The image supplies it where it matches."""

PYTHON_VERSIONS = ('3.10', '3.11', '3.12', '3.13', '3.14')
"""Every interpreter the package supports, as the classifiers list them."""

UV_DOWNLOADS = Path(tempfile.gettempdir()) / 'cgroups-sensor-e2e-uv'
"""Where uv and its interpreters are kept, so only the first run pays for the download."""

WINDOWS_DOWNLOADS = Path(tempfile.gettempdir()) / 'cgroups-sensor-e2e-windows'
"""Where each run stages the Windows interpreter and the probe."""

WINDOWS_MOUNT = 'C:\\probe'
"""Where the staged directory is mounted inside a Windows container."""

MIB = 1024 * 1024

JOB_MEMBERSHIP = (
    'import ctypes;'
    'from ctypes import wintypes;'
    'k = ctypes.WinDLL("kernel32", use_last_error=True);'
    'k.GetCurrentProcess.restype = wintypes.HANDLE;'
    'k.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL));'
    'answer = wintypes.BOOL();'
    'print(bool(k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(answer)) and answer.value))'
)
"""Asks Windows whether the process running it is in a job. Spelled out here rather than asked of the package,
as the core count is."""

WINDOWS_MACHINE_FACTS = """
import ctypes
from ctypes import wintypes


class MemoryStatus(ctypes.Structure):
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


kernel32 = ctypes.WinDLL('kernel32')
kernel32.GetActiveProcessorCount.argtypes = (wintypes.WORD,)
kernel32.GetActiveProcessorCount.restype = wintypes.DWORD

status = MemoryStatus()
status.dwLength = ctypes.sizeof(status)
kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
print(status.ullTotalPhys, status.ullTotalPageFile, kernel32.GetActiveProcessorCount(0xFFFF))
"""
"""A program that prints what this machine says about itself, spelled independently of the package.

`MEMORYSTATUSEX` is re-declared rather than imported for the reason `job.py` re-declares its structures: an
instrument that shares a layout with its subject agrees with a broken one. The cores are asked of the kernel
for a second reason - `os.cpu_count()` honors `PYTHON_CPU_COUNT` and `-X cpu_count` from Python 3.13 on, and
this count does not only check a reading here, it decides the rates and masks the lane sets.
"""

INFORMATIONAL_NOTICES = frozenset({'memory-ancestors-hidden', 'cpu-ancestors-hidden'})
"""Notices that report what the mechanism cannot see.

The shape of the mount raises them, so they explain no `None`. Kept apart because the check that a missing
reading was explained would otherwise be satisfied by a notice that explains something else - and on a
truncated mount, which is a shape the container lane builds, it would be satisfied every time.
"""

DERIVED_AVAILABLE = frozenset({'cgroup-v1', 'cgroup-v2'})
"""Interfaces that work the room out from one walk, so the three raw numbers agree exactly."""

MEASURED_AVAILABLE = frozenset({'windows-job-object'})
"""Interfaces that answer the room from a call of its own, so it can drift from the distance either way."""

DRIFT_ALLOWANCE = 8 * MIB
"""How far either way a separately read room may sit from the distance before it counts as wrong.

The usage is read first and the room second. Memory freed between the two calls puts the room above the
distance, memory committed puts it below. Both sides are checked.

Wide enough for a probe that does nothing but read, narrow enough that a reading off by a factor still fails:
the limits here are hundreds of megabytes.
"""
MEMORY_LIMIT = 512 * MIB
TIGHTER_MEMORY_LIMIT = 256 * MIB
QUOTA_CORES = 0.5
"""Half a core. Below any machine, so it is always a real restriction."""

TIMEOUT_SECONDS = 300

CANNOT_SET_UP = 77
"""What a wrapper exits with when it could not produce the shape a test needs.

`run()` tells it apart from any other exit code, and fails the test naming the layout rather than the sensor.
"""


def _interfaces_of(sources: dict[str, Any]) -> set[str]:
    """The mechanisms among these sources that a reading could have come from.

    A source searching no levels found nowhere to read: its interface is the one that would have carried the
    metric, not one anything came through. Counting it would put the machine-wide interface into the set for a
    metric nothing served, and leave an empty set unreachable wherever a mechanism is mounted at all.
    """
    return {source['interface'] for source in sources.values() if source is not None and source['levels']}


@dataclass(frozen=True)
class Reading:
    """What the probe saw inside the environment under test."""

    version: str
    memory_limit: int | None
    used: int | None
    available: int | None
    cpu_limit: float | None
    cpu_usage: float | None
    cpu_used_ratio: float | None
    raw_memory_limit: int | None
    raw_memory_used: int | None
    raw_memory_available: int | None
    raw_cpu_quota: float | None
    raw_cpu_set_size: int | None
    memory_limit_level: str | None
    cpu_limit_level: str | None
    cpu_rate_level: str | None
    machine_memory_bytes: int | None
    memory_limit_ceiling: int | None
    machine_cpu_count: int | None
    allocated: int
    notices: tuple[str, ...]
    sources: dict[str, Any]

    @property
    def interfaces(self) -> set[str]:
        """The mechanisms that had a level to search, e.g. `{'cgroup-v1'}`."""
        return _interfaces_of(self.sources)

    @property
    def limit_interfaces(self) -> set[str]:
        """The mechanisms the limits came from.

        The consumed CPU time is left out. Where no hierarchy counts anything, the base `cpu.stat` of a
        controller-less cgroup2 is all there is.
        """
        return _interfaces_of({name: source for name, source in self.sources.items() if name != 'cpu_usage'})

    def __repr__(self) -> str:
        """Spell the numbers a failing assertion needs, with the sources summarized rather than dumped.

        Taken from the fields themselves, so a number the probe starts reporting shows up in a failure without
        being listed twice. The three left out carry nothing a failure is read with: two are constants of the
        run, and the sources are summarized below instead.
        """
        spelled = ', '.join(
            f'{field.name}={getattr(self, field.name)!r}'
            for field in dataclasses.fields(self)
            if field.name not in {'version', 'allocated', 'sources'}
        )
        levels = {name: (source or {}).get('levels') for name, source in self.sources.items()}

        return f'Reading({spelled}, interfaces={sorted(self.interfaces)}, levels={levels})'


def parse(output: str) -> Reading:
    """Read the probe's JSON out of its output."""
    lines = [line for line in output.splitlines() if line.startswith('{')]
    if not lines:
        pytest.fail(f'the probe printed no JSON:\n{output}')

    payload = json.loads(lines[-1])
    payload['notices'] = tuple(payload['notices'])

    return Reading(**payload)


def attempt(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one probe command and hand back how it went, whether or not it worked.

    For the tests that assert on a refusal. Everything else goes through `run`.
    """
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
        check=False,
    )


def run(command: list[str], *, env: dict[str, str] | None = None) -> Reading:
    """Run one probe command and parse what it printed."""
    result = attempt(command, env=env)

    if result.returncode == CANNOT_SET_UP:
        pytest.fail(f'the layout this test needs could not be built: {result.stderr.strip()[-300:]}')

    if result.returncode != 0:
        pytest.fail(
            f'{" ".join(command)} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )

    return parse(result.stdout)


def probe_here(wrapper: list[str] | None = None, python_version: str | None = None) -> Reading:
    """Run the probe on this machine, optionally under a wrapper command such as `systemd-run`.

    The interpreter is the one running the suite, unless a version is named. uv then installs that one.
    """
    env = {**os.environ, 'PYTHONPATH': str(SRC_DIR)}

    if python_version is None:
        interpreter = [sys.executable, str(PROBE)]
    else:
        interpreter = ['uv', 'run', '--no-project', '--python', python_version, 'python', str(PROBE)]

    if wrapper is None:
        return run(interpreter, env=env)

    # A wrapper can strip the environment, so the variables travel as an explicit `env` call.
    passthrough = ['env', f'PYTHONPATH={SRC_DIR}', f'HOME={os.environ.get("HOME", "/root")}']

    return run([*wrapper, *passthrough, *interpreter], env=env)


def probe_command(python_version: str = PYTHON_VERSION) -> str:
    """Spell how the probe is started inside a container.

    uv resolves the version and downloads one where the image ships no match, so any image runs the probe.
    """
    return f'/uv/uv run --no-project --python {python_version} python /sensor/probe.py'


@cache
def portable_uv() -> Path:
    """Download the uv build that runs in any image, and hand back its path.

    The musl build is statically linked, so one binary covers musl and glibc images alike. The version follows
    the uv of this machine, so the tests use the uv the project is developed with.
    """
    target = UV_DOWNLOADS / 'uv'
    if target.exists():
        return target

    # `uv --version`, not `uv version`: the latter reports the version of the project it is run in.
    reported = subprocess.run(
        ['uv', '--version'],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout.split()
    if len(reported) < 2:
        pytest.fail('the uv version of this machine cannot be read')

    version = reported[1]

    machine = 'aarch64' if platform.machine() in {'aarch64', 'arm64'} else 'x86_64'
    url = f'https://github.com/astral-sh/uv/releases/download/{version}/uv-{machine}-unknown-linux-musl.tar.gz'

    UV_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    archive = UV_DOWNLOADS / 'uv.tar.gz'
    if not command_works(['curl', '-fsSL', '-o', str(archive), url]):
        pytest.fail(f'{url} cannot be downloaded')

    with tarfile.open(archive) as tar:
        member = next(entry for entry in tar.getmembers() if entry.name.endswith('/uv'))
        member.name = 'uv'
        tar.extract(member, UV_DOWNLOADS, filter='data')

    target.chmod(0o755)

    return target


def container_command(
    *engine_args: str,
    engine: Engine = DOCKER,
    image: str = IMAGE,
    python_version: str = PYTHON_VERSION,
    command: list[str] | None = None,
) -> list[str]:
    """Spell the `run` command that starts the probe in a fresh container.

    Built rather than run, so that a test can also assert on an engine that refuses to start it.
    """
    uv_cache = UV_DOWNLOADS / engine.name / 'cache'
    interpreters = UV_DOWNLOADS / engine.name / 'python'
    uv_cache.mkdir(parents=True, exist_ok=True)
    interpreters.mkdir(parents=True, exist_ok=True)

    return [
        *engine.command,
        'run',
        '--rm',
        '--volume',
        f'{SRC_DIR}:/sensor/src:ro',
        '--volume',
        f'{PROBE}:/sensor/probe.py:ro',
        '--volume',
        f'{portable_uv()}:/uv/uv:ro',
        '--volume',
        f'{uv_cache}:/uv/cache',
        '--volume',
        f'{interpreters}:/uv/python',
        '--env',
        'PYTHONPATH=/sensor/src',
        '--env',
        'UV_CACHE_DIR=/uv/cache',
        '--env',
        'UV_PYTHON_INSTALL_DIR=/uv/python',
        *engine_args,
        qualified(image, engine),
        *(command or ['sh', '-c', probe_command(python_version)]),
    ]


@cache
def windows_probe_stage() -> Path:
    """Lay an interpreter, the package and the probe out in one directory, and hand back its path.

    A Windows container cannot bind-mount the UNC path a repository checked out under WSL lives at, and the
    base images carry no Python at all. So uv installs one here, beside a copy of the package, and the whole
    directory is mounted. That the package has no dependencies is what makes a copy enough.

    The probe is laid next to the package it imports: an interpreter puts the directory of the script it runs
    on `sys.path`, so nothing sets a path here. Measured inside `nanoserver`, where a uv-managed interpreter
    starts and the probe read a job memory limit through it.
    """
    WINDOWS_DOWNLOADS.mkdir(parents=True, exist_ok=True)

    # A directory of its own per run, so a container reads the package as it is now.
    stage = Path(tempfile.mkdtemp(prefix='cgroups-sensor-e2e-', dir=WINDOWS_DOWNLOADS))
    # Removed at exit rather than by the next run, which could be a second session with this one mounted into
    # a container.
    atexit.register(shutil.rmtree, stage, ignore_errors=True)

    # A Windows container runs as an account of its own, and under process isolation the host's own permissions
    # decide what it may touch on a bind mount. A temporary directory is inside the user's profile, on a runner
    # as much as here, and that account matches nothing in the profile's list - so the container starts and
    # then cannot launch the interpreter, with `Access is denied` out of `CreateProcess`. Granted before
    # anything is written, so what is written inherits it, and spelled as a SID because the name of that group
    # is translated.
    if not command_works(['icacls', str(stage), '/grant', '*S-1-1-0:(OI)(CI)RX']):
        pytest.fail(f'the account a container runs as cannot be given access to {stage}')

    # Installed aside and moved, so what the container reads is the interpreter and nothing else. uv leaves a
    # lock file beside it, and a junction for the minor version that points back at a host path.
    managed = stage / 'managed'
    if not command_works(
        ['uv', 'python', 'install', '--install-dir', str(managed), '--no-bin', PYTHON_VERSION],
        timeout=TIMEOUT_SECONDS,
    ):
        pytest.fail(f'uv cannot install Python {PYTHON_VERSION}')

    found = next(managed.glob('cpython-*/python.exe'), None)
    if found is None:
        pytest.fail(f'uv reported installing Python {PYTHON_VERSION} and left no interpreter under {managed}')

    # Resolved, because the junction matches that glob as readily as the directory uv really wrote.
    found.parent.resolve().rename(stage / 'python')
    shutil.rmtree(managed, ignore_errors=True)

    shutil.copytree(
        SRC_DIR / 'cgroups_sensor',
        stage / 'sensor' / 'cgroups_sensor',
        ignore=shutil.ignore_patterns('__pycache__'),
    )
    shutil.copy(PROBE, stage / 'sensor' / 'probe.py')

    return stage


def probe_in_container(
    *engine_args: str,
    engine: Engine = DOCKER,
    image: str = IMAGE,
    python_version: str = PYTHON_VERSION,
    command: list[str] | None = None,
) -> Reading:
    """Run the probe in a fresh container. The arguments go to the engine's `run`."""
    return run(
        container_command(
            *engine_args,
            engine=engine,
            image=image,
            python_version=python_version,
            command=command,
        )
    )


def require_docker() -> None:
    """Fail the lane where the daemon will not answer, once and by name.

    A lane says what it needs, and a missing tool fails it rather than quietly testing less.

    Not hypothetical on Windows: a runner boots from a saved image, and the Docker service sometimes does not
    come up with it - actions/runner-images#13729.
    """
    try:
        result = subprocess.run(['docker', 'info'], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        pytest.fail(f'this lane needs docker and it could not be run: {error}')

    if result.returncode != 0:
        pytest.fail(f'this lane needs the docker daemon and it did not answer: {result.stderr.strip()[-300:]}')


def qualified(image: str, engine: Engine) -> str:
    """Name the registry of an image, for an engine that does not assume one.

    A name that carries one already is left alone: that is a first component with a dot or a colon in it.
    """
    first, slash, _rest = image.partition('/')
    carries_registry = bool(slash) and ('.' in first or ':' in first or first == 'localhost')

    return image if engine.registry is None or carries_registry else f'{engine.registry}/{image}'


def pull_image(image: str, engine: Engine = DOCKER) -> None:
    """Pull one image ahead of the run that needs it, whether or not it arrives.

    Pulling separately gets its own generous timeout, which a slow guest network needs. A failure is no
    verdict: the image may already be here, and where it is not, the `run` that follows says so.
    """
    command_works([*engine.command, 'pull', '--quiet', qualified(image, engine)], timeout=TIMEOUT_SECONDS)


def command_works(command: list[str], *, timeout: int = 60) -> bool:
    """Whether a command runs and succeeds."""
    try:
        return (
            subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def machine_cpu_count() -> int:
    """The cores of this machine, counted independently of the package.

    Not the `get_machine_cpu_count()` of the package: a test that asks the subject for the expected value
    proves nothing. On Linux the two really differ - the package reads `/sys/devices/system/cpu/online` and
    this reads `sysconf`. On Windows both end at `GetActiveProcessorCount`, asked here directly.
    """
    if sys.platform == 'win32':
        cores = windows_machine_facts().cores
        if not cores:
            # Zero would silently turn every rate and mask this lane computes into a limit of nothing.
            pytest.fail('this machine does not say how many cores it has')

        return cores

    return os.sysconf('SC_NPROCESSORS_ONLN')


def in_a_job(python: str) -> bool:
    """Whether a process started this way comes up inside a Windows job object.

    Asked of a child rather than of this process, because the two need not agree: a job set to let its
    children break away leaves the test runner inside one and every process it starts outside. What the probe
    should report follows from what a process started the same way sees.
    """
    answer = subprocess.run([python, '-c', JOB_MEMBERSHIP], capture_output=True, text=True, check=True, timeout=60)

    return answer.stdout.strip() == 'True'


def is_unified() -> bool:
    """Whether the controllers live on the cgroup v2 unified hierarchy."""
    controllers = Path('/sys/fs/cgroup/cgroup.controllers')

    # Every file in cgroupfs reports zero bytes, so the content has to be read rather than sized.
    return controllers.exists() and bool(controllers.read_text().strip())


def delegated_controllers() -> frozenset[str]:
    """The controllers the systemd user manager of this user may hand out.

    Everything a rootless engine starts lands below `user@<uid>.service`, and can be limited only by a
    controller delegated to that unit - which is what its `cgroup.controllers` lists, and not its
    `cgroup.subtree_control`, which is only what the manager has enabled for children so far. systemd 255
    delegates `cpu`, `memory` and `pids`, and not `cpuset`.

    The unit is named from the uid, not read out of `/proc/self/cgroup`: the process asking is not under the
    manager itself. Empty where this user has no manager running.
    """
    uid = os.getuid()
    unit = Path(f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service')

    try:
        return frozenset((unit / 'cgroup.controllers').read_text().split())
    except OSError:
        return frozenset()


def machine_memory_bytes() -> int:
    """The memory of this machine, in bytes. Read here rather than asked of the package, as above."""
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) * 1024

    pytest.fail('/proc/meminfo carries no MemTotal')


@dataclass(frozen=True)
class WindowsMachine:
    """What this Windows machine says about itself, asked of the kernel rather than of the package."""

    memory_bytes: int
    """The memory the machine holds."""

    commit_limit: int
    """What every process together may commit, which a page file lifts above `memory_bytes`. The gap is where
    a job memory limit is enforced while physical memory alone would call it unlimited."""

    cores: int
    """The active cores, across every processor group."""


@cache
def windows_machine_facts() -> WindowsMachine:
    """Ask the machine about itself, once, in one child process."""
    answer = subprocess.run(
        [sys.executable, '-c', WINDOWS_MACHINE_FACTS], capture_output=True, text=True, check=True, timeout=60
    )
    memory, ceiling, cores = (int(number) for number in answer.stdout.split())

    return WindowsMachine(memory_bytes=memory, commit_limit=ceiling, cores=cores)


def notices_about(reading: Reading, metric: str) -> list[str]:
    """Every notice about one metric.

    A notice of the other metric says nothing about this one, so the two are told apart here.
    """
    prefixes = {'memory': ('memory', 'machine-memory'), 'cpu': ('cpu', 'machine-cpu')}[metric]

    return [code for code in reading.notices if code.startswith(prefixes)]


def rejections_about(reading: Reading, metric: str) -> list[str]:
    """The notices that say why a reading of one metric is missing."""
    return [code for code in notices_about(reading, metric) if code not in INFORMATIONAL_NOTICES]


def check_available(reading: Reading) -> None:
    """Check the room against the limit and the usage, as the mechanism reported all three.

    On the raw numbers rather than the reported ones: the sensor brings the reported pair into range, so only
    these can still catch a mechanism whose calls disagree. For a cgroup the three come from one walk and the
    relation is exact. For a job object they come from three calls a moment apart, so the room drifts.
    """
    interface = reading.sources['memory']['interface']
    if reading.raw_memory_limit is None or reading.raw_memory_used is None:
        # The two are answered together or not at all, so a room without a usage is a mechanism half-read.
        assert reading.raw_memory_available is None
        return

    assert reading.raw_memory_available is not None
    distance = reading.raw_memory_limit - reading.raw_memory_used

    if interface in DERIVED_AVAILABLE:
        assert reading.raw_memory_available == distance
    elif interface in MEASURED_AVAILABLE:
        assert abs(reading.raw_memory_available - distance) <= DRIFT_ALLOWANCE
    else:
        pytest.fail(f'{interface} says nothing about how it arrives at the room left, so this lane cannot check it')


def check_invariants(reading: Reading) -> None:
    """Check what must hold of every reading, whatever the environment.

    A reported limit has to be real, and a missing one has to be explained.
    """
    # `__version__` resolves lazily on first access, and nothing else in the suite touches that path.
    assert reading.version

    if reading.memory_limit is not None:
        assert reading.used is not None
        # The probe charged this much before reading, whichever mechanism counts it, so anything below it
        # is not the memory of this process.
        assert reading.allocated <= reading.used <= reading.memory_limit
        assert reading.machine_memory_bytes is not None
        # Against the ceiling rather than the memory of the machine: a job limits commit, and a limit above
        # physical memory but below the commit limit is enforced and is reported.
        assert reading.memory_limit_ceiling is not None
        assert reading.memory_limit < reading.memory_limit_ceiling
        assert reading.available is not None
        assert 0 <= reading.available <= reading.memory_limit - reading.used
        check_available(reading)
    else:
        # Nothing was reported, so either no limit was found or a notice says why it was dropped.
        assert reading.raw_memory_limit is None or rejections_about(reading, 'memory')

    if reading.raw_memory_limit is not None:
        # Whatever became of the limit, the level it was read at is one of the levels that were searched.
        assert reading.memory_limit_level in reading.sources['memory']['levels']
        if reading.raw_memory_used is not None:
            assert 0 <= reading.raw_memory_used <= reading.raw_memory_limit

    if reading.cpu_limit is not None:
        assert reading.cpu_limit > 0
        if reading.machine_cpu_count is not None:
            assert reading.cpu_limit < reading.machine_cpu_count
        # A limit whose level counts no CPU time is still reported, and then says so instead of a rate.
        if reading.cpu_used_ratio is None:
            assert 'cpu-usage-scope-mismatch' in reading.notices
        else:
            # The probe keeps a core busy while it measures, so a rate of nothing was counted elsewhere.
            assert 0.0 < reading.cpu_used_ratio <= 1.0
            assert reading.cpu_rate_level is not None
        assert reading.cpu_limit_level is not None
    else:
        assert reading.cpu_used_ratio is None
        raw_cpu = (reading.raw_cpu_quota, reading.raw_cpu_set_size)
        assert raw_cpu == (None, None) or rejections_about(reading, 'cpu')
