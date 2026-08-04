@echo off
setlocal
set "INSTALL_ROOT=%LOCALAPPDATA%\DianAgent"
set "UPDATER=%~dp0tools\DianAgentUpdater.exe"
set "STARTER=%~dp0tools\start_agent.ps1"
set "SYNC_TOOLS=%~dp0tools\sync_release_tools.ps1"
set "BUNDLE=%~1"

if not exist "%UPDATER%" (
  echo [FAILED] DianAgentUpdater.exe is missing.
  pause
  exit /b 1
)
if not exist "%STARTER%" (
  echo [FAILED] The release-compatible Agent starter is missing.
  pause
  exit /b 1
)
if not exist "%SYNC_TOOLS%" (
  echo [FAILED] The release tools synchronizer is missing.
  pause
  exit /b 1
)
if not defined BUNDLE set /p "BUNDLE=Offline upgrade ZIP path: "
if not exist "%BUNDLE%" (
  echo [FAILED] Upgrade bundle was not found.
  pause
  exit /b 1
)

echo Verifying every file and activating the offline upgrade...
"%UPDATER%" install "%BUNDLE%" --install-root "%INSTALL_ROOT%"
if errorlevel 1 (
  echo [FAILED] Upgrade was rejected or rolled back. Existing data and active version were preserved.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STARTER%" -InstallRoot "%INSTALL_ROOT%" -UpdaterPath "%UPDATER%" -DeferPendingConfirmation
if errorlevel 1 (
  echo The upgraded Agent did not become healthy. Restoring the previous version...
  "%UPDATER%" rollback --install-root "%INSTALL_ROOT%"
  if errorlevel 1 (
    echo [CRITICAL] Automatic rollback could not restore the previous version pointer.
    echo Rollback state was kept at %INSTALL_ROOT%\.offline-upgrade-rollback.
    pause
    exit /b 1
  )
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STARTER%" -InstallRoot "%INSTALL_ROOT%" -UpdaterPath "%UPDATER%"
  if errorlevel 1 echo [WARNING] The previous version was restored but could not be restarted automatically.
  echo [FAILED] Upgrade was rolled back. Check %INSTALL_ROOT%\logs before retrying.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SYNC_TOOLS%" -InstallRoot "%INSTALL_ROOT%" -SourceTools "%~dp0tools"
if errorlevel 1 (
  echo Maintenance tools could not be updated. Restoring the previous Agent version...
  "%UPDATER%" rollback --install-root "%INSTALL_ROOT%"
  if errorlevel 1 (
    echo [CRITICAL] Tool update and automatic Agent rollback both failed.
    pause
    exit /b 1
  )
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STARTER%" -InstallRoot "%INSTALL_ROOT%" -UpdaterPath "%UPDATER%"
  if errorlevel 1 echo [WARNING] The previous version was restored but could not be restarted automatically.
  echo [FAILED] Upgrade was rolled back because maintenance tools were not safely updated.
  pause
  exit /b 1
)
"%UPDATER%" confirm --install-root "%INSTALL_ROOT%"
if errorlevel 1 (
  echo [WARNING] New version is healthy, but rollback state cleanup failed. Do not apply another upgrade yet.
  pause
  exit /b 1
)
start "" explorer.exe "%INSTALL_ROOT%\extension-current"
echo [DONE] Upgrade activated. Open chrome://extensions and click Reload once.
pause
