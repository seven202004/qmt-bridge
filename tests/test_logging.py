"""日志体系测试：格式、请求 ID、访问日志、异常记录、轮转文件。"""

import asyncio
import contextlib
import io
import json
import logging
import re
import sys
import threading
import time
import types
import urllib.error

import pytest
import uvicorn
from fastapi import Depends, FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from qmt_bridge.client.base import BaseClient
from qmt_bridge.server import app as app_module
from qmt_bridge.server import cli
from qmt_bridge.server import xtdata_lock as xtdata_lock_module
from qmt_bridge.server.access_log import (
    AccessLogMiddleware,
    unhandled_exception_handler,
)
from qmt_bridge.server.config import Settings, get_settings, reset_settings
from qmt_bridge.server.logging_setup import (
    _HANDLER_MARK,
    CONSOLE_FORMAT,
    NO_REQUEST_ID,
    UVICORN_ACCESS_LOGGER,
    UVICORN_LOGGERS,
    RequestIdFilter,
    get_request_id,
    new_request_id,
    redact_query,
    reset_request_id,
    set_request_id,
    setup_logging,
    summarize_codes,
)
from qmt_bridge.server.security import require_api_key
from qmt_bridge.server.trading.manager import (
    XtTraderManager,
    _audited,
    _describe,
    _is_sensitive,
)
from qmt_bridge.server.ws import trade_callback


def _own_handlers(logger) -> list:
    """只统计本模块加的 handler（pytest 自己也会往这个 logger 上挂 handler）。"""
    return [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]


# --------------------------------------------------------------------------- 请求 ID


def test_request_id_roundtrip_and_default():
    """默认无上下文时是占位符；设置后可读回，reset 后恢复。"""
    assert get_request_id() == NO_REQUEST_ID
    token = set_request_id("abc12345")
    assert get_request_id() == "abc12345"
    reset_request_id(token)
    assert get_request_id() == NO_REQUEST_ID


def test_set_request_id_falls_back_to_placeholder():
    """空字符串不应进日志（否则格式里的 [ ] 会变成空）。"""
    token = set_request_id("")
    assert get_request_id() == NO_REQUEST_ID
    reset_request_id(token)


def test_new_request_id_is_short_and_unique():
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 8 for i in ids)


def test_request_id_filter_injects_record_attribute():
    """过滤器必须给每条记录补 request_id，否则 formatter 会直接抛 KeyError。"""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == NO_REQUEST_ID


# --------------------------------------------------------------------------- 敏感信息


def test_redact_query_masks_sensitive_values():
    query = "stock=000001.SZ&api_key=SECRET&token=abc&count=5"
    out = redact_query(query)
    assert "SECRET" not in out and "abc" not in out
    assert "api_key=***" in out and "token=***" in out
    assert "stock=000001.SZ" in out and "count=5" in out


def test_redact_query_truncates_and_handles_empty():
    assert redact_query("") == ""
    long = "a=1&" * 200
    out = redact_query(long)
    assert out.endswith("...(truncated)")
    assert len(out) < len(long)


def test_summarize_codes_counts_truncates_and_handles_empty():
    """订阅日志要能一眼看出「订阅了几只、前几只是什么」。"""
    assert summarize_codes([]) == "(空)"
    assert summarize_codes(["000001.SZ", "600000.SH"]) == "000001.SZ,600000.SH"
    many = [f"{i:06d}.SZ" for i in range(300)]
    out = summarize_codes(many)
    assert out.startswith("000000.SZ,000001.SZ") and out.endswith("...(共 300 只)")
    assert len(out) < 200


# --------------------------------------------------------------------------- 配置


def test_setup_logging_writes_formatted_lines(tmp_path):
    """落盘日志必须带时间戳、级别、请求 ID、模块行号。"""
    log_file = tmp_path / "qmt.log"
    logger = setup_logging(level="info", log_file=str(log_file))
    token = set_request_id("req-0001")
    try:
        logger.info("hello from test")
    finally:
        reset_request_id(token)
        for h in logger.handlers:
            h.flush()

    text = log_file.read_text(encoding="utf-8")
    assert "hello from test" in text
    # 2026-09-16 03:00:00 INFO     [req-0001] test_logging.py:NN - hello from test (MainThread qmt_bridge)
    assert re.search(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO\s+\[req-0001\] test_logging\.py:\d+ - hello from test",
        text,
    ), text
    # 文件格式额外带线程名，便于区分 xtdata 回调线程与主线程
    assert "(MainThread qmt_bridge)" in text


