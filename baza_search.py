"""
Скрипт для проверки списка адресов на w7.baza-winner.ru и сохранения
ссылок "поделиться" по найденным объявлениям.

КАК ЗАПУСТИТЬ (один раз, локально на своём компьютере):

    pip install playwright pandas openpyxl --break-system-packages
    python -m playwright install webkit

    python baza_search.py

При запуске скрипт сам создаёт (если их ещё нет) папки "input" и
"output" рядом с собой. Просто положите все xlsx-файлы со списками
адресов в папку "input" — скрипт по очереди обработает каждый файл
и для каждого сохранит отдельный результат в папку "output"
(например, input/лои_июнь.xlsx -> output/лои_июнь_results.csv).

Также можно по-прежнему указать конкретный файл вручную:

    python baza_search.py --input лои_июнь.xlsx --output results.csv

Первый запуск откроет окно браузера и попросит вас залогиниться на
сайте вручную. После того как залогинитесь, вернитесь в терминал и
нажмите Enter — сессия сохранится в папку .browser_profile рядом со
скриптом, и в следующий раз логиниться не придётся.

Скрипт печатает прогресс по каждому адресу в консоль и параллельно
дописывает результаты в CSV-файл построчно — если что-то упадёт
(капча, обрыв сети, вы случайно закроете окно), уже обработанные
адреса не потеряются.
"""

import argparse
import csv
import random
import re
import sys
import time
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import ollama_fixer

PROFILE_DIR = Path(__file__).parent / ".browser_profile"
INPUT_DIR = Path(__file__).parent / "input"
OUTPUT_DIR = Path(__file__).parent / "output"

INPUT_EXTENSIONS = (".xlsx", ".xls")

# Конфиг с "чинибельными" селекторами (см. ollama_fixer). Загружается один
# раз при старте; SEL — короткая ссылка на секцию selectors. После
# самолечения через Ollama конфиг перечитывается функцией reload_config(),
# и SEL начинает указывать на обновлённые значения — поэтому в коде везде
# читаем SEL[...] в момент вызова, а не копируем значения в отдельные
# константы.
CONFIG = ollama_fixer.load_config()
SEL = CONFIG["selectors"]


def reload_config():
    """Перечитывает конфиг с диска в память после самолечения селекторов."""
    global CONFIG, SEL
    CONFIG = ollama_fixer.load_config()
    SEL = CONFIG["selectors"]


# ---------------------------------------------------------------------------
# Утилиты для "человеческих" случайных задержек
# ---------------------------------------------------------------------------

def human_delay(min_ms=150, max_ms=450):
    """Случайная пауза — для имитации человеческой реакции между действиями."""
    time.sleep(random.uniform(min_ms, max_ms) / 1000)


def type_like_human(locator, text):
    """Печатает текст посимвольно со случайной задержкой между нажатиями.

    Используется только для номера дома, который дописывается К УЖЕ
    выбранной подсказке улицы. Сайт ожидает именно посимвольный ввод на
    этом шаге, чтобы понять, что пользователь продолжает уточнять уже
    начатый адрес, и предложить подсказки по дому в его контексте —
    address_input.fill() с готовой полной строкой не срабатывает
    (подсказки по дому не появляются вовсе).

    Для названия улицы (первый шаг ввода) это, наоборот, не годится:
    там сайт сам, "на лету", может подставить готовую подсказку до
    того, как допечатано двухсловное название ("Лихоборские Бугры"), и
    остаток набора дописывается уже поверх неё — оттуда и белиберда в
    поле. Для улицы используется address_input.fill() одним действием.
    """
    locator.click(force=True)
    locator.focus()
    for ch in text:
        locator.press_sequentially(ch)
        time.sleep(random.uniform(60, 220) / 1000)


def between_requests_delay():
    """Более длинная случайная пауза между обработкой разных адресов —
    чтобы не долбить сайт подряд одинаковыми интервалами."""
    time.sleep(random.uniform(7, 9))


# ---------------------------------------------------------------------------
# Разбор адресов из файла
# ---------------------------------------------------------------------------

