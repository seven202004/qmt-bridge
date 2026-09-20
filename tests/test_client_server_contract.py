"""客户端 ↔ 服务端接口契约测试。

守着两件曾经真出过问题的事：

1. 客户端方法打的服务端路径必须真实存在（写错路径 = 调用方永远 404）。
2. 端点模块里声明的每个路由都必须在 ``create_app`` 里注册过 ——
   ``/ws/l2_thousand`` 曾实现、文档化、客户端可调，却漏了 ``include_router``，
  调用方只会拿到连接被拒，而整套测试全绿。

还守着文档与代码的一致性（仓库约定：新增端点必须同步文档）：
``/api/bond/*``、``/api/credit/orders``、``/api/notify/test`` 与 5 个
``QMT_BRIDGE_SCHEDULER_*`` 都曾只存在于代码里。

服务端需要 xtquant，本机通常没有：注入替身模块即可（路由注册不触碰 xtdata）。
"""

import ast
import pathlib
import re
import sys
import types

import pytest

from qmt_bridge.server.config import Settings

REPO = pathlib.Path(__file__).resolve().parents[1]
CLIENT_DIR = REPO / "src" / "qmt_bridge" / "client"
SERVER_DIR = REPO / "src" / "qmt_bridge" / "server"
DOCS_GLOB = "docs/**/*.md"
# 审计报告按设计会列出"已删除的端点"，不参与文档覆盖检查
DOCS_EXCLUDE = {"xtquant-api-audit.md"}

HTTP_VERBS = {"_get", "_post", "_delete"}
DECORATOR_RE = re.compile(r'@router\.(get|post|delete|put|websocket)\(\s*"([^"]+)"')
PREFIX_RE = re.compile(r'APIRouter\([^)]*prefix="([^"]*)"')
DOC_PATH_RE = re.compile(r"`(/(?:api|ws)/[A-Za-z0-9_/{}\-]*)`")
ENV_RE = re.compile(r"\b(QMT_BRIDGE_[A-Z0-9_]+)\b")