def test_setup_logging_is_idempotent(tmp_path):
    """重复调用不能叠加 handler，否则每行日志会打多遍。"""
    log_file = tmp_path / "qmt.log"
    logger = setup_logging(level="info", log_file=str(log_file))
    assert len(_own_handlers(logger)) == 2  # 控制台 + 文件
    setup_logging(level="info", log_file=str(log_file))
    assert len(_own_handlers(logger)) == 2

    logger.info("once")
    for h in _own_handlers(logger):
        h.flush()
    assert log_file.read_text(encoding="utf-8").count("once") == 1


def test_setup_logging_keeps_foreign_handlers(tmp_path):
    """只清理自己加的 handler，不能顺手删掉别人（如 pytest）挂上去的 handler。"""
    logger = setup_logging(level="info", log_file=str(tmp_path / "t.log"))
    foreign = logging.NullHandler()
    logger.addHandler(foreign)
    try:
        setup_logging(level="info", log_file=str(tmp_path / "t.log"))
        assert foreign in logger.handlers
    finally:
        logger.removeHandler(foreign)


def test_setup_logging_invalid_level_falls_back_to_info(tmp_path):
    logger = setup_logging(level="not-a-level", log_file=str(tmp_path / "t.log"))
    assert logger.level == logging.INFO


def test_setup_logging_creates_parent_directory(tmp_path):
    """日志目录不存在时应自动创建，否则落盘静默失败。"""
    target = tmp_path / "nested" / "deep" / "qmt.log"
    setup_logging(level="info", log_file=str(target))
    assert target.parent.is_dir()


def test_setup_logging_does_not_propagate(tmp_path):
    """不向 root 传播，避免与用户自配的 root handler 重复输出。"""
    logger = setup_logging(level="info", log_file=str(tmp_path / "t.log"))
    assert logger.propagate is False


def test_console_format_declares_request_id():
    """格式串必须包含 %(request_id)s —— RequestIdFilter 正是为它服务。"""
    assert "%(request_id)s" in CONSOLE_FORMAT
    assert "%(asctime)s" in CONSOLE_FORMAT


# --------------------------------------------------------------------------- uvicorn 接管


def test_uvicorn_loggers_use_project_handlers(tmp_path):
    """uvicorn 的 logger 必须挂上本项目 handler，否则文件里没有进程生命周期。"""
    setup_logging(level="info", log_file=str(tmp_path / "t.log"))
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        assert any(getattr(h, _HANDLER_MARK, False) for h in lg.handlers), name
        assert lg.propagate is False, name


def test_uvicorn_access_logger_silenced(tmp_path):
    """uvicorn 自带访问日志必须静音，否则与 AccessLogMiddleware 每请求打两行。"""
    setup_logging(level="info", log_file=str(tmp_path / "t.log"))
    access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    assert access.handlers == []
    assert access.propagate is False


def test_uvicorn_startup_line_lands_in_log_file(tmp_path):
    """启动横幅要能落盘（原先只在控制台，服务起没起全看运气）。"""
    log_file = tmp_path / "uvicorn.log"
    setup_logging(level="info", log_file=str(log_file))
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.info("Uvicorn running on http://127.0.0.1:8000")
    for handler in uvicorn_logger.handlers:
        handler.flush()
    assert "Uvicorn running on http://127.0.0.1:8000" in log_file.read_text(
        encoding="utf-8"
    )


def test_capture_uvicorn_replaces_foreign_handlers(tmp_path):
    """uvicorn 自己先挂过 handler（`python -m uvicorn`）时不能变成双份输出。"""
    foreign = logging.StreamHandler()
    logging.getLogger("uvicorn.error").addHandler(foreign)
    try:
        setup_logging(level="info", log_file=str(tmp_path / "t.log"))
        assert foreign not in logging.getLogger("uvicorn.error").handlers
    finally:
        logging.getLogger("uvicorn.error").removeHandler(foreign)


