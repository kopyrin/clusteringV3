"""
scripts/hypothesis_finder.py — поиск гипотез и экспорт дерева связанных запросов.

Режимы:
  (без флагов)  — поиск гипотез (BFS глубиной N), сохранение в hypotheses.txt
  --xlsx        — экспорт дерева связанных запросов в TreeToYandexDirect.xlsx

Настройки читаются из config/hypothesis_finder.yaml.

Запуск:
  python scripts/hypothesis_finder.py
  python scripts/hypothesis_finder.py --xlsx
"""
import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from common import (
    ROOT, DB_FILE, START_WORDS_FILE, OUTPUT_DIR,
    console_status, console_line,
)

# Опциональный импорт openpyxl (нужен только для --xlsx)
try:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# ============================================================
# КОНФИГ
# ============================================================

CONFIG_FILE = ROOT / "config" / "hypothesis_finder.yaml"
DEFAULT_OUTPUT = OUTPUT_DIR / "hypotheses.txt"
OUTPUT_XLSX = OUTPUT_DIR / "TreeToYandexDirect.xlsx"
INFINITE_WEIGHT = 2147483647


def load_config() -> dict:
    """Загружает YAML-конфиг. Если файла нет — использует значения по умолчанию."""
    defaults = {
        "hypothesis_finder": {
            "max_depth": 2,
            "min_link_weight": 100,
            "direct_link_min_weight": 100,
            "show_tree": True,
            "show_hypotheses": True,
            "show_statistics": True,
            "save_json": False,
            "exclude_groups": [
                "Предлоги",
                "Союзы",
                "Частицы",
                "Английские слова",
                "Английские служебные слова",
                "Контент 18+",
            ],
            "paths": {
                "db": "data/output/semantics.db",
                "start_words": "data/input/start_words.txt",
                "output": "data/output/hypotheses.txt",
            },
        }
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        print(f"✅ Конфиг загружен: {CONFIG_FILE.relative_to(ROOT)}")
        return config
    else:
        print(f"⚠️  Конфиг не найден: {CONFIG_FILE.relative_to(ROOT)}")
        print("   Используются настройки по умолчанию.")
        return defaults


def get_paths(config: dict) -> dict:
    """Возвращает абсолютные пути из конфига (или дефолтные)."""
    paths_cfg = config.get("hypothesis_finder", {}).get("paths", {})
    return {
        "db": ROOT / paths_cfg.get("db", "data/output/semantics.db"),
        "start_words": ROOT / paths_cfg.get("start_words", "data/input/start_words.txt"),
        "output": ROOT / paths_cfg.get("output", "data/output/hypotheses.txt"),
    }


# ============================================================
# SemanticExplorer — поиск гипотез (BFS)
# ============================================================

class SemanticExplorer:
    def __init__(self, db_path: Path, config: dict):
        self.config = config.get("hypothesis_finder", {})
        self.db_path = db_path
        self.conn = None
        self.output_file = None

        self.MAX_DEPTH = self.config.get("max_depth", 2)
        self.MIN_LINK_WEIGHT = self.config.get("min_link_weight", 100)
        self.DIRECT_LINK_MIN_WEIGHT = self.config.get("direct_link_min_weight", 100)
        self.SHOW_TREE = self.config.get("show_tree", True)
        self.SHOW_HYPOTHESES = self.config.get("show_hypotheses", True)
        self.SHOW_STATISTICS = self.config.get("show_statistics", True)
        self.SAVE_JSON = self.config.get("save_json", False)
        self.exclude_groups = set(self.config.get("exclude_groups", []))

        self.word_info_cache = {}
        self.word_text_cache = {}
        self.word_groups = {}
        self.stop_word_ids = set()

        self.open_database()
        self.load_groups_cache()

    def open_database(self):
        if not self.db_path.exists():
            sys.exit(f"Ошибка: файл БД {self.db_path} не найден.")
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()

        required_tables = {"words", "links", "group_words", "groups"}
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row[0] for row in self.cursor.fetchall()}
        missing = required_tables - existing
        if missing:
            sys.exit(
                f"Ошибка: в базе {self.db_path} отсутствуют таблицы: "
                f"{', '.join(sorted(missing))}"
            )

        self.cursor.executescript("""
            DROP TABLE IF EXISTS wave;
            DROP TABLE IF EXISTS wave_next;
            DROP TABLE IF EXISTS visited;
            DROP TABLE IF EXISTS stop_words;
            CREATE TABLE wave
            (
                word_id   INTEGER PRIMARY KEY,
                parent_id INTEGER,
                depth     INTEGER,
                weakest   INTEGER
            );
            CREATE TABLE wave_next
            (
                word_id   INTEGER PRIMARY KEY,
                parent_id INTEGER,
                depth     INTEGER,
                weakest   INTEGER
            );
            CREATE TABLE visited
            (
                word_id   INTEGER PRIMARY KEY,
                parent_id INTEGER,
                depth     INTEGER,
                weakest   INTEGER
            );
            CREATE TABLE stop_words
            (
                word_id INTEGER PRIMARY KEY
            );
            CREATE INDEX IF NOT EXISTS idx_links_word1 ON links(word1_id);
            CREATE INDEX IF NOT EXISTS idx_links_word2 ON links(word2_id);
        """)
        self.conn.commit()

    def execute(self, sql, params=()):
        self.cursor.execute(sql, params)

    def fetchone(self, sql, params=()):
        self.cursor.execute(sql, params)
        return self.cursor.fetchone()

    def fetchall(self, sql, params=()):
        self.cursor.execute(sql, params)
        return self.cursor.fetchall()

    def clear_work_tables(self):
        self.execute("DELETE FROM wave")
        self.execute("DELETE FROM wave_next")
        self.execute("DELETE FROM visited")

    def initialize_wave(self, start):
        self.execute(
            "INSERT INTO wave (word_id, parent_id, depth, weakest) VALUES (?, ?, ?, ?)",
            (start, start, 0, INFINITE_WEIGHT),
        )

    def load_visited(self):
        return self.fetchall(
            "SELECT word_id, parent_id, depth, weakest FROM visited "
            "ORDER BY depth DESC, weakest DESC"
        )

    # ============================================================
    # Главный алгоритм
    # ============================================================

    def find_hypotheses(self, start_word=None):
        if start_word is not None:
            start = self.get_word_id(start_word)
            if start is None:
                print(f"Слово '{start_word}' не найдено.")
                return {}
            if self.is_stop_word(start):
                print(f"Стартовое слово '{start_word}' относится к исключаемой группе.")
                return {}
            print(f"Стартовое слово: {start_word} ({start})")
            hypotheses = self.explore_graph(start)
            self.save_hypotheses(start, hypotheses)
            print(f"Найдено гипотез: {len(hypotheses)}")
            return hypotheses

    # ============================================================
    # Поиск гипотез (BFS)
    # ============================================================

    def explore_graph(self, start):
        t0 = time.perf_counter()
        self.clear_work_tables()
        self.initialize_wave(start)
        print(f"Старт: {self.format_word_info(start)}")

        for depth in range(1, self.MAX_DEPTH + 1):
            rows = self.expand_wave(start)
            if rows == 0:
                break

        t1 = time.perf_counter()
        print(f"BFS: {t1 - t0:.3f} с")

        hypotheses = self.build_hypothesis_paths(start)
        t2 = time.perf_counter()
        print(f"restore: {t2 - t1:.3f} с")

        if self.SHOW_TREE and hypotheses:
            self.print_hypothesis_tree(start, hypotheses)
            print(f"tree: {time.perf_counter() - t2:.3f} с")

        return hypotheses

    # ============================================================
    # Восстановление гипотез
    # ============================================================

    def build_hypothesis_paths(self, start):
        hypotheses = defaultdict(list)
        rows = self.load_visited()
        parents = {}
        for word_id, parent_id, depth, weakest in rows:
            parents[word_id] = (parent_id, weakest)

        for word_id, parent_id, depth, weakest in rows:
            if depth < 2:
                continue
            if word_id == start:
                continue

            path = []
            current = word_id
            while True:
                path.append(current)
                if current == start:
                    break
                if current not in parents:
                    path = None
                    break
                current = parents[current][0]

            if path is None:
                continue
            path.reverse()
            if len(path) < 2:
                continue

            hypotheses[word_id].append({"path": path, "weakest": weakest})

        return hypotheses

    # ============================================================
    # Сохранение результатов
    # ============================================================

    def save_hypotheses(self, start, hypotheses):
        if not self.SAVE_JSON or self.output_file is None:
            return
        for target, paths in hypotheses.items():
            record = {
                "start": start,
                "target": target,
                "strength": len(paths),
                "paths": paths,
            }
            self.output_file.write(json.dumps(record, ensure_ascii=False))
            self.output_file.write("\n")

    # ============================================================
    # Отображение результатов
    # ============================================================

    def print_hypothesis_tree(self, start, hypotheses):
        tree = {}
        links = {}
        self.cursor.execute("""
            SELECT v.parent_id, v.word_id, l.count
            FROM visited v
            JOIN links l ON l.word1_id = v.parent_id AND l.word2_id = v.word_id
        """)
        for parent_id, word_id, link_count in self.cursor.fetchall():
            links[(parent_id, word_id)] = link_count

        for paths in hypotheses.values():
            for item in paths:
                path = item["path"]
                node = tree
                parent = None
                path_links = [
                    links.get((path[i], path[i + 1]))
                    for i in range(len(path) - 1)
                ]
                weakest_edge_index = None
                if path_links and all(w is not None for w in path_links):
                    weakest_edge_index = path_links.index(min(path_links))

                for i, vertex in enumerate(path):
                    link = None
                    if parent is not None:
                        link = links.get((parent, vertex))
                    if vertex not in node:
                        node[vertex] = {"link": link, "weak": False, "children": {}}
                    if i > 0 and i - 1 == weakest_edge_index:
                        node[vertex]["weak"] = True
                    parent = vertex
                    node = node[vertex]["children"]

        def walk(nodes, prefix="", is_root=False):
            items = sorted(nodes.items(), key=lambda x: x[1]["link"] or 0, reverse=True)
            for i, (vertex, data) in enumerate(items):
                last = i == len(items) - 1
                if is_root:
                    print(f"● {self.format_word_info(vertex)}")
                else:
                    branch = "└──" if last else "├──"
                    print(
                        f"{prefix}{branch} "
                        f"{self.format_link_weight(data['link'], data['weak'])} "
                        f"─► {self.format_word_info(vertex)}"
                    )
                walk(data["children"], prefix + ("    " if last else "│   "), False)

        walk(tree, "", True)

    def format_word_info(self, word_id):
        info = self.load_word_info(word_id)
        text = f"{info['word']}({info['count']:,})".replace(",", " ")
        groups = self.word_groups.get(word_id, [])
        visible_groups = [g for g in groups if g not in self.exclude_groups]
        if visible_groups:
            text += " [" + ", ".join(visible_groups) + "]"
        elif groups:
            text += " [служебное]"
        else:
            text += " [ ]"
        return text

    def format_link_weight(self, weight, weak=False):
        text = f"{weight:,}".replace(",", " ")
        if weak:
            text = f"[{text}]"
        return f"[{text}]"

    # ============================================================
    # Работа со словами
    # ============================================================

    def get_word_id(self, word):
        row = self.fetchone("SELECT id FROM words WHERE word = ?", (word,))
        return None if row is None else row[0]

    def get_word_text(self, word_id):
        cached = self.word_text_cache.get(word_id)
        if cached is not None:
            return cached
        row = self.fetchone("SELECT word FROM words WHERE id = ?", (word_id,))
        if row is None:
            return str(word_id)
        self.word_text_cache[word_id] = row[0]
        return row[0]

    def load_word_info(self, word_id):
        if word_id in self.word_info_cache:
            return self.word_info_cache[word_id]
        cursor = self.conn.cursor()
        cursor.execute("SELECT word, count FROM words WHERE id = ?", (word_id,))
        row = cursor.fetchone()
        if row:
            info = {"word": row[0], "count": row[1]}
        else:
            info = {"word": str(word_id), "count": 0}
        self.word_info_cache[word_id] = info
        return info

    # ============================================================
    # Работа с группами
    # ============================================================

    def load_groups_cache(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT gw.word_id, g.name
            FROM group_words gw
            JOIN groups g ON g.id = gw.group_id
            ORDER BY gw.word_id
        """)
        self.word_groups = defaultdict(list)
        for word_id, group_name in cursor:
            self.word_groups[word_id].append(group_name)

        # Материализуем stop-слова в SQLite
        self.cursor.execute("DELETE FROM stop_words")
        if self.exclude_groups:
            placeholders = ",".join("?" for _ in self.exclude_groups)
            self.cursor.execute(
                f"""
                INSERT OR IGNORE INTO stop_words (word_id)
                SELECT DISTINCT gw.word_id
                FROM group_words gw
                JOIN groups g ON g.id = gw.group_id
                WHERE g.name IN ({placeholders})
                """,
                tuple(self.exclude_groups),
            )
        rows = self.cursor.execute("SELECT word_id FROM stop_words").fetchall()
        self.stop_word_ids = {row[0] for row in rows}
        self.conn.commit()

    def is_stop_word(self, word_id):
        return word_id in self.stop_word_ids

    def expand_wave(self, start):
        self.cursor.execute("""
                            INSERT INTO wave_next
                            (word_id,
                             parent_id,
                             depth,
                             weakest)
                            SELECT word_id,
                                   parent_id,
                                   depth,
                                   weakest
                            FROM (SELECT l.word2_id  AS word_id,
                                         w.word_id   AS parent_id,
                                         w.depth + 1 AS depth,
                                         CASE
                                             WHEN w.weakest < l.count THEN w.weakest
                                             ELSE l.count
                                             END     AS weakest,
                                         ROW_NUMBER()   OVER (
                                           PARTITION BY l.word2_id
                                           ORDER BY
                                               CASE
                                                    WHEN w.weakest < l.count THEN w.weakest
                                                    ELSE l.count
                                               END DESC,
                                               l.count DESC,
                                               l.word2_id ASC,
                                               w.word_id ASC
                                       ) AS rn
                                  FROM wave w
                                           JOIN links l
                                                ON l.word1_id = w.word_id
                                           LEFT JOIN visited v
                                                 ON v.word_id = l.word2_id
                                           LEFT JOIN stop_words sw1
                                                 ON sw1.word_id = w.word_id
                                           LEFT JOIN stop_words sw2
                                                 ON sw2.word_id = l.word2_id
                                  WHERE l.count >= ?
                                    AND v.word_id IS NULL
                                    AND sw1.word_id IS NULL
                                    AND sw2.word_id IS NULL
                                    AND l.word2_id NOT IN (SELECT direct.word2_id
                                                           FROM links direct
                                                           WHERE direct.word1_id = ?
                                                             AND direct.word2_id IS NOT NULL
                                                             AND direct.count >= ?
                                                           UNION
                                                           SELECT direct.word1_id
                                                           FROM links direct
                                                           WHERE direct.word2_id = ?
                                                             AND direct.word1_id IS NOT NULL
                                                             AND direct.count >= ?))
                            WHERE rn = 1
                            """,
                            (self.MIN_LINK_WEIGHT, start, self.DIRECT_LINK_MIN_WEIGHT, start,
                             self.DIRECT_LINK_MIN_WEIGHT))

        rows = self.fetchone("""
                             SELECT COUNT(*)
                             FROM wave_next
                             """)[0]

        self.cursor.execute("""
                            INSERT OR REPLACE INTO visited
                            SELECT *
                            FROM wave_next
                            """)

        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("""
                            INSERT INTO wave
                            (word_id,
                             parent_id,
                             depth,
                             weakest)
                            SELECT word_id,
                                   parent_id,
                                   depth,
                                   weakest
                            FROM wave_next
                            ORDER BY weakest DESC
                            """)

        self.execute("""
                     DELETE
                     FROM wave_next
                     """)
        self.conn.commit()

        if rows > 0:
            print(f"  Волна {self.MAX_DEPTH}: найдено {rows} вершин")
        return rows


# ============================================================
# SemanticMap — статистика
# ============================================================

class SemanticMap:
    def __init__(self, finder):
        self.finder = finder
        self.nodes = {}
        self.links = {}

    def add(self, start_word, hypotheses):
        for target, paths in hypotheses.items():
            for item in paths:
                for vertex in set(item["path"]):
                    if vertex not in self.nodes:
                        self.nodes[vertex] = {"count": 0, "sources": set()}
                    self.nodes[vertex]["count"] += 1
                    self.nodes[vertex]["sources"].add(start_word)

                path = item["path"]
                for i in range(len(path) - 1):
                    link = (path[i], path[i + 1])
                    if link not in self.links:
                        self.links[link] = {"count": 0, "sources": set()}
                    self.links[link]["count"] += 1
                    self.links[link]["sources"].add(start_word)

    def print_top_nodes(self):
        items = sorted(self.nodes.items(), key=lambda x: x[1]["count"], reverse=True)
        for node_id, info in items:
            if info["count"] < 2:
                continue
            word = self.finder.get_word_text(node_id)
            print(f"{word:30} {info['count']:>6}")

    def print_common_nodes(self, min_sources=2):
        items = sorted(self.nodes.items(), key=lambda x: len(x[1]["sources"]), reverse=True)
        for node, info in items:
            if len(info["sources"]) < min_sources:
                break
            word = self.finder.get_word_text(node)
            print(word)
            print(f"  sources: {len(info['sources'])}")
            print(f"  {', '.join(sorted(info['sources']))}")
            print()


# ============================================================
# TreeExporter — экспорт дерева в XLSX (из yandex_exporter.py)
# ============================================================

class TreeExporter:
    """Экспорт дерева связанных запросов в XLSX для Яндекс.Директ."""

    def __init__(self, db_path: Path, max_depth: int = 3, min_link_weight: int = 1000):
        if not HAS_OPENPYXL:
            raise RuntimeError(
                "openpyxl не установлен. Выполните: .venv\\Scripts\\pip install openpyxl"
            )

        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.MAX_DEPTH = max_depth
        self.MIN_LINK_WEIGHT = min_link_weight
        self.INITIAL_WEAKEST = 2147483647
        self.OUTPUT_XLSX = OUTPUT_XLSX
        self.START_WORDS_FILE = START_WORDS_FILE
        self.all_tree_rows = []

        self.connect()

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.cursor = self.conn.cursor()
        self._create_search_tables()

    def _create_search_tables(self):
        self.cursor.executescript("""
            CREATE TABLE IF NOT EXISTS wave
            (
                word_id   INTEGER PRIMARY KEY,
                parent_id INTEGER,
                depth     INTEGER,
                weakest   INTEGER
            );
            CREATE TABLE IF NOT EXISTS wave_next
            (
                word_id   INTEGER PRIMARY KEY,
                parent_id INTEGER,
                depth     INTEGER,
                weakest   INTEGER
            );
            CREATE TABLE IF NOT EXISTS visited
            (
                word_id   INTEGER PRIMARY KEY,
                parent_id INTEGER,
                depth     INTEGER,
                weakest   INTEGER
            );
        """)
        self.conn.commit()

    def run(self, start_word=None):
        if start_word is not None:
            start_word = start_word.strip()
            start = self.find_word_id(start_word)
            if start is None:
                print(f"Слово '{start_word}' не найдено.")
                return
            console_line(f"Стартовое слово: {start_word} (id: {start})")
            self.process_start_word(start)

    def _reset_search(self):
        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("DELETE FROM wave_next")
        self.cursor.execute("DELETE FROM visited")
        self.conn.commit()

    def _run_bfs(self):
        for depth in range(1, self.MAX_DEPTH + 1):
            new_rows = self.build_next_wave()
            rows = self.cursor.execute("SELECT COUNT(*) FROM visited").fetchone()[0]
            console_status(
                f"Поиск связанных слов: уровень {depth}/{self.MAX_DEPTH} | "
                f"найдено: {rows:,}".replace(",", " ")
            )
            if new_rows == 0:
                break
        print()
        rows = self.cursor.execute("SELECT COUNT(*) FROM visited").fetchone()[0]
        console_line(f"Поиск завершён: найдено {rows:,} слов".replace(",", " "))
        return rows

    def _initialize_start_node(self, start):
        self.cursor.execute(
            "INSERT INTO wave (word_id, parent_id, depth, weakest) VALUES (?, ?, ?, ?)",
            (start, start, 0, self.INITIAL_WEAKEST)
        )

    def process_start_word(self, start):
        self._reset_search()
        self._initialize_start_node(start)
        self._run_bfs()
        console_status("Формирование дерева...")
        self.export_tree(start)

    def build_next_wave(self):
        self.cursor.execute("DELETE FROM wave_next")
        self.cursor.execute("""
            INSERT INTO wave_next
            (word_id, parent_id, depth, weakest)
            SELECT word_id, parent_id, depth, weakest
            FROM (
                SELECT
                    l.word2_id AS word_id,
                    w.word_id AS parent_id,
                    w.depth + 1 AS depth,
                    CASE
                        WHEN w.weakest < l.count THEN w.weakest
                        ELSE l.count
                    END AS weakest,
                    ROW_NUMBER() OVER (
                        PARTITION BY l.word2_id
                        ORDER BY
                            CASE
                                WHEN w.weakest < l.count THEN w.weakest
                                ELSE l.count
                            END DESC,
                            w.word_id ASC
                    ) AS rn
                FROM wave w
                JOIN links l ON l.word1_id = w.word_id
                LEFT JOIN visited v ON v.word_id = l.word2_id
                WHERE l.count >= ?
                  AND v.word_id IS NULL
            )
            WHERE rn = 1
        """, (self.MIN_LINK_WEIGHT,))

        rows = self.cursor.execute("SELECT COUNT(*) FROM wave_next").fetchone()[0]
        self.cursor.execute("INSERT INTO visited SELECT * FROM wave_next")
        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("""
            INSERT INTO wave (word_id, parent_id, depth, weakest)
            SELECT word_id, parent_id, depth, weakest
            FROM wave_next
            ORDER BY weakest DESC, word_id ASC
        """)
        return rows

    def _prepare_tree_rows(self):
        rows = self.cursor.execute("""
            SELECT
                v.word_id, v.parent_id, v.depth, v.weakest,
                w.word AS word,
                COALESCE(GROUP_CONCAT(DISTINCT g.name), '') AS group_name
            FROM visited v
            JOIN words w ON w.id = v.word_id
            LEFT JOIN group_words gw ON gw.word_id = v.word_id
            LEFT JOIN groups g ON g.id = gw.group_id
               AND (g.parent_id IS NULL OR g.parent_id != 1)
            GROUP BY v.word_id, v.parent_id, v.depth, v.weakest, w.word
            ORDER BY v.depth, v.weakest DESC
        """).fetchall()

        return [
            {
                "word_id": word_id,
                "parent_id": parent_id,
                "depth": depth,
                "weakest": weakest,
                "word": word,
                "group": group_name
            }
            for (word_id, parent_id, depth, weakest, word, group_name) in rows
        ]

    def export_tree(self, start):
        tree_rows = self._prepare_tree_rows()
        tree = self.build_parent_index(tree_rows)
        start_word = self.get_word(start)
        flat_rows = []
        self.walk_tree(tree, start, 0, [], start_word, self.MAX_DEPTH, flat_rows)
        self.all_tree_rows.extend(flat_rows)
        console_line(f"Дерево сформировано: {len(flat_rows):,} записей".replace(",", " "))

    def _make_header(self, max_depth):
        header = ["Стартовое слово"]
        header.extend(f"Уровень {level}" for level in range(1, max_depth + 1))
        header.append("Группа")
        return header

    def _fill_worksheet(self, worksheet, flat_rows, max_depth, hide_prefix=False):
        header = self._make_header(max_depth)
        worksheet.append(header)
        prev_row = []
        for flat_row in flat_rows:
            row = flat_row["row"]
            if hide_prefix:
                out = row.copy()
                i = 0
                while i < len(out) and i < len(prev_row) and out[i] == prev_row[i]:
                    out[i] = ""
                    i += 1
                prev_row = row.copy()
            else:
                out = row
            worksheet.append(out)

        if worksheet.max_row >= 1:
            worksheet.auto_filter.ref = worksheet.dimensions
        worksheet.freeze_panes = "A2"

        for column_cells in worksheet.columns:
            max_length = 0
            for cell in column_cells:
                value = "" if cell.value is None else str(cell.value)
                max_length = max(max_length, len(value))
            column_letter = get_column_letter(column_cells[0].column)
            worksheet.column_dimensions[column_letter].width = min(max_length + 2, 60)

    def _fill_outline(self, worksheet, flat_rows):
        worksheet.sheet_properties.outlinePr.summaryBelow = True
        worksheet.sheet_properties.outlinePr.summaryRight = True
        worksheet.sheet_properties.outlinePr.showOutlineSymbols = True
        worksheet.sheet_view.showOutlineSymbols = True

        for excel_row, flat_row in enumerate(flat_rows, start=2):
            level = flat_row["level"]
            worksheet.row_dimensions[excel_row].outlineLevel = level
            if level > 0:
                worksheet.row_dimensions.group(
                    excel_row, excel_row, outline_level=level, hidden=False
                )

        max_outline_level = max(
            (dimension.outlineLevel for dimension in worksheet.row_dimensions.values()),
            default=0
        )
        worksheet.sheet_format.outlineLevelRow = max_outline_level
        return max_outline_level

    def _sort_global(self):
        return sorted(
            self.all_tree_rows,
            key=lambda entry: (
                entry["row"][-1],
                entry["row"][0],
                entry["row"][1],
                entry["row"][2],
                entry["row"][3]
            )
        )

    def build_workbook(self):
        workbook = Workbook()
        workbook.remove(workbook.active)

        worksheet = workbook.create_sheet("Все значения")
        sorted_rows = self._sort_global()
        self._fill_worksheet(worksheet, sorted_rows, self.MAX_DEPTH)
        max_outline_level = self._fill_outline(worksheet, sorted_rows)

        for start_word in self._ordered_start_words():
            flat_rows = [row for row in self.all_tree_rows if row["start_word"] == start_word]
            if not flat_rows:
                continue
            worksheet = workbook.create_sheet(start_word[:31])
            self._fill_worksheet(worksheet, flat_rows, self.MAX_DEPTH, hide_prefix=True)
            max_outline_level = max(max_outline_level, self._fill_outline(worksheet, flat_rows))

        workbook.save(self.OUTPUT_XLSX)
        return max_outline_level

    def _ordered_start_words(self):
        seen = []
        seen_set = set()
        for row in self.all_tree_rows:
            if row["start_word"] not in seen_set:
                seen_set.add(row["start_word"])
                seen.append(row["start_word"])
        return seen

    def build_parent_index(self, csv_rows):
        tree = defaultdict(list)
        for row in csv_rows:
            tree[row["parent_id"]].append({
                "word_id": row["word_id"],
                "parent_id": row["parent_id"],
                "depth": row["depth"],
                "weakest": row["weakest"],
                "word": row["word"],
                "group": row["group"]
            })
        for children in tree.values():
            children.sort(key=lambda child: (child["group"].lower(), child["weakest"], child["word"]))
        return tree

    def _build_tree_row(self, row, child, level):
        new_row = row.copy()
        while len(new_row) <= level:
            new_row.append("")
        new_row[level] = child["word"]
        return new_row

    def walk_tree(self, tree, parent_id, level, row, start_word, max_depth, flat_rows):
        children = tree.get(parent_id, [])
        for child in children:
            new_row = self._build_tree_row(row, child, level)
            out = new_row.copy()
            out.insert(0, start_word)
            while len(out) < max_depth + 1:
                out.append("")
            out.append(child["group"])
            flat_rows.append({
                "start_word": start_word,
                "level": level,
                "row": out
            })
            self.walk_tree(tree, child["word_id"], level + 1, new_row, start_word, max_depth, flat_rows)

    def find_word_id(self, word):
        self.cursor.execute("SELECT id FROM words WHERE word = ?", (word,))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def get_word(self, word_id):
        self.cursor.execute("SELECT word FROM words WHERE id = ?", (word_id,))
        row = self.cursor.fetchone()
        return row[0] if row else None


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Поиск гипотез и экспорт дерева")
    parser.add_argument("--xlsx", action="store_true", help="Экспорт дерева в XLSX")
    args = parser.parse_args()

    config = load_config()
    paths = get_paths(config)
    start_words_file = paths["start_words"]

    if not start_words_file.exists():
        print(f"❌ Файл стартовых слов не найден: {start_words_file}")
        print("   Создайте файл data/input/start_words.txt")
        return

    with open(start_words_file, "r", encoding="utf-8") as f:
        start_words = [line.strip() for line in f if line.strip()]

    if not start_words:
        print("❌ Файл start_words.txt пуст.")
        return

    print(f"Найдено стартовых слов: {len(start_words)}")

    if args.xlsx:
        # Режим XLSX
        print("\n" + "=" * 70)
        print("  ЭКСПОРТ ДЕРЕВА В XLSX")
        print("=" * 70)

        finder_config = config.get("hypothesis_finder", {})
        max_depth = finder_config.get("max_depth", 3)
        min_link_weight = finder_config.get("min_link_weight", 1000)

        try:
            exporter = TreeExporter(paths["db"], max_depth=max_depth, min_link_weight=min_link_weight)
        except RuntimeError as e:
            print(f"❌ {e}")
            return

        try:
            for word in start_words:
                try:
                    exporter.run(word)
                except Exception as exc:
                    console_line(f"Ошибка при обработке слова '{word}': {exc}")

            max_outline_level = exporter.build_workbook()
            print()
            console_line(f"Максимальный outlineLevel по строкам: {max_outline_level}")
            console_line(f"XLSX создан: {exporter.OUTPUT_XLSX}")
            console_line(f"Всего записей: {len(exporter.all_tree_rows):,}".replace(",", " "))
        finally:
            if exporter.conn is not None:
                exporter.conn.close()
                print()
                print("Соединение с базой закрыто.")
    else:
        # Режим поиска гипотез
        finder = SemanticExplorer(paths["db"], config)
        compass = SemanticMap(finder)
        output_file = paths["output"]
        output_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            for word in start_words:
                print(f"\n{'='*60}")
                print(f"Поиск гипотез для: '{word}'")
                print(f"{'='*60}")
                hypotheses = finder.find_hypotheses(word)

                if finder.SHOW_HYPOTHESES:
                    for target, paths_list in list(hypotheses.items())[:20]:
                        print(
                            " -> ".join(finder.get_word_text(x) for x in paths_list[0]["path"]),
                            paths_list[0]["weakest"],
                        )

                compass.add(word, hypotheses)

            if finder.SHOW_STATISTICS:
                print("\n" + "=" * 60)
                print("ИТОГОВАЯ СТАТИСТИКА")
                print("=" * 60)
                print(f"Уникальных вершин: {len(compass.nodes):,}")
                print(f"Уникальных связей : {len(compass.links):,}")
                compass.print_top_nodes()

            with open(output_file, "w", encoding="utf-8") as f:
                f.write(f"Всего уникальных вершин: {len(compass.nodes)}\n")
                f.write(f"Всего уникальных связей: {len(compass.links)}\n\n")
                compass.print_common_nodes(min_sources=2)

            print(f"\n✅ Результаты сохранены в: {output_file}")
        finally:
            if finder.conn is not None:
                finder.conn.close()
                print("\nСоединение с базой закрыто.")


if __name__ == "__main__":
    main()