from __future__ import annotations

import sys

# Only the backend of this platform is imported. `_windows` binds kernel32 at import, so importing it off
# Windows raises. The cgroup backend would import on Windows, and is skipped for the import cost. macOS has no
# mechanism this package reads. It takes the cgroup backend, which reports nothing without a cgroup filesystem,
# and reads only the machine memory its own way. Every other platform takes the cgroup backend as it is.
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
elif sys.platform == 'darwin':
    from ._cgroup import (
        clear_cache,
        machine_cpu_count,
        mechanism_notices,
        memory_limit_ceiling,
        read_cpu,
        read_cpu_usage,
        read_memory,
        sources,
    )
    from ._darwin import machine_memory_bytes
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
