from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from proclimits import _cgroup as cgroup

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
    emfile,
    fail_to_read,
)

if TYPE_CHECKING:
    from collections.abc import Callable

OPENING_A_DIRECTORY = 'EACCES' if sys.platform == 'win32' else 'EISDIR'
"""The errno a directory put in place of a control file fails to open with, which differs by platform."""

UNNAMED_ERRNO = 99999
"""An errno that no platform gives a name."""

MEMORY_FILES = {'memory.max': '1000\n', 'memory.current': '100\n', 'memory.stat': 'inactive_file 0\n'}
"""A memory limit at the top of the mount, so a lookup that succeeds locates the memory controller."""


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
        unflushed_cache=None,
        limit_level=None,
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root / 'memory'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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


def count_discoveries(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Count the walks over `/proc`, one entry for each lookup that ran discovery rather than reusing it."""
    read_hierarchies = cgroup._read_hierarchies
    discoveries: list[None] = []

    def counted() -> cgroup._Hierarchies:
        discoveries.append(None)
        return read_hierarchies()

    monkeypatch.setattr(cgroup, '_read_hierarchies', counted)

    return discoveries


@pytest.mark.parametrize(
    ('error', 'reason'),
    [
        pytest.param(errno.EMFILE, 'EMFILE', id='the process has no descriptor left'),
        pytest.param(errno.ENFILE, 'ENFILE', id='the system has no descriptor left'),
        pytest.param(errno.ENOMEM, 'ENOMEM', id='the kernel has no memory left'),
        pytest.param(errno.EACCES, 'EACCES', id='the file may not be read'),
    ],
)
@pytest.mark.parametrize('failing', ['_PROC_SELF_CGROUP', '_PROC_SELF_MOUNTINFO'])
def test_locate_controllers_retries_a_failed_proc_read(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    error: int,
    reason: str,
) -> None:
    """Names the `/proc` file that failed and why, keeps nothing, and locates on the next lookup."""
    fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files=MEMORY_FILES)
    path = getattr(cgroup, failing)
    fail_to_read(monkeypatch, path, OSError(error, os.strerror(error)), times=1)
    discoveries = count_discoveries(monkeypatch)

    assert cgroup.locate_controllers() == cgroup.Controllers(
        interface=None,
        memory=None,
        cpu_quota=None,
        cpu_usage=None,
        cpu_set=None,
        unlocated_file=f'{path} ({reason})',
    )
    located = cgroup.locate_controllers()
    assert located.memory is not None
    # The success is kept, so a third lookup walks nothing.
    assert cgroup.locate_controllers() == located
    assert len(discoveries) == 2


@pytest.mark.parametrize(
    'error',
    [
        pytest.param(FileNotFoundError(errno.ENOENT, 'No such file or directory'), id='no such file'),
        pytest.param(NotADirectoryError(errno.ENOTDIR, 'Not a directory'), id='a file where a directory belongs'),
    ],
)
def test_locate_controllers_keeps_an_absent_proc(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    """Keeps a `/proc` that is not there, which lasts as long as the process, and names no file."""
    fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files=MEMORY_FILES)
    fail_to_read(monkeypatch, cgroup._PROC_SELF_CGROUP, error, times=None)
    discoveries = count_discoveries(monkeypatch)

    located_nothing = cgroup.Controllers(
        interface=None, memory=None, cpu_quota=None, cpu_usage=None, cpu_set=None, unlocated_file=None
    )
    assert [cgroup.locate_controllers() for _ in range(3)] == [located_nothing] * 3
    assert len(discoveries) == 1


@pytest.mark.usefixtures('_no_cgroup')
def test_locate_controllers_keeps_an_absent_proc_out_of_descriptors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps a `/proc` file that is not there where opening it fails for want of file descriptors."""
    # Out of descriptors, opening a file that is not there fails with EMFILE rather than ENOENT.
    fail_to_read(monkeypatch, cgroup._PROC_SELF_CGROUP, emfile(), times=None)
    discoveries = count_discoveries(monkeypatch)

    located_nothing = cgroup.Controllers(
        interface=None, memory=None, cpu_quota=None, cpu_usage=None, cpu_set=None, unlocated_file=None
    )
    assert [cgroup.locate_controllers() for _ in range(3)] == [located_nothing] * 3
    assert len(discoveries) == 1


@pytest.mark.usefixtures('_no_cgroup')
def test_locate_controllers_retries_where_a_stat_cannot_tell_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps nothing where the open and the stat of a `/proc` file both fail, and locates on the next lookup."""
    path = cgroup._PROC_SELF_CGROUP
    stat = Path.stat

    def refuse_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == path:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return stat(self, follow_symlinks=follow_symlinks)

    discoveries = count_discoveries(monkeypatch)

    # A refused stat says nothing about whether the file is there. Taken for absence, it would be kept.
    with monkeypatch.context() as patch:
        fail_to_read(patch, path, emfile(), times=1)
        patch.setattr(Path, 'stat', refuse_stat)
        failed = cgroup.locate_controllers()

    assert failed.unlocated_file == f'{path} (EMFILE)'
    assert cgroup.locate_controllers().unlocated_file is None
    assert len(discoveries) == 2


def test_locate_controllers_retries_a_value_error(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps no `ValueError`: content that cannot be read now may be read on the next lookup."""
    fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files=MEMORY_FILES)
    path = cgroup._PROC_SELF_CGROUP
    fail_to_read(monkeypatch, path, ValueError('unreadable content'), times=3)
    discoveries = count_discoveries(monkeypatch)

    lookups = [cgroup.locate_controllers() for _ in range(3)]

    assert [lookup.unlocated_file for lookup in lookups] == [f'{path} (ValueError)'] * 3
    assert len(discoveries) == 3


def test_locate_controllers_out_of_descriptors(fake_cgroup: Callable[..., Path]) -> None:
    """Names the file a real descriptor shortage failed, and locates once descriptors are free again."""
    # A descriptor limit is set through `resource`, which is POSIX.
    resource = pytest.importorskip('resource')
    fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files=MEMORY_FILES)

    # Every descriptor below the lowest free one is taken, so a limit there leaves no room for another.
    lowest_free = os.open(os.devnull, os.O_RDONLY)
    os.close(lowest_free)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (lowest_free, hard))
        failed = cgroup.locate_controllers()
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    assert failed.unlocated_file == f'{cgroup._PROC_SELF_CGROUP} (EMFILE)'
    assert cgroup.locate_controllers().memory is not None


