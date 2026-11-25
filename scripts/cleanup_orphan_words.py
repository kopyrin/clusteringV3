"""
Очистка БД от слов, не имеющих ни одной связи в таблице links.

Логика:
  1. Находит слова, которых нет ни в links.word1_id, ни в links.word2_id
  2. Удаляет связанные записи из group_words (иначе будут сироты)
  3. Удаляет сами слова из words
  4. Делает VACUUM для освобождения места

Режимы:
  (без флагов)  — предпросмотр: показать, что будет удалено
  --apply       — реально удалить (с бэкапом и VACUUM)
"""
import os
import shutil
import sqlite3
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


def size_mb(path: Path) -> float:
    return round(path.stat().st_size / 1024 / 1024, 1) if path.exists() else 0


def get_orphan_stats(conn) -> dict:
    """Считает, сколько записей будет удалено."""
    # Слова, которых нет ни в word1_id, ни в word2_id
    orphan_words = conn.execute("""
        SELECT COUNT(*) FROM words
        WHERE id NOT IN (SELECT word1_id FROM links)
          AND id NOT IN (SELECT word2_id FROM links)
    """).fetchone()[0]

    # group_words записи, ссылающиеся на эти слова
    orphan_gw = conn.execute("""
        SELECT COUNT(*) FROM group_words
        WHERE word_id NOT IN (
            SELECT id FROM words
            WHERE id IN (SELECT word1_id FROM links)
               OR id IN (SELECT word2_id FROM links)
        )
    """).fetchone()[0]

    total_words = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    total_links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    total_groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    total_gw = conn.execute("SELECT COUNT(*) FROM group_words").fetchone()[0]

    return {
        "total_words": total_words,
        "total_links": total_links,
        "total_groups": total_groups,
        "total_gw": total_gw,
        "orphan_words": orphan_words,
        "orphan_gw": orphan_gw,
    }


def cleanup(apply: bool):
    if not DB_FILE.exists():
        print(f"\n  [!] БД не найдена: {DB_FILE}")
        return False

    size_before = size_mb(DB_FILE)
    conn = sqlite3.connect(DB_FILE)

    print(f"\n  БД: {DB_FILE}")
    print(f"  Размер: {size_before} МБ\n")

    stats = get_orphan_stats(conn)

    print("  Текущее состояние БД:")
    print(f"    words        : {stats['total_words']:>12,} записей")
    print(f"    links        : {stats['total_links']:>12,} записей")
    print(f"    groups       : {stats['total_groups']:>12,} записей")
    print(f"    group_words  : {stats['total_gw']:>12,} записей")
    print()
    print("  Будет удалено (слова без связей):")
    print(f"    из words       : {stats['orphan_words']:>12,} ({100*stats['orphan_words']/max(1,stats['total_words']):.1f}%)")
    print(f"    из group_words : {stats['orphan_gw']:>12,} ({100*stats['orphan_gw']/max(1,stats['total_gw']):.1f}%)")
    print(f"    из links       :            0 (не трогаем)")
    print(f"    из groups      :            0 (не трогаем)")

    if stats["orphan_words"] == 0:
        print("\n  [ok] Сирот нет — чистить нечего.")
        conn.close()
        return True

    if not apply:
        print("\n  Это ПРЕДПРОСМОТР. Для реального удаления запустите с флагом --apply")
        conn.close()
        return True

    # --- Реальное удаление ---
    print("\n  Применяю изменения...")

    # Бэкап
    backup = Path(str(DB_FILE) + ".bak_before_cleanup")
    print(f"  Бэкап: {backup}")
    shutil.copy2(DB_FILE, backup)

    try:
        # Включаем внешние ключи для безопасности
        conn.execute("PRAGMA foreign_keys = OFF")  # сами контролируем порядок
        conn.execute("BEGIN TRANSACTION")

        # 1. Удаляем group_words, ссылающиеся на слова-сироты
        print("  [1/3] Удаление записей из group_words...")
        cur = conn.execute("""
            DELETE FROM group_words
            WHERE word_id NOT IN (
                SELECT id FROM words
                WHERE id IN (SELECT word1_id FROM links)
                   OR id IN (SELECT word2_id FROM links)
            )
        """)
        print(f"         удалено: {cur.rowcount:,}")

        # 2. Удаляем слова-сироты
        print("  [2/3] Удаление слов без связей из words...")
        cur = conn.execute("""
            DELETE FROM words
            WHERE id NOT IN (SELECT word1_id FROM links)
              AND id NOT IN (SELECT word2_id FROM links)
        """)
        print(f"         удалено: {cur.rowcount:,}")

        # 3. Пересоздаём индексы (на случай, если что-то повредилось)
        print("  [3/3] Пересоздание индексов...")
        indexes = [
            ("idx_links_word1", "links", "word1_id"),
            ("idx_links_word2", "links", "word2_id"),
            ("idx_group_words_word_id", "group_words", "word_id"),
            ("idx_group_words_group_id", "group_words", "group_id"),
        ]
        for idx_name, table, col in indexes:
            conn.execute(f"DROP INDEX IF EXISTS {idx_name}")
            conn.execute(f"CREATE INDEX {idx_name} ON {table}({col})")

        conn.execute("COMMIT")
        print("  [ok] Транзакция завершена.")

        # VACUUM — освобождает место
        print("\n  Выполняю VACUUM (освобождение места)...")
        conn.execute("VACUUM")
        conn.close()

        size_after = size_mb(DB_FILE)
        saved = size_before - size_after
        print(f"  [ok] Размер БД: {size_before} МБ -> {size_after} МБ (освобождено {saved} МБ)")
        print(f"  [ok] Бэкап сохранён: {backup}")
        return True

    except Exception as e:
        conn.execute("ROLLBACK")
        conn.close()
        print(f"\n  [!] Ошибка: {e}")
        print("  Транзакция откатана, БД не изменена.")
        print(f"  Бэкап на всякий случай: {backup}")
        return False


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    apply = "--apply" in sys.argv
    cleanup(apply=apply)