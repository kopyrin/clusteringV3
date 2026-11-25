"""
Поиск семантического ядра от стартовых слов.
Настройки читаются из config/core_builder.yaml.
Запуск: python scripts/core_builder.py
"""
import argparse
import cProfile
import csv
import io
import math
import pstats
import sys
import time
from pathlib import Path
import sqlite3
import yaml

from common import (
    ROOT,
    CORE_CONFIG_FILE,
    DB_FILE,
    START_WORDS_FILE,
    CORE_OUTPUT_CSV,
    CORE_CHECKPOINT,
    REQUIRED_INDEXES,
    ensure_indexes,
    apply_pragmas,
    get_pattern_marks,
)


# ============================================================
# АРГУМЕНТЫ
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Построение семантического ядра")
    parser.add_argument("--timing", action="store_true",
                        help="Показывать время по этапам (load/stat/choose/add)")
    parser.add_argument("--profile", action="store_true",
                        help="Запустить cProfile и показать топ медленных функций")
    parser.add_argument("--check-indexes", action="store_true",
                        help="Только проверить/создать индексы и выйти")
    return parser.parse_args()


ARGS = parse_args()


# ============================================================
# КОНФИГ
# ============================================================

def load_config() -> dict:
    if CORE_CONFIG_FILE.exists():
        with open(CORE_CONFIG_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f)
            return config if config else {}
    return {}


# ============================================================
# ЗАГРУЗКА МЕТОК
# ============================================================

def load_marked_words_from_dicts(config: dict) -> set:
    cfg = config.get("core_builder", {}).get("mark_dicts", {})
    if not cfg.get("enabled", True):
        return set()

    words = set()
    dict_dir = ROOT / "dict"
    if not dict_dir.exists():
        return words

    if cfg.get("use_all", True):
        files = sorted(dict_dir.glob("*.txt"))
    else:
        files = [dict_dir / f for f in cfg.get("files", []) if (dict_dir / f).exists()]

    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    word = line.strip().lower()
                    if word:
                        words.add(word)
        except Exception:
            pass
    return words


def load_marked_groups_from_db(graph, config: dict) -> set:
    cb = config.get("core_builder", {})
    cfg = cb.get("mark_groups")
    if cfg is None or not cfg.get("enabled", True):
        return set()

    groups_cfg = cfg.get("groups", {})
    marked_groups = []
    for key, value in groups_cfg.items():
        if isinstance(value, bool):
            if value:
                marked_groups.append(key)
        elif isinstance(value, dict):
            for group_name, enabled in value.items():
                if enabled:
                    marked_groups.append(group_name)

    if not marked_groups:
        return set()

    placeholders = ", ".join("?" * len(marked_groups))
    rows = graph.cur.execute(
        f"""
        SELECT DISTINCT gw.word_id
        FROM group_words gw
        JOIN groups g ON g.id = gw.group_id
        WHERE g.name IN ({placeholders})
        """,
        tuple(marked_groups),
    ).fetchall()
    return {row["word_id"] for row in rows}


# ============================================================
# КЛАСС Graph (ОПТИМИЗИРОВАННЫЙ)
# ============================================================

