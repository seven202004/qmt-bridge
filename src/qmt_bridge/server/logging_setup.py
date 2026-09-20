"""日志统一配置与请求标识。

本模块解决两个调试痛点：

1. **格式与落盘不统一**：此前 ``cli.py`` 在两个入口各写了一遍 handler 配置，
   格式里没有时间戳，也无法落盘，出问题只能靠控制台回滚。现在由
   :func:`setup_logging` 统一配置 ``qmt_bridge`` logger（控制台 + 可选轮转文件）。

2. **日志与请求无法对应**：日志一旦交错就无法判断属于哪次请求。这里用
   :mod:`contextvars` 携带请求 ID，由 :class:`RequestIdFilter` 注入每条日志记录。

3. **uvicorn 的日志没进文件**：uvicorn 只配置 ``uvicorn.*`` 系列 logger，
   启动横幅、监听地址、优雅关闭、框架层报错全部走它自己的 handler（仅控制台）。
   本模块一并接管这些 logger，落盘日志里才有完整的进程生命周期。

请求 ID 的传播范围（实测确认，不要想当然）：

- **请求内同步代码**：必然带上，包括被请求直接 await 的协程。
- **请求内新建的 asyncio 任务**：会继承当前上下文，因此也带上。
- **`threading.Thread` 新建的线程**：**不会**继承 —— contextvars 不跨线程复制。
  xtdata 行情回调、xttrader 交易回调、调度器都跑在各自的长生命周期线程里，
  它们的日志请求 ID 为 ``-``，靠文件日志里的**线程名**归属到子系统，
  而不是靠请求 ID 归属到某次请求。

因此排查「回调里出的问题」时看线程名，排查「接口报错」时看请求 ID。

用法::

    from .logging_setup import setup_logging

    setup_logging(level="info", log_file="logs/qmt-bridge.log")
"""

from __future__ import annotations

import logging
import logging.handlers
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from pathlib import Path

# 控制台格式：时间戳 + 级别 + 请求 ID + 文件:行号。
# 这里用 filename 而不是 logger 名：本项目多数模块共用 "qmt_bridge" 这个 logger 名，
# 只有源码文件与行号能稳定定位到输出日志的那一行。
CONSOLE_FORMAT = (
    "%(asctime)s %(levelname)-8s [%(request_id)s] %(filename)s:%(lineno)d - %(message)s"
)
# 文件格式额外带线程名与 logger 名：xtdata 回调 / 交易回调 / 调度器都在独立线程里
FILE_FORMAT = CONSOLE_FORMAT + " (%(threadName)s %(name)s)"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 未处于任何请求上下文时的占位符
NO_REQUEST_ID = "-"

# uvicorn 自己的 logger：不接管的话，进程生命周期（启动横幅、监听地址、
# 优雅关闭、框架层错误）只会出现在控制台，落盘日志里一片空白。
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.asgi")
# uvicorn 自带的访问日志与 AccessLogMiddleware 重复（且没有 request_id / 耗时），直接静音
UVICORN_ACCESS_LOGGER = "uvicorn.access"

# 查询串里的敏感参数，落日志时必须打码
# （API Key 正常走请求头，这里兜底防止有人用查询串传参）
_SENSITIVE_QUERY_KEYS = frozenset(
    {"api_key", "apikey", "key", "token", "secret", "password", "pwd", "webhook_secret"}
)
_MAX_QUERY_CHARS = 300

_request_id: ContextVar[str] = ContextVar(
    "qmt_bridge_request_id", default=NO_REQUEST_ID
)

# 标记由本模块添加的 handler，重复调用 setup_logging 时先摘掉，避免日志重复输出
_HANDLER_MARK = "_qmt_bridge_handler"


def new_request_id() -> str:
    """生成短请求 ID（8 位十六进制），便于在日志里肉眼比对。"""
    return uuid.uuid4().hex[:8]


def set_request_id(request_id: str):
    """设置当前上下文的请求 ID，返回可用于 :func:`reset_request_id` 的 token。"""
    return _request_id.set(request_id or NO_REQUEST_ID)


def reset_request_id(token) -> None:
    """恢复调用 :func:`set_request_id` 之前的请求 ID。"""
    _request_id.reset(token)


