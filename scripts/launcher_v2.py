"""
Semantic Facet Analyzer — Лаунчер проекта
Полное управление сборкой семантического ядра: все параметры и этапы.
"""
import os
import sys
import json
import copy
import sqlite3
import subprocess
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    yaml = None


# ============================================================
# 1. UI-ХЕЛПЕРЫ
# ============================================================
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def pause():
    input("\n  Нажмите Enter...")


def error(msg: str):
    print(f"\n  [!] {msg}")


def success(msg: str):
    print(f"  [ok] {msg}")


def screen(title: str):
    """Контекстный менеджер для оформления экрана."""
    class _Screen:
        def __enter__(self):
            clear_screen()
            print("\n" + "=" * 70)
            print(f"  {title}")
            print("=" * 70)
            return self
        def __exit__(self, *args):
            pass
    return _Screen()


def confirm(prompt: str = "Подтверждаете?") -> bool:
    raw = input(f"\n  {prompt} (да/нет): ").strip().lower()
    return raw in ("да", "д", "y", "yes")


def fmt_val(v: Any) -> str:
    if v is None:
        return "не задан"
    if isinstance(v, bool):
        return "вкл" if v else "выкл"
    return str(v)


UNCHANGED = object()


# ============================================================
# 2. ПУТИ ПРОЕКТА
# ============================================================
def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"
CONFIG_DIR = ROOT / "config"
CORE_CONFIG_FILE = CONFIG_DIR / "core_builder.yaml"
DB_FILE = ROOT / "data" / "output" / "semantics.db"
START_WORDS_FILE = ROOT / "data" / "input" / "start_words.txt"
DICT_DIR = ROOT / "dict"
SEED_DIR = ROOT / "data" / "seed"
CORE_OUTPUT_CSV = ROOT / "data" / "output" / "core_words.csv"
CORE_OUTPUT_TXT = ROOT / "data" / "output" / "core_words.txt"
CORE_CHECKPOINT = ROOT / "data" / "output" / "core_words_checkpoint.json"


# ============================================================
# 3. VENV И ЗАПУСК СКРИПТОВ
# ============================================================
def run_script(module_name: str, args: list = None) -> bool:
    script = ROOT / "scripts" / module_name
    if not script.exists():
        error(f"скрипт не найден: scripts/{module_name}")
        return False
    cmd = [str(VENV_PYTHON), str(script)]
    if args:
        cmd.extend(args)
    return subprocess.call(cmd, cwd=str(ROOT)) == 0


def venv_exists() -> bool:
    return VENV_PYTHON.exists()


def create_venv():
    print("\n  Создание виртуального окружения...")
    rc = subprocess.call([sys.executable, "-m", "venv", str(VENV_DIR)], cwd=str(ROOT))
    if rc != 0:
        error("не удалось создать виртуальное окружение.")
        sys.exit(1)
    print("  Виртуальное окружение создано.")


def install_requirements():
    print("\n  Установка зависимостей...")
    if not REQUIREMENTS.exists():
        print(f"  Файл {REQUIREMENTS.name} не найден — пропускаем.")
        return
    rc = subprocess.call(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        cwd=str(ROOT),
    )
    if rc != 0:
        error("установки зависимостей.")
        sys.exit(1)
    print("  Зависимости установлены.")


def ensure_venv():
    if not venv_exists():
        create_venv()
        install_requirements()


# ============================================================
# 4. ПРОВЕРКИ ГОТОВНОСТИ
# ============================================================
def check_status() -> dict:
    kw = ROOT / "data" / "input" / "keywords.txt"
    env = ROOT / ".env"

    has_seed_csv = any(SEED_DIR.glob("*.csv")) if SEED_DIR.exists() else False
    has_seed_parts = any(SEED_DIR.glob("*.part*")) if SEED_DIR.exists() else False
    has_seed_gz = any(SEED_DIR.glob("*.csv.gz")) if SEED_DIR.exists() else False

    db_has_data = False
    if DB_FILE.exists():
        try:
            conn = sqlite3.connect(DB_FILE)
            count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
            conn.close()
            db_has_data = count > 0
        except Exception:
            pass

    kw_size_gb = kw_size_mb = 0
    if kw.exists():
        size_bytes = kw.stat().st_size
        kw_size_gb = size_bytes / (1024 ** 3)
        kw_size_mb = size_bytes / (1024 ** 2)

    return {
        "db": DB_FILE.exists(),
        "db_has_data": db_has_data,
        "db_size_mb": round(DB_FILE.stat().st_size / 1024 / 1024, 1) if DB_FILE.exists() else 0,
        "keywords": kw.exists(),
        "keywords_size_gb": kw_size_gb,
        "keywords_size_mb": kw_size_mb,
        "keywords_is_huge": kw_size_gb > 10,
        "env": env.exists(),
        "seed_csv": has_seed_csv,
        "seed_parts": has_seed_parts,
        "seed_gz": has_seed_gz,
        "need_init": not DB_FILE.exists() or not db_has_data,
    }


def print_status(status: dict):
    print("\n  Статус проекта:\n")
    if not status["db"]:
        print("    [!!] База данных: отсутствует")
        print("         -> выполните пункт 'Инициализация проекта'")
    if not status["keywords"]:
        print("    [--] keywords.txt: не найден")
        print(f"         -> положите файл в: {ROOT / 'data' / 'input' / 'keywords.txt'}")
    else:
        size_str = (f"{status['keywords_size_gb']:.2f} ГБ"
                    if status["keywords_size_gb"] >= 1
                    else f"{status['keywords_size_mb']:.1f} МБ")
        if status["keywords_is_huge"]:
            print(f"    [!] keywords.txt: {size_str} — ОГРОМНЫЙ, рекомендуется сжать")
        else:
            print(f"    [ok] keywords.txt: {size_str}")
    if not status["env"]:
        print("    [--] .env: не настроен")
        print(f"         -> создайте файл: {ROOT / '.env'}")
        print("         -> скопируйте из .env.example и укажите API-ключи")
    if status["db"] and status["keywords"] and status["env"]:
        print("    [ok] Всё готово к работе")
    print()


