#!/bin/sh
# 本地门禁：与 CI 的四道检查保持一致。
#
# 仓库的 GitHub Actions 目前没有在运行（workflow 是 active，但没有任何 run 记录），
# 因此这里提供一条命令的本地兜底；`just verify` 与 .githooks/pre-push 都调它。
#
# 用法: sh scripts/verify.sh
set -e

if [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"      # Windows 虚拟环境
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"              # POSIX 虚拟环境
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    PY="python3"
fi

echo "== ruff check =="
"$PY" -m ruff check src/ tests/ dashboard/

echo "== ruff format --check =="
"$PY" -m ruff format --check src/ tests/ dashboard/

echo "== mypy =="
"$PY" -m mypy src/qmt_bridge/

echo "== pytest =="
"$PY" -m pytest tests/ -q

echo "全部通过 ✓"