def test_read_memory_names_a_failed_lookup(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names the `/proc` file its own lookup could not read, and names none once a lookup succeeds."""
    root = fake_cgroup(mountinfo=V2_MOUNTINFO, self_cgroup=V2_SELF_CGROUP.format(path='/'), files=MEMORY_FILES)
    path = cgroup._PROC_SELF_CGROUP
    fail_to_read(monkeypatch, path, emfile(), times=1)

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        unflushed_cache=None,
        limit_level=None,
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=f'{path} (EMFILE)',
    )
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=100,
        available=900,
        unflushed_cache=None,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_cpu_names_a_failed_lookup(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names the `/proc` file its own lookup could not read, and names none once a lookup succeeds."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '200000 100000\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    path = cgroup._PROC_SELF_CGROUP
    fail_to_read(monkeypatch, path, emfile(), times=1)

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None, cpu_set=None, unreadable_file=None, unconvertible_level=None, unlocated_file=f'{path} (EMFILE)'
    )
    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=cgroup.RawCpuQuota(cores=2.0, limit_level=str(root), usage_level=str(root)),
        cpu_set=None,
        unreadable_file=None,
        unconvertible_level=None,
        unlocated_file=None,
    )


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
            {'memory.high': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'inactive_file 400\n'},
            None,
            id='v2 memory.high alone by default',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {
                'memory.max': '1073741824\n',
                'memory.high': '536870912\n',
                'memory.current': '1000\n',
                'memory.stat': 'inactive_file 400\n',
            },
            1073741824,
            id='v2 memory.high below memory.max by default',
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

    raw = cgroup.read_memory()

    assert (raw.limit, raw.unreadable_file) == (expected, None)


@pytest.mark.parametrize(
    ('mountinfo', 'self_cgroup', 'files', 'expected'),
    [
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {'memory.high': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'inactive_file 400\n'},
            536870912,
            id='v2 memory.high alone',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {
                'memory.max': '1073741824\n',
                'memory.high': '536870912\n',
                'memory.current': '1000\n',
                'memory.stat': 'inactive_file 400\n',
            },
            536870912,
            id='v2 memory.high below memory.max',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {
                'memory.max': '536870912\n',
                'memory.high': '1073741824\n',
                'memory.current': '1000\n',
                'memory.stat': 'inactive_file 400\n',
            },
            536870912,
            id='v2 memory.high above memory.max',
        ),
        pytest.param(
            V2_MOUNTINFO,
            V2_SELF_CGROUP.format(path='/'),
            {
                'memory.max': '536870912\n',
                'memory.high': 'max\n',
                'memory.current': '1000\n',
                'memory.stat': 'inactive_file 400\n',
            },
            536870912,
            id='v2 memory.high unset',
        ),
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {
                'memory/memory.limit_in_bytes': '536870912\n',
                # What `docker --memory-reservation` writes. Tighter, so reading it as a limit would show.
                'memory/memory.soft_limit_in_bytes': '268435456\n',
                'memory/memory.usage_in_bytes': '1000\n',
                'memory/memory.stat': 'total_inactive_file 400\n',
            },
            536870912,
            id='v1 limit',
        ),
    ],
)
def test_read_memory_throttle_limits(
    fake_cgroup: Callable[..., Path],
    mountinfo: str,
    self_cgroup: str,
    files: dict[str, str],
    expected: int,
) -> None:
    """Takes the tighter of `memory.max` and `memory.high` on request, and `memory.limit_in_bytes` under cgroup v1."""
    fake_cgroup(mountinfo=mountinfo, self_cgroup=self_cgroup, files=files)

    assert cgroup.read_memory(throttle_limits=True).limit == expected


def test_read_memory_does_not_open_memory_high_by_default(fake_cgroup: Callable[..., Path]) -> None:
    """Answers from `memory.max` beside a `memory.high` that cannot be read, which is opened only on request."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '1000\n', 'memory.current': '100\n', 'memory.stat': 'inactive_file 0\n'},
    )
    # A directory in place of the file, so the file is there and the read fails.
    (root / 'memory.high').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=100,
        available=900,
        unflushed_cache=None,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )
    # Once asked for, the same file is left out and named.
    raw = cgroup.read_memory(throttle_limits=True)

    assert raw.unreadable_file == f'{root / "memory.high"} ({OPENING_A_DIRECTORY})'
    assert (raw.limit, raw.limit_level) == (1000, str(root))


def test_read_memory_names_memory_max_where_neither_limit_of_a_level_can_be_read(
    fake_cgroup: Callable[..., Path],
) -> None:
    """Names `memory.max` where both limits of one level fail, because it is read first."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod'),
        files={
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
            'memory.max': '2000\n',
            'memory.current': '300\n',
            'memory.stat': 'inactive_file 0\n',
        },
    )
    # Directories in place of both files, so both are there and neither can be read.
    (root / 'pod' / 'memory.max').mkdir()
    (root / 'pod' / 'memory.high').mkdir()

    raw = cgroup.read_memory(throttle_limits=True)

    assert raw.unreadable_file == f'{root / "pod" / "memory.max"} ({OPENING_A_DIRECTORY})'
    assert (raw.limit, raw.limit_level) == (2000, str(root))


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
        unflushed_cache=None,
        limit_level=str(root / 'kubepods' / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root / 'kubepods'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root / 'kubepods' / 'pod'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_partial_level(fake_cgroup: Callable[..., Path]) -> None:
    """Charges the tightest level nothing where its usage cannot be read and no level below it can be."""
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

    # The container is the cgroup of this process, so nothing below it bounds its charge.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=0,
        available=1000,
        unflushed_cache=None,
        limit_level=str(root / 'kubepods' / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "kubepods" / "pod" / "container" / "memory.stat"} (has no inactive_file)',
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_ancestor_without_usage_files(fake_cgroup: Callable[..., Path]) -> None:
    """Charges an ancestor whose usage cannot be read what the limited level below it is charged."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '1000\n',
            'pod/container/memory.current': '100\n',
            # The waiting cache of the container, which is not the level that decides below.
            'pod/container/memory.stat': 'inactive_file 0\nfile_dirty 5\n',
            # The tightest limit sits on the ancestor, whose usage will not read.
            'pod/memory.max': '500\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    # A directory in place of the file, as for a limit that cannot be opened. A level holding a limit holds a
    # usage file beside it, so the figure goes missing by failing to read rather than by being absent.
    (root / 'pod' / 'memory.current').mkdir()

    # The tightest limit is the ancestor's. It is charged at least the 100 of the container, so 400 is left.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=100,
        available=400,
        unflushed_cache=None,
        limit_level=str(root / 'pod'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "pod" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_no_used(fake_cgroup: Callable[..., Path]) -> None:
    """Charges a lone level nothing where its usage cannot be read, and names the file."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '536870912\n', 'memory.current': '1000\n', 'memory.stat': 'anon 600\n'},
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=536870912,
        used=0,
        available=536870912,
        unflushed_cache=None,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "memory.stat"} (has no inactive_file)',
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root / 'pod'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
        pytest.param(
            V1_MOUNTINFO,
            V1_SELF_CGROUP.format(path='/'),
            {'cpu,cpuacct/cpu.cfs_quota_us': '150000\n', 'cpu,cpuacct/cpuacct.usage': '0\n'},
            None,
            id='v1 quota without a period',
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

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    assert (quota.cores if quota is not None else None, unreadable_file) == (expected, None)


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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert (cpu_set.cores if cpu_set is not None else None, unreadable_file) == (expected, None)


def test_read_cpu_set_size_unparsable_entry(fake_cgroup: Callable[..., Path]) -> None:
    """Names the file when one entry of the list does not parse, rather than counting the rest."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpuset.cpus.effective': '0-1,nonsense\n'},
    )

    assert cgroup.read_cpu().unreadable_file == f'{root / "cpuset.cpus.effective"} (holds no list of cores)'


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
    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
    assert cpu_set is not None
    assert cpu_set.cores == 2

    (root / 'cpuset.cpus.effective').unlink()

    assert cgroup.read_cpu_set_size(cgroup.locate_controllers()) == (None, None)


