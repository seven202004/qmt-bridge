"""HTTP 访问日志与未处理异常记录中间件。

调试时最需要的两条信息，此前都拿不到：

1. **一次请求的完整轨迹**：哪个端点、什么参数、谁调的、耗时多久、返回什么状态码。
   现在每次请求结束都会输出一行访问日志，并带上请求 ID；超时的请求会升级为
   WARNING（排查「服务卡住」时先看 WARNING 行）。响应头同时返回 ``X-Request-ID``，
   客户端报错时可直接拿这个 ID 检索服务端日志。

2. **未处理异常的堆栈落到本项目日志里**：FastAPI 默认把未处理异常交给 ASGI 服务器，
   uvicorn 用 ``uvicorn.error`` logger 打印，**不会进入本项目配置的日志文件**，
   排查线上问题时往往只剩一句 ``Internal Server Error``。这里统一接管：
   记录完整堆栈，并返回带请求 ID 的 JSON 错误体。
"""

from __future__ import annotations

import logging
import time

from starlette.datastructures import MutableHeaders

from .logging_setup import (
    get_request_id,
    new_request_id,
    redact_query,
    reset_request_id,
    set_request_id,
)

REQUEST_ID_HEADER = "x-request-id"

# 超过该秒数的请求按 WARNING 记录：访问日志是唯一同时掌握「端点 + 耗时」的地方，
# xtdata 慢、串行化排队、线程池打满最终都表现为「某条访问日志特别慢」。
SLOW_REQUEST_SECONDS = 5.0


class AccessLogMiddleware:
    """纯 ASGI 中间件：为每次 HTTP 请求分配请求 ID 并输出访问日志。"""

    def __init__(self, app, enabled: bool = True):
        self.app = app
        self.enabled = enabled
        self.logger = logging.getLogger("qmt_bridge.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 允许上游（反向代理 / 调用方）透传请求 ID，便于跨服务串联
        incoming = ""
        for key, value in scope.get("headers") or []:
            if key.decode("latin-1").lower() == REQUEST_ID_HEADER:
                incoming = value.decode("latin-1")
                break
        request_id = incoming or new_request_id()
        token = set_request_id(request_id)
        # 同时写进 scope，供更外层的异常处理器读取 —— 异常会先穿过本中间件的
        # finally（在那里会重置 contextvar），若只依赖 contextvar，
        # 500 的堆栈行会丢掉请求 ID，而那恰恰是最需要它的地方。
        scope.setdefault("state", {})["request_id"] = request_id

        method = scope.get("method", "-")
        path = scope.get("path", "/")
        query = redact_query((scope.get("query_string") or b"").decode("latin-1"))
        target = f"{path}?{query}" if query else path
        client = scope.get("client")
        client_addr = f"{client[0]}:{client[1]}" if client else "-"
        started = time.perf_counter()
        status_code = 0

        async def send_with_request_id(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 未处理异常时响应由更外层的 ServerErrorMiddleware 直接发出，不会经过上面的
            # send 包装，因此 status_code 仍为 0 —— 按 500 记访问日志；
            # 堆栈本身由 unhandled_exception_handler 统一记录，此处不重复打印。
            if self.enabled:
                self._log_access(
                    method,
                    target,
                    status_code or 500,
                    _elapsed_ms(started),
                    client_addr,
                )
            reset_request_id(token)

    def _log_access(
        self,
        method: str,
        target: str,
        status_code: int,
        elapsed_ms: float,
        client: str = "-",
    ) -> None:
        """选择级别：5xx 为 error、4xx 为 warning、慢请求为 warning、其余为 info。

        每次请求仍然只输出一行 —— 慢请求是把同一行升级为 WARNING（并加标记），
        而不是再补一行，否则「一行一请求」的对应关系会被打破。
        """
        slow = elapsed_ms >= SLOW_REQUEST_SECONDS * 1000
        if status_code >= 500:
            level = logging.ERROR
        elif status_code >= 400 or slow:
            level = logging.WARNING
        else:
            level = logging.INFO
        self.logger.log(
            level,
            "%s %s -> %d %.1fms client=%s%s",
            method,
            target,
            status_code,
            elapsed_ms,
            client,
            " (慢请求)" if slow else "",
        )


def _elapsed_ms(started: float) -> float:
    """返回自 started 起经过的毫秒数。"""
    return (time.perf_counter() - started) * 1000.0


def unhandled_exception_handler(request, exc: Exception):
    """把未处理异常转换为带请求 ID 的 JSON 500 响应。

    注册到 ``app.add_exception_handler(Exception, ...)`` 后，Starlette 的
    ``ServerErrorMiddleware`` 不再自行返回裸文本 500，异常堆栈由这里统一记录，
    同时 ``AccessLogMiddleware`` 仍会补一行访问日志。
    """
    from fastapi.responses import JSONResponse

    # 优先取中间件写进 scope 的 ID：异常处理器运行在本中间件之外，
    # 此时 contextvar 已被 finally 重置，只有 scope 里的还可靠。
    request_id = getattr(request.state, "request_id", "") or get_request_id()
    # 日志里的请求 ID 由 RequestIdFilter 从 contextvar 读取，而本处理器运行在
    # 线程池线程中（上下文是副本），必须显式设置一次才能让堆栈那行也带上 ID。
    token = set_request_id(request_id)
    try:
        logging.getLogger("qmt_bridge.error").error(
            "%s %s 处理失败: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=exc,
        )
    finally:
        reset_request_id(token)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "error": f"{type(exc).__name__}: {exc}",
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id},
    )
