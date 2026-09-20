@echo off
rem Pede para a Sonda PNCP em execucao encerrar (o mesmo que Encerrar no menu do icone).
"%~dp0.venv\Scripts\pythonw.exe" "%~dp0sonda_pncp.pyw" --encerrar