def parse_address(raw: str):
    """
    Разбивает строку вида:
      "Беловежская улица, дом 21"
      "Ивантеевская улица, дом 28, корпус 3"
    на (улица, номер_дома_для_поиска).

    Номер для поиска собирается в формате, который показывает автокомплит
    сайта: "21", "28к3", "10с1" и т.п.
    """
    raw = raw.strip()

    house_match = re.search(r"дом\s+([0-9]+[A-Za-zА-Яа-я]?)", raw, re.IGNORECASE)
    korpus_match = re.search(r"корпус\s+([0-9]+)", raw, re.IGNORECASE)
    stroenie_match = re.search(r"строение\s+([0-9]+)", raw, re.IGNORECASE)

    street = raw
    if house_match:
        street = raw[: house_match.start()].strip(" ,")

    house = house_match.group(1) if house_match else ""
    if korpus_match:
        house += f"к{korpus_match.group(1)}"
    elif stroenie_match:
        house += f"с{stroenie_match.group(1)}"

    return street, house


STREET_TYPE_WORDS = {
    "улица", "ул",
    "переулок", "пер",
    "проезд", "пр",
    "шоссе", "ш",
    "бульвар", "б-р", "бр",
    "проспект", "пр-кт", "пр-т", "пркт",
    "набережная", "наб",
    "площадь", "пл",
    "аллея", "ал",
    "тупик", "туп",
    "просек", "просека",
    "линия",
    "квартал", "кв-л",
    "микрорайон", "мкр",
}


def strip_street_type(street: str) -> str:
    """Убирает слово-тип улицы ("улица", "ул.", "переулок" и т.п.) из
    начала или конца названия. Вводить в поиск нужно только само
    название (например, "Беловежская") — сайт сам покажет и подставит
    подсказку с сокращением типа ("Беловежская ул.")."""
    tokens = street.strip().split()
    if not tokens:
        return street

    def is_type_word(token: str) -> bool:
        return token.strip(".,").lower() in STREET_TYPE_WORDS

    if len(tokens) > 1 and is_type_word(tokens[0]):
        tokens = tokens[1:]
    elif len(tokens) > 1 and is_type_word(tokens[-1]):
        tokens = tokens[:-1]

    return " ".join(tokens).strip()


def load_addresses(path: str):
    df = pd.read_excel(path, header=None)
    addresses = [str(v).strip() for v in df.iloc[:, 0].tolist() if str(v).strip()]
    return addresses


# ---------------------------------------------------------------------------
# Работа с браузером
# ---------------------------------------------------------------------------

def ensure_logged_in(page):
    page.goto("https://w7.baza-winner.ru/main")
    if "login" in page.url or page.locator("text=Войти").count() > 0:
        print("\n>>> Похоже, вы не залогинены.")
    print(">>> Если вы ещё не залогинены на сайте — сделайте это сейчас в открывшемся окне браузера.")
    input(">>> Когда будете залогинены и увидите главную страницу — нажмите Enter здесь, в терминале...")


def open_search_page(page):
    page.goto(SEL["search_url"])
    page.wait_for_timeout(1500)


DEFAULT_TAG_SNIPPETS = (
    "глубина поиска",
    "источник",
    "кроме снятых",
    "в москве",
    "купить квартиру",
)


def clear_address_filter(page):
    """Убирает адресный фильтр, оставшийся от прошлого поиска.

    Важно: открытие страницы поиска "с нуля" (open_search_page) само по
    себе НЕ гарантирует чистое состояние — сайт при создании нового
    заказа копирует часть настроек из последнего (глубину поиска,
    источник и т.п.), и адрес из предыдущей итерации иногда остаётся
    в виде применённого тега/чипа над полем ввода. Если его не убрать,
    новый введённый адрес просто добавляется к старому тексту в поле
    (получается вроде "Москва г., Ивантеевская ул. улица") и поиск
    ничего не находит.

    Ищем среди тегов-чипов (текст + крестик "×" рядом) те, что не входят
    в список стандартных (глубина поиска / источник / кроме снятых /
    в Москве / купить квартиру) — это и есть адресный тег — и убираем их
    кликом по крестику.
    """
    close_icons = page.locator("text=×")
    count = close_icons.count()
    for i in range(count):
        icon = close_icons.nth(i)
        try:
            row_text = icon.locator("xpath=..").inner_text().strip().lower()
        except Exception:
            continue
        if not row_text or any(snippet in row_text for snippet in DEFAULT_TAG_SNIPPETS):
            continue
        try:
            icon.click(force=True, timeout=1500)
            page.wait_for_timeout(300)
        except Exception:
            pass


