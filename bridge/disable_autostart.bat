@echo off
chcp 65001 >nul
setlocal
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DianAgent.lnk"
if exist "%SHORTCUT%" del /f /q "%SHORTCUT%"
echo 已关闭店策 Agent 的 Windows 登录自动启动。
echo 当前已经运行的 Agent 会在下次重启电脑后停止自动启动。
pause