@pytest.mark.parametrize(
    'error',
    [
        pytest.param(FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT)), id='no such file'),
        pytest.param(NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)), id='a file where a directory was'),
    ],
)
def test_read_cpu_set_size_file_vanishing_before_the_read(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    """Reads a set file as absent where opening it fails as it does once the file or its level is gone."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpuset.cpus.effective': '0-1\n', 'cpu.stat': 'usage_usec 0\n'},
    )
    fail_to_read(monkeypatch, root / 'cpuset.cpus.effective', error, times=None)

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None, cpu_set=None, unreadable_file=None, unconvertible_level=None, unlocated_file=None
    )


def test_read_memory_absent_limit_file_out_of_descriptors(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads an absent limit file as absent where opening it fails for want of file descriptors."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.current': '1000\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.max': '268435456\n',
            'pod/memory.current': '9000\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    # Out of descriptors, opening a file that is not there fails with EMFILE rather than ENOENT.
    fail_to_read(monkeypatch, root / 'pod' / 'container' / 'memory.max', emfile(), times=None)

    raw = cgroup.read_memory()

    assert (raw.limit, raw.unreadable_file) == (268435456, None)


def test_read_cpu_set_size_unreadable_file(fake_cgroup: Callable[..., Path]) -> None:
    """Leaves out a set file that cannot be read, and names it with the errno."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpuset.cpus.effective': '0-1\n'},
    )
    # A directory in place of the file, so the file is there and the read fails.
    (root / 'cpuset.cpus.effective').unlink()
    (root / 'cpuset.cpus.effective').mkdir()

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None,
        cpu_set=None,
        unreadable_file=f'{root / "cpuset.cpus.effective"} ({OPENING_A_DIRECTORY})',
        unconvertible_level=None,
        unlocated_file=None,
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

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    assert unreadable_file is None
    assert quota is not None
    assert quota.cores == 2.0
    assert quota.limit_level == str(root / 'cpu' / 'limited')
    assert quota.usage_level is None


@pytest.mark.parametrize(
    ('unreadable', 'readable', 'options', 'expected'),
    [
        # `memory.high` is not opened by default, so the container holds no limit and the pod's 10 GiB holds.
        pytest.param('memory.max', 'memory.high', {}, (10737418240, 'pod'), id='memory.max by default'),
        # The limit file beside the unreadable one still holds, so the container keeps its 512 MiB.
        pytest.param(
            'memory.max',
            'memory.high',
            {'throttle_limits': True},
            (536870912, 'pod/container'),
            id='memory.max with throttle limits',
        ),
        pytest.param(
            'memory.high',
            'memory.max',
            {'throttle_limits': True},
            (536870912, 'pod/container'),
            id='memory.high with throttle limits',
        ),
    ],
)
def test_read_memory_unreadable_limit(
    fake_cgroup: Callable[..., Path],
    unreadable: str,
    readable: str,
    options: dict[str, bool],
    expected: tuple[int, str],
) -> None:
    """Leaves out a limit file that cannot be read, reads on from the limits that can, and names the file and why."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/memory.max': '10737418240\n',
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
            f'pod/container/{unreadable}': 'not a number\n',
            f'pod/container/{readable}': '536870912\n',
            'pod/container/memory.current': '100\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
        },
    )

    limit, level = expected

    assert cgroup.read_memory(**options) == cgroup.RawMemory(
        limit=limit,
        used=100,
        available=limit - 100,
        unflushed_cache=None,
        limit_level=str(root / level),
        unreadable_file=f'{root / "pod" / "container" / unreadable} (holds no number)',
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_looser_level_without_usage(fake_cgroup: Callable[..., Path]) -> None:
    """Bounds the unread usage of a looser level by the level below, and keeps the room the closer level reads."""
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

    # The pod is charged at least the 100 of the container, which leaves it 900 against the container's 400. Its
    # limit is below the ceiling, so its usage is named though it does not set the room.
    assert cgroup.read_memory(ceiling=16 * 1024**3) == cgroup.RawMemory(
        limit=500,
        used=100,
        available=400,
        unflushed_cache=None,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "pod" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_limit_file_cannot_be_opened(fake_cgroup: Callable[..., Path]) -> None:
    """Names a limit file that is there and cannot be opened, with the errno, and reads past it."""
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

    raw = cgroup.read_memory()

    assert raw.unreadable_file == f'{root / "pod" / "container" / "memory.max"} ({OPENING_A_DIRECTORY})'
    assert (raw.limit, raw.limit_level) == (10737418240, str(root / 'pod'))


@pytest.mark.parametrize(
    ('negative', 'readable', 'options'),
    [
        pytest.param('memory.max', 'memory.high', {}, id='memory.max by default'),
        pytest.param('memory.max', 'memory.high', {'throttle_limits': True}, id='memory.max with throttle limits'),
        pytest.param('memory.high', 'memory.max', {'throttle_limits': True}, id='memory.high with throttle limits'),
    ],
)
def test_read_memory_negative_limit(
    fake_cgroup: Callable[..., Path],
    negative: str,
    readable: str,
    options: dict[str, bool],
) -> None:
    """Names a limit file holding a negative number of bytes, which is no limit and no absence either."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/memory.max': '10737418240\n',
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
            f'pod/container/{negative}': '-5\n',
            f'pod/container/{readable}': '536870912\n',
        },
    )

    expected = f'{root / "pod" / "container" / negative} (holds a negative number)'
    assert cgroup.read_memory(**options).unreadable_file == expected


def test_read_cpu_quota_unreadable_file(fake_cgroup: Callable[..., Path]) -> None:
    """Answers with an ancestor's quota where a closer one cannot be read, and names the file and why."""
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
        quota=cgroup.RawCpuQuota(cores=8.0, limit_level=str(root / 'pod'), usage_level=str(root / 'pod')),
        cpu_set=None,
        unreadable_file=f'{root / "pod" / "container" / "cpu.max"} (holds no quota and period)',
        unconvertible_level=None,
        unlocated_file=None,
    )


