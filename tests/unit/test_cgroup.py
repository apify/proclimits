from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cgroups_sensor import _cgroup as cgroup

from .conftest import (
    HYBRID_MOUNTINFO,
    HYBRID_SELF_CGROUP,
    V1_CPU_SPLIT_MOUNTINFO,
    V1_CPUSET_MOUNTINFO,
    V1_CPUSET_SPLIT_MOUNTINFO,
    V1_MOUNTINFO,
    V1_SELF_CGROUP,
    V2_MOUNTINFO,
    V2_SELF_CGROUP,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_read_hierarchies_v2(fake_cgroup: Callable[..., Path]) -> None:
    """Finds the unified hierarchy and the cgroup this process belongs to in it."""
    # Every cgroup carries `cgroup.procs`, and the mount is only taken as ours where our cgroup is under it.
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/init.scope'),
        files={'init.scope/cgroup.procs': ''},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root
    assert hierarchies.unified.mount_root == '/'
    assert hierarchies.unified.own_path == '/init.scope'
    assert hierarchies.v1 == {}


def test_read_hierarchies_v1(fake_cgroup: Callable[..., Path]) -> None:
    """Finds every controller of a cgroup v1 mount that carries more than one."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/docker/abc'),
        # A mount counts as ours only where our cgroup exists under it, and every cgroup carries this file.
        files={
            'memory/docker/abc/cgroup.procs': '',
            'cpu,cpuacct/docker/abc/cgroup.procs': '',
            'cpuset/docker/abc/cgroup.procs': '',
        },
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is None
    assert hierarchies.v1['memory'].mount_point == root / 'memory'
    assert hierarchies.v1['memory'].own_path == '/docker/abc'
    assert hierarchies.v1['cpuset'].mount_point == root / 'cpuset'
    # Otherwise the CPU metrics get split across versions.
    assert hierarchies.v1['cpu'].mount_point == root / 'cpu,cpuacct'
    assert hierarchies.v1['cpuacct'].mount_point == root / 'cpu,cpuacct'


def test_read_hierarchies_bad_lines(fake_cgroup: Callable[..., Path]) -> None:
    """Keeps reading the mount table past the lines that are not mount entries."""
    mountinfo = (
        'not a mount line\n'
        '24 30 0:21 / /sys rw - sysfs sysfs rw\n'
        # The ` - ` separator is present but the fields around it are too few to be a mount entry.
        '25 30 0:22 - cgroup2 cgroup2 rw\n'
        '26 30 0:23 / /x rw - cgroup2\n'
        f'{V2_MOUNTINFO}\n'
        '25 30 0:22 /'
    )
    root = fake_cgroup(mountinfo=mountinfo, self_cgroup=V2_SELF_CGROUP.format(path='/'), files={})

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root


def test_read_hierarchies_first_cgroup2_mount_wins(fake_cgroup: Callable[..., Path]) -> None:
    """Keeps the first of two cgroup2 mounts that both expose this cgroup, as the mount order suggests."""
    mountinfo = (
        '25 30 0:22 / {root}/first rw shared:4 - cgroup2 cgroup2 rw,nsdelegate\n'
        '26 30 0:22 / {root}/second rw shared:5 - cgroup2 cgroup2 rw,nsdelegate'
    )
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'first/cgroup.controllers': 'cpu memory\n', 'second/cgroup.controllers': 'cpu memory\n'},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root / 'first'


def test_read_hierarchies_conventional_mount_point_wins(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefers the conventional mount point over the mount order when both expose this cgroup."""
    mountinfo = (
        '25 30 0:22 / {root}/elsewhere rw shared:4 - cgroup2 cgroup2 rw,nsdelegate\n'
        '26 30 0:22 / {root}/conventional rw shared:5 - cgroup2 cgroup2 rw,nsdelegate'
    )
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'elsewhere/cgroup.procs': '', 'conventional/cgroup.procs': ''},
    )
    monkeypatch.setattr(cgroup, '_CONVENTIONAL_MOUNT_POINT', root / 'conventional')

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root / 'conventional'


def test_read_hierarchies_skips_a_foreign_cgroup2_mount(fake_cgroup: Callable[..., Path]) -> None:
    """Skips a cgroup2 mount that exposes another subtree, however early it is listed."""
    mountinfo = (
        '25 30 0:22 /other {root}/foreign rw shared:4 - cgroup2 cgroup2 rw,nsdelegate\n'
        '26 30 0:22 / {root}/ours rw shared:5 - cgroup2 cgroup2 rw,nsdelegate'
    )
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V2_SELF_CGROUP.format(path='/mine'),
        files={'foreign/memory.max': '1000\n', 'ours/mine/memory.max': '2000\n'},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root / 'ours'


def test_read_hierarchies_skips_a_mount_without_this_cgroup(fake_cgroup: Callable[..., Path]) -> None:
    """Skips a mount of the right subtree that does not actually hold this cgroup."""
    mountinfo = (
        '25 30 0:22 / {root}/stale rw shared:4 - cgroup2 cgroup2 rw,nsdelegate\n'
        '26 30 0:22 / {root}/live rw shared:5 - cgroup2 cgroup2 rw,nsdelegate'
    )
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V2_SELF_CGROUP.format(path='/mine'),
        files={'stale/other/memory.max': '1000\n', 'live/mine/memory.max': '2000\n'},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root / 'live'


