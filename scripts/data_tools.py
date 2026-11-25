"""
scripts/data_tools.py — единый инструмент для работы с данными проекта.

Подкоманды:
  init          — полная инициализация проекта (БД + seed + импорт)
  init-db       — создание пустой БД с таблицами
  import        — импорт CSV из data/seed/ в БД
  export        — экспорт БД в CSV + 7z-архивы
  extract       — распаковка .csv.gz из data/seed/
  join          — сборка разбитых частей (*.part* → *.csv)
  filter        — фильтрация keywords.txt по частоте
  dict-update   — обновление словарей из JSON-файла

Запуск:
  python scripts/data_tools.py init
  python scripts/data_tools.py filter --threshold 100 --apply
  python scripts/data_tools.py --help
"""
import argparse
import csv
import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from common import (
    ROOT, DB_FILE, SEED_DIR, INPUT_DIR, OUTPUT_DIR, INPUT_FILE,
    Progress, PercentProgress,
    GROUP_RULES, ensure_indexes, REQUIRED_INDEXES,
    open_db,
)


# ============================================================
# ХЕЛПЕРЫ ВЫВОДА
# ============================================================

def header(text: str):
    """Печатает заголовок этапа."""
    print()
    print("=" * 70)
    print(f"  {text}")
    print("=" * 70)
    print()


def step(text: str):
    """Печатает шаг."""
    print(f"  ▸ {text}")


def ok(text: str):
    """Печатает успешное завершение."""
    print(f"    ✅ {text}")


def warn(text: str):
    """Печатает предупреждение."""
    print(f"    ⚠️  {text}")


def err(text: str):
    """Печатает ошибку."""
    print(f"    ❌ {text}")


def size_mb(path: Path) -> float:
    """Размер файла в МБ."""
    return round(path.stat().st_size / 1024 / 1024, 1) if path.exists() else 0


# ============================================================
# 1. INIT-DB — создание пустой БД
# ============================================================