NON_STREET_ROW_MARKERS = ("метро", 'поиск "', "поиск «", "жк ")


def _street_suggestion_offset(page, search_street: str, core_words=None):
    """Считает, сколько раз нужно нажать 'Стрелка вниз', чтобы попасть на
    подсказку-АДРЕС с этим названием улицы, пропустив подсказки других
    категорий (метро, ЖК, вариант "искать как есть") — этот же текстовый
    инпут ("Город, район, адрес, метро, название ЖК") автокомплитит сразу
    несколько типов сущностей, и станции метро часто называются так же,
    как улицы (например, "Пятницкое шоссе" — это и улица, и станция
    метро). Сайт в таких случаях показывает станцию метро ПЕРВОЙ строкой
    в списке, поэтому слепой выбор "просто самого верхнего варианта"
    подставлял в поиск метро вместо улицы — дальше поле оставалось в
    несогласованном состоянии, и ввод номера дома дописывался поверх
    этого мусора (отсюда и белиберда в поле после пары таких адресов).

    Каждый вариант в списке — это две строки: сам текст (например,
    "Пятницкое ш." или "Пятницкое шоссе метро") и город на второй строке.
    Совпадение ищем через тот же подсвеченный фрагмент, что рендерит
    сайт (text=search_street), а полный текст строки берём из
    родительского элемента — по нему уже определяем категорию.

    Есть ещё один источник рассинхрона, отдельный от категорий: для
    улиц с уточняющим словом ("Малая", "Большая", "1-я", "3-я" и т.п.)
    сайт в подсказке часто разворачивает порядок слов — человек пишет
    "Малая Остроумовская", а подсказка называется "Остроумовская Малая
    ул."; "3я песчаная" -> "Песчаная 3-я ул."; "1й краснокурсантский" ->
    "Краснокурсантский 1-й пр-д". Поиск по точной фразе целиком в таких
    случаях не находит вообще ничего (ноль совпадений), потому что такой
    подстроки просто нет ни в одном элементе. Поэтому если точная фраза
    не находится, ищем по отдельным словам запроса (начиная с самого
    длинного — оно наименее случайно совпадает с чем-то посторонним) и
    проверяем, что ВСЕ слова запроса присутствуют в полном тексте
    строки — уже не важно, в каком порядке.

    core_words — слова, которые обязаны присутствовать в строке подсказки
    при поиске по отдельным словам (см. ниже). Если не задано, берутся
    слова из search_street. Отдельный параметр нужен вызывающему коду
    ([search_address]) для случая, когда сам search_street — это полное
    название со словом-типом ("Хорошёвское шоссе"), а сверять по словам
    нужно только по "смысловой" части названия ("Хорошёвское"), не по
    слову-типу — оно в подсказке может быть сокращено ("ш.") и не
    совпадёт при сравнении буквально.

    Возвращает (offset, found) — offset это 0-based индекс подходящей
    строки среди реально отрисованных вариантов (сортировка по Y), found
    False, если ни одного подходящего варианта-адреса не нашлось (только
    метро/ЖК/фолбэк или вообще пусто).

    Вся проверка (фраза целиком + фолбэк по словам) повторяется опросом
    до ~3 секунд, а не один раз сразу после фиксированной паузы. Причина:
    подсказки подгружаются асинхронно, и время на это зависит от текущей
    нагрузки на страницу — за один долгий прогон на аккаунте накапливаются
    десятки открытых "Заказов" (сайт создаёт новый при каждом заходе на
    .../search/new/...), и чем их больше, тем медленнее сайт успевает
    отрисовать подсказку. Единственная фиксированная пауза (400-900 мс)
    отлично работает в начале прогона и перестаёт хватать через несколько
    адресов подряд — отсюда и жалоба "почему через пару адресов всё
    пустое", хотя сами улицы и дома существуют.
    """
    deadline = time.monotonic() + 3.0
    words = core_words if core_words is not None else search_street.split()

    while True:
        offset, found = _find_suggestion_row(page, search_street)
        if found:
            return offset, True

        # Проверяем и при одном слове тоже: это нужно не только для
        # перестановки слов местами, но и для случая, когда search_street —
        # это ИМЯ + слово-тип целиком ("Хорошёвское шоссе"), а сама фраза
        # целиком не совпадает ни с одной строкой, потому что тип в
        # подсказке сокращён ("Хорошёвское ш.") — раньше фолбэк по словам
        # включался только при len(words) > 1, и однословные названия вроде
        # "Хорошёвское" из-за этого не находились вовсе.
        for anchor in sorted(words, key=len, reverse=True):
            offset, found = _find_suggestion_row(page, anchor, required_words=words)
            if found:
                return offset, True

        if time.monotonic() >= deadline:
            return 0, False
        time.sleep(0.3)