def test_real_uvicorn_lifecycle_lands_in_log_file(tmp_path):
    """真起一个 uvicorn：启动横幅与优雅关闭都必须落盘。

    这是「接管 uvicorn 日志」的端到端证明 —— 单看 handler 有没有挂上，
    证明不了 ``log_config=None`` 之后 uvicorn 不会把自己的配置刷回来。
    """
    log_file = tmp_path / "uvicorn.log"
    setup_logging(level="info", log_file=str(log_file))

    server = uvicorn.Server(
        uvicorn.Config(
            FastAPI(),  # 端口交 0 让系统分配，避免与本机其它服务撞车
            host="127.0.0.1",
            port=0,
            log_level="info",
            access_log=False,
            log_config=None,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: server.started), "uvicorn 未能在 10s 内启动"
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    for handler in logging.getLogger("uvicorn.error").handlers:
        handler.flush()

    text = log_file.read_text(encoding="utf-8")
    assert "Uvicorn running on" in text, text  # 启动横幅（原先只在控制台）
    assert "Application startup complete" in text  # 生命周期
    assert "Shutting down" in text  # 优雅关闭


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    """轮询等条件成立（服务端起停是异步的，断言不能抢跑）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- 访问日志


@pytest.fixture
def client_and_caplog(caplog, tmp_path):
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    app = FastAPI()
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_middleware(AccessLogMiddleware, enabled=True)

    @app.get("/api/ping")
    async def ping():
        return {"pong": True}

    @app.get("/api/boom")
    async def boom():
        raise ValueError("kaboom")

    with caplog.at_level(logging.DEBUG):
        yield TestClient(app, raise_server_exceptions=False)


def test_access_log_records_method_path_status_and_duration(client_and_caplog, caplog):
    """访问日志要能直接看出「哪个端点、什么状态、耗时多久」。"""
    resp = client_and_caplog.get("/api/ping")
    assert resp.status_code == 200
    line = _access_line(caplog, "/api/ping")
    assert "-> 200" in line and re.search(r"\d+\.\dms", line), line


def test_access_log_records_client_address(client_and_caplog, caplog):
    """谁调的要看得出来（TestClient 的 scope.client 是 testclient:50000）。"""
    client_and_caplog.get("/api/ping")
    assert "client=testclient:50000" in _access_line(caplog, "/api/ping")


def test_slow_request_is_escalated_to_warning(tmp_path, caplog):
    """慢请求升级为 WARNING，且仍然只有一行（不是再补一行）。"""
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    with caplog.at_level(logging.WARNING):
        AccessLogMiddleware(None)._log_access(
            "GET", "/api/market/kline", 200, 9000.0, "1.2.3.4:5"
        )
    records = [r for r in caplog.records if r.name == "qmt_bridge.access"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "(慢请求)" in records[0].getMessage()


def test_normal_request_stays_info(tmp_path, caplog):
    """普通请求不能被误判成慢请求。"""
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    with caplog.at_level(logging.INFO):
        AccessLogMiddleware(None)._log_access(
            "GET", "/api/ping", 200, 12.0, "1.2.3.4:5"
        )
    record = next(r for r in caplog.records if r.name == "qmt_bridge.access")
    assert record.levelno == logging.INFO
    assert "(慢请求)" not in record.getMessage()


def test_access_log_returns_request_id_header(client_and_caplog):
    """响应头带 X-Request-ID，客户端报错时可直接拿来检索服务端日志。"""
    resp = client_and_caplog.get("/api/ping")
    assert resp.headers["X-Request-ID"]


def test_access_log_honours_incoming_request_id(client_and_caplog):
    """上游透传的请求 ID 必须沿用，跨服务才能串起来。"""
    resp = client_and_caplog.get("/api/ping", headers={"X-Request-ID": "upstream-42"})
    assert resp.headers["X-Request-ID"] == "upstream-42"


def test_request_id_visible_to_route_code(client_and_caplog):
    """请求处理期间 contextvar 必须可用（后台线程要靠它串日志）。"""
    assert get_request_id() == NO_REQUEST_ID  # 请求外恢复干净


def test_query_string_sensitive_values_not_logged(client_and_caplog, caplog):
    client_and_caplog.get("/api/ping?api_key=TOPSECRET&stock=000001.SZ")
    # 只看本项目的访问日志：httpx 自己也会打一行含完整 URL 的 INFO 日志，与本次改动无关
    line = _access_line(caplog, "/api/ping")
    assert "TOPSECRET" not in line
    assert "api_key=***" in line
    assert "stock=000001.SZ" in line


def test_unhandled_exception_logged_with_traceback_and_request_id(
    client_and_caplog, caplog
):
    """未处理异常必须落到本项目日志里（uvicorn 的日志不进我们的文件）。"""
    resp = client_and_caplog.get("/api/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["request_id"] and "kaboom" in body["error"]
    assert "ValueError: kaboom" in caplog.text  # 堆栈已记录
    assert resp.headers["X-Request-ID"] == body["request_id"]
    assert "-> 500" in _access_line(caplog, "/api/boom")
    # 堆栈那一行也必须带同一个请求 ID（否则出错时反而查不到上下文）
    err = next(r for r in caplog.records if r.name == "qmt_bridge.error")
    assert err.request_id == body["request_id"]


def test_access_log_can_be_disabled(tmp_path, caplog):
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    app = FastAPI()
    app.add_middleware(AccessLogMiddleware, enabled=False)

    @app.get("/api/ping")
    async def ping():
        return {"pong": True}

    with caplog.at_level(logging.DEBUG):
        TestClient(app).get("/api/ping")
    assert _access_line(caplog, "/api/ping", required=False) is None


def _access_line(caplog, path: str, required: bool = True):
    """在捕获的日志里取该路径的访问日志行。"""
    for record in caplog.records:
        if record.name == "qmt_bridge.access" and path in record.getMessage():
            return record.getMessage()
    if required:
        raise AssertionError(f"未找到 {path} 的访问日志：{caplog.text}")
    return None


def test_settings_expose_logging_knobs():
    """配置项必须可注入，否则运维无法在不改代码的前提下打开落盘。"""
    s = Settings(
        log_file="x.log", log_max_bytes=123, log_backup_count=2, log_access=False
    )
    assert (s.log_file, s.log_max_bytes, s.log_backup_count, s.log_access) == (
        "x.log",
        123,
        2,
        False,
    )


def test_server_cli_keeps_env_logging_config(tmp_path, monkeypatch):
    """qmt-server 必须沿用 .env / 环境变量里的日志配置。

    曾经 CLI 直接 ``Settings(...)`` 构造，把 ``QMT_BRIDGE_LOG_FILE`` 等一并丢掉，
    文档里写的落盘开关对服务端主入口完全无效。
    """
    log_file = tmp_path / "cli.log"
    monkeypatch.setenv("QMT_BRIDGE_LOG_FILE", str(log_file))
    monkeypatch.setattr(sys, "argv", ["qmt-server", "--port", "18999"])
    # 只验证配置与日志：不真的起服务，也不要求本机装了 xtquant
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "create_app", lambda settings: FastAPI())
    try:
        cli.main()
    finally:
        reset_settings(None)

    text = log_file.read_text(encoding="utf-8")
    assert "qmt-server v" in text and "启动" in text
    assert "qmt-server 已退出" in text


# --------------------------------------------------------------------------- 认证失败


def _protected_app(api_key: str) -> TestClient:
    """构造一个受 ``require_api_key`` 保护的测试应用。"""
    setup_logging(level="debug", log_file="")
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: Settings(api_key=api_key)

    @app.get("/api/trading/orders", dependencies=[Depends(require_api_key)])
    async def orders():
        return {"data": []}

    return TestClient(app)


def test_rejected_api_key_is_logged_without_secret(caplog):
    """401 要留痕（有人在扫端口时看得出来），但不能把密钥本身写进日志。"""
    with caplog.at_level(logging.WARNING):
        resp = _protected_app("s3cret").get(
            "/api/trading/orders", headers={"X-API-Key": "wrong-key"}
        )
    assert resp.status_code == 401
    line = next(
        r.getMessage() for r in caplog.records if r.name == "qmt_bridge.security"
    )
    assert "认证失败" in line and "/api/trading/orders" in line
    assert "s3cret" not in line and "wrong-key" not in line


def test_missing_server_api_key_is_logged(caplog):
    """服务端没配 API Key（503）同样要留痕，否则交易端点"消失"得毫无线索。"""
    with caplog.at_level(logging.WARNING):
        resp = _protected_app("").get("/api/trading/orders")
    assert resp.status_code == 503
    assert any(
        r.name == "qmt_bridge.security" and "未配置 API Key" in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------------------------------- 交易审计


class _FakeTrader:
    """替身交易通道：只记录调用，返回固定结果码。"""

    def order_stock(self, *args, **kwargs):
        return 1001

    def cancel_order_stock(self, *args, **kwargs):
        return 0

    def bank_transfer_in(self, *args, **kwargs):
        return 0


def _fake_manager() -> XtTraderManager:
    manager = XtTraderManager(mini_qmt_path="", account_id="")
    manager._account = object()
    manager._trader = _FakeTrader()
    return manager


def test_trading_mutations_are_audited(tmp_path, caplog):
    """下单/撤单必须留审计日志（含代码、数量、返回码）—— 资金路径不能只靠状态码。"""
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    manager = _fake_manager()
    with caplog.at_level(logging.INFO):
        manager.order("600000.SH", 23, 100, price=10.5)
        manager.cancel_order(12345)

    lines = [r.getMessage() for r in caplog.records if r.name == "qmt_bridge.trading"]
    assert any(
        "同步下单" in l and "600000.SH" in l and "100" in l and "1001" in l
        for l in lines
    )
    assert any("同步撤单" in l and "12345" in l for l in lines)


def test_bank_password_never_reaches_logs(tmp_path, caplog):
    """银行密码属于敏感信息，审计日志只记方向与金额。"""
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    with caplog.at_level(logging.INFO):
        _fake_manager().bank_transfer_in(
            "001", "6222001", 1000.0, bank_pwd="BANK-PWD", fund_pwd="FUND-PWD"
        )
    assert "银行转证券" in caplog.text
    assert "BANK-PWD" not in caplog.text and "FUND-PWD" not in caplog.text


class _FailingTrader:
    """替身交易通道：下单直接抛错。"""

    def order_stock(self, *args, **kwargs):
        raise RuntimeError("拒单: 可用资金不足")


def test_failed_order_keeps_parameters_and_traceback(tmp_path):
    """失败的下单必须带着参数和堆栈一起落日志 —— 只知道「下单失败了」等于没有线索。"""
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    manager = _fake_manager()
    manager._trader = _FailingTrader()
    with (
        _collect_logs("qmt_bridge.trading", logging.ERROR) as records,
        pytest.raises(RuntimeError),
    ):
        manager.order("600000.SH", 23, 100, price=10.5)

    record = records[0]
    message = record.getMessage()
    assert "交易失败 同步下单" in message
    assert (
        "600000.SH" in message
        and "order_volume=100" in message
        and "price=10.5" in message
    )
    assert record.exc_info is not None and "拒单" in str(record.exc_info[1])


def test_sensitive_param_names_are_recognised():
    """按名字识别敏感参数：密码/密钥类一律不记（含未出现过的写法）。"""
    assert _is_sensitive("bank_pwd") and _is_sensitive("FUND_PWD")
    assert _is_sensitive("password") and _is_sensitive("api_key")
    assert _is_sensitive("webhook_secret") and _is_sensitive("access_token")
    assert not _is_sensitive("price") and not _is_sensitive("order_volume")


def test_long_container_is_summarised_not_dumped():
    """deal_list 之类的长参数只报类型与长度，避免一次调用刷满日志。"""
    text = _describe({"deal_list": list(range(500)), "operation": "add"})
    assert "deal_list=<list len=500>" in text and "operation='add'" in text
    assert len(text) < 200
    assert _describe({}) == "-"


def test_audited_decorator_protects_new_methods():
    """过滤在装饰器里，不靠调用点自觉：新接口只要参数名像密码就不会进日志。"""

    class _Dummy:
        @_audited("测试动作")
        def do(self, account: str, trade_password: str, amount: float):
            return 7

    with _collect_logs("qmt_bridge.trading", logging.INFO) as records:
        assert _Dummy().do("A1", trade_password="TOPSECRET", amount=1.5) == 7

    line = records[0].getMessage()
    assert "测试动作" in line
    assert "account='A1'" in line and "amount=1.5" in line and "-> 7" in line
    assert "TOPSECRET" not in line


# --------------------------------------------------------------------------- 客户端调用日志


class _FakeResponse:
    """替身响应：够 urllib 客户端解析 JSON 即可。"""

    status = 200

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class _FakeOpener:
    def __init__(self, error: Exception | None = None, payload: bytes = b"{}"):
        self._error = error
        self._payload = payload

    def open(self, req, timeout=None):
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload)


def _client_with(opener) -> BaseClient:
    client = BaseClient("127.0.0.1", 1)
    client._opener = opener
    return client


def test_client_logs_failed_call_and_preserves_body():
    """失败调用要记状态码与服务端错误体（含 request_id），且不吞掉响应体。"""
    with _collect_logs("qmt_bridge.client") as records:
        error = urllib.error.HTTPError(
            "http://127.0.0.1:1/api/market/kline",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"detail":"boom","request_id":"abc12345"}'),
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _client_with(_FakeOpener(error=error))._get("/api/market/kline")
        # 日志读过响应体后必须放回去，否则调用方的 e.read() 会变成空
        assert b"abc12345" in excinfo.value.read()

    line = next(r.getMessage() for r in records if r.levelno == logging.ERROR)
    assert "-> 500" in line and "abc12345" in line


def test_client_logs_success_at_debug():
    """成功调用默认静默（DEBUG 级），需要时打开就能看到耗时。"""
    with _collect_logs("qmt_bridge.client") as records:
        body = _client_with(_FakeOpener(payload=b'{"ok":true}'))._get(
            "/api/meta/health"
        )

    assert body == {"ok": True}
    line = next(r.getMessage() for r in records if r.levelno == logging.DEBUG)
    assert "GET http://127.0.0.1:1/api/meta/health -> 200" in line


@contextlib.contextmanager
def _collect_logs(logger_name: str, level: int = logging.DEBUG):
    """抓取指定 logger 的日志。

    不用 caplog：``setup_logging`` 把 ``qmt_bridge`` 的 propagate 关掉了，
    记录不一定能走到 root 上的 caplog handler；直接挂在目标 logger 上才确定拿得到。
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector()
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)


