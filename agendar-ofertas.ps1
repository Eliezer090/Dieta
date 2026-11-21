# Registra a coleta diária das ofertas do Koch no Agendador de Tarefas do Windows.
# Rode uma vez:  powershell -ExecutionPolicy Bypass -File agendar-ofertas.ps1
# Para mudar o horário, troque -At abaixo e rode de novo. Para remover:
#   Unregister-ScheduledTask -TaskName "Ofertas Koch" -Confirm:$false
$pasta = Split-Path -Parent $MyInvocation.MyCommand.Path
$acao = New-ScheduledTaskAction -Execute (Join-Path $pasta "rodar-ofertas.bat") -WorkingDirectory $pasta
$quando = New-ScheduledTaskTrigger -Daily -At 9:00
# StartWhenAvailable: se o PC estiver desligado às 9h, roda assim que ligar.
$cfg = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
# Interativo (só com você logado): o site e o WhatsApp precisam de navegador com janela.
$quem = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName "Ofertas Koch" -Action $acao -Trigger $quando -Settings $cfg -Principal $quem -Force | Out-Null
Write-Host "Tarefa 'Ofertas Koch' agendada para todo dia as 9h."