def _yo_agnostic_pattern(text: str) -> str:
    """Строит regex-паттерн из text, где буквы "е" и "ё" (в любом
    регистре) взаимозаменяемы.

    Одно и то же название улицы люди и сам сайт пишут то через "ё", то
    через "е" (например, в адресах пользователя — "Хорошёвское шоссе",
    а в подсказке сайта — "Хорошевское ш."). Это РАЗНЫЕ символы юникода,
    обычное текстовое совпадение (Playwright text=...) их не считает
    одинаковыми — из-за этого подсказка, которая реально есть на
    экране, не находилась вообще (0 совпадений).
    """
    escaped = re.escape(text)
    return re.sub(r"[еЕёЁ]", "[еЕёЁ]", escaped)


def _normalize_for_match(s: str) -> str:
    """Нормализация текста строки подсказки перед сравнением слов:
    убирает дефисы (порядковые "3-я"/"1-й") и приводит "ё" к "е", чтобы
    не зависеть от того, какой из вариантов буквы использован."""
    return s.lower().replace("-", "").replace("ё", "е")


def _find_suggestion_row(page, anchor_text, required_words=None):
    """Ищет строку-подсказку по тексту anchor_text (сам anchor_text может
    быть как всей фразой, так и одним словом из неё) и возвращает
    (offset, found) — см. _street_suggestion_offset. Если required_words
    задан, строка принимается только когда ВСЕ эти слова (без учёта
    регистра, в любом порядке) присутствуют в полном тексте строки.
    """
    pattern = _yo_agnostic_pattern(anchor_text)
    matches = page.locator(f"text=/{pattern}/i")
    count = matches.count()

    rows = []
    for i in range(count):
        match = matches.nth(i)
        try:
            box = match.bounding_box()
        except Exception:
            box = None
        if not box or box["y"] <= 150:
            continue

        row_text = ""
        for xpath in ("xpath=..", "xpath=../.."):
            try:
                candidate_text = match.locator(xpath).first.inner_text()
            except Exception:
                candidate_text = ""
            if candidate_text:
                row_text = candidate_text
                break

        rows.append((box["y"], row_text))

    rows.sort(key=lambda pair: pair[0])

    for offset, (_, row_text) in enumerate(rows):
        lowered = row_text.strip().lower()
        if any(marker in lowered for marker in NON_STREET_ROW_MARKERS):
            continue
        if required_words:
            # Нормализация убирает дефисы (порядковые "3я"/"1й" сайт
            # пишет как "3-я"/"1-й") и приводит "ё" к "е" — иначе
            # "Хорошёвское" не находится внутри "Хорошевское ш.".
            normalized_row = _normalize_for_match(lowered)
            if not all(_normalize_for_match(word) in normalized_row for word in required_words):
                continue
        return offset, True

    return 0, False