@pytest.fixture
def app_paths(monkeypatch):
    """构建完整应用（交易 + 通知全开），返回 (HTTP 路径集合, WS 路径集合)。"""
    for name in (
        "xtquant",
        "xtquant.xtdata",
        "xtquant.xtbson",
        "xtquant.xttrader",
        "xtquant.xttype",
        "xtquant.qmttools",
        "xtquant.qmttools.functions",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    xtquant = sys.modules["xtquant"]
    for attr in ("xtdata", "xtbson", "xttrader", "xttype", "qmttools"):
        setattr(xtquant, attr, sys.modules[f"xtquant.{attr}"])
    sys.modules["xtquant.qmttools"].functions = sys.modules[
        "xtquant.qmttools.functions"
    ]
    sys.modules["xtquant.qmttools.functions"].call_formula_batch = lambda *a, **k: {}

    from qmt_bridge.server.app import create_app

    app = create_app(Settings(trading_enabled=True, notify_enabled=True))

    http_paths = set(app.openapi()["paths"])
    ws_paths = {path for path in _walk_ws_paths(app.routes)}
    return http_paths, ws_paths


def _walk_ws_paths(routes):
    """收集 WebSocket 路径。

    starlette 把 include_router 存成包装对象（路径在 original_router 里），因此递归；
    若某个版本直接把路径摊平，第一个分支就命中。
    """
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            if path.startswith("/ws/"):
                yield path
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk_ws_paths(getattr(inner, "routes", []))


def _client_http_paths() -> set[str]:
    """AST 提取客户端调用过的 HTTP 路径。"""
    found: set[str] = set()
    for module in sorted(CLIENT_DIR.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr in HTTP_VERBS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
    return found


def _client_ws_paths() -> set[str]:
    """AST 提取 client/websocket.py 里拼出的 WS 路径（f"{self.ws_url}/ws/xxx"）。"""
    tree = ast.parse((CLIENT_DIR / "websocket.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        text = "".join(
            part.value for part in node.values if isinstance(part, ast.Constant)
        )
        if "/ws/" in text:
            found.add("/ws/" + text.split("/ws/", 1)[1].strip("/"))
    return found


def test_every_client_path_exists_on_server(app_paths):
    """客户端每个方法都得打到真实存在的端点上。"""
    http_paths, _ = app_paths
    missing = sorted(_client_http_paths() - http_paths)
    assert missing == [], f"客户端调用了服务端不存在的路径: {missing}"


def test_every_client_websocket_path_exists_on_server(app_paths):
    """客户端订阅的 WS 端点必须都注册过（/ws/l2_thousand 曾整条漏掉）。"""
    _, ws_paths = app_paths
    client_paths = _client_ws_paths()
    assert client_paths, "没能从客户端解析出任何 WS 路径，检查解析逻辑"
    missing = sorted(client_paths - ws_paths)
    assert missing == [], f"客户端连了服务端没注册的 WS 端点: {missing}"


def test_every_declared_endpoint_is_registered(app_paths):
    """端点模块里声明的路由必须都在 create_app 里注册过（漏 include_router = 死端点）。"""
    http_paths, ws_paths = app_paths
    modules = [
        *sorted((SERVER_DIR / "routers").glob("*.py")),
        *sorted((SERVER_DIR / "ws").glob("*.py")),
        SERVER_DIR / "notify" / "base.py",
    ]

    declared: list[tuple[str, str]] = []
    for module in modules:
        if module.name == "__init__.py":
            continue
        text = module.read_text(encoding="utf-8")
        match = PREFIX_RE.search(text)
        prefix = match.group(1) if match else ""
        for verb, sub_path in DECORATOR_RE.findall(text):
            declared.append((verb, prefix + sub_path))

    assert declared, "没扫到任何端点声明，检查解析逻辑"
    unwired = [
        f"{verb.upper()} {path}"
        for verb, path in declared
        if path not in (ws_paths if verb == "websocket" else http_paths)
    ]
    assert unwired == [], f"以下端点在 create_app 里没注册: {unwired}"


def test_every_endpoint_is_documented(app_paths):
    """每个已注册端点都要在 docs/rest-api.md 或 docs/api/*.md 里出现。"""
    http_paths, ws_paths = app_paths
    documented = {
        _normalize(path)
        for doc in _doc_files()
        for path in DOC_PATH_RE.findall(doc.read_text(encoding="utf-8"))
    }
    missing = sorted(
        p for p in http_paths | ws_paths if _normalize(p) not in documented
    )
    assert missing == [], (
        f"以下端点没有出现在文档里（补进 docs/rest-api.md 的对应小节）: {missing}"
    )


def test_every_env_var_is_documented():
    """代码读取的 QMT_BRIDGE_* 必须在配置文档或 .env.example 里有说明。"""
    used = {
        var
        for source in [
            *(REPO / "src").rglob("*.py"),
            *(REPO / "dashboard").rglob("*.py"),
        ]
        for var in ENV_RE.findall(source.read_text(encoding="utf-8"))
    }
    documented = {
        var
        for source in [
            REPO / "docs" / "configuration.md",
            REPO / ".env.example",
        ]
        for var in ENV_RE.findall(source.read_text(encoding="utf-8"))
    }
    assert used, "没扫到任何环境变量，检查解析逻辑"
    missing = sorted(used - documented)
    assert missing == [], (
        f"以下环境变量没写进 docs/configuration.md 或 .env.example: {missing}"
    )


def _doc_files() -> list[pathlib.Path]:
    """所有面向用户的文档（排除按设计会列出已删端点的审计报告）。"""
    return [
        doc
        for doc in [*REPO.glob(DOCS_GLOB), REPO / "README.md"]
        if doc.name not in DOCS_EXCLUDE
    ]


def _normalize(path: str) -> str:
    """把 ``{order_id}`` 之类的路径参数统一成 ``{}`` 再比对。"""
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip("/"))
