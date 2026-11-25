"""
Импорт seed-данных из data/seed/*.csv в semantics.db
Индексы создаются ОДИН РАЗ после импорта всех таблиц.
"""
import csv
import sqlite3
import sys
import time
from pathlib import Path


def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
DB_PATH = ROOT / "data" / "output" / "semantics.db"
SEED_DIR = ROOT / "data" / "seed"

TABLES = [
    ("words.csv",       "words",       {"skip_cols": {"id"}}),
    ("groups.csv",      "groups",      {}),
    ("lemmas.csv",      "lemmas",      {}),
    ("group_words.csv", "group_words", {}),
    ("links.csv",       "links",       {}),
]

BATCH = 100_000


def detect_delimiter(csv_path: Path) -> str:
    with csv_path.open("r", encoding="utf-8") as f:
        first_line = f.readline()
    return "\t" if "\t" in first_line else ","


def import_table(cursor, csv_path: Path, table: str, skip_cols: set = None):
    """Импортирует CSV. БЕЗ создания индексов."""
    skip_cols = skip_cols or set()
    delimiter = detect_delimiter(csv_path)
    print(f"Импорт {csv_path.relative_to(ROOT)} → {table}...")

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
                    print(f"\r  {table}: {count:,} строк...", end="", flush=True)
        if batch:
            cursor.executemany(sql, batch)

    elapsed = time.perf_counter() - t0
    print(f"  ✅ {table}: {count:,} строк за {elapsed:.1f} с")
    return count


def create_all_indexes(cursor, conn):
    """Создаёт индексы ОДИН РАЗ, после импорта всех таблиц."""
    print("\n" + "=" * 60)
    print("Создание индексов (один раз, после всего импорта)...")
    print("=" * 60)

    t0 = time.perf_counter()
    indexes = [
        ("idx_links_word1",        "CREATE INDEX IF NOT EXISTS idx_links_word1 ON links(word1_id)"),
        ("idx_links_word2",        "CREATE INDEX IF NOT EXISTS idx_links_word2 ON links(word2_id)"),
        ("idx_links_word2_count",  "CREATE INDEX IF NOT EXISTS idx_links_word2_count ON links(word2_id, count)"),
        ("idx_links_word1_count",  "CREATE INDEX IF NOT EXISTS idx_links_word1_count ON links(word1_id, count)"),
        ("idx_words_word",         "CREATE INDEX IF NOT EXISTS idx_words_word ON words(word)"),
        ("idx_gw_word",            "CREATE INDEX IF NOT EXISTS idx_gw_word ON group_words(word_id)"),
        ("idx_gw_group",           "CREATE INDEX IF NOT EXISTS idx_gw_group ON group_words(group_id)"),
        ("idx_groups_parent",      "CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id)"),
        ("idx_groups_type",        "CREATE INDEX IF NOT EXISTS idx_groups_type ON groups(type)"),
        
    ]

    for name, sql in indexes:
        t1 = time.perf_counter()
        cursor.execute(sql)
        conn.commit()
        print(f"\r  ✅ {table}: {count:,} строк за {elapsed:.1f} с" + " " * 20)  # пробелы для затирания старой строки

    print(f"\n✅ Все индексы созданы за {time.perf_counter() - t0:.1f} с")


def main():
    if not DB_PATH.exists():
        print(f"❌ База не найдена: {DB_PATH}")
        print("   Сначала: python scripts/init_db.py")
        return

    print("=" * 60)
    print("Импорт данных в semantics.db")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
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

    # ⚠️ ИНДЕКСЫ СОЗДАЮТСЯ ЗДЕСЬ — ПОСЛЕ ИМПОРТА ВСЕХ ТАБЛИЦ
    create_all_indexes(cursor, conn)

    conn.close()
    print(f"\n✅ Импорт завершён. Всего строк: {grand:,} за {time.perf_counter() - t0:.0f} с")


if __name__ == "__main__":
    main()