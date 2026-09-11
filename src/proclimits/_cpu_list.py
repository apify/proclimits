from __future__ import annotations


def count_cpu_list(cpu_list: str) -> int | None:
    """Count the CPUs in a list of ranges and single numbers, e.g. `0-3,8`.

    The kernel writes this format in both `cpuset.cpus` and `/sys/devices/system/cpu/online`, and it
    canonicalizes what it stores: the ranges ascend and never overlap. One that runs backwards or starts
    inside the range before it is refused rather than repaired, because no kernel wrote it.

    Returns:
        The number of cores, or `None` when the list does not parse.
    """
    count = 0
    # The highest core counted so far. Below zero because no core number is, so the first range counts whole.
    counted_to = -1

    try:
        for part in cpu_list.split(','):
            first, separator, last = part.partition('-')
            # A single core carries no separator. One that carries it has to carry both ends too.
            if separator and not last:
                return None

            start, end = int(first), int(last or first)
            # A reversed range counts zero cores, and one starting inside the previous counts a core twice.
            # A kernel writes neither: it stores `0-2,1-3` as `0-3`. Verified on cgroup v1 and v2.
            if end < start or start <= counted_to:
                return None

            count += end - start + 1
            counted_to = end
    except ValueError:
        return None

    return count
