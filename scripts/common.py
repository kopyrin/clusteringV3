"""
scripts/common.py — общая библиотека проекта Semantic Facet Analyzer.

Содержит:
  • поиск корня проекта и все пути
  • консольные хелперы (status/line/screen/pause/...)
  • индикаторы прогресса (Progress, PercentProgress)
  • форматирование (human/big/fmt/short)
  • POS-константы (POS_RU, ALLOWED_POS, STOP_POS, ...)
  • GROUP_RULES — справочник фасетных групп
  • работа с БД (apply_pragmas, ensure_indexes, REQUIRED_INDEXES)
  • работа с YAML (load_yaml/save_yaml/_deep_merge)
  • чистка JSON-ответов AI (clean_json)
  • UI-хелперы лаунчера (prompt_int/bool/choice, UNCHANGED)
"""
import copy
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


# ============================================================
# 1. КОРЕНЬ ПРОЕКТА И ПУТИ
# ============================================================

def find_root() -> Path:
    """
    Ищет корень проекта, поднимаясь от текущего файла.
    Признаки корня: есть папка data/ или файл requirements.txt.
    Возвращает абсолютный путь к корню.
    """
    try:
        current = Path(__file__).resolve()
    except NameError:
        current = Path.cwd().resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()

# --- виртуальное окружение ---
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"

# --- конфигурация ---
CONFIG_DIR = ROOT / "config"
CORE_CONFIG_FILE = CONFIG_DIR / "core_builder.yaml"
HYPOTHESIS_CONFIG_FILE = CONFIG_DIR / "hypothesis_finder.yaml"

# --- данные ---
INPUT_DIR = ROOT / "data" / "input"
OUTPUT_DIR = ROOT / "data" / "output"
SEED_DIR = ROOT / "data" / "seed"
DICT_DIR = ROOT / "dict"

INPUT_FILE = INPUT_DIR / "keywords.txt"
MINUS_FILE = INPUT_DIR / "minus.txt"
START_WORDS_FILE = INPUT_DIR / "start_words.txt"

DB_FILE = OUTPUT_DIR / "semantics.db"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"
ID_FILE = OUTPUT_DIR / "keywords_ids.bin"
WEIGHT_FILE = OUTPUT_DIR / "weights.bin"
HITS_FILE = OUTPUT_DIR / "hits.bin"
STATS_META_FILE = OUTPUT_DIR / "stats_meta.json"
UNKNOWN_FILE = OUTPUT_DIR / "unknown_words.txt"

CORE_OUTPUT_CSV = OUTPUT_DIR / "core_words.csv"
CORE_OUTPUT_TXT = OUTPUT_DIR / "core_words.txt"
CORE_CHECKPOINT = OUTPUT_DIR / "core_words_checkpoint.json"

BAD_MODELS_FILE = OUTPUT_DIR / "bad_models.txt"
AUTO_WORDS_FILE = OUTPUT_DIR / "auto_words.pkl"
AUTO_PODGROUP_FILE = OUTPUT_DIR / "auto_podgroup.pkl"
LAST_FACETS_FILE = OUTPUT_DIR / "last_facets.txt"
JUDGES_RAW_FILE = OUTPUT_DIR / "judges_raw.json"
HYPOTHESES_FILE = OUTPUT_DIR / "hypotheses.txt"

# --- гарантируем существование рабочих папок ---
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 2. КОНСОЛЬНЫЕ ХЕЛПЕРЫ
# ============================================================

def clear_screen():
    """Очищает экран (Windows/Linux)."""
    os.system("cls" if os.name == "nt" else "clear")


def pause():
    """Ждёт нажатия Enter."""
    input("\n  Нажмите Enter...")


def error(msg: str):
    """Печатает сообщение об ошибке."""
    print(f"\n  [!] {msg}")


def success(msg: str):
    """Печатает сообщение об успехе."""
    print(f"  [ok] {msg}")


class screen:
    """Контекстный менеджер для оформления экрана."""

    def __init__(self, title: str):
        self.title = title

    def __enter__(self):
        clear_screen()
        print("\n" + "=" * 70)
        print(f"  {self.title}")
        print("=" * 70)
        return self

    def __exit__(self, *args):
        pass


def confirm(prompt: str = "Подтверждаете?") -> bool:
    """Запрос да/нет."""
    raw = input(f"\n  {prompt} (да/нет): ").strip().lower()
    return raw in ("да", "д", "y", "yes")


