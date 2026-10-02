#!/bin/bash
set -eu
cd "$(dirname "$0")"
trap 'status=$?; if [ "$status" -ne 0 ]; then printf "\n启动未完成，请保留上方错误信息。按回车关闭。\n"; read -r _; fi' EXIT
if ! command -v python3 >/dev/null 2>&1; then
  printf '请先安装 Python 3.11 或更新版本，然后重新双击本文件。\n'
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "需要 Python 3.11 或更新版本"'
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,11), "旧虚拟环境版本过低；删除本目录 .venv 后重试"'
if ! .venv/bin/python -c 'import importlib.metadata as m,pathlib; req=pathlib.Path("requirements.txt").read_text().splitlines(); assert all(m.version(r.split("==")[0])==r.split("==")[1] for r in req if r and not r.startswith("#"))' >/dev/null 2>&1; then
  printf '首次启动安装运行依赖；PDF 不会上传至网络。\n'
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi
exec .venv/bin/python server.py
