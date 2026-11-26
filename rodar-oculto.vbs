' Roda rodar-ofertas.bat sem abrir janela de console (chamado pelo Agendador de Tarefas).
Set sh = CreateObject("WScript.Shell")
pasta = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = pasta
sh.Run """" & pasta & "\rodar-ofertas.bat""", 0, True
