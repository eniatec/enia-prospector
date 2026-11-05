#!/bin/sh
# Abre o EN.IA Prospector (http://localhost:8092) no Mac. Feche esta janela para desligar a interface.
cd "$(dirname "$0")" || exit 1
uv run --no-project --python 3.12 --with openpyxl python prospector/server.py