class Graph:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()
        apply_pragmas(self.conn)

        # ГЛОБАЛЬНЫЕ КЭШИ (не сбрасываются между стартовыми словами)
        self._word_cache = {}  # word_id -> word
        self._word_id_cache = {}  # word -> word_id
        self._pos_cache = {}  # word_id -> pos
        self._count_cache = {}  # word_id -> count
        self._total_links_cache = {}  # word_id -> total_links
        self._groups_cache = {}  # word_id -> [groups]
        self._neighbors_cache = {}  # word_id -> {neighbor_id: weight}

        # Предзагружаем ВСЕ данные при старте
        self._preload_all_data()

    def _preload_all_data(self):
        """Загружает ВСЕ данные из БД в память за один проход."""
        print("📥 Предзагрузка данных из БД...")
        t0 = time.perf_counter()

        # Слова
        rows = self.cur.execute("SELECT id, word, pos, count FROM words").fetchall()
        for row in rows:
            self._word_cache[row["id"]] = row["word"]
            self._word_id_cache[row["word"]] = row["id"]
            self._pos_cache[row["id"]] = row["pos"]
            self._count_cache[row["id"]] = row["count"]
        print(f"  ✅ Загружено {len(self._word_cache):,} слов ({time.perf_counter() - t0:.2f}с)")

        # Total links (ОДИН запрос вместо миллионов)
        t1 = time.perf_counter()
        rows = self.cur.execute("""
                                SELECT word_id, COUNT(*) as cnt
                                FROM (SELECT word1_id as word_id
                                      FROM links
                                      UNION ALL
                                      SELECT word2_id as word_id
                                      FROM links)
                                GROUP BY word_id
                                """).fetchall()
        for row in rows:
            self._total_links_cache[row["word_id"]] = row["cnt"]
        print(f"  ✅ Загружено {len(self._total_links_cache):,} total_links ({time.perf_counter() - t1:.2f}с)")

        # Groups
        t2 = time.perf_counter()
        rows = self.cur.execute("""
                                SELECT gw.word_id, g.name
                                FROM group_words gw
                                         JOIN groups g ON g.id = gw.group_id
                                ORDER BY gw.word_id, g.name
                                """).fetchall()
        for row in rows:
            if row["word_id"] not in self._groups_cache:
                self._groups_cache[row["word_id"]] = []
            self._groups_cache[row["word_id"]].append(row["name"])
        print(f"  ✅ Загружено {len(self._groups_cache):,} слов с группами ({time.perf_counter() - t2:.2f}с)")
        print(f"📥 Предзагрузка завершена за {time.perf_counter() - t0:.2f}с\n")

    def get_word_pos(self, word_id: int):
        return self._pos_cache.get(word_id, "")

    def get_word_id(self, word: str):
        return self._word_id_cache.get(word)

    def get_word(self, word_id: int):
        return self._word_cache.get(word_id)

    def get_word_count(self, word_id: int):
        return self._count_cache.get(word_id, 0)

    def get_total_links_for_word(self, word_id: int) -> int:
        return self._total_links_cache.get(word_id, 0)

    def get_neighbors(self, word_id: int) -> dict:
        if word_id in self._neighbors_cache:
            return self._neighbors_cache[word_id]

        rows1 = self.cur.execute(
            "SELECT word2_id, count FROM links WHERE word1_id = ? ORDER BY count DESC LIMIT 2000",
            (word_id,),
        ).fetchall()
        rows2 = self.cur.execute(
            "SELECT word1_id, count FROM links WHERE word2_id = ? ORDER BY count DESC LIMIT 2000",
            (word_id,),
        ).fetchall()

        result = {}
        for row in rows1:
            result[row[0]] = row[1]
        for row in rows2:
            if row[0] not in result:
                result[row[0]] = row[1]
            else:
                result[row[0]] = max(result[row[0]], row[1])

        result = dict(sorted(result.items(), key=lambda item: item[1], reverse=True))
        self._neighbors_cache[word_id] = result
        return result

    def get_word_groups(self, word_id: int) -> list:
        return self._groups_cache.get(word_id, [])

    def close(self):
        self.conn.close()


# ============================================================
# КЛАСС CoreBuilder (ОПТИМИЗИРОВАННЫЙ)
# ============================================================