def record_reads(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[Path]:
    """Record the path each named reader of `cgroup` is called with, in order, and read as usual."""
    read: list[Path] = []

    def recording(reader: Callable[..., Any]) -> Callable[..., Any]:
        def record(path: Path, *args: Any, **kwargs: Any) -> Any:
            read.append(path)
            return reader(path, *args, **kwargs)

        return record

    for name in names:
        monkeypatch.setattr(cgroup, name, recording(getattr(cgroup, name)))

    return read


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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
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
        unflushed_cache=None,
        limit_level=str(root / 'memory'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


@pytest.mark.usefixtures('_no_cgroup')
def test_no_cgroups() -> None:
    """Reports nothing on a system that has no cgroups."""
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        unflushed_cache=None,
        limit_level=None,
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )
    # An absent `/proc` is a fact about the system rather than a failed lookup, so no file is named.
    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None, cpu_set=None, unreadable_file=None, unconvertible_level=None, unlocated_file=None
    )
    assert cgroup.read_cpu_quota(cgroup.locate_controllers()) == (None, None)
    assert cgroup.read_cpu_set_size(cgroup.locate_controllers()) == (None, None)
    assert cgroup.read_cpu_usage() is None


def raise_permission_error(_self: Path) -> bool:
    """Stand in for `Path.exists` on a directory the kernel refuses to traverse."""
    raise PermissionError(13, 'Permission denied')


def test_exists_on_an_unreadable_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reports a path as missing when the lookup is denied. From Python 3.14 `Path.exists()` does that on its own."""
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
        unflushed_cache=None,
        limit_level=None,
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    # The quota binds on the pod, so a rate has to be measured there rather than in the container.
    assert quota == cgroup.RawCpuQuota(cores=1.5, limit_level=str(root / 'pod'), usage_level=str(root / 'pod'))
    assert unreadable_file is None


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

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    assert quota == cgroup.RawCpuQuota(
        cores=1.5,
        limit_level=str(root / 'pod' / 'container'),
        usage_level=str(root / 'pod' / 'container'),
    )
    assert unreadable_file is None


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

    cpu_set, unreadable_file = cgroup.read_cpu_set_size(cgroup.locate_controllers())

    assert unreadable_file is None
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
        unflushed_cache=None,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
    usage = cgroup._read_memory_usage(controller, root)
    assert usage is not None
    assert usage.charged == 0
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=0,
        available=1000,
        unflushed_cache=None,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_cpu_quota_v1_zero_period(fake_cgroup: Callable[..., Path]) -> None:
    """Names the period file rather than dividing by a period of zero, which no kernel writes."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'cpu,cpuacct/cpu.cfs_quota_us': '100000\n',
            'cpu,cpuacct/cpu.cfs_period_us': '0\n',
            'cpu,cpuacct/cpuacct.usage': '0\n',
        },
    )

    expected = f'{root / "cpu,cpuacct" / "cpu.cfs_period_us"} (holds zero or less)'
    assert cgroup.read_cpu().unreadable_file == expected


def test_read_cpu_quota_v1_zero_quota(fake_cgroup: Callable[..., Path]) -> None:
    """Names the quota file for a quota of zero, which no kernel writes."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'cpu,cpuacct/cpu.cfs_quota_us': '0\n',
            'cpu,cpuacct/cpu.cfs_period_us': '100000\n',
            'cpu,cpuacct/cpuacct.usage': '0\n',
        },
    )

    assert cgroup.read_cpu().unreadable_file == f'{root / "cpu,cpuacct" / "cpu.cfs_quota_us"} (holds zero)'


def test_read_cpu_quota_v1_zero_quota_beside_an_unreadable_period(fake_cgroup: Callable[..., Path]) -> None:
    """Names the quota file for a quota of zero, whatever its period file holds."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={'cpu,cpuacct/cpu.cfs_quota_us': '0\n', 'cpu,cpuacct/cpuacct.usage': '0\n'},
    )
    (root / 'cpu,cpuacct' / 'cpu.cfs_period_us').mkdir()

    assert cgroup.read_cpu().unreadable_file == f'{root / "cpu,cpuacct" / "cpu.cfs_quota_us"} (holds zero)'


def test_read_cpu_quota_v1_unlimited_beside_an_unreadable_period(fake_cgroup: Callable[..., Path]) -> None:
    """Names no file where a cgroup v1 level sets no quota, whatever its period file holds."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={'cpu,cpuacct/cpu.cfs_quota_us': '-1\n', 'cpu,cpuacct/cpuacct.usage': '0\n'},
    )
    (root / 'cpu,cpuacct' / 'cpu.cfs_period_us').mkdir()

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None, cpu_set=None, unreadable_file=None, unconvertible_level=None, unlocated_file=None
    )


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
        unflushed_cache=None,
        limit_level=str(root / 'ours' / 'mine'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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
        unflushed_cache=None,
        limit_level=str(root / 'memory'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
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

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    assert unreadable_file is None
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

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    assert unreadable_file is None
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
    """Names the file for a cgroup v2 quota that is not bandwidth, since this interface spells absence as `max`."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': cpu_max, 'cpu.stat': 'usage_usec 0\n'},
    )

    assert cgroup.read_cpu().unreadable_file == f'{root / "cpu.max"} (holds a quota or period of zero or less)'


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

    assert cgroup.read_cpu_quota(cgroup.locate_controllers()) == (None, None)

    # The scope of this process is given a quota of its own, below where the chain would have been cut.
    (root / 'slice' / 'own' / 'cpu.max').write_text('50000 100000\n')

    quota, unreadable_file = cgroup.read_cpu_quota(cgroup.locate_controllers())

    assert unreadable_file is None
    assert quota is not None
    assert quota.cores == 0.5
    assert quota.limit_level == str(root / 'slice' / 'own')


