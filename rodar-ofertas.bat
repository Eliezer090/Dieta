@echo off
rem Chamado todo dia pelo Agendador de Tarefas (veja agendar-ofertas.ps1).
rem 1) coleta os precos e avisa no WhatsApp  2) envia so o ofertas-koch.js para o GitHub.
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
echo ===== %date% %time% ===== >> ofertas.log
"C:\Users\es19237\.coda-venv\Scripts\python.exe" get-ofertas.py >> ofertas.log 2>&1
if errorlevel 1 goto fim

rem "git commit <arquivo>" grava so esse arquivo, mesmo que haja outras alteracoes em andamento.
git diff --quiet -- ofertas-koch.js && goto fim
git commit ofertas-koch.js -m "Precos do site em %date%" >> ofertas.log 2>&1
git push >> ofertas.log 2>&1 || (git pull --rebase --autostash >> ofertas.log 2>&1 && git push >> ofertas.log 2>&1)
:fim
