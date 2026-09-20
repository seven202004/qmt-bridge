"""``setup_logging(console=False)`` 的行为：只挂轮转文件 handler。

用于被父进程（如 qmtmix）以文件重定向拉起时：重定向的 stdout/stderr 落不进
``RotatingFileHandler``，留着只会多出一份无限增长的日志副本。
"""

import logging
import logging.handlers

from qmt_bridge.server.logging_setup import _HANDLER_MARK, setup_logging


def _own_handlers(logger) -> list:
    return [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]


def test_console_off_keeps_only_rotating_file_handler(tmp_path):
    log_file = tmp_path / "bridge.log"

    logger = setup_logging(level="info", log_file=str(log_file), console=False)
    handlers = _own_handlers(logger)

    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)

    logger.info("console off still writes to file")
    assert "console off still writes to file" in log_file.read_text(encoding="utf-8")


def test_console_on_adds_stream_handler(tmp_path):
    """默认（console=True）保持既有行为：控制台 + 文件各一个 handler。"""
    logger = setup_logging(level="info", log_file=str(tmp_path / "bridge.log"))
    kinds = [type(h).__name__ for h in _own_handlers(logger)]

    assert kinds == ["StreamHandler", "RotatingFileHandler"]
