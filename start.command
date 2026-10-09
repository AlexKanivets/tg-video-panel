#!/bin/bash
# Запуск Telegram Video Panel (macOS). Окно Terminal не закрывать, пока пользуетесь панелью.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/ffmpeg-static:/usr/local/bin:/opt/homebrew/bin:$PATH"
echo "== Telegram Video Panel =="
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не найден. Установите его: https://www.python.org/downloads/macos/"
  read -r -p "Нажмите Enter для выхода"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Первый запуск: создаю окружение и ставлю зависимости…"
  python3 -m venv .venv || { read -r -p "Ошибка venv. Enter для выхода"; exit 1; }
fi
.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
.venv/bin/python -m pip install -q -r requirements.txt || { read -r -p "Ошибка установки зависимостей. Enter для выхода"; exit 1; }
# если панель уже запущена — просто открываем её
if curl -s -o /dev/null http://127.0.0.1:8765/api/status; then
  echo "Панель уже работает."; open http://127.0.0.1:8765; exit 0
fi
.venv/bin/python app.py 2>&1 | tee panel.log
