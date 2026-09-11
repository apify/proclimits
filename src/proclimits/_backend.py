from __future__ import annotations

import sys

# Only the backend of this platform is imported. `_windows` binds kernel32 at import, so importing it off
# Windows raises. The cgroup backend would import on Windows, and is skipped for the import cost. Anything
# that is not Windows gets the cgroup backend, which reports nothing where no cgroup filesystem is mounted -
# the honest answer on macOS.
if sys.platform == 'win32':
    from ._windows import (
        clear_cache,
        machine_cpu_count,
        machine_memory_bytes,
        mechanism_notices,
        memory_limit_ceiling,
        read_cpu,
        read_cpu_usage,
        read_memory,
        sources,
    )
else:
    from ._cgroup import (
        clear_cache,
        machine_cpu_count,
        machine_memory_bytes,
        mechanism_notices,
        memory_limit_ceiling,
        read_cpu,
        read_cpu_usage,
        read_memory,
        sources,
    )

__all__ = [
    'clear_cache',
    'machine_cpu_count',
    'machine_memory_bytes',
    'mechanism_notices',
    'memory_limit_ceiling',
    'read_cpu',
    'read_cpu_usage',
    'read_memory',
    'sources',
]
