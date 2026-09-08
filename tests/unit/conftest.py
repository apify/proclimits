from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cgroups_sensor import _cgroup

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

V2_MOUNTINFO = '25 30 0:22 / {root} rw,nosuid,nodev,noexec,relatime shared:4 - cgroup2 cgroup2 rw,nsdelegate'
"""A single unified hierarchy exposed from its top, which is what a container runtime sets up."""

V1_MOUNTINFO = (
    '29 25 0:25 / {root}/systemd rw,nosuid shared:9 - cgroup cgroup rw,name=systemd\n'
    '30 25 0:26 / {root}/memory rw,nosuid shared:14 - cgroup cgroup rw,memory\n'
    '31 25 0:27 / {root}/cpu,cpuacct rw,nosuid shared:15 - cgroup cgroup rw,cpu,cpuacct\n'
    '32 25 0:28 / {root}/cpuset rw,nosuid shared:16 - cgroup cgroup rw,cpuset'
)
"""One hierarchy per controller, with the CPU accounting sharing a mount with the CPU bandwidth controller."""

V1_CPU_SPLIT_MOUNTINFO = (
    '30 25 0:26 / {root}/cpu rw,nosuid shared:14 - cgroup cgroup rw,cpu\n'
    '31 25 0:27 / {root}/cpuacct rw,nosuid shared:15 - cgroup cgroup rw,cpuacct'
)
"""cgroup v1 with the quota and the accounting in hierarchies of their own, each with its own mount point."""

V1_CPUSET_MOUNTINFO = '30 25 0:26 / {root}/cpuset rw,nosuid shared:14 - cgroup cgroup rw,cpuset'
"""A cpuset hierarchy alone, which is a machine where nothing counts CPU time at all."""

V1_CPUSET_SPLIT_MOUNTINFO = (
    V1_CPUSET_MOUNTINFO + '\n31 25 0:27 / {root}/cpuacct rw,nosuid shared:15 - cgroup cgroup rw,cpuacct'
)
"""The set and the accounting in hierarchies of their own, which `cgexec -g cpuset:...` moves a process into."""

HYBRID_MOUNTINFO = (
    '25 30 0:22 / {root}/unified rw shared:4 - cgroup2 cgroup2 rw,nsdelegate\n'
    '30 25 0:26 / {root}/memory rw,nosuid shared:14 - cgroup cgroup rw,memory\n'
    '31 25 0:27 / {root}/cpu,cpuacct rw,nosuid shared:15 - cgroup cgroup rw,cpu,cpuacct'
)
"""What systemd mounts with `systemd.unified_cgroup_hierarchy=0`: the controllers on cgroup v1, and a cgroup2
that carries none of them next to those."""

V2_SELF_CGROUP = '0::{path}\n'
"""The unified hierarchy is the entry with no controllers listed."""

V1_SELF_CGROUP = '4:cpuset:{path}\n3:cpu,cpuacct:{path}\n2:memory:{path}\n1:name=systemd:{path}\n'
"""One entry per cgroup v1 hierarchy, including the named one that carries no controller."""

HYBRID_SELF_CGROUP = '0::/system.slice/docker.service\n3:cpu,cpuacct:/docker/abc\n2:memory:/docker/abc\n'
"""The two interfaces name different cgroups for the same process, which is what makes the layout dangerous."""


@pytest.fixture(autouse=True)
def _isolated_module_state() -> Iterator[None]:
    """Reset the process-wide discovery cache, so that nothing leaks between tests."""
    _cgroup.clear_cache()
    yield
    _cgroup.clear_cache()


@pytest.fixture
def fake_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Return a builder that lays out a fake cgroup filesystem, points `_cgroup` at it and returns its root."""

    def build(*, mountinfo: str, self_cgroup: str, files: dict[str, str]) -> Path:
        root = tmp_path / 'cgroup'
        root.mkdir(parents=True, exist_ok=True)

        for relative_path, content in files.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        mountinfo_path = tmp_path / 'mountinfo'
        mountinfo_path.write_text(mountinfo.format(root=root))
        self_cgroup_path = tmp_path / 'self_cgroup'
        self_cgroup_path.write_text(self_cgroup)

        monkeypatch.setattr(_cgroup, '_PROC_SELF_MOUNTINFO', mountinfo_path)
        monkeypatch.setattr(_cgroup, '_PROC_SELF_CGROUP', self_cgroup_path)

        return root

    return build


@pytest.fixture
def _no_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `_cgroup` at `/proc` files that do not exist, as on a system without cgroups."""
    monkeypatch.setattr(_cgroup, '_PROC_SELF_MOUNTINFO', tmp_path / 'missing')
    monkeypatch.setattr(_cgroup, '_PROC_SELF_CGROUP', tmp_path / 'missing')