# ============================================================
# 5. СПРАВКА ПЕРЕД ЗАПУСКОМ
# ============================================================
def show_help(title: str, desc: str, reads: list, writes: list, time_est: str):
    with screen(title):
        print(f"Что делает:\n  {desc}\n")
        print("Откуда читает:")
        for r in reads:
            print(f"  - {r}")
        print("Куда пишет:")
        for w in writes:
            print(f"  - {w}")
        print(f"\nВремя выполнения: {time_est}")
    input("Нажмите Enter для запуска...  ")


# ============================================================
# 6. YAML КОНФИГ
# ============================================================
DEFAULT_CORE_BUILDER = {
    "max_core_size": 200,
    "min_connections": 1,
    "max_connections": None,
    "min_link_weight": 0,
    "min_word_freq": 0,
    "max_word_freq": None,
    "collect_all": True,
    "checkpoint": {"enabled": False, "interval": 10},
    "output": {"format": "csv", "include_all": True, "show_marks": True},
    "mark_dicts": {"enabled": True, "use_all": True, "files": []},
    "mark_groups": {"enabled": True, "groups": {}},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_core_config() -> dict:
    if yaml is None:
        raise RuntimeError(
            "PyYAML не установлен. Выполните: .venv\\Scripts\\pip install pyyaml"
        )
    cfg = {}
    if CORE_CONFIG_FILE.exists():
        cfg = yaml.safe_load(CORE_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(cfg.get("core_builder"), dict):
        cfg["core_builder"] = {}
    cfg["core_builder"] = _deep_merge(DEFAULT_CORE_BUILDER, cfg["core_builder"])
    return cfg


def save_core_config(cfg: dict) -> bool:
    try:
        if CORE_CONFIG_FILE.exists():
            backup = Path(str(CORE_CONFIG_FILE) + ".bak")
            copy_file = __import__("shutil").copy2
            copy_file(CORE_CONFIG_FILE, backup)
        CORE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CORE_CONFIG_FILE.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        error(f"Не удалось сохранить конфиг: {e}")
        pause()
        return False


# ============================================================
# 7. ВВОД ЗНАЧЕНИЙ
# ============================================================
def prompt_int(label: str, current, allow_none: bool = False, min_value: int = 0):
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
    print(f"\n  {label} (текущее: {current})")
    for i, opt in enumerate(options, 1):
        mark = " <- текущее" if opt == current else ""
        print(f"    {i}. {opt}{mark}")
    raw = input("  Номер (Enter=оставить): ").strip()
    if raw == "":
        return UNCHANGED
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    return UNCHANGED


# ============================================================
# 8. ПОЛЯ ПАРАМЕТРОВ (dataclass)
# ============================================================
@dataclass
class Field:
    key: str
    label: str
    type: str
    help: str
    min: int = 0
    none: bool = False
    options: list = dc_field(default_factory=list)


MAIN_FIELDS = [
    Field("max_core_size", "max_core_size", "int",
          "Макс. количество слов в ядре для КАЖДОГО стартового слова (по умолч. 200)", min=1),
    Field("min_connections", "min_connections", "int",
          "Мин. связей кандидата со словами ядра — отсекает случайные (по умолч. 1)", min=1),
    Field("max_connections", "max_connections", "int",
          "Макс. связей — фильтр от слишком общих слов ('-' = не задан)", min=1, none=True),
    Field("min_link_weight", "min_link_weight", "int",
          "Мин. вес связи (сила ассоциации между словами, по умолч. 0)", min=0),
    Field("collect_all", "collect_all", "bool",
          "Собирать всех кандидатов для лога отсева (по умолч. вкл)"),
]

FREQ_FIELDS = [
    Field("min_word_freq", "min_word_freq", "int",
          "Мин. частота слова в корпусе — отсекает редкие (по умолч. 0)", min=0),
    Field("max_word_freq", "max_word_freq", "int",
          "Макс. частота слова — отсекает слишком общие ('-' = не задан)", min=0, none=True),
]

CHECKPOINT_FIELDS = [
    Field("enabled", "checkpoint.enabled", "bool",
          "Сохранять промежуточный результат в core_words_checkpoint.json (по умолч. выкл)"),
    Field("interval", "checkpoint.interval", "int",
          "Сохранять каждые N итераций (по умолч. 10)", min=1),
]

OUTPUT_FIELDS = [
    Field("format", "output.format", "choice",
          "Формат результата: csv (таблица), txt (только слова), both (оба)",
          options=["csv", "txt", "both"]),
    Field("include_all", "output.include_all", "bool",
          "Включать в CSV отсеянных кандидатов (по умолч. вкл)"),
    Field("show_marks", "output.show_marks", "bool",
          "Показывать колонку 'метки' (dict, group) в CSV (по умолч. вкл)"),
]


# ============================================================
# 9. УНИВЕРСАЛЬНЫЕ ЭКРАНЫ
# ============================================================
def params_screen(title: str, cfg: dict, section: dict, fields: list):
    while True:
        with screen(title):
            print()
            for i, f in enumerate(fields, 1):
                print(f"  {i}. {f.label} = {fmt_val(section.get(f.key))}")
                print(f"     {f.help}")
            print()
            print("  0. <- Назад (изменения сохраняются сразу, бэкап: core_builder.yaml.bak)")
        choice = input("\n  Номер параметра для изменения: ").strip()
        if choice == "0":
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(fields)):
            continue
        f = fields[int(choice) - 1]
        cur = section.get(f.key)
        if f.type == "int":
            new = prompt_int(f.label, cur, allow_none=f.none, min_value=f.min)
        elif f.type == "bool":
            new = prompt_bool(f.label, cur)
        elif f.type == "choice":
            new = prompt_choice(f.label, cur, f.options)
        else:
            continue
        if new is UNCHANGED:
            continue
        section[f.key] = new
        if save_core_config(cfg):
            success(f"{f.label} = {fmt_val(new)}")


def toggle_list_screen(title: str, items: list, selected: set,
                       header_note: str = "") -> tuple:
    while True:
        with screen(title):
            if header_note:
                print(f"\n  {header_note}\n")
            for i, name in enumerate(items, 1):
                mark = "x" if name in selected else " "
                print(f"  {i: >3}. [{mark}] {name}")
            print()
            print("  Номер — вкл/выкл | 'все' — выбрать все | 'сброс' — снять все")
            print("  'e' — переключить общий флаг enabled (если применимо)")
            print("  0. <- Назад")
        raw = input("\n  Ваш выбор: ").strip().lower()
        if raw == "0":
            return selected, False, None
        if raw == "e":
            return selected, False, True
        if raw == "все":
            return set(items), True, None
        if raw == "сброс":
            return set(), True, None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            name = items[int(raw) - 1]
            new_selected = set(selected)
            if name in new_selected:
                new_selected.discard(name)
            else:
                new_selected.add(name)
            return new_selected, True, None


# ============================================================
# 10. МЕТКИ СЛОВАРЕЙ
# ============================================================
def core_mark_dicts_screen(cfg: dict):
    md = cfg["core_builder"]["mark_dicts"]
    while True:
        with screen("МЕТКИ СЛОВАРЕЙ (mark_dicts) — пометки в выводе, НЕ исключения"):
            files = sorted(p.name for p in DICT_DIR.glob("*.txt")) if DICT_DIR.exists() else []
            print(f"\n  Каталог: {DICT_DIR}  (файлов .txt: {len(files)})")
            print()
            print(f"  1. enabled = {fmt_val(md.get('enabled'))}")
            print("     Загружать слова из dict/*.txt для пометки 'dict'")
            print(f"  2. use_all = {fmt_val(md.get('use_all'))}")
            print("     Использовать ВСЕ файлы из dict/ (если выкл — только выбранные)")
            print(f"  3. files   = {md.get('files') or '[]'}")
            print("     Выбрать конкретные файлы (работает при use_all=выкл)")
            print()
            print("  0. <- Назад")
        choice = input("\n  Ваш выбор: ").strip()
        if choice == "0":
            return
        if choice == "1":
            new = prompt_bool("enabled", md.get("enabled"))
            if new is not UNCHANGED:
                md["enabled"] = new
                save_core_config(cfg)
        elif choice == "2":
            new = prompt_bool("use_all", md.get("use_all"))
            if new is not UNCHANGED:
                md["use_all"] = new
                save_core_config(cfg)
        elif choice == "3":
            core_dict_files_screen(cfg)


def core_dict_files_screen(cfg: dict):
    md = cfg["core_builder"]["mark_dicts"]
    if not DICT_DIR.exists():
        with screen("Ошибка"):
            error(f"Каталог dict/ не найден: {DICT_DIR}")
        pause()
        return
    files = sorted(p.name for p in DICT_DIR.glob("*.txt"))
    if not files:
        with screen("Ошибка"):
            error("В каталоге dict/ нет файлов .txt")
        pause()
        return

    selected = set(md.get("files") or [])
    note = ("[!] use_all=вкл — сейчас используются ВСЕ файлы. "
            "Выбор действует при use_all=выкл.") if md.get("use_all") else ""

    while True:
        new_selected, changed, _ = toggle_list_screen(
            "ВЫБОР ФАЙЛОВ СЛОВАРЕЙ (dict/*.txt)", files, selected, note
        )
        if not changed:
            return
        selected = new_selected
        md["files"] = sorted(selected)
        save_core_config(cfg)


# ============================================================
# 11. МЕТКИ ГРУПП
# ============================================================
def get_db_group_names() -> list:
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("SELECT name FROM groups ORDER BY name").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def _enabled_groups_from_cfg(groups_cfg: dict) -> set:
    enabled = set()
    for key, value in (groups_cfg or {}).items():
        if isinstance(value, bool) and value:
            enabled.add(key)
        elif isinstance(value, dict):
            for name, en in value.items():
                if en:
                    enabled.add(name)
    return enabled


def core_mark_groups_screen(cfg: dict):
    mg = cfg["core_builder"]["mark_groups"]
    db_groups = get_db_group_names()
    enabled = _enabled_groups_from_cfg(mg.get("groups", {}))
    all_names = sorted(set(db_groups) | enabled)

    if not all_names:
        with screen("Группы не найдены"):
            error("Группы в БД не найдены (таблица groups пуста или БД отсутствует).")
            print("     Сначала выполните фасетную классификацию (ИИ).")
        new = prompt_bool("mark_groups.enabled", mg.get("enabled"))
        if new is not UNCHANGED:
            mg["enabled"] = new
            save_core_config(cfg)
        pause()
        return

    while True:
        note = f"enabled = {fmt_val(mg.get('enabled'))}   (команда 'e' — переключить)\n  Отмечено групп: {len(enabled)} из {len(all_names)}"
        new_enabled, changed, toggle_flag = toggle_list_screen(
            "МЕТКИ ГРУПП ИЗ БД (mark_groups) — пометки 'group', НЕ исключения",
            all_names, enabled, note
        )
        if toggle_flag is True:
            mg["enabled"] = not mg.get("enabled")
            save_core_config(cfg)
            continue
        if not changed:
            return
        enabled = new_enabled
        mg["groups"] = {name: (name in enabled) for name in all_names}
        save_core_config(cfg)


# ============================================================
# 12. СТАРТОВЫЕ СЛОВА
# ============================================================
def _read_start_words() -> list:
    if not START_WORDS_FILE.exists():
        return []
    with open(START_WORDS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _write_start_words(words: list):
    START_WORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(START_WORDS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(words) + ("\n" if words else ""))


def core_start_words_screen():
    while True:
        words = _read_start_words()
        with screen("СТАРТОВЫЕ СЛОВА (data/input/start_words.txt)"):
            print(f"\n  Всего слов: {len(words)}")
            if not START_WORDS_FILE.exists():
                print(f"  [!] Файл не найден: {START_WORDS_FILE}")
            print()
            for i, w in enumerate(words[:30], 1):
                print(f"  {i: >3}. {w}")
            if len(words) > 30:
                print(f"  ... и ещё {len(words) - 30} (пункт 3 — показать все)")
            print()
            print("  1. Добавить слова (через запятую)")
            print("  2. Удалить слова по номерам (через запятую)")
            print("  3. Показать все слова")
            print("  4. Очистить файл")
            print("  0. <- Назад")
        choice = input("\n  Ваш выбор: ").strip()
        if choice == "0":
            return
        if choice == "1":
            raw = input("  Слова через запятую: ").strip()
            new_words = [w.strip().lower() for w in raw.split(",") if w.strip()]
            added = [w for w in new_words if w and w not in words]
            if added:
                words.extend(added)
                _write_start_words(words)
                success(f"Добавлено: {len(added)}")
            else:
                print("  [--] Новых слов нет (пусто или уже в списке).")
            pause()
        elif choice == "2":
            raw = input("  Номера для удаления (через запятую): ").strip()
            nums = sorted({int(x) for x in raw.split(",") if x.strip().isdigit()}, reverse=True)
            removed = 0
            for n in nums:
                if 1 <= n <= len(words):
                    words.pop(n - 1)
                    removed += 1
            if removed:
                _write_start_words(words)
                success(f"Удалено: {removed}")
            pause()
        elif choice == "3":
            with screen(f"Все стартовые слова ({len(words)})"):
                print()
                for i, w in enumerate(words, 1):
                    print(f"  {i: >4}. {w}")
            pause()
        elif choice == "4":
            if confirm("Точно очистить файл?"):
                _write_start_words([])
                success("Файл очищен.")
                pause()


# ============================================================
# 13. ПРЕСЕТЫ
# ============================================================
CORE_PRESETS = {
    "1": {
        "name": "Быстрый (черновой прогон)",
        "desc": "50 слов на старт, жёсткие фильтры — быстро оценить ядро",
        "values": {
            "max_core_size": 50, "min_connections": 2, "max_connections": None,
            "min_link_weight": 5, "min_word_freq": 10, "max_word_freq": None,
            "checkpoint": {"enabled": False},
            "output": {"include_all": False},
        },
    },
    "2": {
        "name": "Стандартный (рекомендуется)",
        "desc": "200 слов на старт, мягкие фильтры, чекпоинты включены",
        "values": {
            "max_core_size": 200, "min_connections": 1, "max_connections": None,
            "min_link_weight": 0, "min_word_freq": 0, "max_word_freq": None,
            "collect_all": True,
            "checkpoint": {"enabled": True, "interval": 10},
            "output": {"format": "csv", "include_all": True, "show_marks": True},
        },
    },
    "3": {
        "name": "Глубокий (полный анализ)",
        "desc": "500 слов на старт, без фильтров, CSV+TXT, чекпоинты каждые 5 итераций",
        "values": {
            "max_core_size": 500, "min_connections": 1, "max_connections": None,
            "min_link_weight": 0, "min_word_freq": 0, "max_word_freq": None,
            "collect_all": True,
            "checkpoint": {"enabled": True, "interval": 5},
            "output": {"format": "both", "include_all": True, "show_marks": True},
        },
    },
    "4": {
        "name": "Без фильтров (всё подряд)",
        "desc": "Все фильтры сняты — посмотреть полный пул кандидатов",
        "values": {
            "max_core_size": 200, "min_connections": 1, "max_connections": None,
            "min_link_weight": 0, "min_word_freq": 0, "max_word_freq": None,
            "collect_all": True,
            "output": {"include_all": True},
        },
    },
}


def core_presets_screen(cfg: dict):
    while True:
        with screen("ПРЕСЕТЫ ПАРАМЕТРОВ ЯДРА"):
            print()
            for key, p in CORE_PRESETS.items():
                print(f"  {key}. {p['name']}")
                print(f"     {p['desc']}")
                print()
            print("  0. <- Назад")
        choice = input("\n  Ваш выбор: ").strip()
        if choice == "0":
            return
        preset = CORE_PRESETS.get(choice)
        if not preset:
            continue
        print(f"\n  Параметры пресета '{preset['name']}':")
        print("   " + json.dumps(preset["values"], ensure_ascii=False, indent=2).replace("\n", "\n   "))
        if confirm("Применить?"):
            cfg["core_builder"] = _deep_merge(cfg["core_builder"], preset["values"])
            if save_core_config(cfg):
                success("Пресет применён и сохранён.")
            pause()


# ============================================================
# 14. ПРОСМОТР / СБРОС КОНФИГА
# ============================================================
def core_show_config_screen(cfg: dict):
    with screen("ТЕКУЩИЙ КОНФИГ (config/core_builder.yaml)"):
        if CORE_CONFIG_FILE.exists():
            print(f"\n  Файл: {CORE_CONFIG_FILE}\n")
            print(CORE_CONFIG_FILE.read_text(encoding="utf-8"))
        else:
            print("\n  [!] Файл ещё не создан — действуют значения по умолчанию:")
            print("  (файл появится после первого изменения любого параметра)\n")
            print(json.dumps({"core_builder": cfg["core_builder"]}, ensure_ascii=False, indent=2))
    pause()


def core_reset_screen(cfg: dict):
    with screen("СБРОС ПАРАМЕТРОВ К ЗНАЧЕНИЯМ ПО УМОЛЧАНИЮ"):
        print("\n  Будут восстановлены значения:")
        print("   " + json.dumps(DEFAULT_CORE_BUILDER, ensure_ascii=False, indent=2).replace("\n", "\n   "))
        print("\n  Старый конфиг сохранится в core_builder.yaml.bak")
    if confirm("Сбросить?"):
        cfg["core_builder"] = copy.deepcopy(DEFAULT_CORE_BUILDER)
        if save_core_config(cfg):
            success("Сброшено к значениям по умолчанию.")
        pause()


# ============================================================
# 15. СПРАВКА ПО АЛГОРИТМУ
# ============================================================
CORE_HELP_TEXT = """
ЭТАПЫ РАБОТЫ АЛГОРИТМА:
0. Предзагрузка БД в память: words, total_links, groups (один проход)
1. Проверка/создание 7 индексов:
   idx_words_word, idx_links_word1, idx_links_word2,
   idx_links_word1_word2, idx_group_words_word_id,
   idx_group_words_group_id, idx_groups_name
2. Загрузка меток: dict/*.txt ('dict') и группы БД ('group')
   — это ПОМЕТКИ в выводе, слова НЕ исключаются
3. Для каждого слова из start_words.txt (итерации до max_core_size):
   а) соседи всех слов ядра (топ-2000 связей по весу, кэшируются)
   б) фильтры: min/max_connections, min/max_word_freq, min_link_weight
   в) score = (сумма весов связей - максимальный вес) / частота слова
   г) выбирается кандидат с максимальным score
   д) добавляется в ядро, повтор до max_core_size или пока есть кандидаты
4. Чекпоинт каждые checkpoint.interval итераций (если включён)
5. Сохранение core_words.csv / core_words.txt:
   сначала слова без меток, затем с метками; сортировка по частоте

ВСЕ ПАРАМЕТРЫ (config/core_builder.yaml -> core_builder):
  max_core_size      (200)      — макс. слов в ядре на каждое стартовое слово
  min_connections    (1)        — мин. связей кандидата со словами ядра
  max_connections    (не задан) — макс. связей (фильтр слишком общих слов)
  min_link_weight    (0)        — мин. вес связи между словами
  min_word_freq      (0)        — мин. частота слова в корпусе
  max_word_freq      (не задан) — макс. частота слова в корпусе
  collect_all        (вкл)      — собирать всех кандидатов для лога отсева
  checkpoint.enabled (выкл)     — промежуточное сохранение ядра
  checkpoint.interval(10)       — сохранять каждые N итераций
  output.format      (csv)      — csv | txt | both
  output.include_all (вкл)      — включать отсеянных кандидатов в CSV
  output.show_marks  (вкл)      — колонка 'метки' в CSV
  mark_dicts.enabled (вкл)      — метки из dict/*.txt
  mark_dicts.use_all (вкл)      — все файлы dict/ или только files
  mark_dicts.files   ([])       — список файлов при use_all=выкл
  mark_groups.enabled(вкл)      — метки из групп БД
  mark_groups.groups ({})       — какие группы отмечать

ФЛАГИ ЗАПУСКА:
  (без флагов)    — обычный запуск с прогресс-барами
  --timing        — время по этапам: load / stat / choose / add
  --profile       — cProfile: топ-30 функций (cumulative и tottime)
  --check-indexes — только проверить/создать индексы и выйти

ФАЙЛЫ:
  Читает: data/input/start_words.txt, data/output/semantics.db,
          config/core_builder.yaml, dict/*.txt
  Пишет:  data/output/core_words.csv, data/output/core_words.txt,
          data/output/core_words_checkpoint.json
"""


def core_help_screen():
    with screen("СПРАВКА: СБОРКА СЕМАНТИЧЕСКОГО ЯДРА (core_builder.py)"):
        print(CORE_HELP_TEXT)
    pause()


# ============================================================
# 16. ЗАПУСКИ core_builder.py
# ============================================================
def core_launch(cfg: dict, extra_args: list = None, mode: str = "Обычный запуск"):
    cb = cfg["core_builder"]
    if not DB_FILE.exists():
        with screen("Ошибка"):
            error(f"БД не найдена: {DB_FILE}")
            print("     Сначала выполните 'Инициализация проекта' и внесите keywords.txt в базу.")
        pause()
        return
    start_words = _read_start_words()
    if not start_words:
        with screen("Ошибка"):
            error(f"Файл стартовых слов пуст или не найден: {START_WORDS_FILE}")
            print("     Добавьте слова через пункт 'Стартовые слова'.")
        pause()
        return

    with screen(f"СБОРКА ЯДРА — {mode}"):
        print(f"\n  Стартовых слов: {len(start_words)}")
        print("\n  Текущие параметры:")
        print(f"    max_core_size={cb.get('max_core_size')}  min_connections={cb.get('min_connections')}   "
              f"max_connections={fmt_val(cb.get('max_connections'))}")
        print(f"    min_link_weight={cb.get('min_link_weight')}  min_word_freq={cb.get('min_word_freq')}   "
              f"max_word_freq={fmt_val(cb.get('max_word_freq'))}")
        print(f"    collect_all={fmt_val(cb.get('collect_all'))}")
        ckpt = cb.get("checkpoint", {})
        out = cb.get("output", {})
        print(f"    checkpoint: {fmt_val(ckpt.get('enabled'))} (каждые {ckpt.get('interval')} итераций)")
        print(f"    output: format={out.get('format')}  include_all={fmt_val(out.get('include_all'))}   "
              f"show_marks={fmt_val(out.get('show_marks'))}")
        md = cb.get("mark_dicts", {})
        mg = cb.get("mark_groups", {})
        print(f"    метки: dict={fmt_val(md.get('enabled'))} (use_all={fmt_val(md.get('use_all'))})   "
              f"group={fmt_val(mg.get('enabled'))} ({len(_enabled_groups_from_cfg(mg.get('groups', {})))} групп)")
        if extra_args:
            print(f"\n  Флаги запуска: {' '.join(extra_args)}")
        print("\n  Этапы: предзагрузка БД -> индексы -> метки -> итерации отбора")
        print("         (соседи -> фильтры -> score -> лучший кандидат) -> чекпоинты -> сохранение")

        reads = ["data/input/start_words.txt", "data/output/semantics.db", "config/core_builder.yaml"]
        if md.get("enabled"):
            reads.append("dict/*.txt")
        writes = []
        fmt = out.get("format", "csv")
        if fmt in ("csv", "both"):
            writes.append("data/output/core_words.csv")
        if fmt in ("txt", "both"):
            writes.append("data/output/core_words.txt")
        if ckpt.get("enabled"):
            writes.append("data/output/core_words_checkpoint.json")
        print("\n  Читает:")
        for r in reads:
            print(f"    - {r}")
        print("  Пишет:")
        for w in writes:
            print(f"    - {w}")
        print(f"\n  Время: ~{max(1, len(start_words))}–{max(3, len(start_words) * 3)} мин  "
              f"(зависит от max_core_size и связности графа)")

    choice = input("  Enter — запуск, 0 — отмена: ").strip()
    if choice == "0":
        return
    print()
    run_script("core_builder.py", extra_args)
    pause()


def core_check_indexes():
    if not DB_FILE.exists():
        with screen("Ошибка"):
            error(f"БД не найдена: {DB_FILE}")
        pause()
        return
    with screen("ПРОВЕРКА/СОЗДАНИЕ ИНДЕКСОВ БД (--check-indexes)"):
        print("\n  Проверяет и при необходимости создаёт 7 индексов:")
        print("    - idx_words_word            (words.word)")
        print("    - idx_links_word1           (links.word1_id)")
        print("    - idx_links_word2           (links.word2_id)")
        print("    - idx_links_word1_word2     (links.word1_id, word2_id)")
        print("    - idx_group_words_word_id   (group_words.word_id)")
        print("    - idx_group_words_group_id  (group_words.group_id)")
        print("    - idx_groups_name           (groups.name)")
        print("\n  Данные НЕ меняются. На большой БД создание индексов может занять минуты.")
    choice = input("  Enter — запуск, 0 — отмена: ").strip()
    if choice == "0":
        return
    print()
    run_script("core_builder.py", ["--check-indexes"])
    pause()


# ============================================================
# 17. ПОДМЕНЮ СБОРКИ ЯДРА
# ============================================================
def menu_core(status: dict):
    while True:
        try:
            cfg = load_core_config()
        except Exception as e:
            with screen("Ошибка чтения конфига"):
                error("Не удалось прочитать config/core_builder.yaml")
                print(f"  {e}")
                print("  Проверьте, что pyyaml установлен в .venv (есть в requirements.txt).")
            pause()
            return

        cb = cfg["core_builder"]
        with screen("СБОРКА СЕМАНТИЧЕСКОГО ЯДРА (core_builder.py)"):
            cfg_note = "" if CORE_CONFIG_FILE.exists() else "  (файл не создан — значения по умолчанию)"
            print(f"\n  Конфиг: config/core_builder.yaml{cfg_note}")
            print(f"  Сейчас: max_core_size={cb.get('max_core_size')}   "
                  f"min_conn={cb.get('min_connections')}  max_conn={fmt_val(cb.get('max_connections'))}   "
                  f"min_weight={cb.get('min_link_weight')}")
            print(f"          freq: {cb.get('min_word_freq')}–{fmt_val(cb.get('max_word_freq'))}   "
                  f"format={cb.get('output', {}).get('format')}   "
                  f"checkpoint={fmt_val(cb.get('checkpoint', {}).get('enabled'))}")
            print(f"  Стартовых слов: {len(_read_start_words())}")
            print()
            print("  -- ЗАПУСК ---------------------------------------------------")
            print("   1. Запуск сборки ядра (текущие параметры)")
            print("   2. Запуск с таймингом (--timing: load/stat/choose/add)")
            print("   3. Запуск с профилированием (--profile: топ-30 функций)")
            print("   4. Проверить/создать индексы БД (--check-indexes)")
            print("  -- ПАРАМЕТРЫ ------------------------------------------------")
            print("   5. Основные параметры выборки (max_core_size, connections, weight)")
            print("   6. Фильтры частотности (min/max_word_freq)")
            print("   7. Чекпоинты (enabled, interval)")
            print("   8. Параметры вывода (format, include_all, show_marks)")
            print("   9. Метки словарей (mark_dicts: dict/*.txt)")
            print("  10. Метки групп из БД (mark_groups)")
            print("  11. Стартовые слова (start_words.txt)")
            print("  -- СЕРВИС ---------------------------------------------------")
            print("  12. Пресеты (быстрый / стандартный / глубокий / без фильтров)")
            print("  13. Показать текущий конфиг (YAML)")
            print("  14. Сброс к значениям по умолчанию")
            print("  15. Справка: все параметры и этапы алгоритма")
            print()
            print("   0. <- Назад в главное меню")

        choice = input("\n  Ваш выбор: ").strip()
        handlers = {
            "1":  lambda: core_launch(cfg),
            "2":  lambda: core_launch(cfg, ["--timing"], "Запуск с таймингом (--timing)"),
            "3":  lambda: core_launch(cfg, ["--profile"], "Запуск с профилированием (--profile)"),
            "4":  core_check_indexes,
            "5":  lambda: params_screen("ОСНОВНЫЕ ПАРАМЕТРЫ ВЫБОРКИ", cfg, cb, MAIN_FIELDS),
            "6":  lambda: params_screen("ФИЛЬТРЫ ЧАСТОТНОСТИ", cfg, cb, FREQ_FIELDS),
            "7":  lambda: params_screen("ЧЕКПОИНТЫ", cfg, cb["checkpoint"], CHECKPOINT_FIELDS),
            "8":  lambda: params_screen("ПАРАМЕТРЫ ВЫВОДА", cfg, cb["output"], OUTPUT_FIELDS),
            "9":  lambda: core_mark_dicts_screen(cfg),
            "10": lambda: core_mark_groups_screen(cfg),
            "11": core_start_words_screen,
            "12": lambda: core_presets_screen(cfg),
            "13": lambda: core_show_config_screen(cfg),
            "14": lambda: core_reset_screen(cfg),
            "15": core_help_screen,
        }
        if choice == "0":
            return
        handler = handlers.get(choice)
        if handler:
            handler()
        else:
            error("Неверный выбор.")
            pause()


# ============================================================
# 18. ОСТАЛЬНЫЕ ПУНКТЫ МЕНЮ
# ============================================================
def menu_init(status: dict):
    clear_screen()
    print("\n  Инициализация проекта")
    print("  =====================")
    print("  Подготовка рабочего окружения...\n")
    run_script("init_project.py")
    pause()


def menu_build_graph(status: dict):
    if status.get("keywords_is_huge"):
        with screen("ВНИМАНИЕ: keywords.txt ОГРОМНЫЙ"):
            print(f"\n  Размер файла: {status['keywords_size_gb']:.2f} ГБ")
            print("\n  Обработка такого файла займёт ОЧЕНЬ МНОГО времени (часы/дни)")
            print("  и может привести к нехватке памяти.")
            print("\n  РЕКОМЕНДАЦИЯ: сначала сожмите файл через пункт меню")
            print("     'Сжать keywords.txt' — это уберёт мусор и ускорит работу.")
            print("\n  Что делаем?")
            print("    1. Продолжить всё равно (будет долго)")
            print("    0. Вернуться в меню и сжать файл")
        choice = input("\n  Ваш выбор: ").strip()
        if choice != "1":
            return

    show_help(
        "Внести данные в базу данных из keywords.txt",
        "Читает фразы и частоты, лемматизирует слова и добавляет новые связи в граф.",
        ["data/input/keywords.txt", "data/output/semantics.db (таблицы words, links)"],
        ["data/output/semantics.db (обновляются слова и связи)"],
        "~1-5 минут (зависит от размера keywords.txt)",
    )
    run_script("graph_builder.py")
    pause()


def menu_filter_keywords(status: dict):
    kw_path = ROOT / "data" / "input" / "keywords.txt"
    if not kw_path.exists():
        with screen("Ошибка"):
            error("Файл keywords.txt не найден.")
            print(f"     Путь: {kw_path}")
        pause()
        return

    while True:
        size_gb = kw_path.stat().st_size / (1024 ** 3)
        size_mb = kw_path.stat().st_size / (1024 ** 2)
        size_str = f"{size_gb:.2f} ГБ" if size_gb >= 1 else f"{size_mb:.1f} МБ"

        with screen("СЖАТИЕ keywords.txt — удаление мусорных строк"):
            print(f"\n  Файл: {kw_path.relative_to(ROOT)}")
            print(f"  Размер: {size_str}")
            print("\n  Что делает скрипт:")
            print("    - Оставляет только строки с частотой > порога")
            print("    - Удаляет мусор (слова с частотой 1-5)")
            print("    - Файл становится в 5-20 раз меньше")
            print("    - Создаёт бэкап keywords.txt.bak")
            print("\n  РЕКОМЕНДАЦИИ:")
            print("    - Для файлов > 10 ГБ — порог 1000 или 10000")
            print("    - Для файлов 1-10 ГБ — порог 100")
            print("    - Для маленьких файлов — порог 5-10")
            print("\n  ВАЖНО: сначала запустите ПРЕДПРОСМОТР, чтобы увидеть,")
            print("         сколько строк будет удалено при каждом пороге.")
            print("\n" + "-" * 70)
            print("  МЕНЮ:")
            print()
            print("    1. Предпросмотр (без изменений, безопасно)")
            print("    2. Применить с порогом > 5     (мягкая чистка)")
            print("    3. Применить с порогом > 10    (средняя чистка)")
            print("    4. Применить с порогом > 100   (сильная чистка)")
            print("    5. Применить с порогом > 1000  (жёсткая чистка)")
            print("    6. Применить с порогом > 10000 (очень жёсткая)")
            print("    7. Применить с СВОИМ порогом")
            print("    0. <- Назад в главное меню")

        choice = input("\n  Ваш выбор: ").strip()
        if choice == "0":
            return
        if choice == "1":
            print("\n  Запуск предпросмотра (dry-run)...\n")
            run_script("filter_keywords.py")
            pause()
        elif choice in ("2", "3", "4", "5", "6"):
            thresholds = {"2": 5, "3": 10, "4": 100, "5": 1000, "6": 10000}
            threshold = thresholds[choice]
            print(f"\n  Применение с порогом > {threshold}...\n")
            run_script("filter_keywords.py", ["--threshold", str(threshold), "--apply"])
            pause()
        elif choice == "7":
            custom = input("  Введите порог (число): ").strip()
            if not custom.isdigit():
                error("Некорректное число.")
                pause()
                continue
            print(f"\n  Применение с порогом > {custom}...\n")
            run_script("filter_keywords.py", ["--threshold", custom, "--apply"])
            pause()
        else:
            error("Неверный выбор.")
            pause()


def menu_hypotheses(status: dict):
    show_help(
        "Собрать связи через слово (из start_words.txt)",
        "Ищет отсутствующие прямые связи через BFS глубиной 2 (гипотезы семантического расширения).",
        ["data/input/start_words.txt", "data/output/semantics.db"],
        ["data/output/hypotheses.txt"],
        "~2-10 минут (зависит от связности графа)",
    )
    run_script("hypothesis_finder.py")
    pause()


def menu_facets(status: dict):
    show_help(
        "Классификация слов по группам (ИИ)",
        "Отправляет слова из базы на фасетную классификацию через ансамбль LLM-моделей.",
        ["data/output/semantics.db", ".env (API-ключи)"],
        ["data/output/facets.txt", "data/output/bad_models.txt"],
        "Зависит от количества слов и скорости API (минуты/часы)",
    )
    run_script("ai_classifier.py")
    pause()


def menu_test_api(status: dict):
    clear_screen()
    print("\n  Тест API-ключей")
    print("  ===============\n")
    run_script("test_openrouter_api.py")
    pause()

def menu_cleanup_orphans(status: dict):
    """Очистка слов без связей."""
    if not status.get("db"):
        clear_screen()
        print("\n  [!] БД не найдена. Сначала выполните инициализацию.")
        input("\n  Нажмите Enter...")
        return

    while True:
        clear_screen()
        print("\n" + "=" * 70)
        print("  ОЧИСТКА БД ОТ СЛОВ БЕЗ СВЯЗЕЙ")
        print("=" * 70)
        print("\n  Удаляет из words и group_words записи,")
        print("  у которых нет ни одной связи в таблице links.")
        print("\n  Зачем:")
        print("    - уменьшает размер БД (иногда на 30-50%)")
        print("    - ускоряет все запросы (меньше данных)")
        print("    - не влияет на сборку ядра (сироты там не используются)")
        print("\n  Безопасность:")
        print("    - автоматически создаётся бэкап БД")
        print("    - всё в транзакции — при ошибке откат")
        print("    - links и groups не трогаются")
        print("\n" + "-" * 70)
        print("  МЕНЮ:")
        print()
        print("    1. Предпросмотр (показать, что будет удалено)")
        print("    2. Применить очистку (с бэкапом и VACUUM)")
        print("    0. Назад в главное меню")
        print()

        choice = input("  Ваш выбор: ").strip()
        if choice == "0":
            return
        elif choice == "1":
            print("\n  Запуск предпросмотра...\n")
            run_script("cleanup_orphan_words.py")
            input("\n  Нажмите Enter...")
        elif choice == "2":
            print("\n  Запуск очистки...\n")
            run_script("cleanup_orphan_words.py", ["--apply"])
            input("\n  Нажмите Enter...")
        else:
            print("  Неверный выбор.")
            input("  Нажмите Enter...")

def menu_help(status: dict):
    with screen("СПРАВКА"):
        print("  SEMANTIC FACET ANALYZER")
        print("  Инструмент для анализа поисковых запросов с помощью графовых алгоритмов и ИИ.\n")
        print("  КАК НАЧАТЬ:")
        print("  1. Пункт 1: Инициализация проекта (создаст БД и загрузит seed-данные)")
        print("  2. Положить keywords.txt и start_words.txt в data/input/")
        print("  2.5. Если keywords.txt большой — сожмите его через соответствующий пункт")
        print("  3. Пункт 'Внести данные из keywords.txt в базу'")
        print("  4. Пункт 'Собрать ядро' — все параметры, пресеты, режимы запуска")
        print("     и справка по этапам доступны в его подменю")
        print("  5. Гипотезы и классификация (ИИ) — по желанию\n")
        print("  ТРЕБОВАНИЯ:")
        print("  - Python 3.10+")
        print("  - API-ключ хотя бы одного ИИ-PROVIDER (для фасетной классификации)")
        print("  - ~2 ГБ свободного места для базы данных")
    pause()


# ============================================================
# 19. ЭКСПОРТ ДАННЫХ ИЗ БД В ФАЙЛЫ
# ============================================================
def menu_export_db(status: dict):
    with screen("ЭКСПОРТ ДАННЫХ ИЗ БД В ТЕКСТОВЫЕ ФАЙЛЫ"):
        print(f"\n  БД: {DB_FILE}")
        print(f"  Размер БД: {status.get('db_size_mb', 0)} МБ")
        print(f"\n  Экспортирует данные в каталог: {SEED_DIR}")
        print("\n  Будут созданы файлы:")
        print("    - words.txt        (слова и частоты)")
        print("    - links.txt        (связи между словами)")
        print("    - groups.txt       (названия групп)")
        print("    - group_words.txt  (слова в группах)")
        print("\n  Эти файлы можно:")
        print("    - перенести на другой компьютер вместе с проектом")
        print("    - использовать как бэкап данных")
        print("    - загрузить через 'Инициализация проекта' на новом месте")
        print("\n  Существующие файлы в data/seed/ будут перезаписаны.")

    if not confirm("Выполнить экспорт?"):
        return

    print()
    run_script("export_db.py")
    pause()


# ============================================================
# 20. ГЛАВНОЕ МЕНЮ (декларативно)
# ============================================================
# (key, label_fn, visible_fn, handler)
MENU_ITEMS = [
    (
        "init",
        lambda s: "Инициализация проекта (БД + данные)",
        lambda s: s["need_init"],
        menu_init,
    ),
    (
        "export_db",
        lambda s: "Экспорт данных из БД в файлы (для переноса/бэкапа)",
        lambda s: s["db"] and s["db_has_data"],  # <-- показываем только если БД есть и не пустая
        menu_export_db,
    ),
    (
        "filter",
        lambda s: "Сжать keywords.txt (удалить мусор)" + (
            " [! ОГРОМНЫЙ ФАЙЛ — рекомендуется сжать]" if s.get("keywords_is_huge") else ""
        ),
        lambda s: s["keywords"],
        menu_filter_keywords,
    ),
    (
        "build",
        lambda s: "Внести данные в базу данных из keywords.txt",
        lambda s: s["keywords"],
        menu_build_graph,
    ),
    (
        "core",
        lambda s: "Собрать ядро из start_words.txt (все параметры и этапы)",
        lambda s: s["db"],
        menu_core,
    ),
    (
        "hypotheses",
        lambda s: "Собрать связи через слово (из start_words.txt)",
        lambda s: s["db"],
        menu_hypotheses,
    ),
    (
        "facets",
        lambda s: "Классификация слов по группам (ИИ)",
        lambda s: s["env"],
        menu_facets,
    ),
    (
        "test_api",
        lambda s: "Тест API-ключей",
        lambda s: True,
        menu_test_api,
    ),
    (
        "help",
        lambda s: "Справка",
        lambda s: True,
        menu_help,
    ),
]


def render_and_dispatch(status: dict) -> bool:
    print("\n  МЕНЮ ЗАПУСКА\n")
    visible = [(i + 1, key, label_fn(status), handler)
               for i, (key, label_fn, visible_fn, handler) in enumerate(MENU_ITEMS)
               if visible_fn(status)]
    for num, key, label, _ in visible:
        print(f"  {num}. {label}")
    print("  0. Выход\n")

    choice = input("  Выберите действие (0-9): ").strip()
    if choice == "0":
        return False

    if choice.isdigit():
        idx = int(choice)
        match = [item for item in visible if item[0] == idx]
        if match:
            _, _, _, handler = match[0]
            handler(status)
            # <-- ВАЖНО: очищаем экран после выполнения пункта меню,
            # чтобы при возврате в главное меню не было "каши" из предыдущего вывода
            clear_screen()
            return True

    error("Неверный выбор.")
    pause()
    return True


# ============================================================
# 21. ТОЧКА ВХОДА
# ============================================================
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ensure_venv()  # <-- убран первый заголовок (был дубль)

    while True:
        status = check_status()
        clear_screen()
        print("\n  Semantic Facet Analyzer — Лаунчер\n")  # <-- единственный заголовок
        print_status(status)
        if not render_and_dispatch(status):
            clear_screen()
            print("\n  До свидания!\n")
            break


if __name__ == "__main__":
    main()