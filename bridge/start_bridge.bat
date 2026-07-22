@echo off
chcp 65001 >nul
setlocal
set "BRIDGE_DIR=%~dp0"
if defined DIAN_AGENT_PYTHON (
  set "PYTHON_EXE=%DIAN_AGENT_PYTHON%"
) else if exist "%BRIDGE_DIR%.venv\Scripts\python.exe" (
  set "PYTHON_EXE=%BRIDGE_DIR%.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

echo ============================================
echo   店策 Agent - 本地数据服务
echo   http://127.0.0.1:8765
echo   模式: 只读 / 本地存储 / 默认脱敏
echo ============================================
echo.

"%PYTHON_EXE%" "%BRIDGE_DIR%http_receiver.py"

if errorlevel 1 (
  echo.
  echo [错误] 本地服务启动失败。请检查 Python 或端口 8765。
  pause
)
