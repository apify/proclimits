from __future__ import annotations

import pytest

from proclimits._cpu_list import count_cpu_list


@pytest.mark.parametrize(
    ('cpu_list', 'expected'),
    [
        pytest.param('0-3', 4, id='a range'),
        pytest.param('0-1,4,6-7', 5, id='ranges mixed with single cores'),
        pytest.param('7', 1, id='a single core'),
        pytest.param('0-1,2-3', 4, id='ranges that touch without overlapping'),
        pytest.param('0,0', None, id='the same core named twice'),
        pytest.param('0-2,1-3', None, id='overlapping ranges'),
        pytest.param('0-1,1-2', None, id='ranges overlapping by a single core'),
        pytest.param('0-3,1', None, id='a single core already inside a range'),
        pytest.param('0-3,1-2', None, id='a range nested in another'),
        pytest.param('2-3,0-1', None, id='ranges out of order'),
        pytest.param('5-4', None, id='a reversed range counts nothing, so it is not a count'),
        pytest.param('3-1', None, id='a range reversed by more than one'),
        pytest.param('0-,2', None, id='an unfinished range'),
        pytest.param('nonsense', None, id='not a number'),
    ],
)
def test_count_cpu_list(cpu_list: str, expected: int | None) -> None:
    """Counts a canonical CPU list, and refuses one no kernel would have written."""
    assert count_cpu_list(cpu_list) == expected
