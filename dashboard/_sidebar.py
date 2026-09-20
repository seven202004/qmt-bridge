"""共享侧边栏连接配置 — 所有页面 import 此模块以渲染连接 UI。

同时是本仪表盘的日志入口：所有页面都会 import 本模块，日志配置放在这里
只需要配一次。仪表盘原先的失败只弹 ``st.error``，浏览器关掉就再也查不到，
``report_error()`` 让同一件事同时落到日志里（带堆栈）。
"""

import json
import logging
import os
from pathlib import Path

import streamlit as st

from qmt_bridge.server.logging_setup import setup_logging

# 与 API 服务共用同一套日志开关与格式：QMT_BRIDGE_LOG_LEVEL / QMT_BRIDGE_LOG_FILE
# 不设 LOG_FILE 时只输出到控制台（streamlit run 的终端），不偷偷写文件。
setup_logging(
    level=os.environ.get("QMT_BRIDGE_LOG_LEVEL", "info"),
    log_file=os.environ.get("QMT_BRIDGE_LOG_FILE", ""),
)

logger = logging.getLogger("qmt_bridge.dashboard")

_CONFIG_PATH = Path(__file__).parent / ".dashboard_config.json"


def report_error(message: str, exc: Exception, *, as_warning: bool = False) -> None:
    """把页面里的失败同时写进日志并弹给用户。

    仪表盘的每个 ``except`` 原先只做 ``st.error``，出问题事后无从追查；
    统一走这里：日志里是一条带堆栈的 ERROR（有 request_id 时还能和服务端对上），
    界面上仍是用户看得懂的一句话。

    Args:
        message: 给用户看的失败摘要，如 ``"查询失败"``。
        exc: 捕获到的异常。
        as_warning: 界面按 warning 展示（非致命失败，如首页概览拉取不到）。
    """
    # 用 exc_info=exc 而不是 True：这里不在 except 块内，显式带上传入的异常堆栈
    logger.error("%s: %s", message, exc, exc_info=exc)
    (st.warning if as_warning else st.error)(f"{message}: {exc}")


def _load_config() -> dict:
    """从本地文件加载上次保存的连接配置。"""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(host: str, port: int, api_key: str) -> None:
    """将连接配置保存到本地文件。"""
    _CONFIG_PATH.write_text(
        json.dumps({"host": host, "port": port, "api_key": api_key}, ensure_ascii=False),
        encoding="utf-8",
    )


def render_sidebar():
    """渲染侧边栏连接配置并返回 client（可能为 None）。"""
    cfg = _load_config()

    st.sidebar.title("QMT Bridge")
    host = st.sidebar.text_input("服务地址", value=cfg.get("host", "127.0.0.1"), key="_sb_host")
    port = st.sidebar.number_input(
        "端口", value=cfg.get("port", 8000), min_value=1, max_value=65535, step=1, key="_sb_port"
    )
    api_key = st.sidebar.text_input(
        "API Key（交易功能需要）", value=cfg.get("api_key", ""), type="password", key="_sb_api_key"
    )

    if st.sidebar.button("连接 / 刷新", key="_sb_connect"):
        try:
            from qmt_bridge import QMTClient

            client = QMTClient(host, int(port), api_key=api_key)
            health = client.health_check()
            st.session_state["client"] = client
            st.session_state["connected"] = True
            st.session_state["health"] = health
            _save_config(host, int(port), api_key)
            st.sidebar.success("连接成功")
        except Exception as e:
            st.session_state["connected"] = False
            # 堆栈由 exception() 自带，消息里不必再拼一次异常文本
            logger.exception("连接失败")
            st.sidebar.error(f"连接失败: {e}")

    if st.session_state.get("connected"):
        st.sidebar.caption("🟢 已连接")
    else:
        st.sidebar.caption("🔴 未连接 — 请点击「连接 / 刷新」")

    return st.session_state.get("client")


def require_client():
    """渲染侧边栏并要求已连接。未连接时调用 st.stop()。"""
    client = render_sidebar()
    if not st.session_state.get("connected") or client is None:
        st.warning("请先在侧边栏配置连接。")
        st.stop()
    return client
