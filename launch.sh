#!/bin/bash
# Запуск скрипта проверки адресов на macOS.
#
# Что делает:
#   1. Создаёт виртуальное окружение .venv рядом со скриптом (если его ещё нет).
#   2. Ставит туда зависимости из requirements.txt.
#   3. Ставит браузер WebKit для Playwright (если ещё не установлен).
#   4. Запускает baza_search.py.
#
# Использование:
#   ./launch.sh
#   ./launch.sh --input лои_июнь.xlsx --output results.csv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo ">>> Создаю виртуальное окружение в $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo ">>> Устанавливаю зависимости из requirements.txt ..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

echo ">>> Проверяю браузер WebKit для Playwright ..."
python -m playwright install webkit

echo ">>> Запускаю baza_search.py ..."
python baza_search.py "$@"