def test_read_hierarchies_no_unified_when_no_mount_covers(fake_cgroup: Callable[..., Path]) -> None:
    """Reports no unified hierarchy when none of the mounts exposes this cgroup."""
    mountinfo = '25 30 0:22 /elsewhere {root}/only rw shared:4 - cgroup2 cgroup2 rw,nsdelegate'
    fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V2_SELF_CGROUP.format(path='/mine'),
        files={'only/memory.max': '1000\n', 'only/memory.current': '900\n', 'only/memory.stat': 'inactive_file 0\n'},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is None
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        limit_level=None,
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_hierarchies_v1_answers_when_no_cgroup2_mount_covers(fake_cgroup: Callable[..., Path]) -> None:
    """Leaves the cgroup v1 hierarchies to answer when the cgroup2 mount is somebody else's."""
    foreign = '25 30 0:22 /elsewhere {root}/foreign rw shared:4 - cgroup2 cgroup2 rw,nsdelegate'
    root = fake_cgroup(
        mountinfo=f'{foreign}\n{V1_MOUNTINFO}',
        self_cgroup=f'{V2_SELF_CGROUP.format(path="/mine")}2:memory:/\n',
        files={
            'foreign/memory.max': '1000\n',
            'foreign/memory.current': '900\n',
            'foreign/memory.stat': 'inactive_file 0\n',
            'memory/memory.limit_in_bytes': '536870912\n',
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 400\n',
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=536870912,
        used=600,
        available=536870312,
        limit_level=str(root / 'memory'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


@pytest.mark.parametrize(
    ('escaped', 'expected'),
    [
        pytest.param('/plain/path', '/plain/path', id='nothing to decode'),
        pytest.param('/mnt\\040point', '/mnt point', id='space'),
        pytest.param('/tab\\011here', '/tab\there', id='tab'),
        pytest.param('/back\\134slash', '/back\\slash', id='backslash'),
        pytest.param('/literal\\134040', '/literal\\040', id='escaped backslash in front of an octal sequence'),
    ],
)
def test_unescape(escaped: str, expected: str) -> None:
    """Decodes the octal sequences a path field of the mount table escapes special characters as."""
    assert cgroup._unescape(escaped) == expected


def test_read_hierarchies_escaped_paths(fake_cgroup: Callable[..., Path]) -> None:
    """Decodes both path fields: the mount point is opened, and the root is compared against `/proc/self/cgroup`."""
    mountinfo = '25 30 0:22 /docker\\040abc {root}/mnt\\040point rw shared:4 - cgroup2 cgroup2 rw'
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V2_SELF_CGROUP.format(path='/docker abc'),
        files={'mnt point/cgroup.procs': ''},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root / 'mnt point'
    assert hierarchies.unified.mount_root == '/docker abc'


@pytest.mark.parametrize(
    ('self_cgroup', 'expected_unified', 'expected_controllers'),
    [
        pytest.param('0::/init.scope\n', '/init.scope', {}, id='unified only'),
        pytest.param(
            '2:memory:/docker/abc\n1:name=systemd:/docker/abc\n',
            None,
            {'memory': '/docker/abc', 'systemd': '/docker/abc'},
            id='v1 only, with a named hierarchy',
        ),
        pytest.param(
            '2:memory:/system.slice\n0::/user.slice\n',
            '/user.slice',
            {'memory': '/system.slice'},
            id='both interfaces at once',
        ),
        pytest.param('nonsense\n0::/\n', '/', {}, id='unparsable line skipped'),
    ],
)
def test_read_own_paths(
    fake_cgroup: Callable[..., Path],
    self_cgroup: str,
    expected_unified: str | None,
    expected_controllers: dict[str, str],
) -> None:
    """Reads the cgroup this process belongs to in each hierarchy."""
    fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=self_cgroup, files={})

    unified, controllers = cgroup._read_own_paths()

    assert unified == expected_unified
    assert controllers == expected_controllers


@pytest.mark.parametrize(
    ('mount_root', 'cgroup_path', 'expected'),
    [
        pytest.param('/', '/', [''], id='own cgroup at the top of the mount'),
        pytest.param(
            '/',
            '/kubepods/pod/container',
            ['kubepods/pod/container', 'kubepods/pod', 'kubepods', ''],
            id='nested',
        ),
        pytest.param('/docker/abc', '/docker/abc/nested', ['nested', ''], id='mount root stripped'),
        pytest.param('/docker/abc', '/system.slice', [''], id='mount does not cover the own cgroup'),
        # `unshare -C` without a remount leaves the mount pointing above the new namespace's root.
        pytest.param('/..', '/foo', [''], id='mount root above the cgroup namespace'),
    ],
)
def test_candidate_dirs(
    tmp_path: Path,
    mount_root: str,
    cgroup_path: str,
    expected: list[str],
) -> None:
    """Walks from the cgroup of this process up to the top of the mount."""
    hierarchy = cgroup._Hierarchy(mount_point=tmp_path, mount_root=mount_root, own_path=cgroup_path)

    dirs = cgroup._candidate_dirs(hierarchy)

    assert list(dirs) == [tmp_path / relative if relative else tmp_path for relative in expected]


@pytest.mark.parametrize(
    ('mountinfo', 'self_cgroup', 'files', 'expected'),
    [
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'memory.max': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'inactive_file 400\n'},
            536870912,
            id='v2 limit',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'memory.max': 'max\n', 'memory.current': '1000\n', 'memory.stat': 'inactive_file 400\n'},
            None,
            id='v2 unlimited',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            # `memory.high` throttles reclaim instead of triggering an out-of-memory kill, so it is not a limit.
            {'memory.high': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'inactive_file 400\n'},
            None,
            id='memory.high without memory.max is not a limit',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/pod/container'),
            {
                'pod/container/memory.max': 'max\n',
                'pod/container/memory.current': '1000\n',
                'pod/container/memory.stat': 'inactive_file 400\n',
                'pod/memory.max': '268435456\n',
                'pod/memory.current': '9000\n',
                'pod/memory.stat': 'inactive_file 1000\n',
            },
            268435456,
            id='limit inherited from an ancestor',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {
                'memory/memory.limit_in_bytes': '536870912\n',
                'memory/memory.usage_in_bytes': '1000\n',
                'memory/memory.stat': 'total_inactive_file 400\n',
            },
            536870912,
            id='v1 limit',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            # The sentinel is passed through as read - only the sensor layer knows the memory of the machine.
            {
                'memory/memory.limit_in_bytes': '9223372036854771712\n',
                'memory/memory.usage_in_bytes': '1000\n',
                'memory/memory.stat': 'total_inactive_file 400\n',
            },
            9223372036854771712,
            id='v1 unlimited sentinel',
        ),
    ],
)
def test_read_memory(
    fake_cgroup: Callable[..., Path],
    mountinfo: str,
    self_cgroup: str,
    files: dict[str, str],
    expected: int | None,
) -> None:
    """Reads the memory limit under both cgroup interfaces."""
    fake_cgroup(mountinfo=mountinfo, self_cgroup=self_cgroup, files=files)

    assert cgroup.read_memory().limit == expected