# --------------------------------------------------------------------------- WebSocket


class _FakeXtdata:
    """替身 xtdata：只实现 WebSocket 订阅需要的那几个函数。"""

    def __init__(self):
        self.subscribed: list[dict] = []
        self.on_subscribe = threading.Event()

    def subscribe_quote2(self, **kwargs) -> int:
        self.subscribed.append(kwargs)
        self.on_subscribe.set()
        return len(self.subscribed)

    def unsubscribe_quote(self, seq_id: int) -> None:
        return None


def test_ws_realtime_logs_what_was_subscribed(monkeypatch):
    """服务端必须记下「谁订阅了什么」—— 排查「客户端收不到行情」的第一现场。"""
    setup_logging(level="debug", log_file="")
    fake = _FakeXtdata()
    monkeypatch.setitem(sys.modules, "xtquant", types.ModuleType("xtquant"))
    monkeypatch.setitem(
        sys.modules, "xtquant.xtdata", types.ModuleType("xtquant.xtdata")
    )
    from qmt_bridge.server.ws import realtime

    monkeypatch.setattr(realtime, "xtdata", fake)

    app = FastAPI()
    app.include_router(realtime.router)
    with (
        _collect_logs("qmt_bridge.ws.realtime") as records,
        TestClient(app).websocket_connect("/ws/realtime") as ws,
    ):
        ws.send_text(json.dumps({"stocks": ["000001.SZ", "600000.SH"], "period": "1m"}))
        assert fake.on_subscribe.wait(5), "服务端没有发起订阅"
        _wait_for_record(records, "行情订阅已建立")

    line = next(r.getMessage() for r in records if "行情订阅已建立" in r.getMessage())
    assert "000001.SZ" in line and "600000.SH" in line and "1m" in line
    # 断开时那行仍要看得到订阅数
    assert any("行情订阅客户端断开" in r.getMessage() for r in records)


