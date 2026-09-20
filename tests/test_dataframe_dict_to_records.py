"""回归测试：_dataframe_dict_to_records 处理「索引名与列名冲突」的 DataFrame。

历史缺陷：xtdata.get_market_data3() 返回的 DataFrame 索引名是 "time"，同时已存在
同名的 "time" 列，`df.reset_index()` 直接抛
`ValueError: cannot insert time, already exists`，导致 /api/market/market_data3 必然 500。
"""

import pandas as pd
import pytest

from qmt_bridge.server.helpers import _dataframe_dict_to_records


def test_index_name_collides_with_column():
    """索引名与既有列同名时应正常返回记录，不抛 ValueError。"""
    df = pd.DataFrame(
        {
            "time": [1704297600000, 1704384000000],
            "close": [9.98, 10.05],
        },
        index=pd.Index(pd.to_datetime(["2024-01-04", "2024-01-05"]), name="time"),
    )
    out = _dataframe_dict_to_records({"000001.SZ": df})
    assert out == {
        "000001.SZ": [
            {"time": 1704297600000, "close": 9.98},
            {"time": 1704384000000, "close": 10.05},
        ]
    }


def test_normal_index_is_promoted_to_column():
    """无冲突时保持原语义：索引提升为列（get_market_data_ex 路径）。"""
    df = pd.DataFrame(
        {"close": [10.0]},
        index=pd.Index(["20240101"], name="time"),
    )
    out = _dataframe_dict_to_records({"000001.SZ": df})
    assert out == {"000001.SZ": [{"time": "20240101", "close": 10.0}]}


def test_unnamed_index_uses_index_column_name():
    """索引无名字时沿用 pandas 默认列名 index。"""
    df = pd.DataFrame({"close": [10.0]})
    out = _dataframe_dict_to_records({"000001.SZ": df})
    assert out == {"000001.SZ": [{"index": 0, "close": 10.0}]}


def test_empty_and_non_dataframe_values():
    """空 DataFrame 与非 DataFrame 值都返回空列表（保持既有行为）。"""
    assert _dataframe_dict_to_records({"000001.SZ": pd.DataFrame()}) == {"000001.SZ": []}
    assert _dataframe_dict_to_records({"000001.SZ": None}) == {"000001.SZ": []}
    assert _dataframe_dict_to_records({}) == {}


def test_nan_becomes_none_through_records():
    """记录内的 NaN 仍转 None（沿用 _numpy_to_python 语义）。"""
    df = pd.DataFrame({"close": [float("nan")]}, index=pd.Index(["20240101"], name="time"))
    out = _dataframe_dict_to_records({"000001.SZ": df})
    assert out == {"000001.SZ": [{"time": "20240101", "close": None}]}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
