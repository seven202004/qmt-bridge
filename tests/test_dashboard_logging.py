"""仪表盘日志测试。

仪表盘原先的失败只弹 ``st.error``，浏览器一关就再无痕迹；这里守住两件事：
失败同时进日志、日志配置与 API 服务共用同一套开关。

不依赖 streamlit：注入替身模块即可（与 xtquant 测试同一套路）。
"""

import importlib
import logging
import pathlib
import sys
import types

from qmt_bridge.server.logging_setup import _HANDLER_MARK
from tests.test_logging import _collect_logs

DASHBOARD_DIR = pathlib.Path(__file__).resolve().parents[1] / "dashboard"


class _FakeSt:
    """替身 streamlit：只记录界面调用，够 _sidebar 用。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.sidebar = types.SimpleNamespace(
            error=lambda *args: self.calls.append(("sidebar.error", args))
        )

    def error(self, *args):
        self.calls.append(("error", args))

    def warning(self, *args):
        self.calls.append(("warning", args))


def _load_sidebar(monkeypatch, tmp_path, log_file: str = ""):
    """注入替身 streamlit 并加载 dashboard/_sidebar.py。"""
    fake_st = _FakeSt()
    monkeypatch.setitem(sys.modules, "streamlit", fake_st)  # type: ignore[arg-type]
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))
    monkeypatch.setenv("QMT_BRIDGE_LOG_FILE", log_file)
    importlib.import_module("_sidebar")  # 首次导入会执行模块级日志配置
    return importlib.reload(importlib.import_module("_sidebar")), fake_st


def test_report_error_writes_log_with_traceback_and_shows_message(monkeypatch, tmp_path):
    """失败必须同时落到日志（带堆栈）和界面，不能只弹个框。"""
    sidebar, fake_st = _load_sidebar(monkeypatch, tmp_path)

    with _collect_logs("qmt_bridge.dashboard") as records:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            sidebar.report_error("查询失败", exc)

    record = records[0]
    assert record.levelno == logging.ERROR
    assert "查询失败: boom" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is ValueError
    assert ("error", ("查询失败: boom",)) in fake_st.calls


def test_report_error_can_degrade_to_warning(monkeypatch, tmp_path):
    """非致命失败仍按 warning 展示，但照样进日志。"""
    sidebar, fake_st = _load_sidebar(monkeypatch, tmp_path)

    with _collect_logs("qmt_bridge.dashboard") as records:
        sidebar.report_error("获取概览信息失败", RuntimeError("down"), as_warning=True)

    assert ("warning", ("获取概览信息失败: down",)) in fake_st.calls
    assert records[0].levelno == logging.ERROR


def test_dashboard_logging_reuses_server_switches(monkeypatch, tmp_path):
    """QMT_BRIDGE_LOG_FILE 一设，面板日志就落盘（与 API 服务同一套开关/格式）。"""
    log_file = tmp_path / "dashboard.log"
    _load_sidebar(monkeypatch, tmp_path, log_file=str(log_file))

    logger = logging.getLogger("qmt_bridge")
    assert any(getattr(handler, _HANDLER_MARK, False) for handler in logger.handlers)
    logger.info("面板日志落盘")
    for handler in logger.handlers:
        handler.flush()

    assert "面板日志落盘" in log_file.read_text(encoding="utf-8")


def test_dashboard_logging_defaults_to_console_only(monkeypatch, tmp_path):
    """默认不写文件：UI 进程不该在用户不知情时往磁盘塞日志。"""
    _load_sidebar(monkeypatch, tmp_path)

    handlers = logging.getLogger("qmt_bridge").handlers
    assert not [h for h in handlers if isinstance(h, logging.FileHandler)]
