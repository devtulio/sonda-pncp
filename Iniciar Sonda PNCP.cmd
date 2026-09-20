@echo off
rem Sobe a Sonda PNCP em segundo plano (icone na bandeja). Segunda execucao nao duplica.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0sonda_pncp.pyw"