def cmd_init_db(force: bool = False) -> bool:
    """
    Создаёт пустую БД со схемой V2.
    Если БД уже существует — спрашивает подтверждение (или --force).
    """
    header("Создание базы данных")

    if DB_FILE.exists():
        if not force:
            warn("База данных уже существует.")
            answer = input("   Переинициализировать? (y/N): ").strip().lower()
            if answer != "y":
                ok("Пропущено — используем существующую БД.")
                return True
        DB_FILE.unlink()
        print("  Старая база удалена.")

    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Создаю базу: {DB_FILE}")

    conn = sqlite3.connect(str(DB_FILE))
    cursor = conn.cursor()

    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA foreign_keys = OFF")

    # Таблицы
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS words
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       word
                       TEXT
                       NOT
                       NULL
                       UNIQUE,
                       pos
                       TEXT,
                       count
                       INTEGER
                       NOT
                       NULL
                       DEFAULT
                       0,
                       hits
                       INTEGER
                       NOT
                       NULL
                       DEFAULT
                       0
                   )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS links
                   (
                       word1_id
                       INTEGER
                       NOT
                       NULL,
                       word2_id
                       INTEGER
                       NOT
                       NULL,
                       count
                       INTEGER
                       NOT
                       NULL
                       DEFAULT
                       0,
                       hits
                       INTEGER
                       NOT
                       NULL
                       DEFAULT
                       0,
                       PRIMARY
                       KEY
                   (
                       word1_id,
                       word2_id
                   )
                       )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS lemmas
                   (
                       word
                       TEXT
                       PRIMARY
                       KEY,
                       lemma
                       TEXT
                       NOT
                       NULL,
                       pos
                       TEXT
                   )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS groups
                   (
                       id
                       INTEGER
                       PRIMARY
                       KEY,
                       parent_id
                       INTEGER,
                       name
                       TEXT
                       NOT
                       NULL
                       UNIQUE,
                       description
                       TEXT,
                       type
                       INTEGER
                   )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS group_words
                   (
                       word_id
                       INTEGER
                       NOT
                       NULL,
                       group_id
                       INTEGER
                       NOT
                       NULL,
                       PRIMARY
                       KEY
                   (
                       word_id,
                       group_id
                   )
                       )
                   """)

    # Справочник групп — заполняется из seed-CSV при импорте.
    # GROUP_RULES используется как резервный источник.
    # Если нужно заполнить вручную — раскомментируй:
    # cursor.executemany(
    #     "INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)",
    #     list(GROUP_RULES.items())
    # )

    conn.commit()
    conn.close()

    ok(f"База создана: {DB_FILE.name}")
    ok(f"Размер: {DB_FILE.stat().st_size:,} байт")
    ok(f"Справочник групп: {len(GROUP_RULES)} записей (в памяти)")
    return True


# ============================================================
# 2. IMPORT — импорт seed-CSV в БД
# ============================================================

TABLES = [
    ("words.csv", "words", {"skip_cols": {"id"}}),
    ("groups.csv", "groups", {}),
    ("lemmas.csv", "lemmas", {}),
    ("group_words.csv", "group_words", {}),
    ("links.csv", "links", {}),
]

BATCH = 100_000


def detect_delimiter(csv_path: Path) -> str:
    """Определяет разделитель CSV (табуляция или запятая)."""
    with csv_path.open("r", encoding="utf-8") as f:
        first_line = f.readline()
    return "\t" if "\t" in first_line else ","


def import_table(cursor, csv_path: Path, table: str, skip_cols: set = None) -> int:
    """Импортирует один CSV в таблицу БД. БЕЗ создания индексов."""
    skip_cols = skip_cols or set()
    delimiter = detect_delimiter(csv_path)
    print(f"  Импорт {csv_path.name} → {table}...")
    t0 = time.perf_counter()

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        try:
            headers = next(reader)
        except StopIteration:
            print(f"    ⚠️  Файл пуст.")
            return 0

        headers = [h.strip().lstrip("\ufeff") for h in headers]
        filtered_headers = [h for h in headers if h not in skip_cols]
        col_indices = [i for i, h in enumerate(headers) if h not in skip_cols]
        placeholders = ",".join(["?"] * len(filtered_headers))
        col_names = ",".join(filtered_headers)
        sql = f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})"

        count = 0
        batch = []
        for row in reader:
            if not row or len(row) < len(headers):
                continue
            filtered_row = tuple(row[i] for i in col_indices)
            batch.append(filtered_row)
            count += 1
            if len(batch) >= BATCH:
                cursor.executemany(sql, batch)
                batch = []
                if count % 500_000 == 0:
                    print(f"\r    {table}: {count:,} строк...", end="", flush=True)

        if batch:
            cursor.executemany(sql, batch)

    elapsed = time.perf_counter() - t0
    print(f"  ✅ {table}: {count:,} строк за {elapsed:.1f} с")
    return count


def cmd_import() -> bool:
    """Импортирует seed-CSV из data/seed/ в semantics.db."""
    if not DB_FILE.exists():
        err(f"База не найдена: {DB_FILE}")
        print("   Сначала: python scripts/data_tools.py init-db")
        return False

    header("Импорт данных в semantics.db")

    conn = sqlite3.connect(str(DB_FILE))
    cursor = conn.cursor()

    # Ускоряем импорт
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.execute("PRAGMA cache_size = -64000")

    t0 = time.perf_counter()
    grand = 0

    for fname, table, opts in TABLES:
        path = SEED_DIR / fname
        if not path.exists():
            print(f"    ⏭  {fname} не найден.")
            continue
        n = import_table(cursor, path, table, opts.get("skip_cols", set()))
        grand += n

    # Индексы создаются ПОСЛЕ импорта всех таблиц
    print("\n" + "=" * 60)
    print("  Создание индексов (один раз, после всего импорта)...")
    print("=" * 60)
    idx_stats = ensure_indexes(conn, verbose=True)

    conn.close()

    ok(f"Импорт завершён. Всего строк: {grand:,} за {time.perf_counter() - t0:.0f} с")
    return True


# ============================================================
# 3. EXTRACT — распаковка .csv.gz
# ============================================================

CHUNK_EXTRACT = 1024 * 1024  # 1 MB


def cmd_extract() -> bool:
    """Распаковывает .csv.gz архивы из data/seed/ в CSV."""
    if not SEED_DIR.exists():
        err(f"Папка не найдена: {SEED_DIR}")
        return False

    gz_files = list(SEED_DIR.glob("*.csv.gz"))
    if not gz_files:
        ok("Архивов *.csv.gz не найдено.")
        return True

    print(f"  Найдено архивов: {len(gz_files)}")

    for gz in gz_files:
        csv_path = gz.with_suffix("")  # links.csv.gz -> links.csv
        prog = Progress(gz.name, unit="MB")
        with gzip.open(gz, "rb") as fin, open(csv_path, "wb") as fout:
            while chunk := fin.read(CHUNK_EXTRACT):
                fout.write(chunk)
                prog.update(len(chunk) / CHUNK_EXTRACT)
        prog.finish()

    ok("Распаковка завершена.")
    return True


# ============================================================
# 4. JOIN — сборка .part в CSV
# ============================================================

CHUNK_JOIN = 1024 * 1024


def cmd_join() -> bool:
    """Собирает разбитые на части архивы (.part) в CSV."""
    parts = sorted(SEED_DIR.glob("*.part*"))
    if not parts:
        ok("Разбитых частей не найдено.")
        return True

    # Группируем по базовому имени: links.csv.part01 -> links.csv
    groups: dict[str, list[Path]] = {}
    for p in parts:
        base = p.name.split(".part")[0]
        groups.setdefault(base, []).append(p)

    for base, files in groups.items():
        out_path = SEED_DIR / base
        total = sum(f.stat().st_size for f in files)
        prog = PercentProgress(base, total=total / CHUNK_JOIN, unit="MB")
        with open(out_path, "wb") as fout:
            for f in files:
                with open(f, "rb") as fin:
                    while chunk := fin.read(CHUNK_JOIN):
                        fout.write(chunk)
                        prog.update(len(chunk) / CHUNK_JOIN)
        prog.finish()
        print(f"    ✅ Файл собран: {out_path.name} "
              f"({out_path.stat().st_size / 1024 / 1024:.1f} MB)")

    ok("Готово.")
    return True


# ============================================================
# 5. FILTER — фильтрация keywords.txt
# ============================================================

BACKUP_FILE = INPUT_DIR / "keywords.txt.bak"
TEMP_FILE = INPUT_DIR / "keywords.txt.tmp"


def process_file_streaming(input_path: Path, output_path, threshold: float, show_progress: bool = True):
    """
    Читает и фильтрует файл построчно.
    Если output_path=None — только считает (dry-run, безопасно).
    Если output_path указан — сразу записывает результат.
    """
    kept = 0
    dropped = 0
    total = 0
    last_update_time = time.perf_counter()
    last_total = 0

    sample_kept = []
    sample_dropped = []

    if output_path:
        fout = open(output_path, "w", encoding="utf-8")
    else:
        fout = None

    try:
        with open(input_path, "r", encoding="utf-8") as fin:
            for line in fin:
                total += 1
                stripped = line.rstrip("\n\r")
                if not stripped:
                    dropped += 1
                    continue

                parts = stripped.rsplit(maxsplit=1)
                if len(parts) == 2:
                    word_part, freq_part = parts
                    freq_str = freq_part.replace(",", ".").strip()
                    try:
                        freq = float(freq_str)
                        if freq > threshold:
                            if fout:
                                fout.write(line)
                            kept += 1
                            if len(sample_kept) < 3 and show_progress:
                                sample_kept.append(f"  {word_part.strip()!r} → {freq}")
                        else:
                            dropped += 1
                            if len(sample_dropped) < 3 and show_progress:
                                sample_dropped.append(f"  {word_part.strip()!r} → {freq}")
                    except ValueError:
                        dropped += 1
                else:
                    dropped += 1

                if show_progress and (total % 100_000 == 0):
                    now = time.perf_counter()
                    if now - last_update_time >= 0.5:
                        elapsed = now - last_update_time
                        speed = (total - last_total) / elapsed if elapsed > 0 else 0
                        speed_str = f"{speed / 1000:.1f}K строк/с" if speed >= 1000 else f"{speed:.0f} строк/с"
                        line_out = (
                            f"\r📖 Обработано: {total:,} строк | "
                            f"✅ Оставлено: {kept:,} | "
                            f"❌ Отброшено: {dropped:,} | "
                            f"Скорость: {speed_str}"
                        )
                        sys.stdout.write(line_out)
                        sys.stdout.flush()
                        last_update_time = now
                        last_total = total

        if show_progress:
            sys.stdout.write("\n")
            sys.stdout.flush()
    finally:
        if fout:
            fout.close()

    return total, kept, dropped, sample_kept, sample_dropped


def print_report(total, kept, dropped, sample_kept, sample_dropped, dry_run: bool):
    """Печатает отчёт о фильтрации."""
    print("\n" + "=" * 70)
    mode = "🔍 ПРЕДПРОСМОТР (dry-run, файл НЕ изменён)" if dry_run else "✅ ПРИМЕНЕНИЕ"
    print(f"{mode}")
    print("=" * 70)
    print(f"  Всего строк:            {total:>10,}")
    print(f"    ✅ Оставлено:         {kept:>10,}  ({kept / max(total, 1) * 100:.1f}%)")
    print(f"    ❌ Отброшено:         {dropped:>10,}")

    if sample_kept:
        print("\n  Примеры оставленных:")
        for s in sample_kept:
            print(s)
    if sample_dropped:
        print("\n  Примеры отброшенных:")
        for s in sample_dropped:
            print(s)
    print("=" * 70)


def cmd_filter(threshold: float = 1.0, apply: bool = False) -> bool:
    """
    Фильтрация keywords.txt — оставляет только строки с частотой > N.
    По умолчанию — предпросмотр (без изменений).
    С флагом --apply — реально заменяет файл.
    """
    input_path = INPUT_FILE
    backup_path = BACKUP_FILE
    temp_path = TEMP_FILE

    if not input_path.exists():
        err(f"Файл не найден: {input_path}")
        return False

    file_size_gb = input_path.stat().st_size / (1024 ** 3)
    print(f"📂 Входной файл: {input_path.relative_to(ROOT)}")
    print(f"📏 Размер файла: {file_size_gb:.2f} ГБ")
    print(f"🎯 Порог частоты: > {threshold}")
    print()

    t0 = time.perf_counter()
    target_path = temp_path if apply else None
    total, kept, dropped, sample_kept, sample_dropped = process_file_streaming(
        input_path, target_path, threshold, show_progress=True
    )
    elapsed = time.perf_counter() - t0

    print_report(total, kept, dropped, sample_kept, sample_dropped, dry_run=not apply)
    print(f"⏱  Время обработки: {elapsed:.1f}с")

    if not apply:
        print("\n💡 Это предпросмотр. Чтобы реально заменить файл, добавь флаг --apply")
        return True

    print(f"\n⚠️  Оригинальный файл будет заменён отфильтрованной версией.")
    print(f"   Бэкап сохранится в: {backup_path.name}")

    try:
        answer = input("   Продолжить? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n❌ Отменено.")
        return False

    if answer not in ("y", "yes", "д", "да"):
        print("❌ Отменено.")
        if temp_path.exists():
            temp_path.unlink()
        return False

    print("\n🔄 Замена файлов...")
    shutil.copy2(input_path, backup_path)
    shutil.move(str(temp_path), str(input_path))

    new_size_gb = input_path.stat().st_size / (1024 ** 3)
    reduction = (1 - new_size_gb / file_size_gb) * 100

    print(f"💾 Бэкап создан: {backup_path.relative_to(ROOT)}")
    print(f"📉 Размер файла: {file_size_gb:.2f} ГБ → {new_size_gb:.2f} ГБ (−{reduction:.1f}%)")
    ok("Файл успешно обновлён!")
    return True


# ============================================================
# 6. EXPORT — экспорт БД в CSV + 7z
# ============================================================

VOLUME_SIZE_MB = 95


def get_table_columns(conn, table: str) -> list:
    """Получает список колонок таблицы."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