def get_request_id() -> str:
    """获取当前上下文的请求 ID（无上下文时返回 ``"-"``）。"""
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入日志记录，供 formatter 的 ``%(request_id)s`` 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def redact_query(query: str) -> str:
    """对查询串里的敏感参数打码，并截断过长内容。

    Args:
        query: 原始查询串（不含前导 ``?``）。

    Returns:
        可安全落盘的查询串；敏感参数的值替换为 ``***``。
    """
    if not query:
        return ""
    parts: list[str] = []
    for pair in query.split("&"):
        key, sep, _value = pair.partition("=")
        if sep and key.lower() in _SENSITIVE_QUERY_KEYS:
            parts.append(f"{key}=***")
        else:
            parts.append(pair)
    text = "&".join(parts)
    if len(text) > _MAX_QUERY_CHARS:
        text = text[:_MAX_QUERY_CHARS] + "...(truncated)"
    return text


def summarize_codes(codes: Iterable[str], limit: int = 8) -> str:
    """把代码列表压成一行日志文本：数量 + 前若干个，避免长订阅刷屏。

    Args:
        codes: 股票 / 市场代码序列。
        limit: 最多列出的代码个数。

    Returns:
        如 ``"000001.SZ,600000.SH"``；超过 limit 时形如
        ``"000001.SZ,... ...(共 300 只)"``；空序列返回 ``"(空)"``。
    """
    items = [str(code) for code in codes]
    if not items:
        return "(空)"
    head = ",".join(items[:limit])
    return head if len(items) <= limit else f"{head} ...(共 {len(items)} 只)"


def setup_logging(
    level: str = "info",
    log_file: str = "",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    console: bool = True,
) -> logging.Logger:
    """配置 ``qmt_bridge`` 与 uvicorn 的 logger（控制台 + 可选轮转文件），可重复调用。

    Args:
        level: 日志级别名（critical/error/warning/info/debug），非法值回落到 INFO。
        log_file: 日志文件路径；为空表示只输出到控制台。
        max_bytes: 单个日志文件大小上限，超过后轮转。
        backup_count: 保留的历史日志文件数量。
        console: 是否输出到控制台。由父进程重定向拉起时传 False —— 重定向的
            stdout/stderr 无法轮转，留着只会产生一份无限增长的日志副本。

    Returns:
        配置完成的 ``qmt_bridge`` logger。
    """
    logger = logging.getLogger("qmt_bridge")
    logger.setLevel(_resolve_level(level))

    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()

    request_filter = RequestIdFilter()
    handlers: list[logging.Handler] = []

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
        stream.addFilter(request_filter)
        setattr(stream, _HANDLER_MARK, True)
        logger.addHandler(stream)
        handlers.append(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
        file_handler.addFilter(request_filter)
        setattr(file_handler, _HANDLER_MARK, True)
        logger.addHandler(file_handler)
        handlers.append(file_handler)

    # 不向 root 传播：避免与用户自行配置的 root handler 重复输出
    logger.propagate = False
    _capture_uvicorn(logger.level, handlers)
    return logger


def _capture_uvicorn(level: int, handlers: list[logging.Handler]) -> None:
    """把 uvicorn 的日志接管到本项目的 handler 上。

    直接用 ``handlers = [...]`` 覆盖（而不是 append）：``python -m uvicorn`` 启动时
    uvicorn 会先跑一遍 dictConfig 给自己挂 handler，留着就会同一行打两遍。
    root 的 handler 不动，``propagate = False`` 防止再经 root 重复输出。

    ``uvicorn.access`` 只清 handler 不挂新的 —— 访问日志由 AccessLogMiddleware 输出。
    """
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = list(handlers)
        uvicorn_logger.setLevel(level)
        uvicorn_logger.propagate = False

    access_logger = logging.getLogger(UVICORN_ACCESS_LOGGER)
    access_logger.handlers = []
    access_logger.propagate = False


def _resolve_level(level: str) -> int:
    """把级别名解析为 logging 常量，非法值回落 INFO。"""
    return getattr(logging, str(level or "").upper(), logging.INFO)
