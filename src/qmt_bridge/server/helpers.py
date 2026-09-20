"""数据转换辅助函数模块。

本模块提供将 xtquant（xtdata）返回的 numpy/pandas 数据结构
转换为 Python 原生类型（dict/list/int/float）的工具函数，
以便 FastAPI 的 JSON 序列化器能正确处理返回数据。

xtdata 的行情查询接口（如 get_market_data / get_market_data_ex / get_financial_data）
通常返回 pandas DataFrame 或嵌套 dict 结构，其中包含 numpy 数值类型
（np.int64, np.float64 等），这些类型无法直接被 JSON 序列化。
本模块的函数负责将这些数据统一转换为可序列化的 Python 原生类型。
"""

import logging
import math

import numpy as np
import pandas as pd

logger = logging.getLogger("qmt_bridge")
from fastapi import HTTPException


def _parse_price_query(price: str):
    """把价格位查询串解析为 ``xtdata.get_l2thousand_queue`` 可接受的 price 参数。

    Args:
        price: 查询串，支持单个价格 ``"10.5"``、逗号分隔多个价格
            ``"10.5,10.6"``、或 ``"10.5-10.8"`` 区间；空串表示全部价格。

    Returns:
        float | list[float] | tuple[float, float] | None

    Raises:
        HTTPException: 400，查询串不是合法的价格或价格区间。
    """
    price = price.strip()
    if not price:
        return None
    try:
        if "-" in price:
            start_text, _, end_text = price.partition("-")
            start_value, end_value = float(start_text), float(end_text)
            if start_value > end_value:
                raise HTTPException(
                    status_code=400,
                    detail=f"无效的 price 区间（起始价大于结束价）: {price}",
                )
            return (start_value, end_value)
        values = [float(p) for p in price.split(",") if p.strip()]
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"无效的 price 参数: {price}"
        ) from exc
    return values[0] if len(values) == 1 else values


def _numpy_to_python(obj):
    """递归地将嵌套数据结构中的 numpy 类型转换为 Python 原生类型。

    处理规则：
    - pd.DataFrame: 转为按行记录列表（index 提升为普通列）
    - pd.Series: 转为 dict
    - dict: 递归处理所有值
    - list/tuple: 递归处理所有元素
    - float: NaN / Inf / -Inf 转为 None（避免 JSON 序列化错误）
    - np.ndarray: 先转为 list 再递归处理
    - np.integer (int64 等): 转为 Python int
    - np.floating (float64 等): 转为 Python float，NaN/Inf 转 None
    - np.bool_: 转为 Python bool
    - 其他类型: 原样返回

    Args:
        obj: 任意嵌套数据结构，可能包含 numpy 类型。

    Returns:
        转换后的 Python 原生类型数据结构。
    """
    # pandas 容器必须显式处理：DataFrame/Series 都不是 dict/list/ndarray，
    # 若落到函数末尾的「兜底：按 dir() 展开公开属性」分支，会递归展开几十个
    # pandas 属性（T、axes、values、index、columns …），呈组合爆炸且永不返回。
    # 由于 XtdataSerializerMiddleware 串行化 /api/*，单个卡住的请求会连带锁死
    # 整个 API 服务，因此这里必须先拦截。
    if isinstance(obj, pd.DataFrame):
        return _numpy_to_python(obj.reset_index().to_dict(orient="records"))
    if isinstance(obj, pd.Series):
        return _numpy_to_python(obj.to_dict())
    if isinstance(obj, dict):
        return {k: _numpy_to_python(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_numpy_to_python(i) for i in obj]
    if isinstance(obj, float):
        # 处理 Python 原生 float 中的 NaN 和 Inf（JSON 不支持这些特殊值）
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, np.ndarray):
        # numpy 数组先转为 Python list，再递归处理每个元素
        return _numpy_to_python(obj.tolist())
    if isinstance(obj, (np.integer,)):
        # numpy 整数类型（int8/int16/int32/int64）转 Python int
        return int(obj)
    if isinstance(obj, (np.floating,)):
        # numpy 浮点类型（float16/float32/float64）转 Python float
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(obj, (np.bool_,)):
        # numpy 布尔类型转 Python bool
        return bool(obj)
    # 兜底：处理 xtquant C 扩展对象（XtAsset / XtPosition / XtOrder 等）
    # 这些对象不支持 dict() 和 vars()，但可通过 dir() 提取公共属性
    if not isinstance(obj, (str, bytes, int, bool, type(None))):
        try:
            attrs = {
                k: _numpy_to_python(getattr(obj, k))
                for k in dir(obj)
                if not k.startswith("_") and not callable(getattr(obj, k, None))
            }
            if attrs:
                return attrs
        except Exception:
            # 兜底转换失败不影响主流程（原样返回对象），但必须留痕，
            # 否则「响应里出现奇怪结构」这类问题将无从查起。
            logger.debug(
                "属性兜底转换失败，原样返回 %s", type(obj).__name__, exc_info=True
            )
    return obj


def ok_response(data, **extra):
    """统一成功响应格式。"""
    return {"code": 0, "message": "ok", "data": data, **extra}


