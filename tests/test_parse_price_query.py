"""L2 千档价格位查询串解析测试。"""

import pytest
from fastapi import HTTPException

from qmt_bridge.server.helpers import _parse_price_query


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("10.5", 10.5),
        ("10.5, 10.6", [10.5, 10.6]),
        ("10.5-10.8", (10.5, 10.8)),
    ],
)
def test_parse_price_query(raw, expected):
    assert _parse_price_query(raw) == expected


@pytest.mark.parametrize(
    "raw", ["abc", "10.5-", "-10.5", "10.5,10.6-10.8", "10.8-10.5"]
)
def test_parse_price_query_invalid(raw):
    """非法查询串应返回 400 而不是冒泡成 500。"""
    with pytest.raises(HTTPException) as exc_info:
        _parse_price_query(raw)
    assert exc_info.value.status_code == 400
