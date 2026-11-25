"""
Hypothesis Finder
Назначение: поиск отсутствующих связей в графе поисковых запросов.

Алгоритм:
  1. Загружается граф из SQLite.
  2. Для каждой стартовой вершины выполняется BFS глубиной N.
  3. Если конечная вершина не имеет прямой связи со стартовой
     (с весом >= direct_link_min_weight) — путь считается гипотезой.
  4. Для каждой гипотезы вычисляется weakest link.

Настройки читаются из config/hypothesis_finder.yaml.
Запуск: python scripts/hypothesis_finder.py
"""
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
import yaml


# ============================================================
# ПУТИ
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config" / "hypothesis_finder.yaml"
DEFAULT_DB = PROJECT_ROOT / "data" / "output" / "semantics.db"
DEFAULT_START_WORDS = PROJECT_ROOT / "data" / "input" / "start_words.txt"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "output" / "hypotheses.txt"

INFINITE_WEIGHT = 2147483647


# ============================================================
# КОНФИГ
# ============================================================
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
        print(f"✅ Конфиг загружен: {CONFIG_FILE.relative_to(PROJECT_ROOT)}")
        return config
    else:
        print(f"⚠️  Конфиг не найден: {CONFIG_FILE.relative_to(PROJECT_ROOT)}")
        print("   Используются настройки по умолчанию.")
        return defaults


def get_paths(config: dict) -> dict:
    """Возвращает абсолютные пути из конфига (или дефолтные)."""
    paths_cfg = config.get("hypothesis_finder", {}).get("paths", {})
    return {
        "db": PROJECT_ROOT / paths_cfg.get("db", "data/output/semantics.db"),
        "start_words": PROJECT_ROOT / paths_cfg.get("start_words", "data/input/start_words.txt"),
        "output": PROJECT_ROOT / paths_cfg.get("output", "data/output/hypotheses.txt"),
    }


# ============================================================
# SemanticExplorer
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
            text = f"[{text}]"  # можно добавить цвет, если нужна colorama
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
        # -------------------------------------------------
        # 1. Построение следующей волны
        # -------------------------------------------------
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

        # -------------------------------------------------
        # 2. Сколько новых вершин найдено
        # -------------------------------------------------
        rows = self.fetchone("""
                             SELECT COUNT(*)
                             FROM wave_next
                             """)[0]

        # -------------------------------------------------
        # 3. Добавляем новые вершины в общее дерево поиска
        # -------------------------------------------------
        self.cursor.execute("""
                            INSERT OR REPLACE INTO visited
                            SELECT *
                            FROM wave_next
                            """)

        # -------------------------------------------------
        # 4. Подготавливаем следующую волну
        # -------------------------------------------------
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

        # -------------------------------------------------
        # 5. Диагностика
        # -------------------------------------------------
        if rows > 0:
            print(f"  Волна {self.MAX_DEPTH}: найдено {rows} вершин")

        return rows


# ============================================================
# SemanticMap
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
# Точка входа
# ============================================================
def main():
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