Option Explicit

Dim shell, fileSystem, toolsDir, installRoot, repairPath, powershellPath, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

toolsDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
installRoot = fileSystem.GetParentFolderName(toolsDir)
repairPath = fileSystem.BuildPath(toolsDir, "repair_agent.ps1")
powershellPath = shell.ExpandEnvironmentStrings("%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe")

command = """" & powershellPath & """ -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & repairPath & """ -InstallRoot """ & installRoot & """"
exitCode = shell.Run(command, 0, True)
If exitCode <> 0 Then
  shell.Popup "Dian Agent repair was not verified. Open logs\repair-agent.log for details.", 0, "Dian Agent repair failed", 16
End If
WScript.Quit exitCode
