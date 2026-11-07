Option Explicit
' ============================================
'  Тихий запуск агента: без окна консоли.
'  Двойной клик или ярлык на рабочем столе.
'  Открой окно чата -> закрой его -> сервер остановится сам.
' ============================================

Dim fso, shell, dir, py, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Папка этого скрипта, а не папка ярлыка
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = dir

' Сначала venv проекта, иначе системный python
py = dir & "\.venv\Scripts\python.exe"
If Not fso.FileExists(py) Then py = "python"

cmd = """" & py & """ """ & dir & "\start.py"" all"

' 0 = скрытое окно, False = не ждать завершения
shell.Run cmd, 0, False
