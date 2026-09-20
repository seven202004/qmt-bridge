"""回归测试：_numpy_to_python 必须能处理 pandas 容器且立即返回。

历史缺陷：函数没有 DataFrame / Series 分支，会落到末尾「按 dir() 展开公开属性」
的兜底逻辑，递归展开 pandas 的大量公开属性后呈组合爆炸、永不返回。因为
XtdataSerializerMiddleware 串行化 /api/*，一个卡住的请求会连带锁死整个 API 服务
（`/api/market/divid_factors` 就是这条路径）。
"""

import threading

import pandas as pd

from qmt_bridge.server.helpers import _numpy_to_python

# 若回归，调用会挂死；用一个线程 + 超时把「挂死」变成「快速失败」。
TIMEOUT_SECONDS = 10


def _call_with_timeout(fn, *args):
    box: dict = {}

    def run():
        box["result"] = fn(*args)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(TIMEOUT_SECONDS)
    assert not thread.is_alive(), (
        f"_numpy_to_python({type(args[0]).__name__}) 未在 {TIMEOUT_SECONDS}s 内返回"
    )
    return box["result"]


def test_dataframe_becomes_records():
    """DataFrame 应转成按行记录列表，index 提升为普通列。"""
    df = pd.DataFrame(
        {"interest": [0.1, 0.2], "stockBonus": [0.0, 0.5]},
        index=pd.Index(["20240101", "20240102"], name="time"),
    )
    out = _call_with_timeout(_numpy_to_python, df)
    assert out == [
        {"time": "20240101", "interest": 0.1, "stockBonus": 0.0},
        {"time": "20240102", "interest": 0.2, "stockBonus": 0.5},
    ]


def test_empty_dataframe_returns_empty_list():
    """空 DataFrame（查不到数据的常见情形）应返回空列表而非挂死。"""
    assert _call_with_timeout(_numpy_to_python, pd.DataFrame()) == []


def test_series_becomes_dict():
    """Series 应转成 dict。"""
    series = pd.Series([1.0, 2.0], index=["a", "b"])
    assert _call_with_timeout(_numpy_to_python, series) == {"a": 1.0, "b": 2.0}


def test_nested_dataframe_in_dict_and_list():
    """dict/list 里嵌套的 DataFrame 也要被正确转换（多种端点返回该结构）。"""
    df = pd.DataFrame({"close": [10.0]}, index=pd.Index(["20240101"], name="time"))
    out = _call_with_timeout(_numpy_to_python, {"000001.SZ": df})
    assert out == {"000001.SZ": [{"time": "20240101", "close": 10.0}]}
    assert _call_with_timeout(_numpy_to_python, [df]) == [[{"time": "20240101", "close": 10.0}]]


def test_nan_still_becomes_none():
    """原有语义不变：NaN / Inf 仍转为 None（JSON 不支持）。"""
    df = pd.DataFrame({"close": [float("nan"), float("inf")]},
                      index=pd.Index(["20240101", "20240102"], name="time"))
    out = _call_with_timeout(_numpy_to_python, df)
    assert out == [
        {"time": "20240101", "close": None},
        {"time": "20240102", "close": None},
    ]