def _select_top_suggestion(page, address_input, typed_value: str) -> bool:
    """Выбирает САМУЮ ВЕРХНЮЮ подсказку из выпадающего списка клавиатурой
    (Стрелка вниз + Enter), а не кликом по найденному в DOM элементу.

    Раньше подсказка искалась кликом по элементу с нужным текстом
    (page.locator(f"text=...")), с сортировкой найденных элементов по
    вертикальной позиции (y) и кликом по самому верхнему. Это ломалось
    именно тогда, когда подсказок было больше одной: сайт (Polymer/
    iron-компоненты) держит в DOM множество других виртуализированных
    списков и оверлеев (селекторы этажности, комнатности, диапазонов
    и т.п.), полностью не связанных с адресной подсказкой, но формально
    "видимых" для Playwright (is_visible()==True, есть bounding_box) и
    содержащих тот же текст (например, отдельную цифру дома). При
    единственной подсказке совпадений почти не бывает и клик случайно
    попадал в нужный элемент; как только подсказок для номера дома
    становилось несколько (частые номера вроде "2", "3", "10" совпадают
    с текстом где-то ещё на странице), сортировка по Y стабильно
    выбирала чужой, не относящийся к адресу элемент, и дом не
    подставлялся.

    Стрелки клавиатуры работают с реальным открытым попапом самого
    поля ввода, поэтому не зависят от посторонних текстовых совпадений
    в остальном DOM. Если подсказок нет вовсе, ArrowDown+Enter ничего
    не делают — значение поля остаётся прежним.

    (Проверялся вариант дополнительно детектировать сообщение сайта
    "не найдено" — но такой текст всегда присутствует в DOM в скрытом
    виде как заготовка блока "объявлений не найдено" в списке
    результатов, даже когда результаты есть, поэтому он даёт ложные
    срабатывания и не используется.)

    Для шага с номером дома это единственное место, где мы вообще ждём
    появления подсказки (в отличие от улицы, где перед этим уже отдельно
    проверяли _street_suggestion_offset) — поэтому один ArrowDown+Enter
    сразу после фиксированной паузы недостаточно надёжен, если сайт в
    моменте подтормаживает (например, на аккаунте накопилось много
    открытых "Заказов" за долгий прогон): подсказка ещё не подгрузилась,
    ArrowDown не на чём сработать, значение поля не меняется — и дом
    считается "не найден", хотя на самом деле просто не подождали.
    Поэтому повторяем цикл ArrowDown+Enter несколько раз, пока значение
    поля не изменится или не истечёт ~3 секунды.

    Возвращает True, если подсказка была принята (значение поля
    изменилось после ArrowDown+Enter).
    """
    deadline = time.monotonic() + 3.0
    while True:
        address_input.press("ArrowDown")
        human_delay(150, 300)
        address_input.press("Enter")
        human_delay(200, 400)

        if address_input.input_value().strip() != typed_value.strip():
            return True

        if time.monotonic() >= deadline:
            return False


def click_search_button(page):
    """Нажимает кнопку 'Найти' справа от формы параметров."""
    find_btn = page.get_by_text(SEL["find_button_text"], exact=True).first
    find_btn.click(force=True)


def read_found_count(page):
    """Читает счётчик 'Найдено: N' и возвращает N."""
    found_text = page.locator(f'text=/{SEL["found_count_regex"]}/').first.inner_text()
    return int(re.search(r"\d+", found_text).group())