def test_read_memory_tightest_level(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the tightest limit of the chain, so a budget cannot exceed what the kernel enforces."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            # The node-level cgroup is generous and busy, because every pod on the node is charged against it.
            'kubepods/memory.max': '8000\n',
            'kubepods/memory.current': '6000\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
            # The container this process runs in is limited far more tightly, and barely uses its share.
            'kubepods/pod/container/memory.max': '1000\n',
            'kubepods/pod/container/memory.current': '100\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
        },
    )

    # 900 bytes can still be allocated before the container's own limit, and 2000 before the node's.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=100,
        available=900,
        limit_level=str(root / 'kubepods' / 'pod' / 'container'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_when_the_tightest_limit_is_not_the_closest(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the room at whichever level is closest to its own limit, against the tightest limit there is."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            # The tighter limit, and far from it.
            'pod/container/memory.max': '500\n',
            'pod/container/memory.current': '110\n',
            'pod/container/memory.stat': 'inactive_file 10\n',
            # A looser limit that the sibling containers have nearly used up.
            'pod/memory.max': '1000\n',
            'pod/memory.current': '960\n',
            'pod/memory.stat': 'inactive_file 10\n',
        },
    )

    # 50 bytes left at the pod against 400 at this container, so 50 is what can still be allocated. The pair
    # is expressed against the tightest limit, which makes `used` 450 - the charge of neither level, one
    # holding 100 and the other 950.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=450,
        available=50,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_tighter_ancestor(fake_cgroup: Callable[..., Path]) -> None:
    """Reports an ancestor's limit where it is tighter than the one this cgroup holds."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod'),
        files={
            # The pod asks for more than the node has to give, which nothing forbids it to do.
            'kubepods/pod/memory.max': '1000\n',
            'kubepods/pod/memory.current': '260\n',
            'kubepods/pod/memory.stat': 'inactive_file 10\n',
            # The node caps what it hands out, and the sibling pods hold 200 of what is charged here.
            'kubepods/memory.max': '500\n',
            'kubepods/memory.current': '460\n',
            'kubepods/memory.stat': 'inactive_file 10\n',
        },
    )

    # 50 bytes left at the node against 750 at the pod's own limit, so the pod's limit never binds.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=450,
        available=50,
        limit_level=str(root / 'kubepods'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_smallest_distance(fake_cgroup: Callable[..., Path]) -> None:
    """Keeps the smallest distance to a limit, which is not always at the tightest one."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod'),
        files={
            # This pod looks 80 bytes from being killed.
            'kubepods/pod/memory.max': '100\n',
            'kubepods/pod/memory.current': '30\n',
            'kubepods/pod/memory.stat': 'inactive_file 10\n',
            # The node allows 20 more, and what it holds covers the sibling pods too.
            'kubepods/memory.max': '120\n',
            'kubepods/memory.current': '110\n',
            'kubepods/memory.stat': 'inactive_file 10\n',
        },
    )

    # The tighter limit is the pod's, the tighter distance the node's: 20 bytes left, not 80.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=100,
        used=80,
        available=20,
        limit_level=str(root / 'kubepods' / 'pod'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_partial_level(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the ceiling without a usage when the tightest level has none to pair with it."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            'kubepods/memory.max': '8000\n',
            'kubepods/memory.current': '6000\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
            # The tightest limit, with no page cache metric to derive a working set from.
            'kubepods/pod/container/memory.max': '1000\n',
            'kubepods/pod/container/memory.current': '100\n',
            'kubepods/pod/container/memory.stat': 'anon 100\n',
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=None,
        available=None,
        limit_level=str(root / 'kubepods' / 'pod' / 'container'),
        unreadable_level=None,
        usage_unreadable_level=str(root / 'kubepods' / 'pod' / 'container'),
    )


@pytest.mark.parametrize(
    ('mountinfo', 'self_cgroup', 'files'),
    [
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {
                'memory.max': '536870912\n',
                'memory.current': '1000\n',
                'memory.stat': 'anon 600\ninactive_file 400\nfile 400\n',
            },
            id='v2',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {
                'memory/memory.limit_in_bytes': '536870912\n',
                'memory/memory.usage_in_bytes': '1000\n',
                'memory/memory.stat': 'rss 600\ntotal_inactive_file 400\n',
            },
            id='v1',
        ),
    ],
)
def test_read_memory_used(
    fake_cgroup: Callable[..., Path],
    mountinfo: str,
    self_cgroup: str,
    files: dict[str, str],
) -> None:
    """Subtracts the inactive file cache, which the kernel drops on demand and which would not predict a kill."""
    fake_cgroup(mountinfo=mountinfo, self_cgroup=self_cgroup, files=files)

    assert cgroup.read_memory().used == 600


