"""
Инициализация проекта Semantic Facet Analyzer

Единый скрипт подготовки рабочего окружения:
1. Создание структуры базы данных
2. Сборка и распаковка seed-архивов
3. Импорт данных в БД
4. Загрузка словаря лемм (если есть)

Запуск: python scripts/init_project.py
"""
import sys
import time
from pathlib import Path

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))


def header(text: str):
    print()
    print("=" * 70)
    print(f"  {text}")
    print("=" * 70)
    print()


def step(text: str):
    print(f"  ▸ {text}")


def ok(text: str):
    print(f"    ✅ {text}")


def warn(text: str):
    print(f"    ⚠️  {text}")


def err(text: str):
    print(f"    ❌ {text}")


def check_db_exists() -> bool:
    return (ROOT_DIR / "data" / "output" / "semantics.db").exists()


def check_seed_archives() -> bool:
    seed_dir = ROOT_DIR / "data" / "seed"
    if not seed_dir.exists():
        return False
    return any(seed_dir.glob("*.csv.gz")) or any(seed_dir.glob("*.part*"))


def check_lemmas_file() -> bool:
    return (ROOT_DIR / "data" / "seed" / "diclemms.txt").exists()


def run_module(module_name: str):
    """Запускает внешний скрипт как подпроцесс."""
    import subprocess
    script = ROOT_DIR / "scripts" / module_name
    if not script.exists():
        err(f"Скрипт не найден: {script.name}")
        return False
    
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT_DIR),
    )
    return result.returncode == 0


def init_database(force: bool = False):
    header("ЭТАП 1. Создание базы данных")
    
    if check_db_exists() and not force:
        warn("База данных уже существует.")
        answer = input("   Переинициализировать? (y/N): ").strip().lower()
        if answer != "y":
            ok("Пропущено — используем существующую БД.")
            return True
    
    return run_module("init_db.py")


def prepare_seed_data():
    header("ЭТАП 2. Подготовка seed-данных")
    
    if not check_seed_archives():
        warn("Архивы seed-данных не найдены в data/seed/")
        ok("Пропущено — работаем без seed-данных.")
        return True
    
    # 2.1 Сборка частей
    parts = list((ROOT_DIR / "data" / "seed").glob("*.part*"))
    if parts:
        step(f"Найдено частей для сборки: {len(parts)}")
        if not run_module("join_parts.py"):
            err("Сборка частей завершилась с ошибкой.")
            return False
        ok("Части собраны.")
    
    # 2.2 Распаковка
    step("Распаковка сжатых файлов")
    if not run_module("extract_seed.py"):
        err("Распаковка завершилась с ошибкой.")
        return False
    ok("Архивы распакованы.")
    
    return True


def import_data():
    header("ЭТАП 3. Импорт данных в базу")
    
    seed_dir = ROOT_DIR / "data" / "seed"
    csv_files = list(seed_dir.glob("*.csv")) if seed_dir.exists() else []
    
    if not csv_files:
        warn("CSV-файлы для импорта не найдены.")
        ok("Пропущено.")
        return True
    
    step(f"Найдено CSV-файлов: {len(csv_files)}")
    return run_module("import_data.py")


def load_lemmas():
    header("ЭТАП 4. Загрузка словаря лемм")
    
    if not check_lemmas_file():
        warn("Файл diclemms.txt не найден в data/seed/")
        ok("Пропущено — лемматизация будет через pymorphy3.")
        return True
    
    return run_module("load_lemmas.py")


def show_summary():
    header("Итог инициализации")
    
    db_path = ROOT_DIR / "data" / "output" / "semantics.db"
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        ok(f"База данных: {db_path.name} ({size_mb:.2f} MB)")
    else:
        err("База данных не создана.")
    
    seed_dir = ROOT_DIR / "data" / "seed"
    if seed_dir.exists():
        csv_count = len(list(seed_dir.glob("*.csv")))
        ok(f"Seed-данные: {csv_count} CSV-файлов")
    
    dict_dir = ROOT_DIR / "dict"
    if dict_dir.exists():
        dict_count = len(list(dict_dir.glob("*.txt")))
        ok(f"Словари исключений: {dict_count} файлов")
    
    print()
    print("  🎉 Проект готов к работе!")
    print()
    print("  Следующий шаг: положите keywords.txt в data/input/")
    print("  и запустите: python src/graph_builder.py")
    print()


def main():
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   Semantic Facet Analyzer — Инициализация проекта        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    
    start_time = time.perf_counter()
    
    # Проверка аргументов
    force = "--force" in sys.argv
    
    # Этап 1: База данных
    if not init_database(force=force):
        err("Инициализация прервана на этапе 1.")
        sys.exit(1)
    
    # Этап 2: Seed-данные
    if not prepare_seed_data():
        err("Инициализация прервана на этапе 2.")
        sys.exit(1)
    
    # Этап 3: Импорт
    if not import_data():
        err("Инициализация прервана на этапе 3.")
        sys.exit(1)
    
    # Этап 4: Леммы
    if not load_lemmas():
        warn("Загрузка лемм завершилась с ошибкой, но это не критично.")
    
    # Итог
    elapsed = time.perf_counter() - start_time
    show_summary()
    print(f"  Время инициализации: {elapsed:.1f} сек.")
    print()


if __name__ == "__main__":
    main()