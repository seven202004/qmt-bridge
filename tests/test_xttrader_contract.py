"""XtQuantTrader 封装契约测试。

校验 qmt-bridge 对 xttrader 的封装与真实 xtquant 接口保持一致：
1. BridgeTraderCallback 覆盖 XtQuantTraderCallback 声明的全部回调方法（缺失会导致推送报错）
2. XtTraderManager 的系统设置/期货持仓统计方法正确转发到 XtQuantTrader

未安装 xtquant 时整个模块跳过（CI 不装 xtquant）。
"""

import pytest

pytest.importorskip("xtquant", reason="需要真实 xtquant 环境")

from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback

from qmt_bridge.server.trading.callbacks import BridgeTraderCallback
from qmt_bridge.server.trading.manager import XtTraderManager


def _callback_methods() -> list[str]:
    """返回 XtQuantTraderCallback 声明的全部 on_* 回调方法名。"""
    return sorted(n for n in dir(XtQuantTraderCallback) if n.startswith("on_"))


def test_bridge_callback_covers_xttrader_callbacks():
    """桥接回调必须实现 xtquant 声明的每一个回调方法。"""
    missing = [n for n in _callback_methods() if not hasattr(BridgeTraderCallback, n)]
    assert missing == [], f"BridgeTraderCallback 缺少回调方法: {missing}"


def test_bridge_callback_methods_are_callable():
    """每个回调方法都应是可调用的实例方法。"""
    cb = BridgeTraderCallback()
    not_callable = [
        n for n in _callback_methods() if not callable(getattr(cb, n, None))
    ]
    assert not_callable == []


class _FakeTrader:
    """记录调用参数的假 XtQuantTrader，用于验证 manager 的转发逻辑。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"method": name}

        return _record


def _manager_with_fake_trader() -> tuple[XtTraderManager, _FakeTrader]:
    manager = XtTraderManager(mini_qmt_path="", account_id="123456")
    fake = _FakeTrader()
    manager._trader = fake
    manager._account = object()
    return manager, fake


@pytest.mark.parametrize(
    "method, args",
    [
        ("smart_algo_order_async", ("600000.SH", 23, 2000)),
        ("cancel_smart_algo_task_async", (12345,)),
        ("query_smart_algo_task", ()),
    ],
)
def test_smart_algo_methods_exist_on_xttrader(method, args):
    """算法交易方法必须真实存在于 XtQuantTrader（签名层面防止写错名字）。"""
    assert hasattr(XtQuantTrader, method), f"xtquant 无该方法: {method}"


def test_smart_algo_order_async_forwards_all_arguments():
    """算法下单应把 12 个参数按真实签名顺序原样转发给 XtQuantTrader。"""
    manager, fake = _manager_with_fake_trader()
    algo_param = {"m_nValidTimeStart": 1, "m_nValidTimeEnd": 2}
    manager.smart_algo_order_async(
        stock_code="600000.SH", order_type=23, order_volume=2000,
        price_type=5, price=11.65, algo_name="CGS_TWAP",
        start_time="20240101 09:30:00", end_time="20240101 15:00:00",
        algo_param=algo_param, strategy_name="s", order_remark="r",
        account_id="123456",
    )
    name, args, _ = fake.calls[0]
    assert name == "smart_algo_order_async"
    # account, stock_code, order_type, order_volume, price_type, price,
    # strategy_name, order_remark, algo_name, start_time, end_time, algo_param
    assert args[1:] == (
        "600000.SH", 23, 2000, 5, 11.65, "s", "r",
        "CGS_TWAP", "20240101 09:30:00", "20240101 15:00:00", algo_param,
    )


def test_smart_algo_order_passes_empty_param_dict_when_omitted():
    """algo_param 省略时应传空 dict 而非 None（xtquant 会对它写 m_nValidTime* 键）。"""
    manager, fake = _manager_with_fake_trader()
    manager.smart_algo_order_async(
        stock_code="600000.SH", order_type=23, order_volume=100,
        price_type=5, price=0.0, algo_name="CGS_TWAP",
    )
    assert fake.calls[0][1][-1] == {}


def test_smart_algo_query_and_cancel_forward():
    """查任务 / 撤任务 / 取参数应转发到对应 XtQuantTrader 方法。"""
    manager, fake = _manager_with_fake_trader()
    manager.cancel_smart_algo_task_async(task_id=777, account_id="123456")
    manager.query_smart_algo_task(account_id="123456")
    manager.get_smart_algo_param(["TWAP", "VWAP"])
    assert [c[0] for c in fake.calls] == [
        "cancel_smart_algo_task_async", "query_smart_algo_task", "get_smart_algo_param",
    ]
    assert fake.calls[0][1][1] == 777                      # task_id 在 account 之后
    assert fake.calls[2][1] == (["TWAP", "VWAP"],)


@pytest.mark.parametrize("value", ["09:30:00", "9:30:00", "15:00:00"])
def test_smart_algo_time_format_accepts_clock_only(value):
    """算法下单的 start_time/end_time 只接受 "HH:MM:SS"，xtquant 会补当天日期。"""
    assert XtQuantTrader._time_to_timestamp(None, value) > 0


@pytest.mark.parametrize(
    "value",
    ["20240101 09:30:00", "20240101093000", "093000", "2024-01-01"],
)
def test_smart_algo_time_format_rejects_date_prefixed(value):
    """带日期前缀的时间串会被 xtquant 判为格式错误（文档不要写成这种形式）。"""
    with pytest.raises(ValueError):
        XtQuantTrader._time_to_timestamp(None, value)


def test_bank_transfer_async_callbacks_dispatch_events():
    """异步划转回调应派发对应事件类型，否则异步转账结果会丢失。"""
    cb = BridgeTraderCallback()
    events: list[dict] = []
    cb._dispatch = events.append

    class _Resp:
        seq = 7
        success = True
        msg = "ok"

    cb.on_bank_transfer_async_response(_Resp())
    cb.on_ctp_internal_transfer_async_response(_Resp())
    assert [e["type"] for e in events] == [
        "bank_transfer_response",
        "ctp_transfer_response",
    ]
    assert events[0]["data"] == {"seq": 7, "success": True, "msg": "ok"}
