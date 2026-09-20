"""表格数据路由模块 /api/tabular/*。

提供通用表格数据的查询端点，可用于查询各种命名数据表。
底层调用 xtquant.xtdata 的数据接口，包括：
- xtdata.get_financial_data()   — 按表名获取表格数据
- xtdata.get_metatable_list()   — 获取可用数据表列表（table_code → table_name）
- xtdata.get_tabular_formula()  — 按字段列表查询表格公式数据
"""

from fastapi import APIRouter, Query
from xtquant import xtdata

from ..helpers import _numpy_to_python

router = APIRouter(prefix="/api/tabular", tags=["tabular"])


@router.get("/data")
def get_tabular_data(
    table_name: str = Query(..., description="表名"),
    stocks: str = Query("", description="股票代码列表，逗号分隔"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
):
    """按表名查询表格数据。

    通用数据查询接口，通过指定表名查询对应的结构化数据。

    Args:
        table_name: 数据表名称。
        stocks: 逗号分隔的股票代码列表，为空查询全部。
        start_time: 开始时间。
        end_time: 结束时间。

    Returns:
        table: 查询的表名。
        data: 表格数据。

    底层调用: xtdata.get_financial_data(stock_list, table_list=[table_name], ...)
    """
    # 将逗号分隔的代码字符串解析为列表，为空则传空列表
    stock_list = [s.strip() for s in stocks.split(",") if s.strip()] if stocks else []
    raw = xtdata.get_financial_data(
        stock_list, table_list=[table_name], start_time=start_time, end_time=end_time
    )
    return {"table": table_name, "data": _numpy_to_python(raw)}


@router.get("/tables")
def list_tables():
    """列出所有可用的数据表名称。

    Returns:
        tables: 数据表映射，``{table_code: table_name}``。

    底层调用: xtdata.get_metatable_list()
    """
    try:
        tables = xtdata.get_metatable_list()
        return {"tables": _numpy_to_python(tables)}
    except Exception:  # noqa: BLE001 — 该客户端不支持此接口时降级返回空表
        # 接口不可用时返回空映射
        return {"tables": {}}


@router.get("/formula")
def get_tabular_formula(
    fields: str = Query(
        ..., description="字段列表，逗号分隔，格式 表名.字段名，如 Balance.total_assets"
    ),
    stocks: str = Query("", description="股票代码列表，逗号分隔"),
    period: str = Query("1d", description="K 线周期"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
    count: int = Query(-1, description="返回条数"),
    dividend_type: str = Query("none", description="除权类型"),
):
    """按字段列表查询表格公式数据。

    字段名由 ``表名.字段名`` 组成，表名即公式名（见表名列表接口）。

    底层调用: xtdata.get_tabular_formula(codes, fields, period,
        start_time, end_time, count=-1, dividend_type='none')
    """
    code_list = [s.strip() for s in stocks.split(",") if s.strip()] if stocks else []
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    raw = xtdata.get_tabular_formula(
        code_list,
        field_list,
        period,
        start_time,
        end_time,
        count=count,
        dividend_type=dividend_type,
    )
    return {"fields": field_list, "data": _numpy_to_python(raw)}