def test_read_memory_usage_above_the_limit(fake_cgroup: Callable[..., Path]) -> None:
    """Clamps what is in use to the limit, because sitting above it while the kernel reclaims is not actionable."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '1000\n', 'memory.current': '1200\n', 'memory.stat': 'inactive_file 0\n'},
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=1000,
        available=0,
        limit_level=str(root),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_ancestor_without_usage_files(fake_cgroup: Callable[..., Path]) -> None:
    """Counts an ancestor's limit towards the ceiling even when its own usage cannot be read at all."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '1000\n',
            'pod/container/memory.current': '100\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            # The tightest limit sits on the ancestor, whose usage will not read.
            'pod/memory.max': '500\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    # A directory in place of the file, as for a limit that cannot be opened. A level holding a limit holds a
    # usage file beside it, so the figure goes missing by failing to read rather than by being absent.
    (root / 'pod' / 'memory.current').mkdir()

    # The tightest limit is the ancestor's, and nothing says how close it is to it.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=None,
        available=None,
        limit_level=str(root / 'pod'),
        unreadable_level=None,
        usage_unreadable_level=str(root / 'pod'),
    )


def test_read_memory_no_used(fake_cgroup: Callable[..., Path]) -> None:
    """Reports a limit that no usage can be paired with as-is, leaving the judgement to the sensor layer."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'anon 600\n'},
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=536870912,
        used=None,
        available=None,
        limit_level=str(root),
        unreadable_level=None,
        usage_unreadable_level=str(root),
    )


def test_read_memory_missing_files(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the closest level that carries the controller, which the own cgroup need not."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/memory.max': '268435456\n',
            'pod/memory.current': '1000\n',
            'pod/memory.stat': 'inactive_file 400\n',
        },
    )
    # The cgroup of the process exists, it just carries no memory files of its own.
    (root / 'pod' / 'container').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=268435456,
        used=600,
        available=268434856,
        limit_level=str(root / 'pod'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


@pytest.mark.parametrize(
    ('mountinfo', 'self_cgroup', 'files', 'expected'),
    [
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
            2.0,
            id='v2 quota',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'cpu.max': '50000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
            0.5,
            id='v2 fractional quota',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'cpu.max': 'max 100000\n', 'cpu.stat': 'usage_usec 0\n'},
            None,
            id='v2 unlimited',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            # A weight shapes how siblings share contended CPU; it grants no quota and is not a limit.
            {'cpu.weight': '100\n', 'cpu.stat': 'usage_usec 0\n'},
            None,
            id='cpu.weight without a quota is not a limit',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/pod/container'),
            {
                'pod/container/cpu.max': 'max 100000\n',
                'pod/container/cpu.stat': 'usage_usec 0\n',
                'pod/cpu.max': '150000 100000\n',
            },
            1.5,
            id='v2 quota inherited from an ancestor',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {
                'cpu,cpuacct/cpu.cfs_quota_us': '150000\n',
                'cpu,cpuacct/cpu.cfs_period_us': '100000\n',
                'cpu,cpuacct/cpuacct.usage': '0\n',
            },
            1.5,
            id='v1 quota',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {
                'cpu,cpuacct/cpu.cfs_quota_us': '-1\n',
                'cpu,cpuacct/cpu.cfs_period_us': '100000\n',
                'cpu,cpuacct/cpuacct.usage': '0\n',
            },
            None,
            id='v1 unlimited',
        ),
    ],
)
def test_read_cpu_quota(
    fake_cgroup: Callable[..., Path],
    mountinfo: str,
    self_cgroup: str,
    files: dict[str, str],
    expected: float | None,
) -> None:
    """Reads the CPU bandwidth quota under both cgroup interfaces."""
    fake_cgroup(mountinfo=mountinfo, self_cgroup=self_cgroup, files=files)

    quota = cgroup.read_cpu_quota()

    assert (quota.cores if quota is not None else None) == expected


@pytest.mark.parametrize(
    ('mountinfo', 'self_cgroup', 'files', 'expected'),
    [
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'cpuset.cpus.effective': '0-1\n'},
            2,
            id='v2 range',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'cpuset.cpus.effective': '0-1,4,6-7\n'},
            5,
            id='v2 ranges mixed with single cores',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'cpuset.cpus.effective': '\n'},
            None,
            id='an empty set restricts nothing',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {'cpuset/cpuset.cpus': '0-3\n'},
            4,
            id='v1',
        ),
    ],
)
def test_read_cpu_set_size(
    fake_cgroup: Callable[..., Path],
    mountinfo: str,
    self_cgroup: str,
    files: dict[str, str],
    expected: int | None,
) -> None:
    """Counts the cores of a CPU set spelled as a mix of ranges and single numbers."""
    fake_cgroup(mountinfo=mountinfo, self_cgroup=self_cgroup, files=files)

    cpu_set = cgroup.read_cpu_set_size()

    assert (cpu_set.cores if cpu_set is not None else None) == expected


def test_read_cpu_set_size_unparsable_entry(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level when one entry of the list does not parse, rather than counting the rest."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpuset.cpus.effective': '0-1,nonsense\n'},
    )

    assert cgroup.read_cpu().unreadable_level == str(root)


@pytest.mark.parametrize(
    ('mountinfo', 'self_cgroup', 'files'),
    [
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            # cgroup v2 reports the consumed CPU time in microseconds, among other keys.
            {'cpu.stat': 'usage_usec 2500000\nuser_usec 2000000\n'},
            id='v2',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            # cgroup v1 reports it in nanoseconds, in a file of its own.
            {'cpu,cpuacct/cpuacct.usage': '2500000000\n'},
            id='v1',
        ),
    ],
)
def test_read_cpu_usage(
    fake_cgroup: Callable[..., Path],
    mountinfo: str,
    self_cgroup: str,
    files: dict[str, str],
) -> None:
    """Reports the consumed CPU time in seconds under both cgroup interfaces."""
    fake_cgroup(mountinfo=mountinfo, self_cgroup=self_cgroup, files=files)

    assert cgroup.read_cpu_usage() == 2.5


def test_read_cpu_set_size_file_disappears(fake_cgroup: Callable[..., Path]) -> None:
    """Reports nothing when the control file is gone by read time, because discovery outlives the files."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpuset.cpus.effective': '0-1\n'},
    )
    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.cores == 2

    (root / 'cpuset.cpus.effective').unlink()

    assert cgroup.read_cpu_set_size() is None