def search_address(page, street: str, house: str):
    """
    Порядок действий строго такой:
      1. Печатаем название улицы БЕЗ слова-типа ("улица"/"переулок"/...) целиком.
      2. Выбираем подсказку с улицей клавиатурой (Стрелка вниз + Enter).
      3. Печатаем номер дома.
      4. Выбираем подсказку с домом клавиатурой (Стрелка вниз + Enter).
      5. Только после этого нажимаем кнопку 'Найти'.
    Возвращает (найдено_ли_улица, найдено_ли_дом, кол-во_вариантов).
    """
    clear_address_filter(page)

    # Вводим и ищем подсказку по "голому" названию — без "улица"/"пер." и
    # т.п. Сайт сам подставляет тип в свою подсказку ("Беловежская ул."),
    # так что печатать его самим не нужно.
    search_street = strip_street_type(street)

    address_input = page.get_by_role("textbox", name=SEL["address_input_placeholder"])
    address_input.click(force=True)
    human_delay()

    # На всякий случай подчищаем поле — если в нём осталось значение
    # с прошлого адреса, дублирования текста не будет.
    address_input.fill("")
    human_delay()

    # Шаг 1: вводим название улицы целиком одним действием (fill), а не
    # посимвольно. Раньше печатали по буквам (type_like_human) — и для
    # названий из двух слов (например, "Лихоборские Бугры") сайт иногда
    # успевал сам, "на лету", подставить готовую подсказку ("Москва г.,
    # Лихоборские Бугры ул. ") ещё до того, как допечатано последнее
    # слово — а оставшиеся буквы дописывались уже поверх неё, давая
    # "Москва г., Лихоборские Бугры ул. Бугры". fill() выставляет
    # значение поля одним действием и не даёт сайту вклиниться посреди
    # набора текста.
    address_input.fill(search_street)
    human_delay(400, 900)

    # Шаг 2: находим, сколько раз нажать "вниз", чтобы попасть именно на
    # подсказку-адрес (а не на метро/ЖК с тем же названием), и выбираем её.
    core_words = search_street.split()
    offset, found = _street_suggestion_offset(page, search_street, core_words=core_words)

    if not found and search_street != street:
        # Универсальный фолбэк: не для всех типов улиц ("шоссе", "проезд"
        # и т.п. — не только "шоссе") сайт находит подсказку по голому
        # названию без слова-типа. Пробуем ещё раз с полным названием
        # (вместе со словом-типом) — сверяем при этом всё равно по
        # "смысловым" словам без типа (core_words), потому что в самой
        # подсказке тип может быть сокращён иначе ("ш." вместо "шоссе").
        address_input.fill(street)
        human_delay(400, 900)
        offset, found = _street_suggestion_offset(page, street, core_words=core_words)
        if found:
            search_street = street

    if not found:
        # Дошли до этой точки — значит поиск не смог даже найти подсказку
        # улицы (текст на странице не совпал с ожидаемым селектором). Если
        # включён Ollama-режим, отдаём слепок страницы модели: пусть
        # определит, какой селектор разъехался, и запишет исправление в
        # config.json. Обновлённые значения подхватятся со следующего адреса.
        try:
            if ollama_fixer.self_heal_selectors(page, CONFIG, street, house):
                reload_config()
        except Exception as e:
            print(f"    [ollama] Самолечение не сработало: {e}")
        return False, False, 0

    for _ in range(offset):
        address_input.press("ArrowDown")
        human_delay(80, 160)

    if not _select_top_suggestion(page, address_input, search_street):
        return False, False, 0

    human_delay(300, 700)

    if not house:
        # Дома нет — сразу жмём 'Найти'
        click_search_button(page)
        page.wait_for_timeout(1200)
        return True, False, read_found_count(page)

    # Шаг 3: кликаем по полю и вводим номер дома
    address_input.click(force=True)
    human_delay(200, 400)
    value_before_house = address_input.input_value()
    type_like_human(address_input, house)
    human_delay(400, 900)

    # Шаг 4: выбираем верхнюю подсказку с домом клавиатурой. typed_value
    # тут — это всё значение поля вместе с уже подставленной улицей
    # (например, "Москва г., Ивантеевская ул. 2"), а не просто "2" —
    # именно так выглядит значение поля до принятия подсказки.
    if not _select_top_suggestion(page, address_input, value_before_house + house):
        return True, False, 0

    human_delay(300, 700)

    # Шаг 5: и только теперь — кнопка 'Найти'
    click_search_button(page)
    page.wait_for_timeout(1200)
    return True, True, read_found_count(page)


def get_share_link(page):
    """Переключается в 'Список' и жмёт кнопку 'поделиться', возвращает ссылку.

    Тут наложились две проблемы:

    1. CSS-селектор input[value^="..."] — АТРИБУТНЫЙ, он видит только
       исходное значение атрибута value из HTML на момент рендера, а
       сайт подставляет саму ссылку JS-свойством .value уже позже.

    2. Сайт построен на Polymer/iron-компонентах (iron-input,
       iron-icon и т.п.), а такие компоненты обычно рендерят своё
       содержимое в shadow DOM. document.querySelectorAll() (обычный
       JS, page.evaluate) НЕ проникает внутрь shadow DOM — из-за этого
       попытка найти input через JS тоже ничего не находила.

    Решение: используем Playwright-локатор page.locator("input") — он,
    в отличие от обычного querySelectorAll, сам "простреливает" открытые
    shadow-корни. А значение читаем через .input_value() — это реальное
    свойство .value элемента, а не HTML-атрибут.
    """
    page.get_by_text(SEL["list_tab_text"], exact=True).click(force=True)
    page.wait_for_timeout(800)

    share_icon = page.locator(SEL["share_icon_selector"]).first
    try:
        share_icon.click(force=True, timeout=4000)
    except PWTimeout:
        return None

    page.wait_for_timeout(700)

    all_inputs = page.locator("input")
    for _ in range(8):
        count = all_inputs.count()
        for i in range(count):
            candidate = all_inputs.nth(i)
            try:
                value = candidate.input_value(timeout=500)
            except Exception:
                continue
            if value and value.startswith(SEL["share_link_prefix"]):
                return value
        page.wait_for_timeout(300)

    return None


