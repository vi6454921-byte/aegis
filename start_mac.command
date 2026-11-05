#!/bin/bash
# Запуск бота «Аегис» на Mac: двойной щелчок по этому файлу. На Linux: bash start_mac.command
cd "$(dirname "$0")" || exit 1

finish() { echo; read -r -p "Нажмите Enter, чтобы закрыть окно..." _; exit "${1:-0}"; }

# Компоненты ставятся при первом запуске и заново — если после обновления изменился requirements.txt
if [ ! -f .venv/.installed ] || ! cmp -s requirements.txt .venv/.installed; then
  PY=""
  for c in python3.14 python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Не найден Python 3.12 или новее."
    echo "Скачайте его с https://www.python.org/downloads/ , установите и снова запустите этот файл."
    finish 1
  fi
  echo "Устанавливаю нужные компоненты (первый запуск или обновление), это займёт 1-3 минуты..."
  echo
  if ! { [ -x .venv/bin/python ] || "$PY" -m venv .venv; } ||
     ! .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then
    echo
    echo "Не получилось установить компоненты. Проверьте интернет и снова запустите этот файл."
    finish 1
  fi
  cp requirements.txt .venv/.installed
  clear
fi

echo
echo "  Бот запускается. Пока это окно открыто, бот работает."
echo "  Чтобы остановить бота, закройте окно или нажмите Ctrl+C."
echo
WEB_HOST=127.0.0.1 .venv/bin/python -m app
echo
echo "Бот остановлен."
finish 0
