"""交易模块连接失败清理与后台重连测试。

不需要真实 xtquant：``manager.connect()`` 内部才 import xtquant，
这里往 ``sys.modules`` 注入最小假包即可覆盖"客户端拒绝连接"的失败路径
（真实表现：客户端日志 ``quant session X, pid Y not allowed`` → connect 返回 -1）。
"""

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from qmt_bridge.server import app as app_module
from qmt_bridge.server.trading.manager import XtTraderManager

_instances: list = []


class _RejectingTrader:
    """connect() 恒返回 -1 的假客户端，用于验证失败清理。"""

    def __init__(self, path, session_id):
        self.path = path
        self.session_id = session_id
        self.stopped = False
        _instances.append(self)

    def register_callback(self, callback):
        pass

    def start(self):
        pass

    def connect(self):
        return -1

    def stop(self):
        self.stopped = True


def _install_fake_xtquant(monkeypatch, trader_cls) -> None:
    """把 xtquant / xtquant.xttrader / xtquant.xttype 换成最小假包。"""
    package = types.ModuleType("xtquant")
    xttrader = types.ModuleType("xtquant.xttrader")
    xttrader.XtQuantTrader = trader_cls
    xttype = types.ModuleType("xtquant.xttype")
    xttype.StockAccount = lambda account_id, account_type="STOCK": (
        account_id,
        account_type,
    )
    package.xttrader = xttrader
    package.xttype = xttype
    monkeypatch.setitem(sys.modules, "xtquant", package)
    monkeypatch.setitem(sys.modules, "xtquant.xttrader", xttrader)
    monkeypatch.setitem(sys.modules, "xtquant.xttype", xttype)


def test_connect_failure_releases_trader(monkeypatch):
    """connect 被拒时必须 stop() 释放半开会话，并把错误抛给调用方。"""
    _instances.clear()
    _install_fake_xtquant(monkeypatch, _RejectingTrader)

    manager = XtTraderManager(mini_qmt_path="C:/qmt/userdata_mini", account_id="123456")

    with pytest.raises(RuntimeError, match="connect failed: -1"):
        manager.connect()

    assert _instances[0].stopped is True, "失败连接没 stop()，客户端会一直记着这个会话"
    assert manager._trader is None


class _FakeManager:
    """按预设结果序列连接的管理器替身：False = 失败，True = 成功。"""

    def __init__(self, results):
        self._results = list(results)
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        if not self._results.pop(0):
            raise RuntimeError("XtQuantTrader connect failed: -1")


def test_retry_loop_publishes_manager_after_recovery(monkeypatch):
    """首连失败后的后台重连：重试成功后发布 manager 到 app.state 并退出。"""
    monkeypatch.setattr(app_module, "_TRADING_RETRY_SECONDS", 0.01)
    first, second = _FakeManager([False]), _FakeManager([True])
    pending = [first, second]
    monkeypatch.setattr(
        app_module, "_new_trader_manager", lambda settings: pending.pop(0)
    )

    app = FastAPI()
    app.state.trader_manager = None

    async def _run():
        # wait_for 兜底：重试循环若没能退出，测试失败而不是挂住
        await asyncio.wait_for(
            app_module._trading_retry_loop(app, SimpleNamespace()), timeout=5
        )

    asyncio.run(_run())

    assert first.connect_calls == 1
    assert second.connect_calls == 1
    assert app.state.trader_manager is second
