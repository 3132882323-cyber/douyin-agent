@echo off
chcp 65001 >nul
setlocal
set "PROJECT_DIR=%~dp0"

echo.
echo ============================================
echo   店策 Agent v3.0.1 - 轻量安装
echo ============================================
echo   1. 启动并设置本地 Agent
echo   2. 打开浏览器扩展管理页
echo   3. 打开需要选择的扩展文件夹
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%bridge\setup_windows.ps1"
if errorlevel 1 (
  echo.
  echo [未完成] 本地 Agent 安装失败。
  echo 如果提示缺少 Python，请先安装 Python 3.10 或更高版本后重试。
  pause
  exit /b 1
)

set "BROWSER="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined BROWSER if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined BROWSER if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"

if defined BROWSER start "" "%BROWSER%" "chrome://extensions/"
start "" explorer.exe "%PROJECT_DIR%extension"

echo.
echo [最后一步]
echo 在扩展管理页打开“开发者模式”，点击“加载已解压的扩展程序”，
echo 选择刚刚打开的 extension 文件夹。
echo.
echo 安装后默认启用轻量哨兵：零自动扫描，点击时才取数。
pause