def test_read_memory_dirty_cache_is_not_credited(fake_cgroup: Callable[..., Path]) -> None:
    """Credits the inactive file cache less the pages waiting to be written."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'anon 300\ninactive_file 600\nfile_dirty 200\nfile_writeback 100\n',
        },
    )

    # 300 of the 600 of inactive cache is waiting, so 300 is credited and 600 stays charged. The working set
    # alone would be 300.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=600,
        available=400,
        unflushed_cache=300,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_dirty_cache_is_not_credited_v1(fake_cgroup: Callable[..., Path]) -> None:
    """Reads the hierarchical dirty and writeback keys under cgroup v1."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'memory/memory.limit_in_bytes': '1000\n',
            'memory/memory.usage_in_bytes': '900\n',
            # The unprefixed keys carry other values, so reading them would change the result.
            'memory/memory.stat': (
                'rss 300\ndirty 10\nwriteback 5\ninactive_file 100\n'
                'total_inactive_file 600\ntotal_dirty 200\ntotal_writeback 100\n'
            ),
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=600,
        available=400,
        unflushed_cache=300,
        limit_level=str(root / 'memory'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_more_waiting_than_inactive_cache(fake_cgroup: Callable[..., Path]) -> None:
    """Credits nothing where more is waiting to be written than the inactive cache holds."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'inactive_file 100\nfile_dirty 300\nfile_writeback 50\n',
        },
    )

    # The whole 900 of `memory.current` is charged, and no more.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=900,
        available=100,
        unflushed_cache=350,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_without_the_dirty_keys(fake_cgroup: Callable[..., Path]) -> None:
    """Credits the whole inactive cache where neither key is there, and reports the cache as unknown."""
    # A cgroup v1 stat file with no dirty or writeback key at all.
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/'),
        files={
            'memory/memory.limit_in_bytes': '1000\n',
            'memory/memory.usage_in_bytes': '900\n',
            'memory/memory.stat': (
                'cache 500\nrss 300\nmapped_file 40\npgpgin 100\npgpgout 50\n'
                'inactive_anon 0\nactive_anon 300\ninactive_file 400\nactive_file 100\n'
                'hierarchical_memory_limit 1000\n'
                'total_cache 500\ntotal_rss 300\ntotal_active_file 100\ntotal_inactive_file 400\n'
            ),
        },
    )

    # 900 charged less the whole 400 of inactive cache, as the working set alone reports.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=500,
        available=500,
        unflushed_cache=None,
        limit_level=str(root / 'memory'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_dirty_keys_without_the_inactive_cache(fake_cgroup: Callable[..., Path]) -> None:
    """Names the stat file where the inactive cache is missing, whatever else it carries."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'anon 300\nfile_dirty 200\nfile_writeback 100\n',
        },
    )

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=0,
        available=1000,
        unflushed_cache=None,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "memory.stat"} (has no inactive_file)',
        unlocated_file=None,
    )


def test_read_memory_reports_the_cache_of_the_deciding_level(fake_cgroup: Callable[..., Path]) -> None:
    """Keeps the smallest distance of the chain, and reports the waiting cache of the level that set it."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod'),
        files={
            # The tighter limit, and 80 bytes from it. Its stat file carries neither key.
            'kubepods/pod/memory.max': '100\n',
            'kubepods/pod/memory.current': '30\n',
            'kubepods/pod/memory.stat': 'inactive_file 10\n',
            # The node allows less room, and part of what it holds is cache that has not reached the disk.
            'kubepods/memory.max': '120\n',
            'kubepods/memory.current': '110\n',
            'kubepods/memory.stat': 'inactive_file 10\nfile_dirty 4\nfile_writeback 1\n',
        },
    )

    # The node credits 5 of its 10 of inactive cache, so 105 is charged there. The room is 15 there and 80 at
    # the pod. The cache reported is the node's, because the node decided. The pod holds the tightest limit.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=100,
        used=85,
        available=15,
        unflushed_cache=5,
        limit_level=str(root / 'kubepods' / 'pod'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_reports_the_cache_of_the_closest_level_on_a_tie(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the waiting cache of the level closest to this process where two levels leave the same room."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod'),
        files={
            # The tighter limit, closest to this process.
            'kubepods/pod/memory.max': '100\n',
            'kubepods/pod/memory.current': '50\n',
            'kubepods/pod/memory.stat': 'inactive_file 10\nfile_dirty 2\nfile_writeback 0\n',
            # The looser limit, with the same room left.
            'kubepods/memory.max': '120\n',
            'kubepods/memory.current': '72\n',
            'kubepods/memory.stat': 'inactive_file 16\nfile_dirty 6\nfile_writeback 0\n',
        },
    )

    # The pod holds the tightest limit. It credits 8 of its 10 of inactive cache, so 42 is charged there. The node
    # credits 10 of its 16, so 62 is charged there. The room is 58 at both levels. The cache reported is the pod's 2,
    # because the pod is closest to this process. The node's 6 would mean the tie went to the ancestor.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=100,
        used=42,
        available=58,
        unflushed_cache=2,
        limit_level=str(root / 'kubepods' / 'pod'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_reads_the_stat_file_once_per_level(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parses `memory.stat` once per level, after `memory.current`."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '1000\n',
            'pod/container/memory.current': '900\n',
            'pod/container/memory.stat': 'inactive_file 600\nfile_dirty 200\nfile_writeback 100\n',
            'pod/memory.max': '2000\n',
            'pod/memory.current': '1500\n',
            'pod/memory.stat': 'inactive_file 400\nfile_dirty 100\nfile_writeback 50\n',
        },
    )

    read = record_reads(monkeypatch, '_read_stat_values', '_read_number')

    # The container is charged 900 less a credit of 300, and its 400 left is the smaller room of the two.
    assert cgroup.read_memory().used == 600

    # Two levels, so this pins one parse per level rather than one parse in total. Whole paths, because both
    # levels name the file `memory.stat`.
    for level in (root / 'pod' / 'container', root / 'pod'):
        assert read.count(level / 'memory.stat') == 1
        assert read.index(level / 'memory.current') < read.index(level / 'memory.stat')


def test_read_memory_with_the_dirty_keys_at_zero(fake_cgroup: Callable[..., Path]) -> None:
    """Reports zero, not unknown, where both keys are there and read zero."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'inactive_file 600\nfile_dirty 0\nfile_writeback 0\n',
        },
    )

    # The whole 600 of inactive cache is credited, as where neither key is there.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=300,
        available=700,
        unflushed_cache=0,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_unparsable_dirty_key(fake_cgroup: Callable[..., Path]) -> None:
    """Counts the usable key where the other one is not a number."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'inactive_file 600\nfile_dirty not-a-number\nfile_writeback 100\n',
        },
    )

    # The 100 of writeback comes off the 600 of inactive cache.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=400,
        available=600,
        unflushed_cache=100,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_cpu_usage_repeated_key(fake_cgroup: Callable[..., Path]) -> None:
    """Takes the first occurrence of a repeated key."""
    # No kernel repeats a key. `cpu.stat` goes through the parser `memory.stat` uses, so this pins it for both.
    fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.stat': 'usage_usec 2500000\nusage_usec 9900000\n'},
    )

    assert cgroup.read_cpu_usage() == 2.5


def test_read_memory_negative_dirty_key(fake_cgroup: Callable[..., Path]) -> None:
    """Credits no more than the inactive cache holds where a key reads negative."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'inactive_file 600\nfile_dirty -100\nfile_writeback 0\n',
        },
    )

    # The whole 600 of inactive cache is credited and no more.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=300,
        available=700,
        unflushed_cache=0,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_negative_key_beside_a_positive_one(fake_cgroup: Callable[..., Path]) -> None:
    """Lets a key that reads negative take nothing off what the other one reported."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={
            'memory.max': '1000\n',
            'memory.current': '900\n',
            'memory.stat': 'inactive_file 600\nfile_dirty -100\nfile_writeback 500\n',
        },
    )

    # The 500 of writeback stands on its own, so 100 of the 600 is credited and 800 stays charged.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=800,
        available=200,
        unflushed_cache=500,
        limit_level=str(root),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_no_readable_limit(fake_cgroup: Callable[..., Path]) -> None:
    """Reports no limit where none can be read, and names the closest file that cannot."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': 'not a number\n',
            'pod/container/memory.current': '100\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.current': '100\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'pod' / 'memory.max').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=None,
        used=None,
        available=None,
        unflushed_cache=None,
        limit_level=None,
        unreadable_file=f'{root / "pod" / "container" / "memory.max"} (holds no number)',
        usage_unreadable_file=None,
        unlocated_file=None,
    )


