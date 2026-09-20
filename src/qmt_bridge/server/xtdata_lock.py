"""xtdata 请求级串行化模块。

xtquant 的 C 扩展不是线程安全的。FastAPI 的同步路由处理函数在线程池中
并发执行，多个请求同时调用 xtdata.* 会导致内部 BSON 序列化出现数据竞争，
触发 ``Assertion failed: u < 1000000`` 崩溃。

本模块通过 HTTP 中间件 + asyncio.Lock 实现请求级串行化：
- 同一时刻只允许一个 HTTP 请求进入路由处理函数
- 不修改 xtdata 模块本身，避免内部互调死锁风险
- 后台调度器的基础下载任务也通过同一把锁串行化

asyncio.Lock 在事件循环层面工作，持锁期间线程池中的 xtdata 调用正常执行，
释锁后下一个请求才进入处理函数，从而保证 xtdata 不被并发调用。

串行化是本服务最主要的性能瓶颈，因此日志要能直接点名：
排队超过 ``SLOW_LOCK_WAIT_SECONDS`` 记下「前一个端点持锁了多久」，
单次持锁超过 ``LONG_HOLD_SECONDS`` 直接点名该端点 —— 否则只能看到
「大家都在排队」，却不知道该去优化哪个接口。

使用纯 ASGI 中间件（而非 BaseHTTPMiddleware），在服务关闭时排队请求
能立即取消退出，不会产生大量 CancelledError。
"""

import asyncio
import logging
import time

logger = logging.getLogger("qmt_bridge")

# 全局异步锁，确保同一时刻只有一个请求/任务调用 xtdata
xtdata_lock = asyncio.Lock()

# /api/* 中无需串行化的前缀（不调用 xtdata 的端点，如通知接口）
NO_LOCK_PREFIXES: tuple[str, ...] = ("/api/notify",)

# 等待锁超过该秒数就按 WARNING 记录：串行化是本服务的主要排队来源，
# 排到队尾的请求往往在这里被"卡住"，只看总耗时无法区分是 xtdata 慢还是排队慢。
SLOW_LOCK_WAIT_SECONDS = 1.0

# 单次持锁超过该秒数就按 WARNING 记录并点名端点：持锁期间**所有** /api 请求都在排队，
# 这是「整个服务卡住」的直接原因，光知道有人排队还不够，必须能指到具体端点。
LONG_HOLD_SECONDS = 5.0


class XtdataSerializerMiddleware:
    """纯 ASGI 中间件：串行化调用 xtdata 的 HTTP 请求。

    通过 asyncio.Lock 保证同一时刻只有一个请求的同步处理函数在线程池中执行。
    - 仅拦截 HTTP 请求，WebSocket 不受影响。
    - 仅锁 /api/* 路径；/docs、/openapi.json 等静态端点直通。
    - NO_LOCK_PREFIXES 中的路径（如 /api/notify）直通，不参与串行化。
    """

    def __init__(self, app):
        self.app = app
        # 当前（或最近一个）持锁端点。排队告警要靠它点名「谁把队列堵住了」——
        # 等待者拿到锁之后 _holder 就变成自己了，所以必须在**排队前**先读一次。
        self._holder = ""
        self._holder_started = 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/") and not path.startswith(NO_LOCK_PREFIXES):
            waited_from = time.perf_counter()
            blocker, blocker_started = self._holder, self._holder_started
            async with xtdata_lock:
                self._holder, self._holder_started = path, time.perf_counter()
                waited = self._holder_started - waited_from
                if waited >= SLOW_LOCK_WAIT_SECONDS:
                    logger.warning(
                        "xtdata 串行化排队 %.2fs 后开始处理 %s（前一个 %s 持锁 %.2fs；"
                        "串行化期间 /api 请求只能依次排队）",
                        waited,
                        path,
                        blocker or "未知",
                        self._holder_started - blocker_started,
                    )
                else:
                    logger.debug("xtdata 串行化排队 %.3fs: %s", waited, path)
                try:
                    await self.app(scope, receive, send)
                finally:
                    held = time.perf_counter() - self._holder_started
                    if held >= LONG_HOLD_SECONDS:
                        logger.warning(
                            "xtdata 串行化: %s 持锁 %.2fs，期间其它 /api 请求全部在排队",
                            path,
                            held,
                        )
            return
        await self.app(scope, receive, send)
