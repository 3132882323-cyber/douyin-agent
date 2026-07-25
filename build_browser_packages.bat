@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_browser_packages.ps1"
if errorlevel 1 (
  echo.
  echo [错误] 浏览器扩展包生成失败。
  pause
  exit /b 1
)
echo.
echo 已生成现代浏览器版和多浏览器兼容版。
echo 输出目录：%~dp0dist
pause
