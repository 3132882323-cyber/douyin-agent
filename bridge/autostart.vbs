' 抖店千川数据桥 - Bridge 接收进程开机自启
' 静默运行，不弹控制台窗口，日志写入 bridge/bridge.log
Dim shell, fso, py, script, log, localPy
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
script = fso.GetParentFolderName(WScript.ScriptFullName)
localPy = script & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(localPy) Then
  py = localPy
Else
  py = "pythonw"
End If
log = script & "\bridge.log"
shell.CurrentDirectory = script
shell.Run """" & py & """ """ & script & "\http_receiver.py"" >> """ & log & """ 2>&1", 0, False