def find_column(columns: list, candidates: list) -> str:
    """Ищет колонку по списку кандидатов."""
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    return None


def _count(conn, table: str) -> int:
    """Считает записи в таблице."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def _export_table(conn, query: str, path: Path, headers: list, label: str) -> int:
    """Экспортирует таблицу в CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        cursor = conn.execute(query)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(headers)
            for row in cursor:
                writer.writerow(row)
                total += 1
                if total % 500_000 == 0:
                    print(f"    {label}: {total:,} записей...")
        print(f"  [ok] {path.name:<20} : {total:,} записей ({size_mb(path)} МБ)")
    except Exception as e:
        print(f"  [!] {label}: {e}")
        if path.exists():
            path.unlink()
        total = 0
    return total


def find_7z() -> str:
    """Ищет исполняемый файл 7z в системе."""
    for name in ("7z", "7za", "7z.exe"):
        path = shutil.which(name)
        if path:
            return path
    for p in (
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.exists(p):
            return p
    return ""


def compress_to_volumes(csv_path: Path, volume_size_mb: int) -> bool:
    """Сжимает CSV в многотомный 7z-архив."""
    if not csv_path.exists():
        return False

    cmd_7z = find_7z()
    if not cmd_7z:
        print(f"  [!] 7-Zip не найден. Установите с https://www.7-zip.org/")
        print(f"      Файл {csv_path.name} останется несжатым.")
        return False

    archive_name = csv_path.stem + ".7z"
    archive_path = SEED_DIR / archive_name
    print(f"\n  Сжатие: {csv_path.name} ({size_mb(csv_path)} МБ)")
    print(f"    -> {archive_name} (тома по {volume_size_mb} МБ)...")

    for old in SEED_DIR.glob(f"{archive_name}*"):
        old.unlink()

    cmd = [
        cmd_7z, "a",
        f"-v{volume_size_mb}m",
        "-mx=9",
        "-mmt=on",
        str(archive_path),
        str(csv_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [!] Ошибка 7z: {result.stderr[:300]}")
        return False

    volumes = sorted(SEED_DIR.glob(f"{archive_name}*"))
    if not volumes:
        print(f"    [!] Архив не создан.")
        return False

    total_size = sum(v.stat().st_size for v in volumes)
    print(f"    [ok] Создано томов: {len(volumes)}")
    for v in volumes:
        print(f"         - {v.name:<25} {size_mb(v):>7} МБ")
    print(f"         {'Итого':<25} {total_size / 1024 / 1024:>7.1f} МБ")
    return True


def cmd_export() -> bool:
    """Экспортирует БД в CSV и сжимает в многотомные 7z."""
    if not DB_FILE.exists():
        err(f"БД не найдена: {DB_FILE}")
        print("      Сначала выполните инициализацию и внесите данные.")
        return False

    SEED_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_FILE))

    words_cols = get_table_columns(conn, "words")
    links_cols = get_table_columns(conn, "links")
    freq_col = find_column(words_cols, ["freq", "frequency", "count", "total", "value", "weight"])
    weight_col = find_column(links_cols, ["weight", "count", "value", "w", "strength", "score"])

    if not freq_col or not weight_col:
        err("Не удалось определить схему БД.")
        print(f"      words: {words_cols}")
        print(f"      links: {links_cols}")
        conn.close()
        return False

    print(f"\n  Схема БД: words.{freq_col}, links.{weight_col}")

    tables_info = {
        "words": _count(conn, "words"),
        "links": _count(conn, "links"),
        "groups": _count(conn, "groups"),
        "group_words": _count(conn, "group_words"),
    }

    print("\n  Таблицы в БД:")
    for t, c in tables_info.items():
        mark = "[ok]" if c > 0 else "[--]"
        print(f"    {mark} {t:<15} : {c:,} записей")
    print()

    if tables_info["words"] == 0:
        err("Таблица words пуста — нечего экспортировать.")
        conn.close()
        return False

    # Экспорт CSV
    total = 0
    csv_files = []

    print("=" * 70)
    print("  ЭКСПОРТ В CSV")
    print("=" * 70)

    total += _export_table(
        conn,
        f"SELECT word, {freq_col} FROM words ORDER BY {freq_col} DESC",
        SEED_DIR / "words.csv",
        ["word", "freq"],
        "words",
    )
    csv_files.append(SEED_DIR / "words.csv")

    total += _export_table(
        conn,
        f"""
        SELECT w1.word, w2.word, l.{weight_col}
        FROM links l
        JOIN words w1 ON l.word1_id = w1.id
        JOIN words w2 ON l.word2_id = w2.id
        """,
        SEED_DIR / "links.csv",
        ["word1", "word2", "weight"],
        "links",
    )
    csv_files.append(SEED_DIR / "links.csv")

    total += _export_table(
        conn,
        "SELECT name FROM groups ORDER BY name",
        SEED_DIR / "groups.csv",
        ["name"],
        "groups",
    )
    csv_files.append(SEED_DIR / "groups.csv")

    total += _export_table(
        conn,
        """
        SELECT w.word, g.name
        FROM group_words gw
                 JOIN words w ON gw.word_id = w.id
                 JOIN groups g ON gw.group_id = g.id
        """,
        SEED_DIR / "group_words.csv",
        ["word", "group"],
        "group_words",
    )
    csv_files.append(SEED_DIR / "group_words.csv")

    conn.close()

    print(f"\n  Экспорт CSV завершён. Всего записей: {total:,}")

    # Сжатие
    print("\n" + "=" * 70)
    print("  СЖАТИЕ В МНОГОТОМНЫЕ АРХИВЫ")
    print("=" * 70)

    cmd_7z = find_7z()
    if not cmd_7z:
        print("\n  [!] 7-Zip не найден в системе.")
        print("      CSV-файлы остались в data/seed/ без сжатия.")
        return False

    print(f"\n  Найден 7-Zip: {cmd_7z}")
    print(f"  Размер тома: {VOLUME_SIZE_MB} МБ")

    compressed_count = 0
    for csv_file in csv_files:
        if compress_to_volumes(csv_file, VOLUME_SIZE_MB):
            compressed_count += 1

    if compressed_count == 0:
        err("Не удалось сжать ни одного файла.")
        return False

    # Очистка
    print("\n" + "=" * 70)
    print("  ОЧИСТКА")
    print("=" * 70)

    print("\n  CSV-файлы успешно упакованы в архивы.")
    print("  Удалить исходные CSV-файлы? (они занимают много места)")

    answer = input("  Удалить CSV? (да/нет): ").strip().lower()
    if answer in ("да", "д", "y", "yes"):
        for csv_file in csv_files:
            if csv_file.exists():
                csv_file.unlink()
                print(f"    [ok] удалён {csv_file.name}")
        ok("CSV-файлы удалены. Архивы готовы к переносу.")
    else:
        print("\n  [--] CSV-файлы оставлены.")

    print(f"\n  Каталог: {SEED_DIR}")
    return True


# ============================================================
# 7. DICT-UPDATE — обновление словарей из JSON
# ============================================================

DICT_CODES = {
    -1: {"file": "stop.txt", "exclude": True},
    -3: {"file": "brands.txt", "exclude": False},
    -4: {"file": "geo.txt", "exclude": False},
    -5: {"file": "marketplaces.txt", "exclude": False},
    -6: {"file": "colors.txt", "exclude": False},
    -7: {"file": "materials.txt", "exclude": False},
    -8: {"file": "sizes.txt", "exclude": False},
    -9: {"file": "questions.txt", "exclude": False},
    -10: {"file": "codes.txt", "exclude": False},
    -11: {"file": "products.txt"},
    -12: {"file": "appliances.txt", "exclude": False},
    -13: {"file": "purpose.txt", "exclude": False},
    -14: {"file": "construction.txt", "exclude": False},
    -15: {"file": "features.txt", "exclude": False},
    -16: {"file": "actions.txt", "exclude": False},
    -17: {"file": "content.txt", "exclude": False},
    -18: {"file": "commerce.txt", "exclude": False},
    -19: {"file": "rooms.txt", "exclude": False},
    -20: {"file": "styles.txt", "exclude": False},
    -21: {"file": "polysemy.txt", "exclude": False},
    -22: {"file": "media.txt", "exclude": False},
}


def cmd_dict_update(filename: str) -> bool:
    """Обновляет словари из JSON-файла."""
    header("Обновление словарей")

    if not Path(filename).exists():
        err(f"Файл не найден: {filename}")
        return False

    with open(filename, encoding="utf-8") as f:
        data = json.load(f)

    result = {}
    for cfg in DICT_CODES.values():
        result[cfg["file"]] = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        for word, value in node.items():
            if isinstance(value, dict):
                walk(value)
            elif isinstance(value, int) and value < 0:
                cfg = DICT_CODES.get(value)
                if cfg:
                    result[cfg["file"]].add(word)

    walk(data)

    dict_dir = ROOT / "dict"
    dict_dir.mkdir(exist_ok=True)

    for file, words in result.items():
        path = dict_dir / file
        old = set()
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        old.add(line)
        words |= old
        with path.open("w", encoding="utf-8") as f:
            for word in sorted(words):
                f.write(word + "\n")
        print(f"  {file}: {len(words)} слов")

    ok("Словари обновлены.")
    return True


# ============================================================
# 8. INIT — полная инициализация проекта
# ============================================================

def check_db_exists() -> bool:
    return DB_FILE.exists()


def check_seed_archives() -> bool:
    if not SEED_DIR.exists():
        return False
    return any(SEED_DIR.glob("*.csv.gz")) or any(SEED_DIR.glob("*.part*"))


def cmd_init(force: bool = False) -> bool:
    """
    Полная инициализация проекта:
    1. Создание БД
    2. Сборка и распаковка seed-архивов
    3. Импорт данных в БД
    """
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   Semantic Facet Analyzer — Инициализация проекта        ║")
    print("╚══════════════════════════════════════════════════════════╝")

    start_time = time.perf_counter()

    # Этап 1: База данных
    header("ЭТАП 1. Создание базы данных")
    if check_db_exists() and not force:
        warn("База данных уже существует.")
        answer = input("   Переинициализировать? (y/N): ").strip().lower()
        if answer != "y":
            ok("Пропущено — используем существующую БД.")
        else:
            if not cmd_init_db(force=True):
                err("Инициализация прервана на этапе 1.")
                return False
    else:
        if not cmd_init_db(force=force):
            err("Инициализация прервана на этапе 1.")
            return False

    # Этап 2: Seed-данные
    header("ЭТАП 2. Подготовка seed-данных")
    if not check_seed_archives():
        warn("Архивы seed-данных не найдены в data/seed/")
        ok("Пропущено — работаем без seed-данных.")
    else:
        # 2.1 Сборка частей
        parts = list(SEED_DIR.glob("*.part*"))
        if parts:
            step(f"Найдено частей для сборки: {len(parts)}")
            if not cmd_join():
                err("Сборка частей завершилась с ошибкой.")
                return False
            ok("Части собраны.")

        # 2.2 Распаковка
        step("Распаковка сжатых файлов")
        if not cmd_extract():
            err("Распаковка завершилась с ошибкой.")
            return False
        ok("Архивы распакованы.")

    # Этап 3: Импорт
    header("ЭТАП 3. Импорт данных в базу")
    csv_files = list(SEED_DIR.glob("*.csv")) if SEED_DIR.exists() else []
    if not csv_files:
        warn("CSV-файлы для импорта не найдены.")
        ok("Пропущено.")
    else:
        step(f"Найдено CSV-файлов: {len(csv_files)}")
        if not cmd_import():
            err("Инициализация прервана на этапе 3.")
            return False

    # Итог
    elapsed = time.perf_counter() - start_time

    header("Итог инициализации")
    if DB_FILE.exists():
        size_mb_val = DB_FILE.stat().st_size / (1024 * 1024)
        ok(f"База данных: {DB_FILE.name} ({size_mb_val:.2f} MB)")
    else:
        err("База данных не создана.")

    if SEED_DIR.exists():
        csv_count = len(list(SEED_DIR.glob("*.csv")))
        ok(f"Seed-данные: {csv_count} CSV-файлов")

    dict_dir = ROOT / "dict"
    if dict_dir.exists():
        dict_count = len(list(dict_dir.glob("*.txt")))
        ok(f"Словари исключений: {dict_count} файлов")

    print()
    print("  🎉 Проект готов к работе!")
    print()
    print("  Следующий шаг: положите keywords.txt в data/input/")
    print("  и запустите: python scripts/graph_builder.py")
    print()
    print(f"  Время инициализации: {elapsed:.1f} сек.")
    print()
    return True


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Утилиты для работы с данными проекта Semantic Facet Analyzer"
    )
    sub = parser.add_subparsers(dest="cmd", help="Подкоманда")

    # init
    p_init = sub.add_parser("init", help="Полная инициализация проекта")
    p_init.add_argument("--force", action="store_true", help="Переинициализировать БД без спроса")

    # init-db
    p_initdb = sub.add_parser("init-db", help="Создание пустой БД")
    p_initdb.add_argument("--force", action="store_true", help="Удалить существующую БД без спроса")

    # import
    sub.add_parser("import", help="Импорт seed-CSV в БД")

    # export
    sub.add_parser("export", help="Экспорт БД в CSV + 7z")

    # extract
    sub.add_parser("extract", help="Распаковка .csv.gz архивов")

    # join
    sub.add_parser("join", help="Сборка .part-частей в CSV")

    # filter
    p_filter = sub.add_parser("filter", help="Фильтрация keywords.txt по частоте")
    p_filter.add_argument("--threshold", type=float, default=1.0,
                          help="Минимальная частота для сохранения (по умолчанию > 1.0)")
    p_filter.add_argument("--apply", action="store_true",
                          help="Реально заменить файл (по умолчанию — только предпросмотр)")

    # dict-update
    p_dict = sub.add_parser("dict-update", help="Обновление словарей из JSON")
    p_dict.add_argument("filename", help="Путь к JSON-файлу")

    args = parser.parse_args()

    if args.cmd is None:
        parser.print_help()
        return

    if args.cmd == "init":
        cmd_init(force=args.force)
    elif args.cmd == "init-db":
        cmd_init_db(force=args.force)
    elif args.cmd == "import":
        cmd_import()
    elif args.cmd == "export":
        cmd_export()
    elif args.cmd == "extract":
        cmd_extract()
    elif args.cmd == "join":
        cmd_join()
    elif args.cmd == "filter":
        cmd_filter(threshold=args.threshold, apply=args.apply)
    elif args.cmd == "dict-update":
        cmd_dict_update(args.filename)


if __name__ == "__main__":
    main()