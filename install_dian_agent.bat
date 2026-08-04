@echo off
setlocal
set "PROJECT_DIR=%~dp0"

echo.
echo ============================================
echo   Dian Agent v4.0.0 Setup
echo ============================================
echo   Local-first Douyin commerce operations
echo   AI connection is optional
echo.

if exist "%PROJECT_DIR%app\DianAgent.exe" if exist "%PROJECT_DIR%tools\install_release.ps1" goto release_setup
if exist "%PROJECT_DIR%dist\agent\DianAgent.exe" if exist "%PROJECT_DIR%tools\install_release.ps1" goto release_setup
goto source_setup

:release_setup
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%tools\install_release.ps1" -SourceRoot "%PROJECT_DIR%"
goto setup_done

:source_setup
  echo Release executable was not found. Using source development setup.
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%bridge\setup_windows.ps1"

:setup_done

if errorlevel 1 (
  echo.
  echo [FAILED] Dian Agent setup did not complete.
  echo See the message above or the local logs for details.
  pause
  exit /b 1
)

echo.
echo [DONE] Dian Agent is installed and its local data will be preserved during upgrades.
echo Open chrome://extensions and load the installed extension folder if prompted.
pause