def test_read_cpu_set_size_unreadable_file(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level whose set cannot be read, instead of reporting no restriction."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpuset.cpus.effective': '0-1\n'},
    )
    # A directory in place of the file, so the file is there and the read fails.
    (root / 'cpuset.cpus.effective').unlink()
    (root / 'cpuset.cpus.effective').mkdir()

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None, cpu_set=None, unreadable_level=str(root), unconvertible_level=None
    )


def test_locate_cpu_usage_skips_a_controller_less_unified_hierarchy(fake_cgroup: Callable[..., Path]) -> None:
    """Counts the time where the limits are, not in a cgroup2 whose `cpu.stat` exists without the controller."""
    root = fake_cgroup(
        mountinfo=HYBRID_MOUNTINFO,
        self_cgroup=HYBRID_SELF_CGROUP,
        files={
            'unified/cgroup.controllers': '\n',
            'unified/system.slice/docker.service/cpu.stat': 'usage_usec 777000000\n',
            'cpu,cpuacct/docker/abc/cpuacct.usage': '1000000000\n',
        },
    )

    controller = cgroup.locate_controllers().cpu_usage

    assert controller is not None
    assert controller.is_v2 is False
    assert controller.dirs[0] == root / 'cpu,cpuacct' / 'docker' / 'abc'
    assert cgroup.read_cpu_usage() == 1.0


def test_locate_cpu_usage_uses_the_unified_hierarchy_when_nothing_else_counts(
    fake_cgroup: Callable[..., Path],
) -> None:
    """Falls back to the base file when no accounting hierarchy exists, because it is then all there is."""
    fake_cgroup(
        mountinfo=HYBRID_MOUNTINFO.replace(',cpuacct', ''),
        self_cgroup=HYBRID_SELF_CGROUP.replace('3:cpu,cpuacct:', '3:cpu:'),
        files={
            'unified/cgroup.controllers': '\n',
            'unified/system.slice/docker.service/cpu.stat': 'usage_usec 777000000\n',
        },
    )

    controller = cgroup.locate_controllers().cpu_usage

    assert controller is not None
    assert controller.is_v2 is True
    assert cgroup.read_cpu_usage() == 777.0


def test_locate_cpu_usage_keeps_a_unified_hierarchy_that_carries_the_cpu_controller(
    fake_cgroup: Callable[..., Path],
) -> None:
    """Reads the unified hierarchy where it does carry the CPU controller, whatever else is listed with it."""
    fake_cgroup(
        # Only `cpuacct` stays on cgroup v1, which leaves the CPU controller free to be bound to the cgroup2
        # mount. The two hierarchies name different cgroups, so reading the wrong one is visible below.
        mountinfo=HYBRID_MOUNTINFO.replace('cpu,cpuacct', 'cpuacct'),
        self_cgroup=HYBRID_SELF_CGROUP.replace('cpu,cpuacct', 'cpuacct'),
        files={
            'unified/cgroup.controllers': 'cpuset cpu memory pids\n',
            'unified/system.slice/docker.service/cpu.stat': 'usage_usec 2500000\n',
            'cpuacct/docker/abc/cpuacct.usage': '999000000\n',
        },
    )

    controller = cgroup.locate_controllers().cpu_usage

    assert controller is not None
    assert controller.is_v2 is True
    assert cgroup.read_cpu_usage() == 2.5


def test_cpu_usage_dir_rejects_a_group_of_the_same_name(fake_cgroup: Callable[..., Path]) -> None:
    """Refuses a counter that only shares a name with the level the quota binds at."""
    root = fake_cgroup(
        mountinfo=V1_CPU_SPLIT_MOUNTINFO,
        self_cgroup='3:cpu:/limited\n4:cpuacct:/ours\n',
        files={
            'cpu/limited/cpu.cfs_quota_us': '200000\n',
            'cpu/limited/cpu.cfs_period_us': '100000\n',
            # A group of the same name, in the hierarchy this process is not in.
            'cpuacct/limited/cpuacct.usage': '999999999999\n',
            'cpuacct/ours/cpuacct.usage': '111\n',
        },
    )

    quota = cgroup.read_cpu_quota()

    assert quota is not None
    assert quota.cores == 2.0
    assert quota.limit_level == str(root / 'cpu' / 'limited')
    assert quota.usage_level is None


def test_read_memory_unreadable_limit(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level whose limit cannot be read, instead of answering with a looser ancestor."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/memory.max': '10737418240\n',
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
            'pod/container/memory.max': 'not a number\n',
            'pod/container/memory.current': '100\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        limit_level=None,
        unreadable_level=str(root / 'pod' / 'container'),
        usage_unreadable_level=None,
    )


