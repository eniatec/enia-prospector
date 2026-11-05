@echo off
rem Abre o EN.IA Prospector (http://localhost:8092). Feche esta janela para desligar a interface.
cd /d "%~dp0"
uv run --no-project --python 3.12 --with openpyxl python prospector\server.py
pause
