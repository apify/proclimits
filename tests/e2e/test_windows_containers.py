from __future__ import annotations

import os
import sys

import pytest

from . import harness

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows containers need a Windows host')

MOUNT = harness.WINDOWS_MOUNT

MEMORY_LIMIT_ARGUMENT = '512m'
MEMORY_LIMIT_BYTES = 512 * harness.MIB


def _base_image() -> str:
    """The image to run the probe in, whose build has to match the host.

    A lane names it, because only the lane knows which runner it is on. The fallback reads the host: a build
    of Windows Server 2025 or a client of the same generation takes the 2025 tag, anything older the 2022 one.
    """
    named = os.environ.get('E2E_WINDOWS_IMAGE')
    if named:
        return named

    tag = 'ltsc2025' if sys.getwindowsversion().build >= 26100 else 'ltsc2022'

    return f'mcr.microsoft.com/windows/nanoserver:{tag}'


@pytest.fixture(scope='module', autouse=True)
def _image() -> None:
    """Check the daemon answers, then pull the base image so no test is charged for a slow unpack.

    A runner of Windows Server 2025 caches no images at all, so the pull is real work there rather than a
    formality.
    """
    harness.require_docker()
    harness.pull_image(_base_image())


def _probe_in_container(*docker_arguments: str) -> harness.Reading:
    """Run the probe inside a Windows container, and read what it saw.

    The staged directory carries the interpreter as well as the package, so the image needs neither.

    Isolation is named rather than left to the runtime. Where the build of the image does not match the build
    of the host, docker silently falls back to running the container as a virtual machine - and `--cpus` sizes
    that machine rather than capping a job, so a lane that let it happen would quietly test something else.
    """
    stage = harness.windows_probe_stage()

    return harness.run(
        [
            'docker',
            'run',
            '--rm',
            '--isolation=process',
            *docker_arguments,
            '-v',
            f'{stage}:{MOUNT}',
            _base_image(),
            rf'{MOUNT}\python\python.exe',
            rf'{MOUNT}\sensor\probe.py',
        ]
    )


def test_expected_interface() -> None:
    """Fails the lane rather than testing less, where the container did not reach a job object."""
    reading = _probe_in_container()

    assert reading.interfaces == {'windows-job-object'}
    # A container is a job, so a process inside one always has a level, limits or no limits.
    assert reading.sources['memory']['levels'] == ['job']


def test_memory_limit() -> None:
    """Reads the memory limit of a container from the job the container is."""
    reading = _probe_in_container('--memory', MEMORY_LIMIT_ARGUMENT)

    harness.check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT_BYTES
    assert reading.memory_limit_level == 'job'
    assert reading.notices == ()


def test_without_limits() -> None:
    """Reports nothing for a container started with no limits, and explains nothing away."""
    reading = _probe_in_container()

    harness.check_invariants(reading)
    assert (reading.memory_limit, reading.cpu_limit) == (None, None)
    assert reading.notices == ()


def test_cpus_become_a_rate_cap() -> None:
    """Turns `--cpus` into cores, the runtime having written it as a share of the machine on the job."""
    reading = _probe_in_container('--cpus', '1.5')

    harness.check_invariants(reading)
    assert reading.cpu_limit == pytest.approx(1.5, abs=0.05)
    assert reading.cpu_limit_level == 'job'


def test_the_machine_is_the_host() -> None:
    """Counts the cores of the host, which is the machine a container sharing the kernel is judged against."""
    # Wanted twice over: the filter that drops a limit covering the machine keeps working, and a rate can only
    # be turned into cores with the host count.
    reading = _probe_in_container('--memory', MEMORY_LIMIT_ARGUMENT)

    assert reading.machine_cpu_count == harness.machine_cpu_count()
