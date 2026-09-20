"""XtTraderManager — XtQuantTrader 实例的生命周期管理器。

本模块提供 ``XtTraderManager`` 类，负责：
- 在 FastAPI 应用启动时初始化并连接 XtQuantTrader 实例
- 在应用关闭时断开连接并释放资源
- 封装所有交易操作（下单、撤单、查询、信用交易、银证转账等）

该管理器作为整个 qmt-bridge 服务端与 QMT 迅投客户端之间的桥梁，
所有 REST API 路由中的交易操作最终都委托给此类来执行。

所有方法签名严格对齐 xtquant.xttrader 的真实 API。
"""

import functools
import inspect
import logging
import random
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("qmt_bridge.trading")

# 审计日志里按参数名过滤敏感字段：名字里带这些片段的参数一律不入日志
_SENSITIVE_FIELD_HINTS = ("pwd", "password", "secret", "key", "token")
# 单个字段值的最大长度，超长只报类型与长度（deal_list / dict_param 可能很大）
_MAX_FIELD_REPR = 120


def _is_sensitive(field: str) -> bool:
    """参数名是否属于敏感字段（银行密码、资金密码、密钥）。"""
    lowered = field.lower()
    return any(hint in lowered for hint in _SENSITIVE_FIELD_HINTS)


def _format_value(value: Any) -> str:
    """格式化单个字段值；超长容器不整段打印，只报类型与长度。"""
    text = repr(value)
    if len(text) <= _MAX_FIELD_REPR:
        return text
    try:
        size = len(value)
    except TypeError:
        return text[:_MAX_FIELD_REPR] + "..."
    return f"<{type(value).__name__} len={size}>"


def _describe(fields: dict[str, Any]) -> str:
    """把实参拼成一行 ``k=v``；敏感字段已在上游剔除。"""
    return " ".join(f"{k}={_format_value(v)}" for k, v in fields.items()) or "-"


def _bind_fields(
    func: Callable[..., Any], self: Any, args: tuple, kwargs: dict
) -> dict[str, Any]:
    """按被装饰方法的签名把实参绑定成「参数名 → 值」，敏感字段在此剔除。"""
    try:
        bound = inspect.signature(func).bind(self, *args, **kwargs)
    except TypeError:
        # 绑不上就宁可不记参数，也不能让日志把交易调用带崩
        return {}
    return {
        name: value
        for name, value in bound.arguments.items()
        if name != "self" and not _is_sensitive(name)
    }