def test_read_memory_looser_level_without_usage(fake_cgroup: Callable[..., Path]) -> None:
    """Drops the working set for an unread usage on a looser level, which can still be the closest to its limit."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            # The tightest limit, and it says how close it is.
            'pod/container/memory.max': '500\n',
            'pod/container/memory.current': '100\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            # Looser, and silent about its own usage.
            'pod/memory.max': '1000\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'pod' / 'memory.current').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=None,
        available=None,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_level=None,
        usage_unreadable_level=str(root / 'pod'),
    )


def test_read_memory_limit_file_cannot_be_opened(fake_cgroup: Callable[..., Path]) -> None:
    """Names a level whose limit file is there but cannot be read at all, as for one that holds nonsense."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/memory.max': '10737418240\n',
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
            'pod/container/memory.current': '100\n',
        },
    )
    # A directory in place of the file, so the file is there and the read fails.
    (root / 'pod' / 'container' / 'memory.max').mkdir()

    assert cgroup.read_memory().unreadable_level == str(root / 'pod' / 'container')


def test_read_memory_negative_limit(fake_cgroup: Callable[..., Path]) -> None:
    """Names a level whose limit is a negative number of bytes, which is no limit and no absence either."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/memory.max': '10737418240\n',
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
            'pod/container/memory.max': '-5\n',
        },
    )

    assert cgroup.read_memory().unreadable_level == str(root / 'pod' / 'container')


def test_read_cpu_quota_unreadable_level(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level whose quota cannot be read, instead of answering with a looser ancestor."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/cpu.max': '800000 100000\n',
            'pod/cpu.stat': 'usage_usec 0\n',
            'pod/container/cpu.max': 'nonsense\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
        },
    )

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None,
        cpu_set=None,
        unreadable_level=str(root / 'pod' / 'container'),
        unconvertible_level=None,
    )


def test_read_cpu_set_size_inherited_under_v1(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the set a cgroup v1 level inherits, which its own file spells as empty."""
    root = fake_cgroup(
        mountinfo=V1_CPUSET_MOUNTINFO,
        self_cgroup='4:cpuset:/child\n',
        files={
            'cpuset/cpuset.cpus': '0-1\n',
            'cpuset/child/cpuset.cpus': '\n',
            'cpuset/child/cpuset.effective_cpus': '0-1\n',
        },
    )

    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.cores == 2
    assert cpu_set.limit_level == str(root / 'cpuset' / 'child')


def test_read_cpu_set_size_without_a_counter_of_its_own(fake_cgroup: Callable[..., Path]) -> None:
    """Reports no counter when the accounting hierarchy does not carry the level the set applies to."""
    fake_cgroup(
        mountinfo=V1_CPUSET_SPLIT_MOUNTINFO,
        self_cgroup='4:cpuset:/limited\n5:cpuacct:/\n',
        files={'cpuset/limited/cpuset.cpus': '0-1\n', 'cpuacct/cpuacct.usage': '5000000000\n'},
    )

    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.cores == 2
    assert cpu_set.usage_level is None


