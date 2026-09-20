"""xtdata 调用签名契约测试。

校验路由处理器把请求模型正确映射到真实 xtquant 函数：用记录器替换 xtdata 函数，
再用 ``inspect.signature().bind()`` 证明调用参数能被真实签名接受。
这能拦住“凭记忆写接口名/参数名”导致的 AttributeError 与 TypeError。

未安装 xtquant 时整个模块跳过（CI 不装 xtquant）。
"""

import inspect

import pytest

pytest.importorskip("xtquant", reason="需要真实 xtquant 环境")

from xtquant import xtdata

from qmt_bridge.server import models
from qmt_bridge.server.routers import (
    download,
    formula,
    sector,
    tabular,
    tick,
)


def _patch(monkeypatch, module, name, state, ret=None):
    """把 module.name 换成记录器，并保留原函数用于签名校验。"""
    state["originals"][name] = getattr(module, name)

    def _recorder(*args, _name=name, _ret=ret, **kwargs):
        state["calls"].append((_name, args, kwargs))
        return {} if _ret is None else _ret

    monkeypatch.setattr(module, name, _recorder)


def _assert_signatures_match(state):
    problems = []
    for name, args, kwargs in state["calls"]:
        try:
            inspect.signature(state["originals"][name]).bind(*args, **kwargs)
        except TypeError as exc:
            problems.append(f"{name}(*{args}, **{kwargs}): {exc}")
    assert problems == [], f"与真实 xtquant 签名不符: {problems}"


@pytest.fixture
def state():
    return {"calls": [], "originals": {}}


def test_formula_endpoints_signatures(monkeypatch, state):
    """公式端点：批量调用/生成指数/创建/导入必须匹配真实签名。"""
    _patch(monkeypatch, formula, "_call_formula_batch", state)
    _patch(monkeypatch, xtdata, "call_formula", state)
    _patch(monkeypatch, xtdata, "create_formula", state)
    _patch(monkeypatch, xtdata, "generate_index_data", state)
    _patch(monkeypatch, xtdata, "import_formula", state)

    formula.call_formula(
        models.CallFormulaRequest(formula_name="MA", stock_code="000001.SZ")
    )
    formula.call_formula_batch(
        models.CallFormulaBatchRequest(formula_names=["MA"], stock_codes=["000001.SZ"])
    )
    formula.create_formula(
        models.CreateFormulaRequest(formula_name="MA", formula_content="c")
    )
    formula.generate_index_data(
        models.GenerateIndexDataRequest(formula_name="MyIndex", stocks=["000001.SZ"])
    )
    formula.import_formula(
        models.ImportFormulaRequest(formula_name="MA", formula_file="a.rzrk")
    )

    _assert_signatures_match(state)
    assert len(state["calls"]) == 5


def test_tabular_and_tick_endpoints_signatures(monkeypatch, state):
    """表格与 L2 端点：表名列表、表格公式、千档队列、经纪商队列、委托排名。"""
    _patch(
        monkeypatch, xtdata, "get_metatable_list", state, ret={"Balance": "资产负债表"}
    )
    _patch(monkeypatch, xtdata, "get_tabular_formula", state)
    _patch(monkeypatch, xtdata, "get_l2thousand_queue", state)
    _patch(monkeypatch, xtdata, "get_broker_queue_data", state)
    _patch(monkeypatch, xtdata, "get_order_rank", state)

    assert tabular.list_tables() == {"tables": {"Balance": "资产负债表"}}
    tabular.get_tabular_formula(fields="Balance.total_assets", stocks="000001.SZ")
    tick.get_l2_thousand_queue(stock="000001.SZ", gear_num=-1, price="10.5-10.8")
    tick.get_broker_queue(stocks="00700.HK,00701.HK")
    tick.get_order_rank(
        stock="000001.SZ",
        order_time="20230101",
        order_type="buy",
        order_price=1.5,
        order_volume=100,
        order_left_volume=0,
    )

    _assert_signatures_match(state)
    assert len(state["calls"]) == 5


def test_sector_and_download_endpoints_signatures(monkeypatch, state):
    """板块与下载端点：文件夹/板块父节点参数、ST 与表格数据下载。"""
    _patch(monkeypatch, xtdata, "create_sector_folder", state)
    _patch(monkeypatch, xtdata, "create_sector", state)
    _patch(monkeypatch, xtdata, "download_his_st_data", state)
    _patch(monkeypatch, xtdata, "download_tabular_data", state)

    sector.create_sector_folder(models.CreateSectorFolderRequest(folder_name="f"))
    sector.create_sector(models.CreateSectorRequest(sector_name="s"))
    download.download_his_st_data()
    download.download_tabular_data(
        models.TabularDataDownloadRequest(stocks=["000001.SZ"], period="1d")
    )

    _assert_signatures_match(state)
    assert len(state["calls"]) == 4
