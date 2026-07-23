"""
Ollama-режим "самолечения" селекторов для baza_search.py.

Идея простая. Сайт w7.baza-winner.ru сделан на кастомных веб-компонентах
(shadow DOM), и его вёрстка/тексты периодически меняются — из-за этого
"хрупкие" селекторы (плейсхолдер поля адреса, текст кнопки "Найти",
иконка "поделиться" и т.п.) в какой-то момент перестают совпадать, и
поиск по адресу молча не находит подсказку.

Все такие селекторы вынесены в config.json. Когда поиск адреса не смог
даже дойти до подсказки улицы И включён Ollama-режим (ollama.enabled в
config.json), скрипт:

  1. Снимает "слепок" текущей страницы (URL, все плейсхолдеры полей,
     видимый текст, текущие ожидаемые селекторы).
  2. Отправляет это локальной модели Ollama и просит вернуть JSON с
     ИСПРАВЛЕННЫМИ значениями тех селекторов, которые, судя по слепку,
     разъехались с реальной страницей.
  3. Проверяет ответ (это должен быть JSON только с известными ключами),
     мержит его в config.json и перечитывает конфиг в память — так
     следующий адрес уже пойдёт с обновлёнными селекторами, без
     перезапуска скрипта.

Если Ollama-режим выключен — модуль ничего не делает, весь код работает
как раньше на значениях по умолчанию.

Никаких внешних зависимостей: обращение к Ollama идёт через стандартный
urllib по HTTP (по умолчанию http://localhost:11434).
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

# Значения по умолчанию. Всё, что можно "чинить" моделью, лежит в
# selectors — ключи здесь же служат белым списком: ответ модели с любыми
# другими ключами игнорируется.
DEFAULT_CONFIG = {
    "ollama": {
        "enabled": False,
        "host": "http://localhost:11434",
        "model": "qwen3:8b",
        "timeout_seconds": 120,
    },
    "selectors": {
        "search_url": "https://w7.baza-winner.ru/search/new/sell-msk-flat",
        "address_input_placeholder": "Город, район, адрес, метро, название ЖК",
        "find_button_text": "Найти",
        "list_tab_text": "Список",
        "share_icon_selector": 'iron-icon[icon="social:share"]',
        "share_link_prefix": "https://online.baza-winner.ru",
        "found_count_regex": r"Найдено: \d+",
    },
}


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладывает override поверх base, не теряя ключей base,
    которых нет в override (нужно, чтобы новый ключ-селектор из
    DEFAULT_CONFIG появился даже у пользователя со старым config.json)."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    """Читает config.json (если есть) и накладывает поверх значений по
    умолчанию. Всегда возвращает полный конфиг со всеми ключами."""
    cfg = DEFAULT_CONFIG
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = _deep_merge(DEFAULT_CONFIG, on_disk)
        except Exception as e:
            print(f"    [ollama] config.json не прочитан ({e}), беру значения по умолчанию.")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_enabled(cfg: dict) -> bool:
    return bool(cfg.get("ollama", {}).get("enabled"))


# ---------------------------------------------------------------------------
# Обращение к Ollama
# ---------------------------------------------------------------------------

def _ollama_chat(cfg: dict, system_prompt: str, user_prompt: str) -> str:
    """Шлёт запрос в /api/chat локального Ollama и возвращает текст ответа.

    format=json просит Ollama выдать строго валидный JSON (без пояснений
    вокруг), stream=false — получить ответ одним куском.
    """
    ollama = cfg.get("ollama", {})
    host = ollama.get("host", "http://localhost:11434").rstrip("/")
    model = ollama.get("model", "qwen3:8b")
    timeout = ollama.get("timeout_seconds", 120)

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Слепок страницы для модели
# ---------------------------------------------------------------------------

def collect_diagnostics(page, cfg: dict, street: str, house: str) -> dict:
    """Собирает компактный "слепок" страницы, по которому модель сможет
    понять, какие селекторы разъехались.

    Специально НЕ отправляем весь page.content() — это огромный SPA, и
    7B-модель захлебнётся. Вместо этого отдаём то, что реально помогает
    сопоставить старые селекторы с новыми: список всех плейсхолдеров
    (Playwright-локатор простреливает открытый shadow DOM) и кусок
    видимого текста, где есть подписи кнопок/вкладок.
    """
    diag = {
        "url": "",
        "current_selectors": dict(cfg.get("selectors", {})),
        "searched_street": street,
        "searched_house": house,
        "placeholders_on_page": [],
        "visible_text_excerpt": "",
    }

    try:
        diag["url"] = page.url
    except Exception:
        pass

    # Все плейсхолдеры полей ввода — среди них должен быть новый вариант
    # адресного поля, если сайт сменил подпись.
    try:
        placeholder_nodes = page.locator("[placeholder]")
        seen = set()
        for i in range(min(placeholder_nodes.count(), 40)):
            try:
                value = placeholder_nodes.nth(i).get_attribute("placeholder")
            except Exception:
                value = None
            if value and value not in seen:
                seen.add(value)
                diag["placeholders_on_page"].append(value)
    except Exception:
        pass

    # Кусок видимого текста — тут ловятся тексты кнопок ("Найти"),
    # вкладок ("Список"), счётчика ("Найдено: N") и т.п.
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        diag["visible_text_excerpt"] = body_text[:4000]
    except Exception:
        pass

    return diag


# ---------------------------------------------------------------------------
# Самолечение
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Ты помогаешь чинить веб-скрапер сайта недвижимости на русском языке. "
    "Скрапер использует набор селекторов/текстов, чтобы находить элементы на "
    "странице поиска. Один или несколько из них перестали совпадать с реальной "
    "страницей. По слепку страницы определи, какие значения нужно исправить, и "
    "верни ТОЛЬКО JSON-объект с исправленными значениями. "
    "Разрешённые ключи: address_input_placeholder, find_button_text, "
    "list_tab_text, share_icon_selector, share_link_prefix, found_count_regex, "
    "search_url. "
    "Включай в ответ ТОЛЬКО те ключи, которые реально нужно поменять; если "
    "уверенного исправления нет — верни пустой объект {}. Не придумывай значения, "
    "которых нет в слепке. Никакого текста вне JSON."
)


def _sanitize_patch(patch: dict, allowed_keys) -> dict:
    """Оставляет из ответа модели только известные ключи с непустыми
    строковыми значениями — защита от галлюцинаций и мусора."""
    clean = {}
    for key, value in patch.items():
        if key in allowed_keys and isinstance(value, str) and value.strip():
            clean[key] = value.strip()
    return clean


def self_heal_selectors(page, cfg: dict, street: str, house: str) -> bool:
    """Пытается починить селекторы через Ollama и записать их в config.json.

    Возвращает True, если конфиг был обновлён (тогда вызывающий код должен
    перечитать селекторы в память). Любая ошибка (Ollama недоступна, ответ
    не разобран и т.п.) — это просто False и сообщение в консоль, поиск при
    этом продолжается как обычно на прежних значениях.
    """
    if not is_enabled(cfg):
        return False

    print("    [ollama] Поиск не дошёл до подсказки — спрашиваю модель, что починить ...")

    try:
        diag = collect_diagnostics(page, cfg, street, house)
    except Exception as e:
        print(f"    [ollama] Не удалось снять слепок страницы: {e}")
        return False

    user_prompt = (
        "Скрапер не смог найти подсказку адреса на странице поиска. "
        "Ниже слепок страницы в JSON. Поле current_selectors — это то, что "
        "скрапер сейчас ищет; placeholders_on_page и visible_text_excerpt — "
        "что реально есть на странице. Верни JSON с исправленными селекторами.\n\n"
        + json.dumps(diag, ensure_ascii=False, indent=2)
    )

    try:
        raw = _ollama_chat(cfg, _SYSTEM_PROMPT, user_prompt)
    except urllib.error.URLError as e:
        print(f"    [ollama] Сервер Ollama недоступен ({e}). Запустите setup_ollama.sh.")
        return False
    except Exception as e:
        print(f"    [ollama] Ошибка запроса к Ollama: {e}")
        return False

    try:
        patch = json.loads(raw)
    except Exception:
        print(f"    [ollama] Модель вернула не-JSON, пропускаю. Ответ: {raw[:200]!r}")
        return False

    if not isinstance(patch, dict):
        print("    [ollama] Модель вернула не объект, пропускаю.")
        return False

    allowed = set(DEFAULT_CONFIG["selectors"].keys())
    clean = _sanitize_patch(patch, allowed)

    # Отбрасываем ключи, значение которых не изменилось — чтобы не писать
    # в конфиг "исправления", идентичные текущим.
    current = cfg.get("selectors", {})
    clean = {k: v for k, v in clean.items() if v != current.get(k)}

    if not clean:
        print("    [ollama] Модель не предложила исправлений.")
        return False

    cfg.setdefault("selectors", {}).update(clean)
    save_config(cfg)

    print("    [ollama] Обновил селекторы в config.json:")
    for key, value in clean.items():
        print(f"        {key} = {value!r}")
    return True
