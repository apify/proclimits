from __future__ import annotations

import json
import subprocess
import time
from typing import TYPE_CHECKING, Any

import pytest

from .harness import (
    IMAGE,
    MEMORY_LIMIT,
    PROBE,
    SRC_DIR,
    TIMEOUT_SECONDS,
    check_invariants,
    parse,
    pull_image,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .harness import Reading

pytestmark = [pytest.mark.kubernetes, pytest.mark.usefixtures('_cluster')]

CLUSTER = 'cgroups-sensor-e2e'
POD = 'sensor-probe'
SRC_MAP = 'sensor-src'
PROBE_MAP = 'sensor-probe-script'
POD_WAIT_SECONDS = 300


def kubectl(*args: str, check: bool = True) -> str:
    """Run one kubectl command against the test cluster."""
    result = subprocess.run(
        ['kubectl', '--context', f'kind-{CLUSTER}', *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(f'kubectl {" ".join(args)} exited {result.returncode}\n{result.stderr}')

    return result.stdout


@pytest.fixture(scope='session')
def _cluster() -> Iterator[None]:
    """Bring up a kind cluster carrying the probe image where none is running, and take that one down afterwards."""
    existing = subprocess.run(
        ['kind', 'get', 'clusters'],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    pre_existing = CLUSTER in existing.stdout.split()

    if not pre_existing:
        created = subprocess.run(
            ['kind', 'create', 'cluster', '--name', CLUSTER, '--wait', '120s'],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if created.returncode != 0:
            pytest.fail(f'the cluster could not be created: {created.stderr.strip()[-300:]}')

    pull_image(IMAGE)
    subprocess.run(
        ['kind', 'load', 'docker-image', IMAGE, '--name', CLUSTER],
        capture_output=True,
        timeout=900,
        check=False,
    )

    # The package and the probe travel as config maps, so no image has to be built. The files go in one by
    # one, so only the Python sources travel.
    sources = sorted((SRC_DIR / 'cgroups_sensor').glob('*.py'))
    kubectl('delete', 'configmap', SRC_MAP, PROBE_MAP, '--ignore-not-found')
    kubectl('create', 'configmap', SRC_MAP, *[f'--from-file={path}' for path in sources])
    kubectl('create', 'configmap', PROBE_MAP, f'--from-file={PROBE}')

    yield

    kubectl('delete', 'configmap', SRC_MAP, PROBE_MAP, '--ignore-not-found', check=False)
    if not pre_existing:
        subprocess.run(
            ['kind', 'delete', 'cluster', '--name', CLUSTER],
            capture_output=True,
            timeout=300,
            check=False,
        )


def manifest(resources: dict[str, Any]) -> str:
    """Spell the probe pod, with the container resources it should be given."""
    return json.dumps(
        {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {'name': POD},
            'spec': {
                'restartPolicy': 'Never',
                'containers': [
                    {
                        'name': 'probe',
                        'image': IMAGE,
                        'imagePullPolicy': 'Never',
                        'command': ['python3', '/probe/probe.py'],
                        'env': [{'name': 'PYTHONPATH', 'value': '/sensor'}],
                        'resources': resources,
                        'volumeMounts': [
                            {'name': 'src', 'mountPath': '/sensor/cgroups_sensor'},
                            {'name': 'probe', 'mountPath': '/probe'},
                        ],
                    }
                ],
                'volumes': [
                    {'name': 'src', 'configMap': {'name': SRC_MAP}},
                    {'name': 'probe', 'configMap': {'name': PROBE_MAP}},
                ],
            },
        }
    )


def probe_in_pod(resources: dict[str, Any]) -> Reading:
    """Run the probe in a one-shot pod and read what it printed."""
    kubectl('delete', 'pod', POD, '--ignore-not-found', '--now')

    apply = subprocess.run(
        ['kubectl', '--context', f'kind-{CLUSTER}', 'apply', '-f', '-'],
        input=manifest(resources),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if apply.returncode != 0:
        pytest.fail(f'the pod could not be created:\n{apply.stderr}')

    # Only the phase is watched. `Unschedulable` is no verdict: the scheduler retries, and a pod that waits
    # for the node or for the previous pod still runs. What it never got past is in the `describe` below.
    phase = ''
    for _ in range(POD_WAIT_SECONDS):
        phase = kubectl('get', f'pod/{POD}', '-o', 'jsonpath={.status.phase}', check=False).strip()
        if phase in {'Succeeded', 'Failed'}:
            break

        time.sleep(1)

    logs = kubectl('logs', f'pod/{POD}', check=False)
    if phase != 'Succeeded':
        pytest.fail(f'the pod ended as {phase or "unknown"}\n{logs}\n{kubectl("describe", f"pod/{POD}", check=False)}')

    reading = parse(logs)
    kubectl('delete', 'pod', POD, '--ignore-not-found', '--now', check=False)

    return reading


def test_pod_with_limits() -> None:
    """Reads container limits from inside the pod's cgroup namespace, where the container sees itself at the root."""
    reading = probe_in_pod(
        {
            # Small on purpose: kubernetes copies limits into requests when none are given, and a pod
            # requesting the whole node never schedules.
            'requests': {'cpu': '50m', 'memory': '64Mi'},
            'limits': {'cpu': '500m', 'memory': str(MEMORY_LIMIT)},
        }
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == 0.5


def test_pod_without_limits() -> None:
    """Reports no restriction for a pod that sets none, inside the same kubepods hierarchy."""
    reading = probe_in_pod({})

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.cpu_limit is None


def test_guaranteed_pod() -> None:
    """Reads the limits of a pod whose requests equal its limits, the class kubelet puts in a cgroup apart."""
    reading = probe_in_pod(
        {
            'requests': {'cpu': '500m', 'memory': str(MEMORY_LIMIT)},
            'limits': {'cpu': '500m', 'memory': str(MEMORY_LIMIT)},
        }
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == 0.5


def test_pod_limited_on_memory_only() -> None:
    """Reads a memory limit where the pod caps nothing else, so no CPU limit is reported."""
    reading = probe_in_pod({'limits': {'memory': str(MEMORY_LIMIT)}})

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit is None


def test_pod_limited_on_cpu_only() -> None:
    """The mirror image of the test above."""
    reading = probe_in_pod({'limits': {'cpu': '500m'}})

    check_invariants(reading)
    assert reading.cpu_limit == 0.5
    assert reading.memory_limit is None
