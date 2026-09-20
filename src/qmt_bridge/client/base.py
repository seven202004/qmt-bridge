"""BaseClient — HTTP 传输层，提供认证和基础请求能力。

本模块是所有客户端 Mixin 的基类，封装了与 QMT Bridge 服务端通信所需的
HTTP GET/POST/DELETE 方法，以及 API Key 认证头的构造。

仅依赖 Python 标准库（json, urllib），确保跨平台兼容性。

每次请求在 DEBUG 级记录「方法 / URL / 状态码 / 耗时」，失败时按 ERROR 记录状态码
与服务端返回的 JSON 错误体（含 request_id，可直接拿去向服务端日志对账）。
默认级别下不产生任何输出，调用方按需 ``logging.getLogger("qmt_bridge.client")``
打开即可。
"""

import io
import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import quote

logger = logging.getLogger("qmt_bridge.client")

# 默认 HTTP 超时（秒）。避免因服务端异常或网络故障导致客户端永久阻塞。
DEFAULT_TIMEOUT: float = 30.0

# 失败日志里响应体的截断长度，避免一个超长错误页刷满日志
_MAX_LOGGED_BODY = 500


class BaseClient:
    """轻量级 HTTP/WebSocket 客户端基类。

    所有 Mixin 类（MarketMixin、TradingMixin 等）通过多继承共享本类提供的
    ``_get``、``_post``、``_delete`` 和 ``_headers`` 辅助方法，实现与
    QMT Bridge 服务端的 HTTP 通信。
    """

    def __init__(
        self,
        host: str,
        port: int = 8000,
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """初始化客户端连接。

        Args:
            host: QMT Bridge 服务端 IP 地址或主机名，如 ``"192.168.1.100"``
            port: 服务端口，默认 8000
            api_key: API Key，交易端点需要认证时必填
            timeout: HTTP 请求超时（秒），默认 30s。设为 None 表示无超时（不推荐）。
        """
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}"
        self.api_key = api_key
        self.timeout = timeout
        # Bypass proxy for direct connections to QMT Bridge server
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})  # empty dict = no proxies
        )

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """构造请求头，包含 API Key（如已配置）。

        Returns:
            请求头字典，当设置了 api_key 时会包含 ``X-API-Key`` 字段
        """
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _get(self, path: str, params: dict | None = None) -> dict:
        """发送 GET 请求并返回解析后的 JSON。

        Args:
            path: API 路径，如 ``"/api/market/full_tick"``
            params: 查询参数字典，值为 None 的键会被跳过

        Returns:
            服务端返回的 JSON 响应（已解析为 dict）
        """
        if params:
            # 将参数编码为 URL 查询字符串，跳过 None 值
            query = "&".join(
                f"{k}={quote(str(v))}" for k, v in params.items() if v is not None
            )
            url = f"{self.base_url}{path}?{query}"
        else:
            url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers=self._headers())
        return self._open(req)

    def _post(self, path: str, body: dict | list) -> dict:
        """发送 POST 请求（JSON 请求体）并返回解析后的 JSON。

        Args:
            path: API 路径，如 ``"/api/trading/order"``
            body: 请求体，会被序列化为 JSON。批量接口（如 ``/api/trading/batch_order``）
                的请求体是**数组**，因此这里允许 list。

        Returns:
            服务端返回的 JSON 响应（已解析为 dict）
        """
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", **self._headers()}
        req = urllib.request.Request(url, data=data, headers=headers)
        return self._open(req)

    def _delete(self, path: str, params: dict | None = None) -> dict:
        """发送 DELETE 请求并返回解析后的 JSON。

        Args:
            path: API 路径，如 ``"/api/sector/remove"``
            params: 查询参数字典

        Returns:
            服务端返回的 JSON 响应（已解析为 dict）
        """
        if params:
            query = "&".join(
                f"{k}={quote(str(v))}" for k, v in params.items() if v is not None
            )
            url = f"{self.base_url}{path}?{query}"
        else:
            url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="DELETE", headers=self._headers())
        return self._open(req)

    def _open(self, req: urllib.request.Request) -> dict:
        """发送请求并解析 JSON 响应，同时留下调用日志。

        Args:
            req: 已构造好的请求对象（带 URL、请求头、可选请求体）。

        Returns:
            服务端返回的 JSON 响应（已解析为 dict）。

        Raises:
            urllib.error.HTTPError: 服务端返回非 2xx；错误体（含 request_id）
                已记入日志，且**放回** ``exc``，调用方仍可 ``exc.read()``。
        """
        started = time.perf_counter()
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
                logger.debug(
                    "%s %s -> %d %.1fms",
                    req.get_method(),
                    req.full_url,
                    resp.status,
                    (time.perf_counter() - started) * 1000.0,
                )
                return body
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            # 放回去：不能因为我们记了日志就让调用方的 exc.read() 变成空。
            # addinfourl 在构造时就把 read 绑到了旧 fp 上，换 fp 之后必须重绑一次。
            exc.fp = io.BytesIO(detail)
            exc.read = exc.fp.read  # type: ignore[assignment]
            logger.error(
                "%s %s -> %d %s",
                req.get_method(),
                req.full_url,
                exc.code,
                detail[:_MAX_LOGGED_BODY].decode(errors="replace"),
            )
            raise

    def _to_dataframes(self, data: dict) -> dict:
        """将 ``{stock_code: [records]}`` 格式的数据转换为 ``{stock_code: DataFrame}``。

        当未安装 pandas 时，原样返回 dict 数据，实现优雅降级。

        Args:
            data: 服务端返回的行情数据，键为股票代码，值为记录列表

        Returns:
            安装了 pandas 时返回 ``{str: DataFrame}``，否则原样返回
        """
        try:
            import pandas as pd
        except ImportError:
            return data

        result: dict[str, pd.DataFrame] = {}
        for stock, records in data.items():
            if not records:
                result[stock] = pd.DataFrame()
                continue
            result[stock] = pd.DataFrame(records)
        return result
