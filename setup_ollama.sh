#!/bin/bash
# Установка Ollama и небольшой модели для режима "самолечения" селекторов
# на macOS.
#
# Что делает:
#   1. Ставит Ollama (через Homebrew, если он есть; иначе скачивает
#      официальный .zip с сайта и распаковывает в /Applications).
#   2. Поднимает локальный сервер Ollama (http://localhost:11434), если он
#      ещё не запущен.
#   3. Скачивает модель qwen3:8b (см. выбор модели в README).
#   4. Включает Ollama-режим в config.json рядом со скриптом.
#
# Использование:
#   ./setup_ollama.sh
#
# После этого запускайте парсер как обычно (./launch.sh) — при провале
# поиска адреса скрипт сам обратится к модели, чтобы починить селекторы.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Модель по умолчанию. Можно поменять на любую из ollama.com/library —
# см. таблицу выбора модели в README (раздел "Ollama-режим").
MODEL="qwen3:8b"
OLLAMA_HOST="http://localhost:11434"

# ---------------------------------------------------------------------------
# 1. Установка Ollama
# ---------------------------------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
    echo ">>> Ollama уже установлена ($(ollama --version 2>/dev/null | head -n1))."
elif command -v brew >/dev/null 2>&1; then
    echo ">>> Ставлю Ollama через Homebrew ..."
    brew install ollama
else
    echo ">>> Homebrew не найден. Скачиваю Ollama с официального сайта ..."
    TMP_ZIP="$(mktemp -t ollama).zip"
    curl -fsSL "https://ollama.com/download/Ollama-darwin.zip" -o "$TMP_ZIP"
    echo ">>> Распаковываю в /Applications ..."
    unzip -o -q "$TMP_ZIP" -d /Applications
    rm -f "$TMP_ZIP"
    # CLI лежит внутри .app — добавим симлинк, чтобы был в PATH.
    if [ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]; then
        sudo ln -sf "/Applications/Ollama.app/Contents/Resources/ollama" /usr/local/bin/ollama || true
    fi
fi

# ---------------------------------------------------------------------------
# 2. Запуск сервера Ollama, если он ещё не поднят
# ---------------------------------------------------------------------------
if curl -fsS "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
    echo ">>> Сервер Ollama уже отвечает на $OLLAMA_HOST."
else
    echo ">>> Поднимаю сервер Ollama в фоне ..."
    nohup ollama serve >/tmp/ollama_serve.log 2>&1 &
    echo ">>> Жду, пока сервер поднимется ..."
    for _ in $(seq 1 30); do
        if curl -fsS "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! curl -fsS "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
        echo "!!! Сервер Ollama не поднялся за 30 секунд. Смотрите лог: /tmp/ollama_serve.log" >&2
        exit 1
    fi
    echo ">>> Сервер Ollama поднят."
fi

# ---------------------------------------------------------------------------
# 3. Скачивание модели
# ---------------------------------------------------------------------------
echo ">>> Скачиваю модель $MODEL (это может занять несколько минут при первом запуске) ..."
ollama pull "$MODEL"

# ---------------------------------------------------------------------------
# 4. Включаем Ollama-режим в config.json
# ---------------------------------------------------------------------------
echo ">>> Включаю Ollama-режим в config.json ..."
python3 - "$MODEL" "$OLLAMA_HOST" <<'PY'
import json
import sys
from pathlib import Path

model, host = sys.argv[1], sys.argv[2]
# Скрипт уже сделал cd в свою папку, поэтому config.json тут — рядом с ним.
cfg_path = Path("config.json")

cfg = {}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}

ollama = cfg.setdefault("ollama", {})
ollama["enabled"] = True
ollama["model"] = model
ollama["host"] = host

cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"    config.json обновлён: ollama.enabled=true, model={model}")
PY

echo ""
echo ">>> Готово. Ollama-режим включён."
echo ">>> Теперь запускайте парсер как обычно: ./launch.sh"
