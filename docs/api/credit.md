# 融资融券

信用交易下单、持仓查询、负债查询、可融资/融券标的等。

!!! note "需要认证"
    融资融券方法需要在创建客户端时传入 `api_key` 参数。

!!! note "需要信用账户"
    所有两融操作（`credit_order` / `query_credit_*`）都以 **信用账户（`account_type='CREDIT'`）** 构造
    `StockAccount`（`_resolve_account` 已按此处理），`account_id` 应传**信用资金账号**，而非普通证券账号；
    传错账号会影响信用查询/两融下单的正确路由。

## 持仓与资产

- **持仓** `query_credit_positions`：底层为 `XtQuantTrader.query_stock_positions(...)`，在 CREDIT 类型账户下返回**担保品持仓 + 融资/融券持仓**（`XtPosition` 列表）。
- **资产** `query_credit_detail`：返回 `XtCreditDetail`，含 `m_dBalance`(总资产)、`m_dTotalDebt`(总负债)、`m_dAvailable`(可用资金)、`m_dFrozenCash`(冻结资金)、`m_dMarketValue`(持仓市值)、`m_dStockValue`(股票市值) 等信用资产/负债/保证金维度。

::: qmt_bridge.client.credit.CreditMixin
    options:
      show_root_heading: false
      heading_level: 2
