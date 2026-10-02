@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  set "PYTHON=python"
) else (
  set "PYTHON=py -3"
)
%PYTHON% -c "import sys; assert sys.version_info >= (3,11), '需要 Python 3.11 或更新版本'"
if errorlevel 1 goto fail
if not exist ".venv\Scripts\python.exe" %PYTHON% -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -c "import importlib.metadata as m,pathlib; req=pathlib.Path('requirements.txt').read_text().splitlines(); assert all(m.version(r.split('==')[0])==r.split('==')[1] for r in req if r and not r.startswith('#'))" >nul 2>&1
if errorlevel 1 (
  echo 首次启动安装运行依赖；PDF 不会上传至网络。
  .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe server.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo 启动未完成，请保留上方错误信息。
pause
exit /b 1
