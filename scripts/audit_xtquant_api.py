"""审计 qmt-bridge 对 xtquant 原生 API 的封装是否与真实签名一致。

需要已安装 ``xtquant``（在装有 MiniQMT 的 Windows 机器上运行）。
检查三类问题，任一非空则退出码为 1：

1. 属性不存在 —— 源码里引用了真实 xtquant 中不存在的函数/方法
2. 参数不匹配 —— 调用的位置参数过多、必填参数缺失或关键字参数签名里没有
3. 覆盖统计   —— 打印尚未封装的公共接口清单，便于人工判断是否值得补

其中第 1 类同时覆盖 ``xtdata.get_client().xxx`` 这种低层客户端调用。该客户端的
实现类取决于连接方式：本地直连 miniQMT 时是 ``xtquant.datacenter.IPythonApiClient``，
经 xtdatacenter 连接时是 ``xtquant.xtdatacenter.RPCClient``。两者的方法名都与模块级
函数不同（例如客户端侧 ``get_etf_info(code)`` 取单只，模块级 ``get_etf_info()`` 取全部），
因此本脚本按「两个类取并集」判定属性是否存在。

用法::

    python scripts/audit_xtquant_api.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

try:
    from xtquant import xtdata
    from xtquant.datacenter import IPythonApiClient
    from xtquant.xtdatacenter import RPCClient
    from xtquant.xttrader import XtQuantTrader
except ImportError:  # pragma: no cover - 仅在缺少 xtquant 时触发
    sys.exit("需要安装 xtquant（MiniQMT 环境）后才能运行本脚本")

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _signature(node: ast.FunctionDef, drop_self: bool) -> dict:
    """从 AST 函数节点提取参数信息。"""
    args = node.args
    positional = [a.arg for a in args.posonlyargs + args.args]
    if drop_self and positional and positional[0] == "self":
        positional = positional[1:]
    return {
        "pos": positional,
        "required": len(positional) - len(args.defaults),
        "kwonly": [a.arg for a in args.kwonlyargs],
        "varargs": args.vararg is not None,
        "varkw": args.kwarg is not None,
    }


def _module_functions(module) -> dict[str, dict]:
    """取模块的公开函数签名（含通过 import * 引入的名字）。"""
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    out = {
        n.name: _signature(n, drop_self=False)
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    }
    return out


def _method_functions(cls) -> dict[str, dict]:
    """取类的公开方法签名。"""
    import inspect

    out = {}
    for name, member in vars(cls).items():
        if name.startswith("_") or not callable(member):
            continue
        try:
            params = list(inspect.signature(member).parameters.values())
        except (TypeError, ValueError):
            continue
        params = params[1:] if params and params[0].name == "self" else params
        pos = [
            p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        out[name] = {
            "pos": [p.name for p in pos],
            "required": len([p for p in pos if p.default is p.empty]),
            "kwonly": [p.name for p in params if p.kind == p.KEYWORD_ONLY],
            "varargs": any(p.kind == p.VAR_POSITIONAL for p in params),
            "varkw": any(p.kind == p.VAR_KEYWORD for p in params),
        }
    return out


def _iter_calls():
    """遍历源码，产出 (文件, 行号, 基对象, 属性名, Call 节点)。

    基对象取值：``xtdata``（模块级函数）、``xtdata_client``（get_client() 返回的
    RPCClient）、``trader``（XtQuantTrader 实例）。
    """
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target, call = node.func, node
            elif isinstance(node, ast.Attribute):
                target, call = node, None
            else:
                continue
            if not isinstance(target, ast.Attribute):
                continue
            kind = _classify(target.value)
            if kind:
                yield path, target.lineno, kind, target.attr, call


def _client_chain(node) -> bool:
    """判断节点是否为 ``xtdata.get_client()``。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_client"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "xtdata"
    )


def _classify(base) -> str | None:
    """判断属性访问的基对象属于哪一类。"""
    if isinstance(base, ast.Name) and base.id == "xtdata":
        return "xtdata"
    if _client_chain(base):
        return "xtdata_client"
    if (
        isinstance(base, ast.Attribute)
        and base.attr == "_trader"
        and isinstance(base.value, ast.Name)
        and base.value.id == "self"
    ):
        return "trader"
    return None


def main() -> int:
    xtdata_sigs = _module_functions(xtdata)
    trader_sigs = _method_functions(XtQuantTrader)
    # 两种连接模式（本地直连 / xtdatacenter）使用不同的客户端类，取并集判定
    client_sigs = _method_functions(RPCClient) | _method_functions(IPythonApiClient)
    sigs = {"xtdata": xtdata_sigs, "trader": trader_sigs, "xtdata_client": client_sigs}
    owners = {
        "xtdata": xtdata,
        "trader": XtQuantTrader,
        "xtdata_client": (RPCClient, IPythonApiClient),
    }

    missing: list[str] = []
    mismatch: list[str] = []
    seen: set[tuple[str, str]] = set()

    for path, lineno, kind, name, call in _iter_calls():
        where = f"{path.relative_to(SRC.parent)}:{lineno}"
        owner = owners[kind]
        exists = (
            any(hasattr(o, name) for o in owner)
            if isinstance(owner, tuple)
            else hasattr(owner, name)
        )
        if not exists:
            if (where, name) not in seen:
                seen.add((where, name))
                missing.append(f"{where}  {kind}.{name} 不存在")
            continue
        if call is None:
            continue
        sig = sigs[kind].get(name)
        if sig is None:
            continue
        npos = len(call.args)
        nkw = [k.arg for k in call.keywords if k.arg]
        problems = []
        if npos > len(sig["pos"]) and not sig["varargs"]:
            problems.append(f"位置参数过多 {npos}>{len(sig['pos'])}")
        if npos + len(nkw) < sig["required"]:
            problems.append(f"缺少必填参数 {sig['pos'][npos : sig['required']]}")
        unknown = [
            k
            for k in nkw
            if k not in sig["pos"] and k not in sig["kwonly"] and not sig["varkw"]
        ]
        if unknown:
            problems.append(f"未知关键字参数 {unknown}")
        if problems:
            key = (where, name)
            if key not in seen:
                seen.add(key)
                mismatch.append(f"{where}  {kind}.{name}  {'; '.join(problems)}")

    source = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    used_xtdata = set(re.findall(r"xtdata\.([A-Za-z_0-9]+)", source))
    used_trader = set(re.findall(r"_trader\.([A-Za-z_0-9]+)", source))

    print(f"[1/3] 属性不存在: {len(missing)}")
    for line in missing:
        print("      ", line)
    print(f"[2/3] 参数不匹配: {len(mismatch)}")
    for line in mismatch:
        print("      ", line)

    unwrapped = sorted(n for n in xtdata_sigs if n not in used_xtdata)
    unwrapped_trader = sorted(n for n in trader_sigs if n not in used_trader)
    print(
        f"[3/3] 未封装: xtdata {len(unwrapped)}/{len(xtdata_sigs)}，"
        f"XtQuantTrader {len(unwrapped_trader)}/{len(trader_sigs)}"
    )
    print("      xtdata:", ", ".join(unwrapped))
    print("      trader:", ", ".join(unwrapped_trader))

    if missing or mismatch:
        print("\n结论: 存在与真实 xtquant 不一致的封装，需修复")
        return 1
    print("\n结论: 所有 xtdata / XtQuantTrader 引用与真实签名一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
