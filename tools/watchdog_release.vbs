Option Explicit

Dim shell, fileSystem, toolsDir, installRoot, watchdogPath, powershellPath, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

toolsDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
installRoot = fileSystem.GetParentFolderName(toolsDir)
watchdogPath = fileSystem.BuildPath(toolsDir, "watchdog_release.ps1")
powershellPath = shell.ExpandEnvironmentStrings("%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe")

command = """" & powershellPath & """ -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & watchdogPath & """ -InstallRoot """ & installRoot & """"
shell.Run command, 0, False
