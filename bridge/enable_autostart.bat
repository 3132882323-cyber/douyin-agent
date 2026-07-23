@echo off
chcp 65001 >nul
setlocal
echo 正在设置店策 Agent 自动启动，请稍候...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
if errorlevel 1 (
  echo.
  echo 设置失败。请确认已安装 Python 3.10 或更高版本，然后重试。
  pause
  exit /b 1
)
echo.
echo 设置完成，可以关闭此窗口。
pause

