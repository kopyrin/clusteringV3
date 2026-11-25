"""
Экспорт данных из semantics.db в CSV-файлы (data/seed/)
с автоматическим сжатием в многотомные архивы (< 95 МБ).

Для удобного переноса через интернет (email, мессенджеры, облака).
"""
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
DB_FILE = ROOT / "data" / "output" / "semantics.db"
SEED_DIR = ROOT / "data" / "seed"

VOLUME_SIZE_MB = 95  # размер тома архива


def size_mb(path: Path) -> float:
    return round(path.stat().st_size / 1024 / 1024, 1) if path.exists() else 0


def get_table_columns(conn, table: str) -> list:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


def find_column(columns: list, candidates: list) -> str:
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    return None


def _count(conn, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def _export_table(conn, query: str, path: Path, headers: list, label: str) -> int:
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
    # Стандартные пути установки в Windows
    for p in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.exists(p):
            return p
    return ""


def compress_to_volumes(csv_path: Path, volume_size_mb: int) -> bool:
    """Сжимает CSV в многотомный 7z-архив рядом с файлом."""
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

    # Удаляем старые тома этого архива, если есть
    for old in SEED_DIR.glob(f"{archive_name}*"):
        old.unlink()

    cmd = [
        cmd_7z, "a",
        f"-v{volume_size_mb}m",   # размер тома
        "-mx=9",                  # максимальное сжатие
        "-mmt=on",                # многопоточность
        str(archive_path),
        str(csv_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [!] Ошибка 7z: {result.stderr[:300]}")
        return False

    # Подсчёт томов
    volumes = sorted(SEED_DIR.glob(f"{archive_name}*"))
    if not volumes:
        print(f"    [!] Архив не создан.")
        return False

    total_size = sum(v.stat().st_size for v in volumes)
    print(f"    [ok] Создано томов: {len(volumes)}")
    for v in volumes:
        print(f"         - {v.name:<25} {size_mb(v):>7} МБ")
    print(f"         {'Итого':<25} {size_mb(Path(str(volumes[0]).replace('.001','')))*0 + total_size/1024/1024:>7.1f} МБ")

    return True


def export():
    if not DB_FILE.exists():
        print(f"\n  [!] БД не найдена: {DB_FILE}")
        print("      Сначала выполните 'Инициализация проекта' и внесите данные.")
        return False

    SEED_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)

    # Определяем схему БД
    words_cols = get_table_columns(conn, "words")
    links_cols = get_table_columns(conn, "links")

    freq_col = find_column(words_cols, ["freq", "frequency", "count", "total", "value", "weight"])
    weight_col = find_column(links_cols, ["weight", "count", "value", "w", "strength", "score"])

    if not freq_col or not weight_col:
        print(f"\n  [!] Не удалось определить схему БД.")
        print(f"      words: {words_cols}")
        print(f"      links: {links_cols}")
        conn.close()
        return False

    print(f"\n  Схема БД: words.{freq_col}, links.{weight_col}")

    # Проверка данных
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
        print("  [!] Таблица words пуста — нечего экспортировать.")
        conn.close()
        return False

    # --- ЭКСПОРТ CSV ---
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

    # --- СЖАТИЕ В МНОГОТОМНЫЕ АРХИВЫ ---
    print("\n" + "=" * 70)
    print("  СЖАТИЕ В МНОГОТОМНЫЕ АРХИВЫ")
    print("=" * 70)

    cmd_7z = find_7z()
    if not cmd_7z:
        print("\n  [!] 7-Zip не найден в системе.")
        print("      Для создания архивов установите 7-Zip: https://www.7-zip.org/")
        print("      CSV-файлы остались в data/seed/ без сжатия.")
        return False

    print(f"\n  Найден 7-Zip: {cmd_7z}")
    print(f"  Размер тома: {VOLUME_SIZE_MB} МБ")

    compressed_count = 0
    for csv_file in csv_files:
        if compress_to_volumes(csv_file, VOLUME_SIZE_MB):
            compressed_count += 1

    if compressed_count == 0:
        print("\n  [!] Не удалось сжать ни одного файла.")
        return False

    # --- УДАЛЕНИЕ ИСХОДНЫХ CSV ---
    print("\n" + "=" * 70)
    print("  ОЧИСТКА")
    print("=" * 70)
    print("\n  CSV-файлы успешно упакованы в архивы.")
    print("  Удалить исходные CSV-файлы? (они занимают много места)")
    print("  Архивы останутся в data/seed/ и будут загружены при инициализации.")
    print()

    answer = input("  Удалить CSV? (да/нет): ").strip().lower()
    if answer in ("да", "д", "y", "yes"):
        for csv_file in csv_files:
            if csv_file.exists():
                csv_file.unlink()
                print(f"    [ok] удалён {csv_file.name}")
        print("\n  [ok] CSV-файлы удалены. Архивы готовы к переносу.")
    else:
        print("\n  [--] CSV-файлы оставлены.")

    print(f"\n  Каталог: {SEED_DIR}")
    print("\n  Чтобы перенести проект на другой компьютер:")
    print("    1. Скопируйте папку data/seed/ вместе с остальными файлами")
    print("    2. На новом компьютере запустите 'Инициализация проекта'")
    print("       — она автоматически распакует архивы и загрузит данные в БД")
    return True


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    export()