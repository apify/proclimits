from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

import pytest

from proclimits import _backend, _cgroup, _darwin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

PAGE_SIZE = 16384
PHYS_PAGES = 1048576


def fake_sysconf(**answers: int | Exception) -> Callable[[str], int]:
    """A stand-in for `os.sysconf` that answers the named values, raising where the answer is an exception."""

    def sysconf(name: str) -> int:
        answer = answers[name]
        if isinstance(answer, Exception):
            raise answer

        return answer

    return sysconf


def reimport_backend() -> None:
    """Import the backend again from a module that holds none of its names.

    A reload reuses the module namespace, so a name a branch forgot to bind would survive from the previous
    import and pass for a binding. Dropping the names first makes a missing binding fail at import.
    """
    for name in _backend.__all__:
        _backend.__dict__.pop(name, None)

    importlib.reload(_backend)


@pytest.fixture
def _darwin_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Import the backend as macOS would, and import it again for the real platform afterwards.

    The reload rebinds the attributes of the module object `_sensor` holds, so the second reload has to run
    after `sys.platform` is back, or later tests would read through the macOS variant.
    """
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'platform', 'darwin')
            reimport_backend()
            yield
    finally:
        reimport_backend()


def test_machine_memory_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiplies the physical pages by the page size."""
    # `raising=False`, because Windows has no `os.sysconf` to replace.
    monkeypatch.setattr(
        _darwin.os, 'sysconf', fake_sysconf(SC_PHYS_PAGES=PHYS_PAGES, SC_PAGE_SIZE=PAGE_SIZE), raising=False
    )

    assert _darwin.machine_memory_bytes() == PHYS_PAGES * PAGE_SIZE


@pytest.mark.parametrize(
    ('phys_pages', 'page_size'),
    [
        pytest.param(-1, PAGE_SIZE, id='physical pages unknown'),
        pytest.param(PHYS_PAGES, -1, id='page size unknown'),
        pytest.param(0, PAGE_SIZE, id='no physical pages'),
        pytest.param(PHYS_PAGES, 0, id='no page size'),
    ],
)
def test_machine_memory_bytes_not_positive(monkeypatch: pytest.MonkeyPatch, phys_pages: int, page_size: int) -> None:
    """Reports nothing where `sysconf` does not know one of the two values."""
    monkeypatch.setattr(
        _darwin.os, 'sysconf', fake_sysconf(SC_PHYS_PAGES=phys_pages, SC_PAGE_SIZE=page_size), raising=False
    )

    assert _darwin.machine_memory_bytes() is None


@pytest.mark.parametrize(
    'error',
    [
        pytest.param(ValueError('unrecognized configuration name'), id='name unknown to this Python'),
        pytest.param(OSError('sysconf failed'), id='sysconf failed'),
    ],
)
@pytest.mark.parametrize(
    'failing',
    [
        pytest.param('SC_PHYS_PAGES', id='physical pages'),
        pytest.param('SC_PAGE_SIZE', id='page size'),
    ],
)
def test_machine_memory_bytes_sysconf_raises(monkeypatch: pytest.MonkeyPatch, error: Exception, failing: str) -> None:
    """Reports nothing where `sysconf` raises for either value."""
    answers: dict[str, int | Exception] = {'SC_PHYS_PAGES': PHYS_PAGES, 'SC_PAGE_SIZE': PAGE_SIZE, failing: error}
    monkeypatch.setattr(_darwin.os, 'sysconf', fake_sysconf(**answers), raising=False)

    assert _darwin.machine_memory_bytes() is None


@pytest.mark.usefixtures('_darwin_backend')
def test_backend_on_macos() -> None:
    """Takes the machine memory from the macOS reading and everything else from the cgroup backend."""
    assert _backend.machine_memory_bytes is _darwin.machine_memory_bytes

    # The ceiling stays the cgroup one, which reads `/proc/meminfo` and so reports nothing on macOS.
    for name in set(_backend.__all__) - {'machine_memory_bytes'}:
        assert getattr(_backend, name) is getattr(_cgroup, name), name
