"""实时行情 WebSocket 端点 — /ws/realtime。

本模块提供实时行情数据的 WebSocket 推送服务。

使用流程：
1. 客户端建立 WebSocket 连接
2. 客户端发送 JSON 订阅请求：{"stocks": ["000001.SZ"], "period": "tick"}
3. 服务端通过 xtdata.subscribe_quote 订阅行情
4. xtdata 在后台线程中推送行情数据，通过 asyncio.run_coroutine_threadsafe
   桥接到 WebSocket 发送给客户端
5. 客户端断开连接时自动取消订阅

实时 K 线构建（REST 拉历史 + WS 推增量）：
    period 不仅支持 "tick"，也支持 "1m"/"5m"/"1d" 等 K 线周期。
    subscribe_quote(period="1m") 推送的是 xtdata 聚合好的分钟 K 线柱，
    而非原始 tick，客户端无需自行合成。

    客户端标准用法：
    1. 先调 REST GET /api/market/market_data_ex (period="1m") 拉取历史 K 线
    2. 再连本 WS 端点，订阅相同周期 {"stocks": [...], "period": "1m"}
    3. 收到推送后按时间戳与本地数组末尾比较：
       - 时间戳相同 → 更新最后一根（盘中未完结柱）
       - 时间戳更大 → 追加新柱

    若历史数据使用了复权（如 dividend_type="front"），订阅时也必须传相同的
    dividend_type，否则推送的未复权价格与已复权历史数据不在同一价格尺度上。
    底层通过 xtdata.subscribe_quote2() 支持该参数。
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from xtquant import xtdata

from ..helpers import _numpy_to_python
from ..logging_setup import summarize_codes

router = APIRouter()

logger = logging.getLogger("qmt_bridge.ws.realtime")


@router.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket):
    """实时行情 WebSocket 端点。

    接受客户端的行情订阅请求，将 xtdata 推送的实时行情数据转发给客户端。
    支持订阅多只股票，支持 tick/1m/5m/1d 等多种周期。

    协议：
        客户端发送订阅请求 JSON::

            {"stocks": ["000001.SZ", "600000.SH"], "period": "tick"}

        可选传入 ``"dividend_type"``（``none``/``front``/``back``/``front_ratio``/
        ``back_ratio``）以与 REST 历史数据的复权方式保持一致。

        服务端持续推送行情数据 JSON，直到客户端断开连接。
    """
    await ws.accept()
    seq_ids: list[int] = []  # 记录所有订阅的序列号，用于断开时取消订阅
    loop = asyncio.get_event_loop()

    try:
        # 等待客户端发送订阅请求
        msg = await ws.receive_text()
        payload = json.loads(msg)
        stocks: list[str] = payload.get("stocks", [])
        period: str = payload.get("period", "tick")
        # 复权方式，缺省 None 时行为与 subscribe_quote 一致
        dividend_type: str | None = payload.get("dividend_type") or None

        async def _send(data):
            """异步发送数据到 WebSocket 客户端（忽略发送失败）。"""
            try:
                await ws.send_json(data)
            except Exception:
                # 推送失败通常意味着客户端已断开，不值得刷 ERROR，但 debug 级要留痕
                logger.debug("行情推送失败 client=%s", ws.client, exc_info=True)

        def on_data(data):
            """xtdata 行情回调 — 在 xtdata 后台线程中被调用。

            将 numpy/pandas 数据转换为原生 Python 类型后，
            通过 run_coroutine_threadsafe 投递到 asyncio 事件循环发送。
            """
            clean = _numpy_to_python(data)
            asyncio.run_coroutine_threadsafe(_send(clean), loop)

        # 逐只股票订阅行情
        for stock in stocks:
            # subscribe_quote2 是 subscribe_quote 的底层实现，额外支持复权参数
            seq = xtdata.subscribe_quote2(
                stock_code=stock,
                period=period,
                dividend_type=dividend_type,
                callback=on_data,
            )
            seq_ids.append(seq)

        # 断开时那行只有「订阅数」，这里补上「订阅了什么」：
        # 客户端说收不到行情时，先看这行确认服务端到底有没有按它的请求去订阅。
        logger.info(
            "行情订阅已建立 client=%s 周期=%s 复权=%s 股票=%d只 %s",
            ws.client, period, dividend_type or "none", len(seq_ids),
            summarize_codes(stocks),
        )

        # 保持连接存活，等待客户端断开
        while True:
            await ws.receive_text()

    except WebSocketDisconnect:
        logger.info("行情订阅客户端断开 client=%s 订阅数=%d", ws.client, len(seq_ids))
    except Exception:
        logger.exception("行情 WebSocket 异常 client=%s", ws.client)
        raise
    finally:
        # 清理：取消所有行情订阅
        for seq in seq_ids:
            xtdata.unsubscribe_quote(seq)