def test_read_cpu_set_size_from_an_ancestor(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the set of an ancestor when the own cgroup carries none, because it applies there too."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpuset.cpus.effective': '0-1\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )

    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.cores == 2
    # The set restricts the pod, so that is the level it is read at and the one whose time it is measured
    # against.
    assert cpu_set.limit_level == str(root / 'pod')
    assert cpu_set.usage_level == str(root / 'pod')


def test_read_cpu_set_size_without_any_accounting_hierarchy(fake_cgroup: Callable[..., Path]) -> None:
    """Reports no counter where nothing on the machine counts CPU time at all."""
    fake_cgroup(
        mountinfo=V1_CPUSET_MOUNTINFO,
        self_cgroup='4:cpuset:/limited\n',
        files={'cpuset/limited/cpuset.cpus': '0-1\n'},
    )

    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.cores == 2
    assert cpu_set.usage_level is None


def test_read_cpu_set_size_across_split_hierarchies(fake_cgroup: Callable[..., Path]) -> None:
    """Finds the counter of the level the set applies to, even in another hierarchy."""
    root = fake_cgroup(
        mountinfo=V1_CPUSET_SPLIT_MOUNTINFO,
        self_cgroup='4:cpuset:/limited\n5:cpuacct:/limited\n',
        files={
            'cpuset/limited/cpuset.cpus': '0-1\n',
            'cpuacct/limited/cpuacct.usage': '5000000000\n',
        },
    )

    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.limit_level == str(root / 'cpuset' / 'limited')
    assert cpu_set.usage_level == str(root / 'cpuacct' / 'limited')


def test_locate_controllers_hybrid(fake_cgroup: Callable[..., Path]) -> None:
    """Falls back to cgroup v1 for a controller the unified hierarchy does not carry."""
    root = fake_cgroup(
        mountinfo=f'{V2_MOUNTINFO}\n{V1_MOUNTINFO}',
        self_cgroup=f'{V2_SELF_CGROUP.format(path="/")}2:memory:/\n',
        files={
            'memory/memory.limit_in_bytes': '536870912\n',
            'memory/memory.usage_in_bytes': '1000\n',
            'memory/memory.stat': 'total_inactive_file 400\n',
        },
    )

    memory = cgroup.locate_controllers().memory

    assert memory is not None
    assert memory.is_v2 is False
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=536870912,
        used=600,
        available=536870312,
        limit_level=str(root / 'memory'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


@pytest.mark.usefixtures('_no_cgroup')
def test_no_cgroups() -> None:
    """Reports nothing on a system that has no cgroups."""
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        limit_level=None,
        unreadable_level=None,
        usage_unreadable_level=None,
    )
    assert cgroup.read_cpu_quota() is None
    assert cgroup.read_cpu_set_size() is None
    assert cgroup.read_cpu_usage() is None


def raise_permission_error(_self: Path) -> bool:
    """Stand in for `Path.exists` on a directory the kernel refuses to traverse."""
    raise PermissionError(13, 'Permission denied')


def test_exists_on_an_unreadable_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports a path as missing when the lookup is denied. Only Python 3.14 does that on its own."""
    monkeypatch.setattr(Path, 'exists', raise_permission_error)

    assert cgroup._exists(tmp_path) is False


def test_locate_controllers_with_unreadable_directories(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finds nothing rather than raising when the cgroup chain holds a directory it may not traverse."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '1000\n', 'memory.current': '100\n', 'memory.stat': 'inactive_file 0\n'},
    )
    monkeypatch.setattr(Path, 'exists', raise_permission_error)

    assert cgroup.locate_controllers().memory is None
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        limit_level=None,
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_hierarchies_undecodable_path(fake_cgroup: Callable[..., Path], tmp_path: Path) -> None:
    """Keeps reading when another mount point is not valid UTF-8, instead of losing every hierarchy."""
    root = fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files={})
    mountinfo = tmp_path / 'mountinfo'
    mountinfo.write_bytes(b'24 30 0:21 / /mnt/\xff\xfe rw - ext4 /dev/sda1 rw\n' + mountinfo.read_bytes())

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.unified is not None
    assert hierarchies.unified.mount_point == root


def test_read_cpu_quota_tightest_level(fake_cgroup: Callable[..., Path]) -> None:
    """Takes the tightest quota when two levels hold different ones, and says where it binds."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.max': '400000 100000\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpu.max': '150000 100000\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )

    # The quota binds on the pod, so a rate has to be measured there rather than in the container.
    assert cgroup.read_cpu_quota() == cgroup.RawCpuQuota(
        cores=1.5,
        limit_level=str(root / 'pod'),
        usage_level=str(root / 'pod'),
    )


def test_read_cpu_quota_on_the_own_group(fake_cgroup: Callable[..., Path]) -> None:
    """Names the group of this process when the quota binds there."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.max': '150000 100000\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpu.max': '400000 100000\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )

    assert cgroup.read_cpu_quota() == cgroup.RawCpuQuota(
        cores=1.5,
        limit_level=str(root / 'pod' / 'container'),
        usage_level=str(root / 'pod' / 'container'),
    )


def test_read_cpu_usage_at_a_named_level(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the counter of the level it is asked for, which counts the siblings of this process too."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.stat': 'usage_usec 2500000\n',
            'pod/cpu.stat': 'usage_usec 90000000\n',
        },
    )

    assert cgroup.read_cpu_usage(str(root / 'pod')) == 90.0


def test_read_cpu_usage_at_a_level_outside_the_chain(fake_cgroup: Callable[..., Path]) -> None:
    """Answers nothing for a level this mechanism does not count, whatever a counter there says."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.stat': 'usage_usec 2500000\n',
            'elsewhere/cpu.stat': 'usage_usec 123456789\n',
        },
    )

    # A readable counter, and the level belongs to somebody else: a rate taken from it would divide the time
    # of one scope by the limit of another.
    assert cgroup.read_cpu_usage(str(root / 'elsewhere')) is None


def test_read_cpu_set_size_reads_the_closest_level(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the set of the own cgroup, not of an ancestor that allows more."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpuset.cpus.effective': '0-1\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpuset.cpus.effective': '0-7\n',
            'pod/cpu.stat': 'usage_usec 0\n',
            'cpuset.cpus.effective': '0-15\n',
        },
    )

    cpu_set = cgroup.read_cpu_set_size()

    assert cpu_set is not None
    assert cpu_set.cores == 2
    assert cpu_set.usage_level == str(root / 'pod' / 'container')