def _market_data_to_records(
    raw: dict, stock_list: list[str], field_list: list[str]
) -> dict[str, list[dict]]:
    """将 xtdata.get_market_data() 的返回结果转换为 JSON 友好的记录格式。

    xtdata.get_market_data() 返回格式为 ``{field: DataFrame}``，
    其中每个 DataFrame 的行索引为股票代码、列索引为时间戳。
    本函数将其转换（pivot）为 ``{stock: [{date, field1, field2, ...}, ...]}`` 格式，
    即按股票分组的时间序列记录列表。

    Args:
        raw: xtdata.get_market_data() 的原始返回值，格式为 {字段名: DataFrame}。
             DataFrame 的 index 为股票代码列表，columns 为时间戳。
        stock_list: 需要提取的股票代码列表，如 ["000001.SZ", "600000.SH"]。
        field_list: 需要提取的字段列表，如 ["open", "high", "low", "close", "volume"]。

    Returns:
        格式为 {stock_code: [record_dict, ...]} 的字典。
        每个 record_dict 包含 "date" 键和各字段的值。

    示例::

        {
            "000001.SZ": [
                {"date": "20240101", "open": 10.5, "close": 10.8, "volume": 12345},
                {"date": "20240102", "open": 10.8, "close": 11.0, "volume": 23456},
            ]
        }
    """
    result: dict[str, list[dict]] = {}
    for stock in stock_list:
        # rows 用于按时间戳聚合同一时间点的多个字段值
        rows: dict[str, dict] = {}
        for field in field_list:
            df = raw.get(field)
            if df is None:
                continue
            if stock in df.index:
                # 遍历该股票在该字段下的所有时间戳数据
                for date, value in df.loc[stock].items():
                    entry = rows.setdefault(str(date), {"date": str(date)})
                    # 如果值有 .item() 方法（numpy 标量），用 .item() 转为 Python 原生类型
                    entry[field] = value.item() if hasattr(value, "item") else value
        result[stock] = list(rows.values())
    return result


def _dataframe_dict_to_records(data: dict) -> dict[str, list[dict]]:
    """将 xtdata.get_market_data_ex() / get_local_data() 的返回结果转换为记录格式。

    这些接口返回格式为 ``{stock_code: DataFrame}``，
    其中 DataFrame 的 index 为时间戳，columns 为字段名（open/close/volume 等）。
    本函数将每个 DataFrame 转换为字典列表。

    Args:
        data: {stock_code: DataFrame} 格式的原始数据。

    Returns:
        格式为 {stock_code: [record_dict, ...]} 的字典。
        若某只股票的 DataFrame 为空，则对应值为空列表。

    示例::

        {
            "000001.SZ": [
                {"time": "20240101", "open": 10.5, "close": 10.8},
                ...
            ]
        }
    """
    result: dict[str, list[dict]] = {}
    for stock, df in data.items():
        if isinstance(df, pd.DataFrame) and not df.empty:
            # reset_index() 将时间戳索引变为普通列，to_dict("records") 转为字典列表。
            # 但 xtdata.get_market_data3() 返回的 DataFrame 索引名本身就是 "time"，
            # 且已存在同名的 "time" 列，直接 reset_index() 会抛
            # ValueError: cannot insert time, already exists。此时列中已带时间信息，
            # 丢掉冗余索引即可（保持与其他转换函数一致的 time 列形态）。
            index_name = df.index.name if df.index.name is not None else "index"
            if index_name in df.columns:
                records = df.to_dict(orient="records")
            else:
                records = df.reset_index().to_dict(orient="records")
            # 对每条记录递归清洗 numpy 类型
            result[stock] = [_numpy_to_python(r) for r in records]
        else:
            result[stock] = []
    return result


def _financial_data_to_records(data: dict) -> dict:
    """将 xtdata.get_financial_data() 的返回结果转换为记录格式。

    get_financial_data() 返回格式为 ``{stock_code: {table_name: DataFrame}}``，
    即按股票和财务报表类型的两层嵌套结构。
    本函数将内层的每个 DataFrame 转换为字典列表。

    Args:
        data: {stock_code: {table_name: DataFrame}} 格式的原始财务数据。
              table_name 为财务报表类型名，如 "Balance"（资产负债表）、
              "Income"（利润表）、"CashFlow"（现金流量表）等。

    Returns:
        格式为 {stock_code: {table_name: [record_dict, ...]}} 的字典。

    示例::

        {
            "000001.SZ": {
                "Balance": [{"m_timetag": ..., "totalAssets": ...}, ...],
                "Income": [{"m_timetag": ..., "revenue": ...}, ...],
            }
        }
    """
    result: dict = {}
    for stock, tables in data.items():
        stock_data: dict = {}
        if isinstance(tables, dict):
            for table_name, df in tables.items():
                if isinstance(df, pd.DataFrame) and not df.empty:
                    records = df.reset_index().to_dict(orient="records")
                    # 对每条记录递归清洗 numpy 类型
                    stock_data[table_name] = [_numpy_to_python(r) for r in records]
                else:
                    stock_data[table_name] = []
        result[stock] = stock_data
    return result