def _audited(action: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """给资金/持仓变动类方法加审计日志：成功记参数 + 返回码，失败记参数 + 堆栈。

    参数从被装饰方法的签名自动取，新增参数不会漏记；名字命中
    :data:`_SENSITIVE_FIELD_HINTS` 的字段永久跳过 —— 银行密码、资金密码绝不能
    进日志，所以过滤放在装饰器里而不是各个调用点，避免新接口忘了脱敏。

    Args:
        action: 日志里的动作名，如 ``"同步下单"``。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            fields = _bind_fields(func, self, args, kwargs)
            try:
                result = func(self, *args, **kwargs)
            except Exception:
                # 失败路径也要带上参数：只知道「下单失败了」等于没有线索
                logger.exception("交易失败 %s: %s", action, _describe(fields))
                raise
            logger.info("交易 %s: %s -> %r", action, _describe(fields), result)
            return result

        return wrapper

    return decorator


class XtTraderManager:
    """XtQuantTrader 实例管理器。

    在 FastAPI lifespan 启动阶段、当交易功能启用时被创建。
    负责维护与 QMT 迅投交易终端的连接，并提供统一的交易操作接口。

    Attributes:
        mini_qmt_path: MiniQMT 客户端安装路径，用于连接交易终端。
        account_id: 默认交易账户 ID。
    """

    def __init__(self, mini_qmt_path: str = "", account_id: str = ""):
        self.mini_qmt_path = mini_qmt_path
        self.account_id = account_id
        # 类型标 Any：xtquant 没有 stub，具体类型 mypy 认不出来；
        # 不标会被推断成 None，进而否定下面全部 self._trader.xxx() 调用。
        self._trader: Any = None  # XtQuantTrader 实例，连接后赋值
        self._account: Any = None  # 默认 StockAccount 实例

    def connect(self):
        """初始化并连接 XtQuantTrader 实例。"""
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount

        from .callbacks import BridgeTraderCallback

        path = self.mini_qmt_path
        # ponytail: 原 `hash(path)&0xFFFF` 是确定性固定值，broker 被强杀后残留 session
        # 会占用该 id，导致下次 `connect()` 复用同一 session 被拒(-1)。
        # 改为每次启动随机一个大整数，避免与残留/其它连接的 session 冲突。
        session_id = random.randint(1, 0x7FFFFFFF)

        self._trader = XtQuantTrader(path, session_id)
        self._account = StockAccount(self.account_id)
        self._callback = BridgeTraderCallback()

        self._trader.register_callback(self._callback)
        self._trader.start()

        result = self._trader.connect()
        if result != 0:
            raise RuntimeError(f"XtQuantTrader connect failed: {result}")

        result = self._trader.subscribe(self._account)
        if result != 0:
            logger.warning("subscribe_account returned %s", result)

        logger.info("XtQuantTrader connected, account=%s", self.account_id)

    def disconnect(self):
        """断开连接并清理资源。"""
        if self._trader is not None:
            try:
                self._trader.stop()
            except Exception:
                logger.exception("Error stopping XtQuantTrader")
            self._trader = None

    def _resolve_account(self, account_id: str = "", account_type: str = "STOCK"):
        """解析交易账户，返回 StockAccount 实例。

        Args:
            account_id: 资金账号；空则用默认账户（self.account_id）。
            account_type: xtquant ``StockAccount`` 的账户类型（如 STOCK/CREDIT/FUTURE）。
                融资融券/信用账户必须传 ``'CREDIT'``，否则 ``query_credit_*`` / 两融下单
                无法按信用账户正确路由；普通证券账户保持默认 ``'STOCK'``。

        Returns:
            :class:`xtquant.xttype.StockAccount` 实例。
        """
        if account_type == "STOCK" and (
            not account_id or account_id == self.account_id
        ):
            # 普通账户 + 默认账号：复用连接时缓存的默认账户，避免重复构造
            return self._account
        from xtquant.xttype import StockAccount

        return StockAccount(account_id or self.account_id, account_type)

    # ------------------------------------------------------------------
    # 委托操作
    # ------------------------------------------------------------------

    @_audited("同步下单")
    def order(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """同步下单 → _trader.order_stock()"""
        account = self._resolve_account(account_id)
        return self._trader.order_stock(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    @_audited("异步下单")
    def order_async(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """异步下单 → _trader.order_stock_async()"""
        account = self._resolve_account(account_id)
        return self._trader.order_stock_async(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    @_audited("同步撤单")
    def cancel_order(self, order_id: int, account_id: str = ""):
        """同步撤单 → _trader.cancel_order_stock()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock(account, order_id)

    @_audited("异步撤单")
    def cancel_order_async(self, order_id: int, account_id: str = ""):
        """异步撤单 → _trader.cancel_order_stock_async()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_async(account, order_id)

    @_audited("按系统编号撤单")
    def cancel_order_stock_sysid(self, market: str, sysid: str, account_id: str = ""):
        """按系统编号同步撤单 → _trader.cancel_order_stock_sysid()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_sysid(account, market, sysid)

    @_audited("按系统编号异步撤单")
    def cancel_order_stock_sysid_async(
        self, market: str, sysid: str, account_id: str = ""
    ):
        """按系统编号异步撤单 → _trader.cancel_order_stock_sysid_async()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_order_stock_sysid_async(account, market, sysid)

    # ------------------------------------------------------------------
    # 查询操作
    # ------------------------------------------------------------------

    def query_orders(self, account_id: str = "", cancelable_only: bool = False):
        """查询当日委托列表 → _trader.query_stock_orders()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_orders(account, cancelable_only)

    def query_positions(self, account_id: str = ""):
        """查询当前持仓列表 → _trader.query_stock_positions()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_positions(account)

    def query_asset(self, account_id: str = ""):
        """查询账户资产信息 → _trader.query_stock_asset()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_asset(account)

    def query_trades(self, account_id: str = ""):
        """查询当日成交列表 → _trader.query_stock_trades()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_trades(account)

    def query_order_detail(self, order_id: int = 0, account_id: str = ""):
        """根据委托编号查询单笔委托详情（遍历过滤）。"""
        account = self._resolve_account(account_id)
        orders = self._trader.query_stock_orders(account, False)
        if orders:
            for o in orders:
                if getattr(o, "order_id", None) == order_id:
                    return o
        return None

    def query_single_order(self, order_id: int, account_id: str = ""):
        """按委托编号查询单笔委托 → _trader.query_stock_order()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_order(account, order_id)

    def query_single_trade(self, trade_id: int, account_id: str = ""):
        """按成交编号查询单笔成交（遍历 query_stock_trades 过滤）。"""
        account = self._resolve_account(account_id)
        trades = self._trader.query_stock_trades(account)
        if trades:
            for t in trades:
                if getattr(t, "traded_id", None) == trade_id:
                    return t
        return None

    def query_single_position(self, stock_code: str, account_id: str = ""):
        """查询单只股票持仓 → _trader.query_stock_position()"""
        account = self._resolve_account(account_id)
        return self._trader.query_stock_position(account, stock_code)

    # ------------------------------------------------------------------
    # 信用交易操作（融资融券）
    # ------------------------------------------------------------------

    @_audited("信用下单")
    def credit_order(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int = 5,
        price: float = 0.0,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """信用交易下单（通过 order_type 常量区分融资/融券）→ _trader.order_stock()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.order_stock(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
        )

    @_audited("信用撤单")
    def cancel_credit_order(self, order_id: int, account_id: str = ""):
        """信用账户撤单 → _trader.cancel_order_stock()

        与委托查询同理：账户对象必须按 CREDIT 解析。拿普通账户口径去撤，撤的是
        **另一个账号**的委托，返回码还可能是成功——静默撤错单，比失败更糟。
        """
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.cancel_order_stock(account, order_id)

    def query_credit_orders(self, account_id: str = "", cancelable_only: bool = False):
        """查询信用账户当日委托 → _trader.query_stock_orders()

        委托查询与普通账户用同一个 xttrader API，但**账户对象必须按 CREDIT 解析**：
        拿普通账户口径去查，返回的是另一个账号的委托，静默给出错误答案——风控据此
        对账/撤单会打偏。
        """
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_stock_orders(account, cancelable_only)

    def query_credit_positions(self, account_id: str = ""):
        """查询信用账户持仓 → _trader.query_stock_positions()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_stock_positions(account)

    def query_credit_detail(self, account_id: str = ""):
        """查询信用账户资产详情 → _trader.query_credit_detail()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_credit_detail(account)

    def query_stk_compacts(self, account_id: str = ""):
        """查询信用负债合约 → _trader.query_stk_compacts()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_stk_compacts(account)

    def query_credit_slo_code(self, account_id: str = ""):
        """查询融券标的券列表 → _trader.query_credit_slo_code()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_credit_slo_code(account)

    def query_credit_subjects(self, account_id: str = ""):
        """查询信用标的券列表 → _trader.query_credit_subjects()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_credit_subjects(account)

    def query_credit_assure(self, account_id: str = ""):
        """查询信用担保品信息 → _trader.query_credit_assure()"""
        account = self._resolve_account(account_id, account_type="CREDIT")
        return self._trader.query_credit_assure(account)

    # ------------------------------------------------------------------
    # 资金划转
    # ------------------------------------------------------------------

    @_audited("资金划转")
    def fund_transfer(
        self, transfer_direction: int, amount: float, account_id: str = ""
    ):
        """资金划转 → _trader.fund_transfer()"""
        account = self._resolve_account(account_id)
        return self._trader.fund_transfer(account, transfer_direction, amount)

    # ------------------------------------------------------------------
    # 银证转账（完整实现，对齐 xttrader 真实 API）
    # ------------------------------------------------------------------

    @_audited("银行转证券")
    def bank_transfer_in(
        self,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
        account_id: str = "",
    ):
        """银行转证券 → _trader.bank_transfer_in()"""
        account = self._resolve_account(account_id)
        return self._trader.bank_transfer_in(
            account,
            bank_no,
            bank_account,
            balance,
            bank_pwd,
            fund_pwd,
        )

    @_audited("证券转银行")
    def bank_transfer_out(
        self,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
        account_id: str = "",
    ):
        """证券转银行 → _trader.bank_transfer_out()"""
        account = self._resolve_account(account_id)
        return self._trader.bank_transfer_out(
            account,
            bank_no,
            bank_account,
            balance,
            bank_pwd,
            fund_pwd,
        )

    @_audited("异步银行转证券")
    def bank_transfer_in_async(
        self,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
        account_id: str = "",
    ):
        """异步银行转证券 → _trader.bank_transfer_in_async()"""
        account = self._resolve_account(account_id)
        return self._trader.bank_transfer_in_async(
            account,
            bank_no,
            bank_account,
            balance,
            bank_pwd,
            fund_pwd,
        )

    @_audited("异步证券转银行")
    def bank_transfer_out_async(
        self,
        bank_no: str,
        bank_account: str,
        balance: float,
        bank_pwd: str = "",
        fund_pwd: str = "",
        account_id: str = "",
    ):
        """异步证券转银行 → _trader.bank_transfer_out_async()"""
        account = self._resolve_account(account_id)
        return self._trader.bank_transfer_out_async(
            account,
            bank_no,
            bank_account,
            balance,
            bank_pwd,
            fund_pwd,
        )

    def query_bank_info(self, account_id: str = ""):
        """查询绑定银行信息 → _trader.query_bank_info()"""
        account = self._resolve_account(account_id)
        return self._trader.query_bank_info(account)

    def query_bank_amount(
        self, bank_no: str, bank_account: str, bank_pwd: str, account_id: str = ""
    ):
        """查询银行余额 → _trader.query_bank_amount()"""
        account = self._resolve_account(account_id)
        return self._trader.query_bank_amount(account, bank_no, bank_account, bank_pwd)

    def query_bank_transfer_stream(
        self,
        start_date: str,
        end_date: str,
        bank_no: str = "",
        bank_account: str = "",
        account_id: str = "",
    ):
        """查询银证转账流水 → _trader.query_bank_transfer_stream()"""
        account = self._resolve_account(account_id)
        return self._trader.query_bank_transfer_stream(
            account,
            start_date,
            end_date,
            bank_no,
            bank_account,
        )

    # ------------------------------------------------------------------
    # CTP 跨市场资金划转
    # ------------------------------------------------------------------

    def ctp_transfer_option_to_future(
        self, opt_account_id: str, ft_account_id: str, balance: float
    ):
        """期权→期货 资金划转 → _trader.ctp_transfer_option_to_future()"""
        return self._trader.ctp_transfer_option_to_future(
            opt_account_id,
            ft_account_id,
            balance,
        )

    def ctp_transfer_future_to_option(
        self, opt_account_id: str, ft_account_id: str, balance: float
    ):
        """期货→期权 资金划转 → _trader.ctp_transfer_future_to_option()"""
        return self._trader.ctp_transfer_future_to_option(
            opt_account_id,
            ft_account_id,
            balance,
        )

    # ------------------------------------------------------------------
    # 证券划转
    # ------------------------------------------------------------------

    @_audited("证券划转")
    def secu_transfer(
        self,
        transfer_direction: int,
        stock_code: str,
        volume: int,
        transfer_type: int,
        account_id: str = "",
    ):
        """证券划转 → _trader.secu_transfer()"""
        account = self._resolve_account(account_id)
        return self._trader.secu_transfer(
            account,
            transfer_direction,
            stock_code,
            volume,
            transfer_type,
        )

    # ------------------------------------------------------------------
    # SMT 约定式交易操作（完整实现）
    # ------------------------------------------------------------------

    def smt_query_quoter(self, account_id: str = ""):
        """查询 SMT 报价方信息 → _trader.smt_query_quoter()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_query_quoter(account)

    def smt_query_compact(self, account_id: str = ""):
        """查询 SMT 约定合约列表 → _trader.smt_query_compact()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_query_compact(account)

    def smt_query_order(self, account_id: str = ""):
        """查询 SMT 委托 → _trader.smt_query_order()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_query_order(account)

    @_audited("SMT 协商下单")
    def smt_negotiate_order_async(
        self,
        src_group_id: str,
        order_code: str,
        date: str,
        amount: float,
        apply_rate: float,
        dict_param: dict | None = None,
        account_id: str = "",
    ):
        """异步 SMT 协商下单 → _trader.smt_negotiate_order_async()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_negotiate_order_async(
            account,
            src_group_id,
            order_code,
            date,
            amount,
            apply_rate,
            dict_param or {},
        )

    @_audited("SMT 预约委托")
    def smt_appointment_order_async(
        self,
        order_code: str,
        date: str,
        amount: float,
        apply_rate: float,
        account_id: str = "",
    ):
        """异步 SMT 预约委托 → _trader.smt_appointment_order_async()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_appointment_order_async(
            account,
            order_code,
            date,
            amount,
            apply_rate,
        )

    @_audited("SMT 取消预约")
    def smt_appointment_cancel_async(self, apply_id: str, account_id: str = ""):
        """异步取消 SMT 预约 → _trader.smt_appointment_cancel_async()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_appointment_cancel_async(account, apply_id)

    @_audited("SMT 合约展期")
    def smt_compact_renewal_async(
        self,
        cash_compact_id: str,
        order_code: str,
        defer_days: int,
        defer_num: int,
        apply_rate: float,
        account_id: str = "",
    ):
        """异步 SMT 合约展期 → _trader.smt_compact_renewal_async()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_compact_renewal_async(
            account,
            cash_compact_id,
            order_code,
            defer_days,
            defer_num,
            apply_rate,
        )

    @_audited("SMT 合约归还")
    def smt_compact_return_async(
        self,
        src_group_id: str,
        cash_compact_id: str,
        order_code: str,
        occur_amount: float,
        account_id: str = "",
    ):
        """异步 SMT 合约归还 → _trader.smt_compact_return_async()"""
        account = self._resolve_account(account_id)
        return self._trader.smt_compact_return_async(
            account,
            src_group_id,
            cash_compact_id,
            order_code,
            occur_amount,
        )

    # ------------------------------------------------------------------
    # 新股申购查询
    # ------------------------------------------------------------------

    def query_new_purchase_limit(self, account_id: str = ""):
        """查询新股申购额度 → _trader.query_new_purchase_limit()"""
        account = self._resolve_account(account_id)
        return self._trader.query_new_purchase_limit(account)

    def query_ipo_data(self):
        """查询 IPO 新股日历数据 → _trader.query_ipo_data()"""
        return self._trader.query_ipo_data()

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    def get_account_status(self, account_id: str = ""):
        """获取账户连接状态（本地判断）。"""
        try:
            return {"connected": self._trader is not None}
        except Exception:  # noqa: BLE001 — 状态查询不能反把接口弄挂，一律视为未连接
            return {"connected": False}

    def query_account_status(self):
        """查询账户状态 → _trader.query_account_status()"""
        return self._trader.query_account_status()

    def query_secu_account(self, account_id: str = ""):
        """查询证券子账户 → _trader.query_secu_account()"""
        account = self._resolve_account(account_id)
        return self._trader.query_secu_account(account)

    def query_account_infos(self):
        """查询所有已注册账户的信息 → _trader.query_account_infos()"""
        return self._trader.query_account_infos()

    # ------------------------------------------------------------------
    # COM 查询（期权/期货）
    # ------------------------------------------------------------------

    def query_com_fund(self, account_id: str = ""):
        """查询 COM 账户资金 → _trader.query_com_fund()"""
        account = self._resolve_account(account_id)
        return self._trader.query_com_fund(account)

    def query_com_position(self, account_id: str = ""):
        """查询 COM 账户持仓 → _trader.query_com_position()"""
        account = self._resolve_account(account_id)
        return self._trader.query_com_position(account)

    # ------------------------------------------------------------------
    # 数据导出与外部同步（对齐 xttrader 真实签名）
    # ------------------------------------------------------------------

    @_audited("导出交易数据")
    def export_data(
        self,
        result_path: str,
        data_type: str,
        start_time: str = "",
        end_time: str = "",
        user_param: str = "",
        account_id: str = "",
    ):
        """导出交易数据 → _trader.export_data()"""
        account = self._resolve_account(account_id)
        return self._trader.export_data(
            account,
            result_path,
            data_type,
            start_time,
            end_time,
            user_param,
        )

    def query_data(
        self,
        result_path: str,
        data_type: str,
        start_time: str = "",
        end_time: str = "",
        user_param: str = "",
        account_id: str = "",
    ):
        """查询导出数据 → _trader.query_data()"""
        account = self._resolve_account(account_id)
        return self._trader.query_data(
            account,
            result_path,
            data_type,
            start_time,
            end_time,
            user_param,
        )

    @_audited("外部同步交易记录")
    def sync_transaction_from_external(
        self, operation: str, data_type: str, deal_list: list, account_id: str = ""
    ):
        """从外部同步交易记录 → _trader.sync_transaction_from_external()"""
        account = self._resolve_account(account_id)
        return self._trader.sync_transaction_from_external(
            operation,
            data_type,
            account,
            deal_list,
        )

    # ------------------------------------------------------------------
    # 算法交易
    # ------------------------------------------------------------------

    @_audited("算法下单")
    def smart_algo_order_async(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        algo_name: str,
        start_time: str = "",
        end_time: str = "",
        algo_param: dict | None = None,
        strategy_name: str = "",
        order_remark: str = "",
        account_id: str = "",
    ):
        """算法交易异步下单 → _trader.smart_algo_order_async()

        Args:
            stock_code: 证券代码，如 ``"600000.SH"``。
            order_type: 委托类型，23 买入 / 24 卖出。
            order_volume: 委托数量。
            price_type: 报价类型。
            price: 委托价格，市价类报价传 0。
            algo_name: 算法名称，可用 ``get_smart_algo_param()`` 查询。
            start_time: 算法执行起始时间，格式 ``"HH:MM:SS"``（如 ``"09:30:00"``）。
                xtquant 会与**当天日期**组合成时间戳，因此只能指定当日时段，
                传带日期的字符串会被判为格式错误。
            end_time: 算法执行截止时间，同样为 ``"HH:MM:SS"``，必须晚于 start_time，
                否则 xtquant 抛 ``Exception("起始时间小于截止时间")``。
            algo_param: 算法参数字典。
            strategy_name: 策略名称。
            order_remark: 委托备注。
            account_id: 资金账号，空则用默认账户。

        Returns:
            异步请求序号，最终结果由 ``on_smart_algo_order_async_response`` 推送。
        """
        account = self._resolve_account(account_id)
        return self._trader.smart_algo_order_async(
            account,
            stock_code,
            order_type,
            order_volume,
            price_type,
            price,
            strategy_name,
            order_remark,
            algo_name,
            start_time,
            end_time,
            algo_param or {},
        )

    @_audited("撤销算法任务")
    def cancel_smart_algo_task_async(self, task_id: int, account_id: str = ""):
        """撤销算法交易任务 → _trader.cancel_smart_algo_task_async()"""
        account = self._resolve_account(account_id)
        return self._trader.cancel_smart_algo_task_async(account, task_id)

    def query_smart_algo_task(self, account_id: str = ""):
        """查询当日算法交易任务 → _trader.query_smart_algo_task()"""
        account = self._resolve_account(account_id)
        return self._trader.query_smart_algo_task(account)

    def get_smart_algo_param(self, algo_name_list: list[str]):
        """查询算法参数说明 → _trader.get_smart_algo_param()"""
        return self._trader.get_smart_algo_param(algo_name_list)