def test_read_cpu_usage_reads_the_closest_level(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the counter of the own cgroup, not the one of an ancestor that counts the siblings too."""
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.stat': 'usage_usec 2500000\n',
            'pod/cpu.stat': 'usage_usec 90000000\n',
            'cpu.stat': 'usage_usec 900000000\n',
        },
    )

    assert cgroup.read_cpu_usage() == 2.5


def test_read_memory_limit_of_zero(fake_cgroup: Callable[..., Path]) -> None:
    """Takes a limit of zero as the tightest limit there is, not as an absent one."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '0\n',
            'pod/container/memory.current': '100\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.max': '1000\n',
            'pod/memory.current': '200\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=0,
        used=0,
        available=0,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_usage_below_the_cache(fake_cgroup: Callable[..., Path]) -> None:
    """Floors the charged memory at zero where the cache reads larger than the usage it is part of."""
    # The two files are read a moment apart, and only a cgroup that grew between the reads can spell them
    # this way round.
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '1000\n', 'memory.current': '100\n', 'memory.stat': 'inactive_file 400\n'},
    )
    controller = cgroup.locate_controllers().memory

    assert controller is not None
    assert cgroup._read_working_set(controller, root) == 0
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=0,
        available=1000,
        limit_level=str(root),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_cpu_quota_v1_zero_period(fake_cgroup: Callable[..., Path]) -> None:
    """Names the level rather than dividing by a period of zero, which no kernel writes."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'cpu,cpuacct/cpu.cfs_quota_us': '100000\n',
            'cpu,cpuacct/cpu.cfs_period_us': '0\n',
            'cpu,cpuacct/cpuacct.usage': '0\n',
        },
    )

    assert cgroup.read_cpu().unreadable_level == str(root / 'cpu,cpuacct')


def test_read_hierarchies_first_v1_mount_wins(fake_cgroup: Callable[..., Path]) -> None:
    """Keeps the first mount of a cgroup v1 controller, because mounts are listed in creation order."""
    mountinfo = (
        '30 25 0:26 / {root}/first rw,nosuid shared:14 - cgroup cgroup rw,memory\n'
        '31 25 0:27 / {root}/second rw,nosuid shared:15 - cgroup cgroup rw,memory'
    )
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={'first/cgroup.procs': '', 'second/cgroup.procs': ''},
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.v1['memory'].mount_point == root / 'first'


def test_read_hierarchies_skips_a_foreign_v1_mount(fake_cgroup: Callable[..., Path]) -> None:
    """Skips a cgroup v1 mount of another subtree, as the unified hierarchy does."""
    mountinfo = (
        '30 25 0:26 /other {root}/foreign rw,nosuid shared:14 - cgroup cgroup rw,memory\n'
        '31 25 0:27 / {root}/ours rw,nosuid shared:15 - cgroup cgroup rw,memory'
    )
    root = fake_cgroup(
        mountinfo=mountinfo,
        self_cgroup=V1_SELF_CGROUP.format(path='/mine'),
        files={
            'foreign/memory.limit_in_bytes': '1000\n',
            'foreign/memory.usage_in_bytes': '900\n',
            'foreign/memory.stat': 'total_inactive_file 0\n',
            'ours/mine/memory.limit_in_bytes': '2000\n',
            'ours/mine/memory.usage_in_bytes': '100\n',
            'ours/mine/memory.stat': 'total_inactive_file 0\n',
        },
    )

    hierarchies = cgroup._read_hierarchies()

    assert hierarchies.v1['memory'].mount_point == root / 'ours'
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=2000,
        used=100,
        available=1900,
        limit_level=str(root / 'ours' / 'mine'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_memory_v1_uses_the_hierarchical_key(fake_cgroup: Callable[..., Path]) -> None:
    """Reads `total_inactive_file` under cgroup v1, which counts the children as the limit does."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'memory/memory.limit_in_bytes': '1000\n',
            'memory/memory.usage_in_bytes': '900\n',
            'memory/memory.stat': 'rss 500\ninactive_file 100\ntotal_inactive_file 400\n',
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=500,
        available=500,
        limit_level=str(root / 'memory'),
        unreadable_level=None,
        usage_unreadable_level=None,
    )


def test_read_cpu_quota_across_split_hierarchies(fake_cgroup: Callable[..., Path]) -> None:
    """Finds the counter of the level the quota binds at, even in another hierarchy."""
    root = fake_cgroup(
        mountinfo=V1_CPU_SPLIT_MOUNTINFO,
        self_cgroup='3:cpu:/slice/own\n4:cpuacct:/slice/own\n',
        files={
            'cpu/slice/cpu.cfs_quota_us': '50000\n',
            'cpu/slice/cpu.cfs_period_us': '100000\n',
            'cpu/slice/own/cpu.cfs_quota_us': '-1\n',
            'cpu/slice/own/cpu.cfs_period_us': '100000\n',
            'cpuacct/slice/cpuacct.usage': '7000000000\n',
            'cpuacct/slice/own/cpuacct.usage': '1000000\n',
        },
    )

    quota = cgroup.read_cpu_quota()

    assert quota is not None
    assert quota.cores == 0.5
    assert quota.limit_level == str(root / 'cpu' / 'slice')
    # The counter of that level lives under the other mount point.
    assert quota.usage_level == str(root / 'cpuacct' / 'slice')
    assert cgroup.read_cpu_usage(quota.usage_level) == 7.0


def test_read_cpu_quota_without_accounting(fake_cgroup: Callable[..., Path]) -> None:
    """Reports no counter when the level the quota binds at counts no CPU time anywhere."""
    fake_cgroup(
        mountinfo=V1_CPU_SPLIT_MOUNTINFO,
        self_cgroup='3:cpu:/slice/own\n4:cpuacct:/other\n',
        files={
            'cpu/slice/own/cgroup.procs': '',
            'cpu/slice/cpu.cfs_quota_us': '50000\n',
            'cpu/slice/cpu.cfs_period_us': '100000\n',
            'cpuacct/other/cpuacct.usage': '1000000\n',
        },
    )

    quota = cgroup.read_cpu_quota()

    assert quota is not None
    assert quota.cores == 0.5
    assert quota.usage_level is None


@pytest.mark.parametrize(
    'cpu_max',
    [
        pytest.param('0 100000\n', id='no quota at all'),
        pytest.param('-1 100000\n', id='a negative quota'),
        pytest.param('100000 0\n', id='a period of zero'),
    ],
)
def test_read_cpu_quota_v2_degenerate(fake_cgroup: Callable[..., Path], cpu_max: str) -> None:
    """Names the level for a cgroup v2 quota that is not bandwidth, since this interface spells absence as `max`."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': cpu_max, 'cpu.stat': 'usage_usec 0\n'},
    )

    assert cgroup.read_cpu().unreadable_level == str(root)


def test_read_cpu_quota_appearing_after_discovery(fake_cgroup: Callable[..., Path]) -> None:
    """Sees a quota written to a level that carried none when the chain was discovered."""
    # A systemd scope carries no `cpu.max` until it is given a quota, while the slice above it has one.
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/slice/own'),
        files={
            'slice/own/cpu.stat': 'usage_usec 0\n',
            'slice/cpu.max': 'max 100000\n',
            'slice/cpu.stat': 'usage_usec 0\n',
        },
    )

    assert cgroup.read_cpu_quota() is None

    # The scope of this process is given a quota of its own, below where the chain would have been cut.
    (root / 'slice' / 'own' / 'cpu.max').write_text('50000 100000\n')

    quota = cgroup.read_cpu_quota()

    assert quota is not None
    assert quota.cores == 0.5
    assert quota.limit_level == str(root / 'slice' / 'own')