def fmt_val(v: Any) -> str:
    """Человекочитаемое значение для UI (None → 'не задан', bool → 'вкл/выкл')."""
    if v is None:
        return "не задан"
    if isinstance(v, bool):
        return "вкл" if v else "выкл"
    return str(v)


# Маркер «значение не изменилось» для UI-ввода
UNCHANGED = object()
_KEEP = UNCHANGED  # алиас для совместимости со старым launcher.py


def console_status(text: str):
    """
    Обновляет текущий статус в одной строке консоли.
    Используется для операций, которые выполняются последовательно
    и должны заменять предыдущий статус, а не создавать новую строку.
    """
    print(f"\r{text:<80}", end="", flush=True)


def console_line(text: str = ""):
    """
    Выводит отдельную строку в консоль.
    Используется для сообщений, которые должны сохраниться в журнале
    и не заменяться последующими статусами.
    """
    print(text)


# ============================================================
# 3. ИНДИКАТОРЫ ПРОГРЕССА
# ============================================================

class Progress:
    """Лёгкий индикатор прогресса для консоли (счётчик строк/байт/штук)."""

    def __init__(self, label: str, unit: str = "строк", interval: float = 0.2):
        self.label = label
        self.unit = unit
        self.interval = interval
        self.count = 0
        self.t0 = time.perf_counter()
        self._last = 0.0
        self._render(force=True)

    def _line(self) -> str:
        el = time.perf_counter() - self.t0
        sp = self.count / el if el > 0 else 0
        return f"  {self.label}: {self.count:,} {self.unit}  ({sp:,.0f}/сек)  {el:,.0f}с"

    def _render(self, force: bool = False):
        now = time.perf_counter()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        sys.stdout.write("\r" + self._line().ljust(78))
        sys.stdout.flush()

    def update(self, n: int = 1):
        self.count += n
        self._render()

    def finish(self):
        sys.stdout.write("\r" + self._line().ljust(78) + "\n")
        sys.stdout.flush()


class PercentProgress:
    """Процентный индикатор прогресса с прогресс-баром."""

    def __init__(self, label: str, total: float, unit: str = "MB", interval: float = 0.2):
        self.label = label
        self.total = total if total > 0 else 1
        self.unit = unit
        self.interval = interval
        self.done = 0.0
        self.t0 = time.perf_counter()
        self._last = 0.0
        self._render(force=True)

    def _line(self) -> str:
        el = time.perf_counter() - self.t0
        pct = self.done / self.total * 100
        sp = self.done / el if el > 0 else 0
        filled = int(pct / 100 * 30)
        bar = "█" * filled + "░" * (30 - filled)
        return (
            f"  {self.label}: [{bar}] {pct:5.1f}%  "
            f"{self.done:,.0f}/{self.total:,.0f} {self.unit}  {sp:,.1f}/с"
        )

    def _render(self, force: bool = False):
        now = time.perf_counter()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        sys.stdout.write("\r" + self._line().ljust(90))
        sys.stdout.flush()

    def update(self, n: float = 1):
        self.done += n
        self._render()

    def finish(self):
        sys.stdout.write("\r" + self._line().ljust(90) + "\n")
        sys.stdout.flush()


# ============================================================
# 4. ФОРМАТИРОВАНИЕ ЧИСЕЛ И ВРЕМЕНИ
# ============================================================

def human(n) -> str:
    """Человекочитаемый размер: 1234 → '1.2 КБ'."""
    n = float(n)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


def big(n) -> str:
    """Короткая запись больших чисел: 1.2 млн, 3.4 млрд."""
    if n >= 1e9:
        return f"{n / 1e9:.2f} млрд"
    if n >= 1e6:
        return f"{n / 1e6:.1f} млн"
    if n >= 1e3:
        return f"{n / 1e3:.1f} тыс"
    return f"{n:.0f}"