class CoreBuilder:
    def __init__(
            self,
            graph: Graph,
            start_word: str,
            config: dict,
            marked_words: set,
            marked_group_ids: set,
            start_index: int = 0,
            total_starts: int = 0,
    ):
        self.graph = graph
        self.config = config.get("core_builder", {})
        self.marked_words = marked_words
        self.marked_group_ids = marked_group_ids
        self.candidates_marks = {}

        self.start_word = start_word
        self.start_id = graph.get_word_id(start_word)
        if self.start_id is None:
            raise ValueError(f'Слово "{start_word}" не найдено.')

        self.min_connections = self.config.get("min_connections", 1)
        self.max_core_size = self.config.get("max_core_size", 200)
        self.collect_all = self.config.get("collect_all", True)
        self.start_index = start_index
        self.total_starts = total_starts

        self.core = [self.start_id]
        self.core_set = {self.start_id}
        self.candidates = {}
        self.statistics = []

        self._core_metadata = {}
        self._core_metadata[self.start_id] = {
            "word": self.graph.get_word(self.start_id),
            "pos": self.graph.get_word_pos(self.start_id),
            "count": self.graph.get_word_count(self.start_id),
            "groups": self.graph.get_word_groups(self.start_id),
        }

        self.all_candidates_log = {}
        self.reject_stats = {
            "min_connections": 0,
            "max_connections": 0,
            "min_word_freq": 0,
            "max_word_freq": 0,
        }

        self._last_added_word = None
        self._start_time = time.perf_counter()
        self._timing = {
            "load_candidates": 0.0,
            "calculate_statistics": 0.0,
            "choose_best": 0.0,
            "add": 0.0,
            "iterations": 0,
        }

    def _render_progress(self, bar_width: int = 30) -> str:
        current = self.size
        total = self.max_core_size
        ratio = min(current / total, 1.0) if total > 0 else 0
        filled = int(round(bar_width * ratio))
        empty = bar_width - filled
        bar = "█" * filled + "░" * empty
        percent = ratio * 100
        elapsed = time.perf_counter() - self._start_time
        last_word = self._last_added_word or "—"
        if len(last_word) > 15:
            last_word = last_word[:12] + "..."
        candidates_count = len(self.candidates)
        return f"[{bar}] {current}/{total} ({percent:.1f}%)  +{last_word:<15}  {elapsed:.1f}с  канд:{candidates_count}"

    def _print_progress(self):
        progress_info = f"[{self.start_index}/{self.total_starts}]" if self.total_starts > 0 else ""
        line = f"\r{progress_info} {self.start_word:<20} {self._render_progress()}"
        sys.stdout.write(line)
        sys.stdout.flush()

    def build(self, show_progress: bool = True):
        iteration = 1
        total_time = 0.0
        self._start_time = time.perf_counter()

        if show_progress:
            self._print_progress()

        while self.size < self.max_core_size:
            t0 = time.perf_counter()

            t_load = time.perf_counter()
            self.load_candidates()
            self._timing["load_candidates"] += time.perf_counter() - t_load

            if not self.candidates:
                break

            t_stat = time.perf_counter()
            self.calculate_statistics()
            self._timing["calculate_statistics"] += time.perf_counter() - t_stat

            t_choose = time.perf_counter()
            best = self.choose_best()
            self._timing["choose_best"] += time.perf_counter() - t_choose

            if best is None:
                break

            word = self.graph.get_word(best)
            self._last_added_word = word

            t_add = time.perf_counter()
            self.add(best)
            self._timing["add"] += time.perf_counter() - t_add

            elapsed = time.perf_counter() - t0
            total_time += elapsed
            iteration += 1
            self._timing["iterations"] += 1

            if show_progress:
                self._print_progress()

            ckpt = self.config.get("checkpoint", {})
            if ckpt.get("enabled") and iteration % ckpt.get("interval", 10) == 0:
                self.save_checkpoint()

        if show_progress:
            sys.stdout.write("\n")
            sys.stdout.flush()

        return total_time

    def load_candidates(self):
        self.candidates = {}
        min_link_weight = self.config.get("min_link_weight", 0)

        for core_id in self.core:
            neighbors = self.graph.get_neighbors(core_id)
            for neighbor_id, weight in neighbors.items():
                if neighbor_id in self.core_set:
                    continue
                if weight < min_link_weight:
                    continue

                word = self.graph.get_word(neighbor_id)
                marks = []
                if word in self.marked_words:
                    marks.append("dict")
                if neighbor_id in self.marked_group_ids:
                    marks.append("group")

                if neighbor_id not in self.candidates:
                    self.candidates[neighbor_id] = {}
                    self.candidates_marks[neighbor_id] = marks

                self.candidates[neighbor_id][core_id] = weight

    def calculate_statistics(self):
        self.statistics = []
        cfg = self.config

        for candidate_id, links in self.candidates.items():
            weights = list(links.values())
            connections = len(weights)
            min_conn = cfg.get("min_connections", 1)
            max_conn = cfg.get("max_connections")

            if connections < min_conn:
                self.reject_stats["min_connections"] += 1
                word = self.graph.get_word(candidate_id)
                self.all_candidates_log[candidate_id] = {
                    "word": word, "word_id": candidate_id,
                    "weight": sum(weights), "pos": self.graph.get_word_pos(candidate_id),
                    "status": "rejected", "reason": "min_connections",
                }
                continue

            if max_conn is not None and connections > max_conn:
                self.reject_stats["max_connections"] += 1
                word = self.graph.get_word(candidate_id)
                self.all_candidates_log[candidate_id] = {
                    "word": word, "word_id": candidate_id,
                    "weight": sum(weights), "pos": self.graph.get_word_pos(candidate_id),
                    "status": "rejected", "reason": "max_connections",
                }
                continue

            word_count = self.graph.get_word_count(candidate_id)
            min_freq = cfg.get("min_word_freq", 0)
            max_freq = cfg.get("max_word_freq")

            if word_count < min_freq:
                self.reject_stats["min_word_freq"] += 1
                word = self.graph.get_word(candidate_id)
                self.all_candidates_log[candidate_id] = {
                    "word": word, "word_id": candidate_id,
                    "weight": sum(weights), "pos": self.graph.get_word_pos(candidate_id),
                    "status": "rejected", "reason": "min_word_freq",
                }
                continue

            if max_freq is not None and word_count > max_freq:
                self.reject_stats["max_word_freq"] += 1
                word = self.graph.get_word(candidate_id)
                self.all_candidates_log[candidate_id] = {
                    "word": word, "word_id": candidate_id,
                    "weight": sum(weights), "pos": self.graph.get_word_pos(candidate_id),
                    "status": "rejected", "reason": "max_word_freq",
                }
                continue

            total = sum(weights) - max(weights)
            score = total / word_count if word_count > 0 else 0

            self.statistics.append({
                "id": candidate_id,
                "connections": connections,
                "sum": total,
                "word_count": word_count,
                "avg": total / connections,
                "score": score,
                "min": min(weights),
                "max": max(weights),
                "total_links": self.graph.get_total_links_for_word(candidate_id),
            })

        self.statistics.sort(key=lambda x: x["score"], reverse=True)

    def choose_best(self):
        for item in self.statistics:
            if self.check_candidate(item["id"]):
                return item["id"]
        return None

    def check_candidate(self, candidate_id):
        return True

    def core_words(self):
        return [self.graph.get_word(x) for x in self.core]

    def add(self, word_id: int):
        if word_id in self.core_set:
            return
        self.core.append(word_id)
        self.core_set.add(word_id)

        word = self.graph.get_word(word_id)
        pos = self.graph.get_word_pos(word_id)
        count = self.graph.get_word_count(word_id)
        groups = self.graph.get_word_groups(word_id)

        if not groups:
            groups = get_pattern_marks(word)

        self._core_metadata[word_id] = {
            "word": word, "pos": pos, "count": count, "groups": groups,
        }

    @property
    def size(self):
        return len(self.core)

    def save_checkpoint(self):
        import json
        words = self.core_words()
        CORE_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
        with open(CORE_CHECKPOINT, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False, indent=2)

    def print_timing_summary(self):
        total = sum(self._timing[k] for k in ("load_candidates", "calculate_statistics", "choose_best", "add"))
        if total < 0.001:
            return
        print(f"  ⏱  Тайминг '{self.start_word}' ({self._timing['iterations']} итераций):")
        for key in ("load_candidates", "calculate_statistics", "choose_best", "add"):
            t = self._timing[key]
            pct = (t / total * 100) if total > 0 else 0
            print(f"      {key:<22} {t:6.3f}с  ({pct:5.1f}%)")
        print(f"      {'ИТОГО':<22} {total:6.3f}с")