def test_read_memory_bounds_a_silent_parent_by_the_own_cgroup(fake_cgroup: Callable[..., Path]) -> None:
    """Charges a parent whose usage cannot be read at least what the cgroup of this process is charged."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            # The cgroup of this process holds no limit, and its usage reads.
            'pod/container/memory.current': '300\n',
            'pod/container/memory.stat': 'inactive_file 50\n',
            'pod/memory.max': '1000\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'pod' / 'memory.current').mkdir()

    # The container is charged 250, so the pod is charged at least that.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=250,
        available=750,
        unflushed_cache=None,
        limit_level=str(root / 'pod'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "pod" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_bound_takes_the_largest_charge_below(fake_cgroup: Callable[..., Path]) -> None:
    """Bounds a silent level by the largest charge read anywhere below it, limited or not."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            'kubepods/pod/container/memory.max': '2000\n',
            'kubepods/pod/container/memory.current': '300\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
            # No limit here, and more charged than the container, because a sibling container is charged too.
            'kubepods/pod/memory.current': '400\n',
            'kubepods/pod/memory.stat': 'inactive_file 0\n',
            'kubepods/memory.max': '1000\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'kubepods' / 'memory.current').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=400,
        available=600,
        unflushed_cache=None,
        limit_level=str(root / 'kubepods'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "kubepods" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_bound_takes_the_largest_charge_of_several_unlimited_levels(
    fake_cgroup: Callable[..., Path],
) -> None:
    """Bounds a silent level by the largest charge among the levels below it that hold no limit, not the closest."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            # Neither level below holds a limit, and the one further from this process is charged more.
            'kubepods/pod/container/memory.current': '300\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
            'kubepods/pod/memory.current': '400\n',
            'kubepods/pod/memory.stat': 'inactive_file 0\n',
            'kubepods/memory.max': '1000\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'kubepods' / 'memory.current').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=400,
        available=600,
        unflushed_cache=None,
        limit_level=str(root / 'kubepods'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "kubepods" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_bound_keeps_a_larger_charge_read_further_down(fake_cgroup: Callable[..., Path]) -> None:
    """Bounds a silent level by the largest charge read below it, not by the charge of the level right below it."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            'kubepods/pod/container/memory.max': '2000\n',
            'kubepods/pod/container/memory.current': '400\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
            # Charged less than the container below it, as two reads a moment apart can be.
            'kubepods/pod/memory.max': '3000\n',
            'kubepods/pod/memory.current': '300\n',
            'kubepods/pod/memory.stat': 'inactive_file 0\n',
            'kubepods/memory.max': '1000\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'kubepods' / 'memory.current').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=400,
        available=600,
        unflushed_cache=None,
        limit_level=str(root / 'kubepods'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "kubepods" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_bound_reads_past_a_level_that_cannot_be_read(fake_cgroup: Callable[..., Path]) -> None:
    """Counts a level below whose usage cannot be read as zero, and keeps the charge read below that."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            'kubepods/pod/container/memory.current': '300\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
            # No limit here, and no usage to bound the level above with.
            'kubepods/pod/memory.stat': 'inactive_file 0\n',
            'kubepods/memory.max': '1000\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'kubepods' / 'pod' / 'memory.current').mkdir()
    (root / 'kubepods' / 'memory.current').mkdir()

    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=1000,
        used=300,
        available=700,
        unflushed_cache=None,
        limit_level=str(root / 'kubepods'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "kubepods" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


@pytest.mark.parametrize(
    ('files', 'name', 'reason'),
    [
        pytest.param({'pod/memory.stat': 'inactive_file 0\n'}, 'memory.current', 'absent', id='no usage file'),
        pytest.param(
            {'pod/memory.current': 'nonsense\n', 'pod/memory.stat': 'inactive_file 0\n'},
            'memory.current',
            'holds no number',
            id='a usage that is no number',
        ),
        pytest.param({'pod/memory.current': '100\n'}, 'memory.stat', 'absent', id='no stat file'),
    ],
)
def test_read_memory_names_why_a_usage_cannot_be_read(
    fake_cgroup: Callable[..., Path],
    files: dict[str, str],
    name: str,
    reason: str,
) -> None:
    """Names the usage file of a level holding a limit, and what kept it from being read."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod'),
        files={
            # The controller is found by its usage file, so a level above carries one.
            'memory.current': '0\n',
            'pod/memory.max': '1000\n',
            **files,
        },
    )

    assert cgroup.read_memory().usage_unreadable_file == f'{root / "pod" / name} ({reason})'


@pytest.mark.parametrize(
    ('files', 'name'),
    [
        pytest.param({'pod/memory.stat': 'inactive_file 0\n'}, 'memory.current', id='no usage file'),
        pytest.param({'pod/memory.current': '100\n'}, 'memory.stat', id='no stat file'),
    ],
)
def test_read_memory_names_an_absent_usage_file_out_of_descriptors(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    name: str,
) -> None:
    """Names an absent usage file as absent where opening it fails for want of file descriptors."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod'),
        files={
            'memory.current': '0\n',
            'pod/memory.max': '1000\n',
            **files,
        },
    )
    fail_to_read(monkeypatch, root / 'pod' / name, emfile(), times=None)

    assert cgroup.read_memory().usage_unreadable_file == f'{root / "pod" / name} (absent)'


def test_read_memory_silent_level_between_two_that_answer(fake_cgroup: Callable[..., Path]) -> None:
    """Lets a level above a silent one set the room where it is the closest to its limit."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            'kubepods/pod/container/memory.max': '500\n',
            'kubepods/pod/container/memory.current': '100\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
            'kubepods/pod/memory.max': '1000\n',
            'kubepods/pod/memory.stat': 'inactive_file 0\n',
            'kubepods/memory.max': '2000\n',
            'kubepods/memory.current': '1950\n',
            'kubepods/memory.stat': 'inactive_file 0\nfile_dirty 7\n',
        },
    )
    (root / 'kubepods' / 'pod' / 'memory.current').mkdir()

    # 400 left at the container, at least 900 at the pod, and 50 at kubepods.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=450,
        available=50,
        unflushed_cache=7,
        limit_level=str(root / 'kubepods' / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "kubepods" / "pod" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_reads_a_level_without_a_limit_only_for_a_bound(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads the usage of a level holding no limit only where a level above it cannot be read."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.current': '300\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.max': '1000\n',
            'pod/memory.current': '400\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    read = record_reads(monkeypatch, '_read_number')
    own_usage = root / 'pod' / 'container' / 'memory.current'

    assert cgroup.read_memory().used == 400
    assert own_usage not in read

    (root / 'pod' / 'memory.current').unlink()
    (root / 'pod' / 'memory.current').mkdir()

    assert cgroup.read_memory().used == 300
    assert own_usage in read


def test_read_memory_bound_skips_the_levels_below_a_charge_read(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounds a silent level by a charge read below it, without reading the levels under that one."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/kubepods/pod/container'),
        files={
            'kubepods/pod/container/memory.current': '300\n',
            'kubepods/pod/container/memory.stat': 'inactive_file 0\n',
            'kubepods/pod/memory.max': '1000\n',
            'kubepods/pod/memory.current': '400\n',
            'kubepods/pod/memory.stat': 'inactive_file 0\n',
            # Tighter than the pod, so the bound sets the room: at least 400 charged leaves at most 500.
            'kubepods/memory.max': '900\n',
            'kubepods/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'kubepods' / 'memory.current').mkdir()
    read = record_reads(monkeypatch, '_read_number')

    # The pod is charged 400, which already covers the container below it.
    assert cgroup.read_memory().used == 400
    assert root / 'kubepods' / 'pod' / 'memory.current' in read
    assert root / 'kubepods' / 'pod' / 'container' / 'memory.current' not in read


def test_read_memory_names_the_closest_silent_usage(fake_cgroup: Callable[..., Path]) -> None:
    """Names the usage file of the closest level when two levels holding a limit cannot be read."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '1000\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.max': '2000\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'pod' / 'container' / 'memory.current').mkdir()
    (root / 'pod' / 'memory.current').mkdir()

    expected = f'{root / "pod" / "container" / "memory.current"} ({OPENING_A_DIRECTORY})'
    assert cgroup.read_memory().usage_unreadable_file == expected


def test_read_memory_names_no_usage_at_a_level_reaching_the_ceiling(fake_cgroup: Callable[..., Path]) -> None:
    """Names no unreadable usage at a level whose limit reaches the ceiling."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/limited'),
        files={
            'memory/limited/memory.limit_in_bytes': '536870912\n',
            'memory/limited/memory.usage_in_bytes': '104857600\n',
            'memory/limited/memory.stat': 'total_inactive_file 0\n',
            # How cgroup v1 spells "no limit", on a level whose usage cannot be read.
            'memory/memory.limit_in_bytes': '9223372036854771712\n',
            'memory/memory.stat': 'total_inactive_file 0\n',
        },
    )
    (root / 'memory' / 'memory.usage_in_bytes').mkdir()

    # The own group sets the room, and its usage was read, so the reading is exact.
    assert cgroup.read_memory(ceiling=16 * 1024**3) == cgroup.RawMemory(
        limit=536870912,
        used=104857600,
        available=432013312,
        unflushed_cache=None,
        limit_level=str(root / 'memory' / 'limited'),
        unreadable_file=None,
        usage_unreadable_file=None,
        unlocated_file=None,
    )
    # Without a ceiling the sentinel cannot be told from a limit, so its usage is named.
    expected = f'{root / "memory" / "memory.usage_in_bytes"} ({OPENING_A_DIRECTORY})'
    assert cgroup.read_memory().usage_unreadable_file == expected


@pytest.mark.parametrize(
    'ceiling',
    [
        pytest.param(1500, id='closer limit above the ceiling'),
        pytest.param(2000, id='closer limit at the ceiling'),
    ],
)
def test_read_memory_names_a_farther_usage_past_a_level_reaching_the_ceiling(
    fake_cgroup: Callable[..., Path],
    ceiling: int,
) -> None:
    """Names the usage of a farther level whose limit counts, past a closer one whose limit reaches the ceiling."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '2000\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.max': '1000\n',
            'pod/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'pod' / 'container' / 'memory.current').mkdir()
    (root / 'pod' / 'memory.current').mkdir()

    expected = f'{root / "pod" / "memory.current"} ({OPENING_A_DIRECTORY})'
    assert cgroup.read_memory(ceiling=ceiling).usage_unreadable_file == expected


def test_read_memory_names_a_level_reaching_the_ceiling_that_sets_the_room(fake_cgroup: Callable[..., Path]) -> None:
    """Names the usage of a level whose limit reaches the ceiling where its lower bound sets the room."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/slice/pod/container'),
        files={
            'slice/pod/container/memory.max': '800\n',
            'slice/pod/container/memory.current': '100\n',
            'slice/pod/container/memory.stat': 'inactive_file 0\n',
            # No limit here, and charged for its other containers too.
            'slice/pod/memory.current': '1000\n',
            'slice/pod/memory.stat': 'inactive_file 0\n',
            'slice/memory.max': '1600\n',
            'slice/memory.stat': 'inactive_file 0\n',
        },
    )
    (root / 'slice' / 'memory.current').mkdir()

    # 700 left at the container, and at most 600 at the slice, which is charged at least the 1000 of the pod.
    assert cgroup.read_memory(ceiling=1600) == cgroup.RawMemory(
        limit=800,
        used=200,
        available=600,
        unflushed_cache=None,
        limit_level=str(root / 'slice' / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "slice" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_memory_prefers_a_read_level_on_a_tie(fake_cgroup: Callable[..., Path]) -> None:
    """Lets a level whose usage was read set the room on a tie, so its waiting cache is reported."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/memory.max': '500\n',
            'pod/container/memory.stat': 'inactive_file 0\n',
            'pod/memory.max': '1000\n',
            'pod/memory.current': '500\n',
            'pod/memory.stat': 'inactive_file 0\nfile_dirty 3\n',
        },
    )
    (root / 'pod' / 'container' / 'memory.current').mkdir()

    # 500 left at the container, charged nothing for want of a level below it, and 500 left at the pod.
    assert cgroup.read_memory() == cgroup.RawMemory(
        limit=500,
        used=0,
        available=500,
        unflushed_cache=3,
        limit_level=str(root / 'pod' / 'container'),
        unreadable_file=None,
        usage_unreadable_file=f'{root / "pod" / "container" / "memory.current"} ({OPENING_A_DIRECTORY})',
        unlocated_file=None,
    )


def test_read_cpu_quota_names_the_closest_unreadable_file(fake_cgroup: Callable[..., Path]) -> None:
    """Names the quota file of the closest level when two levels hold one that cannot be read."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.max': 'nonsense\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpu.max': '0 100000\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )

    expected = f'{root / "pod" / "container" / "cpu.max"} (holds no quota and period)'
    assert cgroup.read_cpu().unreadable_file == expected


def test_read_cpu_keeps_the_set_beside_an_unreadable_quota(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the set where the quota beside it cannot be read, and names the quota file."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': 'nonsense\n', 'cpuset.cpus.effective': '0-1\n', 'cpu.stat': 'usage_usec 0\n'},
    )

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None,
        cpu_set=cgroup.RawCpuSet(cores=2, limit_level=str(root), usage_level=str(root)),
        unreadable_file=f'{root / "cpu.max"} (holds no quota and period)',
        unconvertible_level=None,
        unlocated_file=None,
    )


def test_read_cpu_keeps_the_quota_beside_an_unreadable_set(fake_cgroup: Callable[..., Path]) -> None:
    """Reports the quota where the set beside it cannot be read, and names the set file."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'cpu.max': '50000 100000\n', 'cpuset.cpus.effective': '0-1,nonsense\n', 'cpu.stat': 'usage_usec 0\n'},
    )

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=cgroup.RawCpuQuota(cores=0.5, limit_level=str(root), usage_level=str(root)),
        cpu_set=None,
        unreadable_file=f'{root / "cpuset.cpus.effective"} (holds no list of cores)',
        unconvertible_level=None,
        unlocated_file=None,
    )


def test_read_cpu_set_size_does_not_walk_up_past_an_unreadable_set(fake_cgroup: Callable[..., Path]) -> None:
    """Reports no set where the closest one cannot be read, rather than the set of an ancestor."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpuset.cpus.effective': '0-3\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )
    # A cgroup v2 partition takes its cores out of the effective set of its parent, so the pod's set need not
    # contain the container's.
    (root / 'pod' / 'container' / 'cpuset.cpus.effective').mkdir()

    assert cgroup.read_cpu() == cgroup.RawCpu(
        quota=None,
        cpu_set=None,
        unreadable_file=f'{root / "pod" / "container" / "cpuset.cpus.effective"} ({OPENING_A_DIRECTORY})',
        unconvertible_level=None,
        unlocated_file=None,
    )


@pytest.mark.parametrize(
    ('configured', 'expected'),
    [
        pytest.param('0-1\n', 2, id='a configured set'),
        # An empty file spells an inherited set. The set of the parent is no stand-in for it.
        pytest.param('\n', None, id='an inherited set'),
        pytest.param('0-1,nonsense\n', None, id='a configured set that cannot be read either'),
    ],
)
def test_read_cpu_set_size_past_an_unreadable_effective_set_under_v1(
    fake_cgroup: Callable[..., Path],
    configured: str,
    expected: int | None,
) -> None:
    """Reads the configured set of the same level where the effective one cannot be read, and names the latter."""
    root = fake_cgroup(
        mountinfo=V1_MOUNTINFO,
        self_cgroup=V1_SELF_CGROUP.format(path='/child'),
        files={'cpuset/cpuset.cpus': '0-3\n', 'cpuset/child/cpuset.cpus': configured},
    )
    # A directory in place of the file, so the file is there and the read fails.
    effective = root / 'cpuset' / 'child' / 'cpuset.effective_cpus'
    effective.mkdir()

    raw = cgroup.read_cpu()

    assert raw.unreadable_file == f'{effective} ({OPENING_A_DIRECTORY})'
    assert (raw.cpu_set.cores if raw.cpu_set is not None else None) == expected


def test_read_cpu_names_an_unreadable_quota_over_a_closer_set(fake_cgroup: Callable[..., Path]) -> None:
    """Names the unreadable quota of the parent over the unreadable set of the own cgroup."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/pod/container'),
        files={
            'pod/container/cpuset.cpus.effective': '0-1,nonsense\n',
            'pod/container/cpu.stat': 'usage_usec 0\n',
            'pod/cpu.max': 'nonsense\n',
            'pod/cpu.stat': 'usage_usec 0\n',
        },
    )

    # The set file is closer, and the quota file is named all the same.
    expected = f'{root / "pod" / "cpu.max"} (holds no quota and period)'
    assert cgroup.read_cpu().unreadable_file == expected


@pytest.mark.parametrize(
    ('error', 'reason'),
    [
        pytest.param(errno.EACCES, 'EACCES', id='a file nobody may read'),
        pytest.param(errno.EMFILE, 'EMFILE', id='a process out of file descriptors'),
    ],
)
def test_read_memory_names_the_errno_a_limit_failed_with(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    error: int,
    reason: str,
) -> None:
    """Names the errno a limit file failed with, which tells a descriptor shortage from a permission."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '1000\n', 'memory.current': '100\n', 'memory.stat': 'inactive_file 0\n'},
    )
    refused = root / 'memory.max'
    fail_to_read(monkeypatch, refused, OSError(error, os.strerror(error)), times=None)

    assert cgroup.read_memory().unreadable_file == f'{refused} ({reason})'


def test_read_memory_names_the_errno_a_stat_file_failed_with(
    fake_cgroup: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names the errno of a stat file that is there and cannot be opened, rather than calling it absent."""
    root = fake_cgroup(
        mountinfo=V2_MOUNTINFO,
        self_cgroup=V2_SELF_CGROUP.format(path='/'),
        files={'memory.max': '1000\n', 'memory.current': '100\n', 'memory.stat': 'inactive_file 0\n'},
    )
    refused = root / 'memory.stat'
    fail_to_read(monkeypatch, refused, OSError(errno.EACCES, os.strerror(errno.EACCES)), times=None)

    assert cgroup.read_memory().usage_unreadable_file == f'{refused} (EACCES)'


@pytest.mark.parametrize(
    ('error', 'reason'),
    [
        pytest.param(OSError(errno.EACCES, 'Permission denied'), 'EACCES', id='an errno with a name'),
        pytest.param(OSError(UNNAMED_ERRNO, 'Unknown'), str(UNNAMED_ERRNO), id='an errno with no name'),
        pytest.param(OSError('no errno'), 'OSError', id='an error with no errno'),
        pytest.param(
            UnicodeDecodeError('utf-8', b'\x81', 0, 1, 'invalid start byte'),
            'UnicodeDecodeError',
            id='content that is not text',
        ),
    ],
)
def test_failure_reason(error: OSError | ValueError, reason: str) -> None:
    """Names a failed read by its errno where it has one, and by the kind of error otherwise."""
    assert cgroup._failure_reason(error) == reason
