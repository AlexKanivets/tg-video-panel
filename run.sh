#!/usr/bin/env bash
# Локальный запуск Telegram Video Panel (Linux). Панель откроется на http://127.0.0.1:8765
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null || { echo "Нужен Python 3.9+: sudo apt install python3 python3-venv"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv || exit 1
.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
.venv/bin/python -m pip install -q -r requirements.txt || exit 1
exec .venv/bin/python app.py
