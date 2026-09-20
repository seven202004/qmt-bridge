"""L2/Tick 逐笔数据路由模块 /api/tick/*。

提供 Level-2 行情的逐笔报价、逐笔委托、逐笔成交数据端点，
以及千档行情（L2 thousand）委托队列查询接口。
千档行情的实时推送由 WebSocket 端点 ``/ws/l2_thousand`` 提供。
底层调用 xtquant.xtdata 的 L2 数据接口，包括：
- xtdata.get_l2_quote()            — 获取 L2 逐笔报价
- xtdata.get_l2_order()            — 获取 L2 逐笔委托
- xtdata.get_l2_transaction()      — 获取 L2 逐笔成交
- xtdata.get_l2thousand_queue()    — 获取 L2 千档委托队列快照
"""

from fastapi import APIRouter, Query
from xtquant import xtdata

from ..helpers import _numpy_to_python, _parse_price_query

router = APIRouter(prefix="/api/tick", tags=["tick"])


@router.get("/l2_quote")
def get_l2_quote(
    stock: str = Query(..., description="股票代码，如 000001.SZ"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
    count: int = Query(-1, description="返回条数"),
):
    """获取 L2 逐笔报价数据。

    Args:
        stock: 股票代码，如 000001.SZ。
        start_time: 开始时间。
        end_time: 结束时间。
        count: 返回条数，-1 表示不限。

    Returns:
        该股票的 L2 逐笔报价数据。

    底层调用: xtdata.get_l2_quote(field_list=[], stock_code=..., ...)
    """
    raw = xtdata.get_l2_quote(
        field_list=[],
        stock_code=stock,
        start_time=start_time,
        end_time=end_time,
        count=count,
    )
    return {"stock": stock, "data": _numpy_to_python(raw)}


@router.get("/l2_order")
def get_l2_order(
    stock: str = Query(..., description="股票代码"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
    count: int = Query(-1, description="返回条数"),
):
    """获取 L2 逐笔委托数据。

    包含每一笔委托挂单的详细信息（价格、数量、方向等）。

    Args:
        stock: 股票代码。
        start_time: 开始时间。
        end_time: 结束时间。
        count: 返回条数，-1 表示不限。

    Returns:
        该股票的 L2 逐笔委托数据。

    底层调用: xtdata.get_l2_order(field_list=[], stock_code=..., ...)
    """
    raw = xtdata.get_l2_order(
        field_list=[],
        stock_code=stock,
        start_time=start_time,
        end_time=end_time,
        count=count,
    )
    return {"stock": stock, "data": _numpy_to_python(raw)}


@router.get("/l2_transaction")
def get_l2_transaction(
    stock: str = Query(..., description="股票代码"),
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
    count: int = Query(-1, description="返回条数"),
):
    """获取 L2 逐笔成交数据。

    包含每一笔撮合成交的详细信息（价格、数量、买卖标志等）。

    Args:
        stock: 股票代码。
        start_time: 开始时间。
        end_time: 结束时间。
        count: 返回条数，-1 表示不限。

    Returns:
        该股票的 L2 逐笔成交数据。

    底层调用: xtdata.get_l2_transaction(field_list=[], stock_code=..., ...)
    """
    raw = xtdata.get_l2_transaction(
        field_list=[],
        stock_code=stock,
        start_time=start_time,
        end_time=end_time,
        count=count,
    )
    return {"stock": stock, "data": _numpy_to_python(raw)}


# ---------------------------------------------------------------------------
# L2 千档委托队列端点
# ---------------------------------------------------------------------------


@router.get("/l2_thousand_queue")
def get_l2_thousand_queue(
    stock: str = Query(..., description="股票代码"),
    gear_num: int = Query(-1, description="档位，-1 表示全部档位"),
    price: str = Query(
        "",
        description="价格位：单个价格、逗号分隔多个价格、或 a-b 区间；留空表示全部价格",
    ),
):
    """获取 L2 千档委托队列快照。

    Args:
        stock: 股票代码。
        gear_num: 档位，-1 表示全部档位。
        price: 价格位，支持单个价格、逗号分隔多个价格、或 ``起始-结束`` 区间；留空表示全部价格。

    Returns:
        该股票指定档位/价格位的千档委托队列数据。

    底层调用: xtdata.get_l2thousand_queue(stock_code, gear_num=None, price=None)
    """
    raw = xtdata.get_l2thousand_queue(
        stock,
        gear_num=None if gear_num < 0 else gear_num,
        price=_parse_price_query(price),
    )
    return {"stock": stock, "data": _numpy_to_python(raw)}


@router.get("/broker_queue")
def get_broker_queue(
    stocks: str = Query(..., description="股票代码列表，逗号分隔"),
):
    """获取经纪商队列数据（港股）。

    Args:
        stocks: 逗号分隔的股票代码列表。

    Returns:
        经纪商队列数据。

    底层调用: xtdata.get_broker_queue_data(stock_list=[])
    """
    stock_list = [s.strip() for s in stocks.split(",") if s.strip()]
    raw = xtdata.get_broker_queue_data(stock_list)
    return {"stocks": stock_list, "data": _numpy_to_python(raw)}


@router.get("/order_rank")
def get_order_rank(
    stock: str = Query(..., description="股票代码"),
    order_time: str = Query(..., description="委托时间，YYYYMMDD 或 YYYYMMDDhhmmss"),
    order_type: str = Query(..., description="委托类型，如 buy/sell"),
    order_price: float = Query(..., description="委托价格"),
    order_volume: int = Query(..., description="委托量"),
    order_left_volume: int = Query(..., description="委托未成交量"),
):
    """获取委托在千档队列中的排名。

    需要千档行情权限，数据源为本地缓存的千档委托。

    Args:
        stock: 股票代码。
        order_time: 委托时间，``YYYYMMDD`` 或 ``YYYYMMDDhhmmss``。
        order_type: 委托类型，如 ``buy``/``sell``。
        order_price: 委托价格。
        order_volume: 委托量。
        order_left_volume: 委托未成交量。

    Returns:
        ``pricerank`` 价格排名数据。

    底层调用: xtdata.get_order_rank(code, order_time, order_type,
        order_price, order_volume, order_left_volume)
    """
    raw = xtdata.get_order_rank(
        stock, order_time, order_type, order_price, order_volume, order_left_volume
    )
    return {"stock": stock, "data": _numpy_to_python(raw)}
