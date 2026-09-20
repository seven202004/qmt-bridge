"""信用委托查询路由契约：/api/credit/orders。

信用账户的委托与普通账户共用同一个 xttrader 查询 API（``query_stock_orders``），
唯一的差别是**账户对象必须按 CREDIT 类型解析**。若沿用普通账户口径，返回的是
另一个账号的委托——不报错、不抛异常，只是静默给出错误答案，风控据此对账/撤单
就会打偏（qmtmix 侧 2026-09-16 之前的"信用账户委托查不到"正是这个形状）。

本用例不依赖真实 xtquant：``_resolve_account`` 内部按需 import，这里用替身模块注入。
"""

import sys
import types


def _install_fake_xtquant(monkeypatch):
    """把 ``xtquant.xttype.StockAccount`` 换成可断言的替身。"""

    class _Account:
        def __init__(self, account_id, account_type="STOCK"):
            self.account_id = account_id
            self.account_type = account_type

    pkg = types.ModuleType("xtquant")
    pkg.__path__ = []
    xttype = types.ModuleType("xtquant.xttype")
    xttype.StockAccount = _Account
    pkg.xttype = xttype
    monkeypatch.setitem(sys.modules, "xtquant", pkg)
    monkeypatch.setitem(sys.modules, "xtquant.xttype", xttype)
    return _Account


class _FakeTrader:
    """记录调用的假 XtQuantTrader。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return [{"order_id": 1}]

        return _record


def _manager(monkeypatch):
    from qmt_bridge.server.trading.manager import XtTraderManager

    account_cls = _install_fake_xtquant(monkeypatch)
    manager = XtTraderManager(mini_qmt_path="", account_id="161000050")
    fake = _FakeTrader()
    manager._trader = fake
    manager._account = account_cls("161000050", "STOCK")
    return manager, fake


def test_query_credit_orders_resolves_credit_account(monkeypatch):
    """信用委托必须按 CREDIT 账户查询，且 cancelable_only 原样转发。"""
    manager, fake = _manager(monkeypatch)

    manager.query_credit_orders(account_id="16980005", cancelable_only=True)

    name, args, _ = fake.calls[0]
    assert name == "query_stock_orders"
    assert args[0].account_type == "CREDIT", "按普通账户查会返回另一个账号的委托"
    assert args[0].account_id == "16980005"
    assert args[1] is True


def test_query_credit_orders_defaults_to_not_cancelable_only(monkeypatch):
    manager, fake = _manager(monkeypatch)

    manager.query_credit_orders(account_id="16980005")

    assert fake.calls[0][1][1] is False


def test_query_credit_positions_still_resolves_credit_account(monkeypatch):
    """既有信用查询同样按 CREDIT 解析（本次改动的对照基线）。"""
    manager, fake = _manager(monkeypatch)

    manager.query_credit_positions(account_id="16980005")

    assert fake.calls[0][0] == "query_stock_positions"
    assert fake.calls[0][1][0].account_type == "CREDIT"


def test_credit_router_exposes_orders_endpoint():
    """路由必须真的挂上 GET /api/credit/orders（删掉/写错前缀都会被这条挡住）。"""
    from qmt_bridge.server.routers.credit import router

    paths = {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/credit/orders", "GET") in paths


def test_credit_client_calls_credit_orders_path(monkeypatch):
    """客户端 mixin 必须打 /api/credit/orders（而不是 /api/trading/orders）。"""
    from qmt_bridge.client.credit import CreditMixin

    seen = {}

    class _Client(CreditMixin):
        def _get(self, path, params=None):
            seen["path"] = path
            seen["params"] = params
            return {}

    # Mixin 现在继承 BaseClient（这样 mypy 才知道 self._get 是什么），构造需要 host/port
    _Client("127.0.0.1", 1).query_credit_orders(
        account_id="16980005", cancelable_only=True
    )
    assert seen["path"] == "/api/credit/orders"
    assert seen["params"] == {"account_id": "16980005", "cancelable_only": True}


def test_cancel_credit_order_resolves_credit_account(monkeypatch):
    """信用撤单必须按 CREDIT 账户撤：普通口径撤的是另一个账号的委托，还可能"撤成功"。"""
    manager, fake = _manager(monkeypatch)

    manager.cancel_credit_order(order_id=413138945, account_id="16980005")

    name, args, _ = fake.calls[0]
    assert name == "cancel_order_stock"
    assert args[0].account_type == "CREDIT"
    assert args[0].account_id == "16980005"
    assert args[1] == 413138945


def test_credit_router_exposes_cancel_endpoint():
    """路由必须真的挂上 POST /api/credit/cancel。"""
    from qmt_bridge.server.routers.credit import router

    paths = {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/credit/cancel", "POST") in paths


def test_credit_client_calls_credit_cancel_path():
    """客户端 mixin 必须打 /api/credit/cancel（而不是 /api/trading/cancel）。"""
    from qmt_bridge.client.credit import CreditMixin

    seen = {}

    class _Client(CreditMixin):
        def _post(self, path, body=None):
            seen["path"] = path
            seen["body"] = body
            return {}

    _Client("127.0.0.1", 1).cancel_credit_order(
        order_id=413138945, account_id="16980005"
    )
    assert seen["path"] == "/api/credit/cancel"
    assert seen["body"] == {"order_id": 413138945, "account_id": "16980005"}