def fmt(seconds) -> str:
    """Время в формате ЧЧ:ММ:СС."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def short(n) -> str:
    """Короткая запись для прогресс-бара: 1.2G, 3.4M, 5.6k."""
    n = float(n)
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


# ============================================================
# 5. POS-КОНСТАНТЫ (части речи)
# ============================================================

# Словарь OpenCorpora → русский
POS_RU = {
    "NOUN": "существительное",
    "ADJF": "прилагательное",
    "ADJS": "краткое прилагательное",
    "COMP": "сравнительная степень",
    "VERB": "глагол",
    "INFN": "инфинитив",
    "PRTF": "причастие",
    "PRTS": "краткое причастие",
    "GRND": "деепричастие",
    "NUMR": "числительное",
    "ADVB": "наречие",
    "NPRO": "местоимение",
    "PRED": "предикатив",
    "PREP": "предлог",
    "CONJ": "союз",
    "PRCL": "частица",
    "INTJ": "междометие",
}

# Разрешённые части речи для построения графа и классификации
ALLOWED_POS = (
    "существительное",
    "прилагательное",
    "краткое прилагательное",
    "сравнительная степень",
    "глагол",
    "инфинитив",
    "причастие",
    "краткое причастие",
    "деепричастие",
    "наречие",
)

# Служебные части речи — исключаем из графа
STOP_POS = (
    "предлог",
    "союз",
    "частица",
    "местоимение",
    "предикатив",
    "междометие",
)

# Минимальная длина леммы (иначе теряем вк, вб, тг, ру, ок, ai, tv)
MIN_LEMMA_LEN = 2

# Для фасетной классификации — расширенный набор (включая "неизвестно")
ALLOWED_POS_FACET = {
    "существительное",
    "глагол",
    "прилагательное",
    "краткое прилагательное",
    "неизвестно",
}


# ============================================================
# 6. СПРАВОЧНИК ФАСЕТНЫХ ГРУПП
# ============================================================

# Группы используются при инициализации БД и для сопоставления
# ID ↔ название. Если группы уже загружены из seed-CSV — справочник
# используется как резервный источник и для проверки.
GROUP_RULES = {
    1: "Лингвистика", 2: "Существительные", 4: "Прилагательное",
    5: "Краткое прилагательное", 6: "Сравнительная степень",
    8: "Глагол", 9: "Инфинитив", 10: "Причастие", 11: "Краткое причастие",
    12: "Деепричастие", 13: "Наречия", 14: "Местоимения", 15: "Числительные",
    16: "Предлоги", 17: "Союзы", 18: "Частицы", 19: "Междометия",
    20: "Предикативы", 21: "Предметные области", 22: "Свойства товара",
    23: "Тематики", 24: "Человек", 35: "Мебель", 36: "Именованные объекты",
    37: "Бренды", 38: "Компании", 40: "Программы", 45: "Валюты",
    46: "Географические объекты", 47: "Организации", 48: "Технологии",
    50: "Английские служебные слова", 53: "Маркетплейсы",
    55: "Автомобильные бренды", 56: "Видео и стриминг",
    57: "Искусственный интеллект", 58: "Английские слова", 64: "Отели",
    65: "Банки", 66: "Операторы связи", 67: "Криптовалюты",
    70: "Фильмы и сериалы", 71: "Персонажи", 72: "Контент 18+",
    73: "Посуда", 74: "Текстиль", 75: "Крепеж", 76: "Боли", 77: "Возражения",
    79: "Освещение", 80: "Медиа", 81: "Форматы файлов",
    82: "Свойства материалов", 83: "Строительные материалы", 84: "Помещения",
    85: "Модели", 86: "Проблемы", 87: "Производители", 88: "Профессии",
    89: "Эконом-сегмент", 90: "Цена", 91: "Целевая аудитория",
    92: "Характеристики", 93: "Вес", 94: "Объем", 95: "Напряжение",
    96: "IT", 132: "Документы", 176: "Общество", 177: "Животные",
    181: "Спорт", 182: "Политика", 184: "Наука", 185: "Мероприятия",
    187: "Социальные сети", 189: "Продукты питания", 195: "Безопасность",
    199: "Финансы", 201: "Медицина", 205: "Кино", 208: "История",
    214: "Музыка", 220: "Растения", 221: "Образование", 227: "Игрушки",
    231: "Косметика", 235: "Страхование", 236: "Туризм", 242: "Сленг",
    250: "Микрозаймы", 251: "Еда", 254: "Религия", 256: "Купоны и скидки",
    260: "Интернет", 262: "Психология", 263: "Авиация", 267: "Экономика",
    270: "Игры", 271: "Военное дело", 275: "Города", 279: "Топливо",
    284: "Напитки", 286: "Развлечения", 292: "Автомобили",
    293: "Электроника", 294: "Бизнес", 313: "Искусство",
    320: "Поисковые системы", 321: "Сантехника", 328: "Химия",
    329: "Законодательство", 363: "Строительство и ремонт", 400: "Качество",
    402: "Литература", 410: "Инструменты", 413: "Одежда", 417: "Люди",
    424: "Термин", 437: "Сельское хозяйство", 451: "Транспорт",
    492: "Бытовая техника и оборудование",
    493: "Промышленное и техническое оборудование",
    496: "Мечта, желание, цель", 497: "Психология и саморазвитие",
    501: "Места продаж", 502: "Материалы", 503: "Комплектующие",
    504: "Услуги", 505: "Цвета", 506: "Стиль", 507: "Размеры",
    508: "Форма", 509: "Вместимость", 510: "Покрытие и отделка",
    511: "Ткани", 512: "Назначение", 513: "Конструкция", 514: "Артикулы",
    515: "Виды товаров", 516: "Торговое оборудование", 517: "Возраст",
    518: "Пол", 519: "Доход", 520: "Семейное положение", 521: "Интересы",
    522: "Хобби", 523: "Образ жизни", 524: "Ценности", 525: "Потребности",
    526: "Мотивация", 527: "Страхи", 528: "Эмоции", 529: "Привычки",
}


# ============================================================
# 7. РАБОТА С БД
# ============================================================

REQUIRED_INDEXES = [
    ("idx_words_word", "words", "word"),
    ("idx_links_word1", "links", "word1_id"),
    ("idx_links_word2", "links", "word2_id"),
    ("idx_links_word1_word2", "links", "word1_id, word2_id"),
    ("idx_group_words_word_id", "group_words", "word_id"),
    ("idx_group_words_group_id", "group_words", "group_id"),
    ("idx_groups_name", "groups", "name"),
]


def apply_pragmas(conn: sqlite3.Connection):
    """Применяет стандартные PRAGMA для ускорения работы с БД."""
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("PRAGMA cache_size=-10000")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.OperationalError:
        pass


def ensure_indexes(conn: sqlite3.Connection, verbose: bool = True) -> dict:
    """
    Проверяет наличие REQUIRED_INDEXES и создаёт недостающие.
    Возвращает статистику: existing/created/errors/skipped_tables.
    """
    cur = conn.cursor()
    stats = {
        "existing": [],
        "created": [],
        "errors": [],
        "skipped_tables": set(),
    }
    existing_by_table = {}
    for table in ("words", "links", "group_words", "groups"):
        try:
            rows = cur.execute(f"PRAGMA index_list({table})").fetchall()
        except sqlite3.OperationalError as e:
            if verbose:
                print(f"  ⚠️  Таблица {table} не найдена: {e}")
            stats["skipped_tables"].add(table)
            continue
        indexes = {}
        for row in rows:
            idx_name = row[1]
            cols = cur.execute(f"PRAGMA index_info({idx_name})").fetchall()
            col_names = tuple(c[2] for c in cols)
            indexes[idx_name] = col_names
        existing_by_table[table] = indexes

    for idx_name, table, columns_expr in REQUIRED_INDEXES:
        if table in stats["skipped_tables"]:
            continue
        cols_tuple = tuple(c.strip() for c in columns_expr.split(","))
        indexes = existing_by_table.get(table, {})
        found = False
        for existing_name, existing_cols in indexes.items():
            if existing_cols == cols_tuple:
                stats["existing"].append((idx_name, table, columns_expr, existing_name))
                found = True
                break
        if not found:
            sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({columns_expr})"
            try:
                if verbose:
                    print(f"  🔧 Создаю индекс: {idx_name} ON {table}({columns_expr}) ...",
                          end=" ", flush=True)
                t0 = time.perf_counter()
                cur.execute(sql)
                conn.commit()
                elapsed = time.perf_counter() - t0
                if verbose:
                    print(f"OK ({elapsed:.2f}с)")
                stats["created"].append((idx_name, table, columns_expr, elapsed))
            except sqlite3.OperationalError as e:
                if verbose:
                    print(f"ОШИБКА: {e}")
                stats["errors"].append((idx_name, table, columns_expr, str(e)))
    return stats


def open_db(db_path: Path = None, readonly: bool = False, timeout: int = 30) -> sqlite3.Connection:
    """Открывает БД с правильными параметрами."""
    db_path = db_path or DB_FILE
    if readonly:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        apply_pragmas(conn)
    return conn


# ============================================================
# 8. YAML КОНФИГ
# ============================================================

def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивное слияние словарей (override побеждает)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path: Path) -> dict:
    """Загружает YAML-файл. Если файла нет — возвращает {}."""
    if yaml is None:
        raise RuntimeError(
            "PyYAML не установлен. Выполните: .venv\\Scripts\\pip install pyyaml"
        )
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def save_yaml(path: Path, data: dict, backup: bool = True) -> bool:
    """
    Сохраняет YAML-файл. Если backup=True — создаёт .bak копию.
    Возвращает True при успехе, False при ошибке.
    """
    if yaml is None:
        error("PyYAML не установлен.")
        return False
    try:
        if backup and path.exists():
            bak = Path(str(path) + ".bak")
            shutil.copy2(path, bak)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                           default_flow_style=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        error(f"Не удалось сохранить {path.name}: {e}")
        return False


# ============================================================
# 9. ЧИСТКА JSON ОТВЕТОВ AI
# ============================================================

def clean_json(text: str) -> str:
    """
    Подготавливает текст ответа AI к разбору JSON.
    Удаляет Markdown-обёртку ```...```,
    затем пытается выделить JSON-массив [...] или объект {...}.
    Если JSON-фрагмент найти не удалось — возвращает очищенный текст.
    """
    if not text:
        return ""
    text = text.strip()
    # Удаляем Markdown-обёртку
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Ищем JSON-фрагмент
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        p1 = text.find(open_ch)
        p2 = text.rfind(close_ch)
        if p1 != -1 and p2 != -1 and p2 > p1:
            return text[p1:p2 + 1]
    return text


# ============================================================
# 10. UI-ХЕЛПЕРЫ ВВОДА (для лаунчера)
# ============================================================

@dataclass
class Field:
    """Описание одного параметра для универсального экрана редактирования."""
    key: str
    label: str
    type: str          # "int" | "bool" | "choice"
    help: str
    min: int = 0
    none: bool = False
    options: list = dc_field(default_factory=list)


def prompt_int(label: str, current, allow_none: bool = False, min_value: int = 0):
    """Запрос целого числа. UNCHANGED — оставить как было."""
    cur_str = "не задан" if current is None else str(current)
    hint = "Enter=оставить, число=новое" + (", '-'=не задан" if allow_none else "")
    raw = input(f"  {label} [{cur_str}] ({hint}): ").strip()
    if raw == "":
        return UNCHANGED
    if allow_none and raw.lower() in ("-", "none", "нет"):
        return None
    if raw.isdigit():
        v = int(raw)
        if v < min_value:
            error(f"Минимальное значение: {min_value}")
            pause()
            return UNCHANGED
        return v
    error("Некорректное число.")
    pause()
    return UNCHANGED


def prompt_bool(label: str, current: bool):
    """Запрос булева значения. UNCHANGED — оставить как было."""
    cur = "вкл" if current else "выкл"
    raw = input(f"  {label} [{cur}] (Enter=оставить, 1=вкл, 0=выкл): ").strip().lower()
    if raw == "":
        return UNCHANGED
    if raw in ("1", "y", "yes", "д", "да", "+", "вкл"):
        return True
    if raw in ("0", "n", "no", "н", "нет", "-", "выкл"):
        return False
    return UNCHANGED


def prompt_choice(label: str, current, options: list):
    """Запрос выбора из списка. UNCHANGED — оставить как было."""
    print(f"\n  {label} (текущее: {current})")
    for i, opt in enumerate(options, 1):
        mark = "  <- текущее" if opt == current else ""
        print(f"    {i}. {opt}{mark}")
    raw = input("  Номер (Enter=оставить): ").strip()
    if raw == "":
        return UNCHANGED
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return UNCHANGED


# ============================================================
# 11. ЗАПУСК СКРИПТОВ ЧЕРЕЗ VENV
# ============================================================

def run_script(module_name: str, args: list = None) -> bool:
    """
    Запускает скрипт из scripts/ через .venv/python.
    Возвращает True при успехе (код возврата 0).
    """
    script = ROOT / "scripts" / module_name
    if not script.exists():
        error(f"скрипт не найден: scripts/{module_name}")
        return False
    cmd = [str(VENV_PYTHON), str(script)]
    if args:
        cmd.extend(args)
    import subprocess
    return subprocess.call(cmd, cwd=str(ROOT)) == 0


# ============================================================
# 12. РАЗНОЕ
# ============================================================

def get_pattern_marks(word: str) -> list:
    """Эвристические метки для слова (цифры/короткое/латиница/аббр)."""
    marks = []
    if any(c.isdigit() for c in word):
        marks.append("цифры")
    if len(word) < 3:
        marks.append("короткое")
    if word.isascii() and word.isalpha():
        marks.append("латиница")
    if 2 <= len(word) <= 5 and word.isalpha():
        marks.append("аббр")
    return marks


def copy2_if_exists(src: Path, dst: Path):
    """Безопасное копирование: если src нет — ничего не делает."""
    if src.exists():
        shutil.copy2(src, dst)


def ensure_parent(path: Path):
    """Гарантирует существование родительской папки."""
    path.parent.mkdir(parents=True, exist_ok=True)