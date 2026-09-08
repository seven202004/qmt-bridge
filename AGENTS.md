# Repository Guidelines

## 项目结构与模块组织

QMT Bridge 将 miniQMT (xtquant) 的行情与交易能力通过 HTTP/WebSocket 暴露为 API。Python 3.10+，核心源码位于 `src/qmt_bridge/`：

- `server/` — FastAPI 服务端：`app.py`、`cli.py`、`config.py`、`security.py`、`scheduler.py`、`downloader.py`；子目录 `routers/`（REST 端点）、`ws/`（WebSocket）、`trading/`（交易）、`notify/`（通知推送）
- `client/` — 跨平台零依赖客户端，采用 Mixin 组合：`base.py` 提供 HTTP 传输，各领域模块以 `<领域>Mixin` 命名，最终拼合为 `QMTClient`
- `scripts/` — 独立可运行脚本（`download_all.py`、`diagnose_bson.py`）
- `tests/` — pytest 测试（`conftest.py` 存放共享 fixtures）
- `dashboard/` — Streamlit 仪表盘；`docs/` — MkDocs 文档

## 构建、测试与开发命令

```bash
just install-all    # pip install -e ".[full,docs,dashboard]"  安装全部依赖
just serve          # 启动 API 服务 (qmt-server)
just scheduler      # 启动定时下载调度器 (qmt-scheduler)
just download-all   # 下载 A 股历史行情 + 财务数据
just test           # python -m pytest tests/
just check          # ruff format + ruff check (src/ tests/)
just typecheck      # python -m mypy src/qmt_bridge/
just build          # 构建 wheel/sdist
just docs           # mkdocs serve -a 127.0.0.1:8001
just dashboard      # streamlit run dashboard/app.py
```

## 编码与命名规范

- Python 3.10+；注释与 docstring 使用中文，与现有代码一致
- 使用 **ruff** 做格式化与 lint、**mypy** 做类型检查；提交前运行 `just check` 与 `just typecheck`
- 服务端依赖声明在 `pyproject.toml` 的 `[project.optional-dependencies]`；`client/` 保持零依赖（仅 stdlib）
- 客户端模块以 `<领域>Mixin` 命名（如 `MarketMixin`），方法通过 `self._get/_post` 调用服务端
- 版本号单一来源：`src/qmt_bridge/_version.py`，发布前手动更新
- 日志统一使用 `logging` 模块，避免残留 `print` 调试输出

## 测试指南

- 框架：pytest；夹具放 `tests/conftest.py`
- 文件命名 `test_*.py`，用例函数 `test_*`
- 运行：`just test` 或 `python -m pytest tests/ -v`
- CI（`.github/workflows/ci.yml`）针对 Python 3.10–3.13 矩阵安装 `.[server,ws]` 并执行 pytest

## 提交与 PR 指南

- 提交信息格式：`type: 中文描述 (vX.Y.Z)`，`type` 取 `feat`/`fix`/`refactor`/`chore`，末尾附当前版本号
- PR 需描述改动并关联 issue，CI 通过后方可合并
- 废弃代码不直接删除：移入 `.trash/` 并加时间戳后缀 `filename.YYYYMMDD_HHMMSS.py`
- 新增 `scripts/` 脚本时同步更新 `justfile` 中的快捷命令

## 安全与配置

- 环境变量 / `.env` 注入配置，参考 `.env.example`；交易端点默认强制 API Key 认证
- 新增 HTTP 端点时在 `server/` 中定义并同步客户端 Mixin 与 `docs/` 文档
