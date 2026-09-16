from __future__ import annotations

import os


def machine_memory_bytes() -> int | None:
    """Read the total memory of the machine, in bytes.

    Returns:
        The total memory, or `None` when it cannot be read.
    """
    # The doubt this answers: under Rosetta the two values could use different page sizes, 16K and 4K. The
    # product would then be 4x wrong. That invites a switch to `sysctlbyname('hw.memsize')` through ctypes. In
    # Apple's sources both use the page size of the calling task. Libc `gen/FreeBSD/sysconf.c` computes
    # `SC_PHYS_PAGES` as `hw.memsize / hw.pagesize`. Libc `gen/FreeBSD/getpagesize.c` reads `HW_PAGESIZE`. XNU
    # `bsd/kern/kern_mib.c` answers it with `vm_map_page_size(get_task_map(current_task()))`. So the product is
    # `hw.memsize`. The Rosetta case is read from the source, not measured.
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        page_size = os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError):
        return None

    # `sysconf` answers -1 for a value it does not know.
    if pages <= 0 or page_size <= 0:
        return None

    return pages * page_size