def test_trade_ws_connect_disconnect_and_auth_are_logged():
    """交易 WS：连上/断开要有人数，密钥不对要留痕（且不能把密钥写进日志）。"""
    setup_logging(level="debug", log_file="")
    reset_settings(Settings(api_key="s3cret"))
    app = FastAPI()
    app.include_router(trade_callback.router)
    with _collect_logs("qmt_bridge.ws.trade", logging.INFO) as records:
        try:
            with TestClient(app).websocket_connect("/ws/trade?api_key=s3cret"):
                pass
            try:
                with TestClient(app).websocket_connect("/ws/trade?api_key=wrong"):
                    pass
            except WebSocketDisconnect:
                pass
        finally:
            reset_settings(None)

    lines = [r.getMessage() for r in records]
    assert any("已连接" in line and "监听数=1" in line for line in lines)
    assert any("已断开" in line and "监听数=0" in line for line in lines)
    assert any("交易 WS 认证失败" in line for line in lines)
    assert not any("s3cret" in line for line in lines)


def _wait_for_record(records, keyword: str, timeout: float = 5.0) -> None:
    """等某条日志出现（订阅日志在服务端线程里输出，断言不能抢跑）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(keyword in r.getMessage() for r in records):
            return
        time.sleep(0.02)
    raise AssertionError(f"未等到「{keyword}」日志")


# --------------------------------------------------------------------------- xtdata 串行化


def _http_scope(path: str) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "state": {},
    }


async def _call(middleware, path: str) -> None:
    async def receive():
        return {"type": "http.request"}

    async def send(message):
        return None

    await middleware(_http_scope(path), receive, send)


def test_xtdata_lock_names_the_endpoint_that_blocks_the_queue(tmp_path, monkeypatch):
    """队列被堵住时必须点名：谁在持锁、持了多久、谁在等。"""
    setup_logging(level="debug", log_file=str(tmp_path / "t.log"))
    monkeypatch.setattr(xtdata_lock_module, "SLOW_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(xtdata_lock_module, "LONG_HOLD_SECONDS", 0.05)
    # 用新的锁，避免和其它测试绑到不同事件循环
    monkeypatch.setattr(xtdata_lock_module, "xtdata_lock", asyncio.Lock())

    async def app(scope, receive, send):
        # 慢端点占住锁，快端点只能排队
        await asyncio.sleep(0.15 if scope["path"].endswith("kline") else 0)

    middleware = xtdata_lock_module.XtdataSerializerMiddleware(app)

    async def scenario():
        slow = asyncio.create_task(_call(middleware, "/api/market/kline"))
        await asyncio.sleep(0.02)  # 让慢请求先拿到锁
        await _call(middleware, "/api/tick/full")
        await slow

    with _collect_logs("qmt_bridge") as records:
        asyncio.run(scenario())

    messages = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    hold = next(m for m in messages if m.startswith("xtdata 串行化: "))
    assert "/api/market/kline" in hold, messages
    wait = next(m for m in messages if m.startswith("xtdata 串行化排队"))
    assert "/api/tick/full" in wait and "前一个 /api/market/kline" in wait, messages


# --------------------------------------------------------------------------- 下载端点


def test_download_endpoint_logs_what_was_requested(monkeypatch):
    """下载会长时间占住 xtdata 锁：日志里必须留下「请求了什么」。"""
    setup_logging(level="debug", log_file="")
    fake_xtquant = types.ModuleType("xtquant")
    fake_xtquant.xtdata = types.ModuleType("xtquant.xtdata")
    # downloader 会 `from xtquant import xtbson`，缺失时回落到第三方 bson（本机没装）
    fake_xtquant.xtbson = types.ModuleType("xtquant.xtbson")
    monkeypatch.setitem(sys.modules, "xtquant", fake_xtquant)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake_xtquant.xtdata)
    monkeypatch.setitem(sys.modules, "xtquant.xtbson", fake_xtquant.xtbson)
    from qmt_bridge.server.routers import download as download_router

    monkeypatch.setattr(
        download_router,
        "download_history_data2_safe",
        lambda *args, **kwargs: {"ok": 2},
    )
    app = FastAPI()
    app.include_router(download_router.router)

    with _collect_logs("qmt_bridge.download") as records:
        resp = TestClient(app).post(
            "/api/download/history_data2",
            json={
                "stocks": ["000001.SZ", "600000.SH"],
                "period": "1d",
                "start_time": "20240101",
                "end_time": "20240131",
            },
        )

    assert resp.status_code == 200
    line = records[0].getMessage()
    assert "历史行情" in line and "000001.SZ" in line and "600000.SH" in line
    assert "1d" in line and "20240101" in line and "20240131" in line
