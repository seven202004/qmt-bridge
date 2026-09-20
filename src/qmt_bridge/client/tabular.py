"""TabularMixin — 表格数据（Metatable）客户端方法。

封装了 xtquant Metatable 系统的查询接口。Metatable 是 xtquant 提供的
结构化数据查询系统，可以访问各种命名数据表（如财务数据表等）。

底层对应 xtquant 的 ``xtdata.get_financial_data()``、
``xtdata.get_metatable_list()``、``xtdata.get_tabular_formula()`` 等函数。
"""
from .base import BaseClient


class TabularMixin(BaseClient):
    """表格数据客户端方法集合，对应 /api/tabular/* 端点。"""

    def get_tabular_data(
        self,
        table_name: str,
        stocks: list[str] | None = None,
        start_time: str = "",
        end_time: str = "",
    ) -> dict:
        """从指定数据表中获取数据。

        底层调用 ``xtdata.get_financial_data()``。Metatable 是 xtquant 提供的
        结构化数据查询系统，可以访问各种命名数据表。

        Args:
            table_name: 数据表名称，通常由 ``list_tables()`` 获取可用表名
            stocks: 股票代码列表，为空则不按股票筛选
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            查询结果（以股票代码为键的字典）
        """
        resp = self._get("/api/tabular/data", {
            "table_name": table_name,
            "stocks": ",".join(stocks) if stocks else "",
            "start_time": start_time,
            "end_time": end_time,
        })
        return resp.get("data", {})

    def list_tables(self) -> dict:
        """获取可用的数据表列表。

        底层调用 ``xtdata.get_metatable_list()``。

        Returns:
            数据表映射 ``{table_code: table_name}``
        """
        resp = self._get("/api/tabular/tables")
        return resp.get("tables", {})

    def get_tabular_formula(
        self,
        fields: list[str],
        stocks: list[str] | None = None,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
        count: int = -1,
        dividend_type: str = "none",
    ) -> dict:
        """按字段列表查询公式表格数据。

        底层调用 ``xtdata.get_tabular_formula(codes, fields, period, ...)``。
        字段名格式为 ``表名.字段名``（如 ``Balance.total_assets``），表名即公式名。

        Args:
            fields: 字段名列表，如 ``["Balance.total_assets"]``
            stocks: 股票代码列表
            period: K 线周期
            start_time: 开始时间
            end_time: 结束时间
            count: 返回条数，-1 表示全部
            dividend_type: 除权类型

        Returns:
            公式表格数据
        """
        resp = self._get("/api/tabular/formula", {
            "fields": ",".join(fields),
            "stocks": ",".join(stocks) if stocks else "",
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
        })
        return resp.get("data", {})