# ---------------------------------------------------------------------------
# Основной цикл
# ---------------------------------------------------------------------------

def ensure_dirs():
    """Создаёт папки input/ и output/ рядом со скриптом, если их ещё нет."""
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def process_file(page, in_path: Path, out_path: Path):
    """Обрабатывает один входной xlsx-файл и построчно пишет результат в out_path."""
    addresses = load_addresses(str(in_path))
    print(f"\n=== Файл: {in_path.name} (адресов: {len(addresses)}) ===")

    is_new_file = not out_path.exists()

    with open(out_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        if is_new_file:
            writer.writerow(["Адрес", "Найдено вариантов", "Ссылка"])

        for i, raw_address in enumerate(addresses, 1):
            street, house = parse_address(raw_address)
            print(f"\n[{i}/{len(addresses)}] {raw_address}  ->  улица='{street}', дом='{house}'")

            try:
                open_search_page(page)
                street_ok, house_ok, count = search_address(page, street, house)

                link = ""
                if count > 0:
                    link = get_share_link(page) or ""

                print(f"    Найдено: {count} вариантов. Ссылка: {link or '—'}")
                writer.writerow([raw_address, count, link])
                f.flush()

            except Exception as e:
                print(f"    ОШИБКА на адресе '{raw_address}': {e}")
                writer.writerow([raw_address, "ошибка", str(e)])
                f.flush()

            between_requests_delay()

    print(f"Готово: {in_path.name} -> {out_path.resolve()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="xlsx-файл со списком адресов (необязательно)")
    parser.add_argument("--output", help="куда сохранить результаты (необязательно)")
    parser.add_argument(
        "--ollama",
        action="store_true",
        help="включить Ollama-режим самолечения селекторов на этот запуск "
        "(по умолчанию берётся из config.json)",
    )
    args = parser.parse_args()

    # Флаг командной строки включает Ollama-режим поверх config.json на
    # текущий запуск — удобно, если не хочется трогать конфиг.
    if args.ollama:
        CONFIG.setdefault("ollama", {})["enabled"] = True

    if ollama_fixer.is_enabled(CONFIG):
        print(f">>> Ollama-режим ВКЛЮЧЁН (модель: {CONFIG['ollama'].get('model')}). "
              "При провале поиска селекторы будут чиниться автоматически.")
    else:
        print(">>> Ollama-режим выключен. Включить: ./setup_ollama.sh или флаг --ollama.")

    ensure_dirs()

    # Список задач: (входной файл, выходной файл)
    tasks = []
    if args.input:
        in_path = Path(args.input)
        out_path = Path(args.output) if args.output else OUTPUT_DIR / f"{in_path.stem}_results.csv"
        tasks.append((in_path, out_path))
    else:
        input_files = sorted(
            p for p in INPUT_DIR.iterdir()
            if p.is_file()
            and p.suffix.lower() in INPUT_EXTENSIONS
            and not p.name.startswith("~$")  # временные файлы Excel/Word при открытом файле
            and not p.name.startswith(".")
        )
        if not input_files:
            print(f"В папке {INPUT_DIR} нет xlsx-файлов. Положите туда файлы со списками адресов и запустите скрипт снова.")
            return
        for in_path in input_files:
            out_path = OUTPUT_DIR / f"{in_path.stem}_results.csv"
            tasks.append((in_path, out_path))

    print(f"Файлов к обработке: {len(tasks)}")

    with sync_playwright() as p:
        context = p.webkit.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()

        ensure_logged_in(page)

        for in_path, out_path in tasks:
            process_file(page, in_path, out_path)

        context.close()

    print("\nВсе файлы обработаны.")


if __name__ == "__main__":
    main()