# ============================================================
# ВЫВОД РЕЗУЛЬТАТОВ
# ============================================================

def save_results(config: dict, builders: list):
    cfg = config.get("core_builder", {}).get("output", {})
    fmt = cfg.get("format", "csv")
    include_all = cfg.get("include_all", True)
    show_marks = cfg.get("show_marks", True)

    CORE_OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    if fmt in ("csv", "both"):
        with open(CORE_OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            header = ["слово", "частота", "часть речи", "группы"]
            if show_marks:
                header.append("метки")
            writer.writerow(header)

            for builder in builders:
                all_words = []
                for word_id in builder.core:
                    meta = builder._core_metadata.get(word_id, {})
                    word = meta.get("word", str(word_id))
                    pos = meta.get("pos", "")
                    weight = meta.get("count", 0)
                    groups = meta.get("groups", [])
                    if not groups:
                        word = meta.get("word", "")
                        groups = get_pattern_marks(word)
                    groups_str = ", ".join(groups) if groups else ""
                    marks = builder.candidates_marks.get(word_id, [])
                    marks_str = ", ".join(marks) if marks else ""
                    all_words.append({
                        "word": word, "weight": weight, "pos": pos,
                        "groups": groups_str, "marks": marks_str,
                    })

                if include_all:
                    for word_id, log in builder.all_candidates_log.items():
                        word = log["word"]
                        pos = log["pos"]
                        weight = log["weight"]
                        groups = builder.graph.get_word_groups(word_id) if word_id else []
                        if not groups:
                            word = log.get("word", "")
                            groups = get_pattern_marks(word)
                        groups_str = ", ".join(groups) if groups else ""
                        marks = builder.candidates_marks.get(word_id, [])
                        marks_str = ", ".join(marks) if marks else ""
                        all_words.append({
                            "word": word, "weight": weight, "pos": pos,
                            "groups": groups_str, "marks": marks_str,
                        })

                no_marks = [w for w in all_words if not w["marks"]]
                with_marks = [w for w in all_words if w["marks"]]
                no_marks.sort(key=lambda x: x["weight"], reverse=True)
                with_marks.sort(key=lambda x: x["weight"], reverse=True)

                for row in no_marks + with_marks:
                    csv_row = [row["word"], row["weight"], row["pos"], row["groups"]]
                    if show_marks:
                        csv_row.append(row["marks"])
                    writer.writerow(csv_row)

    if fmt in ("txt", "both"):
        txt_file = CORE_OUTPUT_CSV.with_suffix(".txt")
        with open(txt_file, "w", encoding="utf-8") as f:
            for builder in builders:
                core_rows = []
                for word_id in builder.core:
                    meta = builder._core_metadata.get(word_id, {})
                    word = meta.get("word", str(word_id))
                    weight = meta.get("count", 0)
                    core_rows.append((weight, word))
                core_rows.sort(key=lambda x: x[0], reverse=True)
                for weight, word in core_rows:
                    f.write(f"{word}\n")


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def print_parameters(config: dict):
    cb = config.get("core_builder", {})
    print("\n" + "=" * 70)
    print("📋 ПАРАМЕТРЫ АЛГОРИТМА (config/core_builder.yaml)")
    print("=" * 70)
    print("\n🎯 ОСНОВНЫЕ ПАРАМЕТРЫ ВЫБОРКИ:")
    print(f"  • max_core_size    = {cb.get('max_core_size', 200)}")
    print(f"    Макс. количество слов в ядре для каждого стартового слова")
    print(f"  • min_connections  = {cb.get('min_connections', 1)}")
    print(f"    Мин. связей кандидата со словами ядра (отсекает случайные)")
    print(f"  • max_connections  = {cb.get('max_connections', 'не задан')}")
    print(f"    Макс. связей (фильтр от слишком общих слов)")
    print(f"  • min_link_weight  = {cb.get('min_link_weight', 0)}")
    print(f"    Мин. вес связи (сила ассоциации между словами)")
    print("\n📊 ФИЛЬТРЫ ЧАСТОТНОСТИ:")
    print(f"  • min_word_freq    = {cb.get('min_word_freq', 0)}")
    print(f"    Мин. частота слова в корпусе (отсекает редкие)")
    print(f"  • max_word_freq    = {cb.get('max_word_freq', 'не задан')}")
    print(f"    Макс. частота слова (отсекает слишком общие)")
    print("\n💾 ЧЕКПОИНТЫ:")
    ckpt = cb.get("checkpoint", {})
    print(f"  • checkpoint.enabled  = {ckpt.get('enabled', False)}")
    print(f"    Сохранять промежуточные результаты в core_words_checkpoint.json")
    print(f"  • checkpoint.interval = {ckpt.get('interval', 10)}")
    print(f"    Сохранять каждые N итераций")
    print("\n📁 ВЫВОД РЕЗУЛЬТАТОВ:")
    out = cb.get("output", {})
    print(f"  • output.format     = {out.get('format', 'csv')}")
    print(f"    Формат файла: csv, txt или both")
    print(f"  • output.include_all = {out.get('include_all', True)}")
    print(f"    Включать отсеянных кандидатов (с причинами отсева)")
    print(f"  • output.show_marks  = {out.get('show_marks', True)}")
    print(f"    Показывать колонку с метками (dict, group)")
    print("\n🏷️  МЕТКИ СЛОВ (пометки в выводе, не исключения):")
    md = cb.get("mark_dicts", {})
    mg = cb.get("mark_groups", {})
    print(f"  • mark_dicts.enabled  = {md.get('enabled', True)}")
    print(f"    Загружать слова из dict/*.txt для пометки")
    print(f"  • mark_dicts.use_all  = {md.get('use_all', True)}")
    print(f"    Использовать все файлы из dict/ или только указанные")
    print(f"  • mark_dicts.files    = {md.get('files', [])}")
    print(f"    Список файлов (если use_all=false)")
    print(f"  • mark_groups.enabled = {mg.get('enabled', True)}")
    print(f"    Загружать слова из групп БД для пометки")
    groups_list = list(mg.get("groups", {}).keys())
    print(f"  • mark_groups.groups  = {groups_list if len(groups_list) <= 5 else groups_list[:5] + ['...']}")
    print("\n💡 КАК РАБОТАЕТ АЛГОРИТМ:")
    print("  1. Берём стартовое слово из start_words.txt")
    print("  2. Находим всех его соседей в графе (связанные слова)")
    print("  3. Для каждого кандидата: score = сумма_связей / частота_слова")
    print("  4. Применяем фильтры (min/max connections, min/max freq)")
    print("  5. Выбираем кандидата с максимальным score")
    print("  6. Добавляем в ядро, повторяем до max_core_size")
    print("  7. Переходим к следующему стартовому слову")
    print("=" * 70)


def run():
    if not DB_FILE.exists():
        print(f"❌ БД не найдена: {DB_FILE}")
        return

    if not START_WORDS_FILE.exists():
        print(f"❌ Файл стартовых слов не найден: {START_WORDS_FILE}")
        return

    with open(START_WORDS_FILE, "r", encoding="utf-8") as f:
        start_words = [line.strip() for line in f if line.strip()]

    if not start_words:
        print("❌ Файл start_words.txt пуст.")
        return

    config = load_config()
    print_parameters(config)

    graph = Graph(DB_FILE)

    print("\n🔍 Проверка индексов БД...")
    idx_stats = ensure_indexes(graph.conn, verbose=True)
    if idx_stats["created"]:
        print(f"✅ Создано {len(idx_stats['created'])} индексов")
    elif idx_stats["existing"]:
        print(f"✅ Все нужные индексы уже существуют ({len(idx_stats['existing'])})")
    if idx_stats["errors"]:
        print(f"❌ Ошибок: {len(idx_stats['errors'])}")

    print("\n📚 Загрузка меток:")
    marked_words = load_marked_words_from_dicts(config)
    marked_group_ids = load_marked_groups_from_db(graph, config)

    builders = []
    total_start = time.perf_counter()
    total_starts = len(start_words)

    print(f"\n🚀 Старт. Слов: {total_starts} | Помечено: {len(marked_words) + len(marked_group_ids):,}")
    if ARGS.timing:
        print("🔍 Режим тайминга включён")
    print()

    for idx, start_word in enumerate(start_words, 1):
        start_id = graph.get_word_id(start_word)
        if start_id is None:
            sys.stdout.write(f"  [{idx}/{total_starts}] {start_word:<20} [пропуск — нет в БД]\n")
            sys.stdout.flush()
            continue

        builder = CoreBuilder(
            graph=graph,
            start_word=start_word,
            config=config,
            marked_words=marked_words,
            marked_group_ids=marked_group_ids,
            start_index=idx,
            total_starts=total_starts,
        )
        builder.build(show_progress=True)
        builders.append(builder)

        if ARGS.timing:
            builder.print_timing_summary()

    total_elapsed = time.perf_counter() - total_start

    if builders:
        save_results(config, builders)
        total_words = sum(b.size for b in builders)
        print(f"\n✅ Готово. Слов в ядрах: {total_words:,} | Время: {total_elapsed:.1f}с")
        print(f"   Результат: {CORE_OUTPUT_CSV.relative_to(ROOT)}")

    graph.close()


def main():
    if ARGS.check_indexes:
        if not DB_FILE.exists():
            print(f"❌ БД не найдена: {DB_FILE}")
            return
        conn = sqlite3.connect(DB_FILE)
        print(f"📂 БД: {DB_FILE}")
        stats = ensure_indexes(conn, verbose=True)
        if stats["created"]:
            print(f"\n✅ Создано {len(stats['created'])} индексов")
        elif stats["existing"]:
            print(f"\n✅ Все нужные индексы уже существуют")
        conn.close()
        return

    if ARGS.profile:
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            run()
        finally:
            profiler.disable()
            print("\n" + "=" * 70)
            print("📊 ПРОФИЛИРОВАНИЕ — топ-30 по совокупному времени")
            print("=" * 70)
            stream = io.StringIO()
            stats = pstats.Stats(profiler, stream=stream)
            stats.sort_stats("cumulative")
            stats.print_stats(30)
            print(stream.getvalue())
            print("=" * 70)
            print("📊 ТОП-30 по собственному времени")
            print("=" * 70)
            stream2 = io.StringIO()
            stats2 = pstats.Stats(profiler, stream=stream2)
            stats2.sort_stats("tottime")
            stats2.print_stats(30)
            print(stream2.getvalue())
    else:
        run()


if __name__ == "__main__":
    main()