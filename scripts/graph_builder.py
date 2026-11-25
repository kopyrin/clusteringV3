"""
Построение графа связей из keywords.txt — v10.

Словарь из БД (lemmas + words), лемматизации в горячем цикле нет.
Файл может иметь 3+ колонки: запрос <TAB> показы <TAB> точные показы.
ВЕС (сумма показов) и DF (число разных запросов) считаются РАЗДЕЛЬНО.

Этапы:
  scan  — keywords.txt -> ID-файл + вес + DF
  links — ID-файл -> пары (фильтр по словарю) -> таблица links
  prune — отсечение по DF пары

Что нового в v10 (совместимо с ID-файлом v3 и прогрессом v9):
  * этап 2 НЕ стартует автоматически без ограничения словаря (--all-lemmas для явного согласия)
  * --links-restart: принудительный пересчёт связей с нуля (защита от смешения словарей)
  * подпись словаря (top-lemmas / min-lemma-hits) хранится отдельно и сверяется при resume
  * weights.bin / hits.bin переименовываются в .bak, а не удаляются
  * согласованный чекпоинт: stats_meta.json, откат к согласованной точке при resume
  * links WITHOUT ROWID (авто-миграция пустой таблицы), prune без rowid
  * flush_links генератором (без списка кортежей на 8 млн строк)
  * словарь raw_to_id НЕ грузится для links-only / report / prune-only / status / df-hist
  * один dry_run (с корзиной веса "0"), beta по чередующимся записям
  * --status, --df-hist, --prune-only, --no-prune, --scan-only
  * контроль свободного места, --cache-gb, --links-buffer, --report-chunks

Запуск:
  python scripts/graph_builder.py --dry-run
  python scripts/graph_builder.py --status
  python scripts/graph_builder.py                      # scan (после него остановится)
  python scripts/graph_builder.py --report
  python scripts/graph_builder.py --links-only --top-lemmas 200000 --min-pair-hits 10
  python scripts/graph_builder.py --df-hist
  python scripts/graph_builder.py --prune-only --min-pair-hits 20
"""

from pathlib import Path
import argparse
import bisect
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import struct
import sys
import time
from array import array
from collections import defaultdict, Counter
from itertools import combinations

# ============================================================
# НАСТРОЙКИ
# ============================================================
MIN_QUERY_FREQ = 2           # порог по ВЕСУ строки (показы)
MIN_PAIR_HITS = 3            # пара должна быть минимум в N РАЗНЫХ запросах
MIN_PAIR_COUNT = 0           # доп. порог по сумме весов (0 = выкл)
TOP_LEMMAS = 0               # 0 = все; иначе топ-N лемм по DF
MIN_LEMMA_HITS = 0           # 0 = выкл; иначе лемма минимум в N запросах
MAX_WORDS_PER_QUERY = 8
MIN_LEMMA_LEN = 2

STOP_POS = ("предлог", "союз", "частица", "местоимение", "предикатив", "междометие")
CYRILLIC_ONLY = True
ALLOWED_POS = ("существительное", "прилагательное", "краткое прилагательное",
               "сравнительная степень", "глагол", "инфинитив",
               "причастие", "краткое причастие", "деепричастие", "наречие")

TOP_EDGES_PER_NODE = 0
LEMMATIZE_UNKNOWN = False
UNKNOWN_DUMP_LIMIT = 100_000
UNKNOWN_CAP = 1_000_000
MARK_FILTERED_FORMS = True

QUERY_COL = 0                # 0-based индекс колонки с запросом
FREQ_COL = -1                # 0-based индекс колонки с весом (-1 = последняя)

NUM_WORKERS = 4
READ_CHUNK = 32 * 1024 * 1024
ID_FILE_BUFFER_SIZE = 32 * 1024 * 1024
LINKS_BUFFER_LIMIT = 3_000_000
CHECKPOINT_SEC = 120
STATUS_UPDATE_EVERY_SEC = 5
PROGRESS_EVERY = -1          # -1 = авто (5 сек лог / 300 сек лог), 0 = выключить
QUIET = False
STATS_IMPORT_CHUNK = 200_000
VACUUM_AFTER_PRUNE = False
CACHE_GB = 1.0
MAKE_HITS_INDEX = True

SAMPLE_CHUNKS = 32
SAMPLE_CHUNK_SIZE = 4 * 1024 * 1024
SAMPLE_MAX_RECORDS = 300_000

REPORT_CHUNKS = 32
REPORT_CHUNK_SIZE = 1 * 1024 * 1024
REPORT_TOPN = (50_000, 100_000, 200_000, 300_000, 500_000, 1_000_000, 2_000_000)
HALF_PAIRS_CAP = 20_000_000

HIT_BITS = 32
HIT_MASK = 0xFFFFFFFF
BIG_RANK = 1 << 30
CNT_BITS = 20
CNT_MASK = (1 << CNT_BITS) - 1

PROGRESS_VERSION = 10
COMPAT_VERSIONS = (9, 10)
ID_FILE_MAGIC = b"KWID"
ID_FILE_VERSION = 3

ID_STRUCT_HEADER = struct.Struct("<4sB3x")
ID_STRUCT_COUNT = struct.Struct("<H")
_PACKERS = [struct.Struct(f"<{k}I") for k in range(65)]

_CYR = re.compile(r"^[а-яё-]+$")

POS_RU = {
    "NOUN": "существительное", "ADJF": "прилагательное", "ADJS": "краткое прилагательное",
    "COMP": "сравнительная степень", "VERB": "глагол", "INFN": "инфинитив",
    "PRTF": "причастие", "PRTS": "краткое причастие", "GRND": "деепричастие",
    "NUMR": "числительное", "ADVB": "наречие", "NPRO": "местоимение",
    "PRED": "предикатив", "PREP": "предлог", "CONJ": "союз",
    "PRCL": "частица", "INTJ": "междометие",
}

SQL_UPSERT_LINKS = """INSERT INTO links(word1_id, word2_id, count, hits) VALUES(?,?,?,?)
ON CONFLICT(word1_id, word2_id) DO UPDATE SET
count = count + excluded.count, hits = hits + excluded.hits"""

SQL_LINKS_WOR = """CREATE TABLE links(
    word1_id INTEGER NOT NULL, word2_id INTEGER NOT NULL,
    count INTEGER DEFAULT 0, hits INTEGER DEFAULT 0,
    PRIMARY KEY(word1_id, word2_id)) WITHOUT ROWID"""

SQL_LINKS_ROWID = """CREATE TABLE links(
    word1_id INTEGER NOT NULL, word2_id INTEGER NOT NULL,
    count INTEGER DEFAULT 0, hits INTEGER DEFAULT 0,
    PRIMARY KEY(word1_id, word2_id))"""


def find_root() -> Path:
    try:
        current = Path(__file__).resolve()
    except NameError:
        current = Path.cwd().resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


PROJECT_ROOT = find_root()
INPUT_DIR = PROJECT_ROOT / "data" / "input"
INPUT_FILE = INPUT_DIR / "keywords.txt"
MINUS_FILE = INPUT_DIR / "minus.txt"
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = OUTPUT_DIR / "semantics.db"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"
ID_FILE = OUTPUT_DIR / "keywords_ids.bin"
WEIGHT_FILE = OUTPUT_DIR / "weights.bin"
HITS_FILE = OUTPUT_DIR / "hits.bin"
STATS_META_FILE = OUTPUT_DIR / "stats_meta.json"
UNKNOWN_FILE = OUTPUT_DIR / "unknown_words.txt"

# ============================================================
# Состояние
# ============================================================
conn = None
cursor = None
raw_to_id = {}
allowed_lemmas = {}
minus_words = set()
next_word_id = 1
max_word_id = 0
interrupted = False

LINE_DELIM = "\t"
N_COLS = 3

word_weight = None           # array('q') сумма показов
word_hits = None             # array('i') число запросов
stats_size = 0
node_ok = None
links_buffer = defaultdict(int)
unknown_words = Counter()

progress = {
    "version": PROGRESS_VERSION, "stage": "scan",
    "byte_offset": 0, "id_file_offset": 0, "records_written": 0,
    "links_records": 0, "lines": 0, "config": {}, "links_config": {},
    "timestamp": None,
}

stats = {
    "start": 0.0, "init_end": 0.0,
    "scan_start": 0.0, "scan_end": 0.0,
    "links_start": 0.0, "links_end": 0.0,
    "lines": 0, "lines_kept": 0, "records": 0,
    "tokens_kept": 0, "tokens_unknown": 0, "tokens_filtered": 0,
    "pairs_total": 0, "pairs_kept": 0, "links_saved": 0, "links_final": 0,
    "commits": 0, "dict_forms": 0, "dict_lemmas": 0, "dict_zero": 0,
}

try:
    import psutil
    _proc = psutil.Process()
except Exception:
    _proc = None


def rss():
    if _proc is None:
        return ""
    try:
        return f" | RAM {human(_proc.memory_info().rss)}"
    except Exception:
        return ""


def fmt(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def human(n):
    n = float(n)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


def big(n):
    if n >= 1e9:
        return f"{n / 1e9:.2f} млрд"
    if n >= 1e6:
        return f"{n / 1e6:.1f} млн"
    if n >= 1e3:
        return f"{n / 1e3:.1f} тыс"
    return f"{n:.0f}"

# ============================================================
# Прогресс: одна строка в терминале, редкие строки в логе
# ============================================================
IS_TTY = False
_last_len = 0
_last_ts = 0.0


def init_console():
    global IS_TTY, PROGRESS_EVERY
    try:
        IS_TTY = sys.stdout.isatty()
    except Exception:
        IS_TTY = False
    if PROGRESS_EVERY < 0:
        PROGRESS_EVERY = (600.0 if QUIET else 5.0) if IS_TTY else 300.0


def term_width():
    try:
        return max(40, shutil.get_terminal_size((100, 25)).columns - 1)
    except Exception:
        return 79


def short(n):
    n = float(n)
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def rss_s():
    if _proc is None:
        return ""
    try:
        return f" | RAM {_proc.memory_info().rss / 1024 ** 3:.1f}Г"
    except Exception:
        return ""


def status_line(text, force=False):
    """В терминале — перезапись одной строки, в логе — строка раз в PROGRESS_EVERY."""
    global _last_len, _last_ts
    if PROGRESS_EVERY <= 0 and not force:
        return
    now = time.perf_counter()
    if not force and now - _last_ts < PROGRESS_EVERY:
        return
    _last_ts = now
    if IS_TTY:
        text = text[:term_width()]
        sys.stdout.write("\r" + text + " " * max(0, _last_len - len(text)))
        _last_len = len(text)
    else:
        sys.stdout.write(text.rstrip() + "\n")
        _last_len = 0
    sys.stdout.flush()


def status_end():
    global _last_len
    if IS_TTY and _last_len:
        sys.stdout.write("\n")
        sys.stdout.flush()
        _last_len = 0

def free_space(path=None):
    try:
        return shutil.disk_usage(str(path or OUTPUT_DIR)).free
    except Exception:
        return 0


def check_disk(need_bytes, label):
    free = free_space()
    print(f"💽 Свободно на диске: {human(free)} | нужно ориентировочно {human(need_bytes)} ({label})")
    if free and free < need_bytes:
        print("   ⚠️ Места может не хватить. Освободи диск или уменьши словарь.")
    if free and free < 5 * 1024 ** 3:
        print("   ⛔ Меньше 5 ГБ — останавливаюсь, чтобы не убить базу на середине.")
        return False
    return True


# ============================================================
# Прогресс
# ============================================================
def scan_config():
    """Ключи РОВНО как в v9 — иначе существующий прогресс станет несовместимым."""
    return {
        "min_freq": MIN_QUERY_FREQ, "max_words": MAX_WORDS_PER_QUERY,
        "min_lemma_len": MIN_LEMMA_LEN, "cyr": CYRILLIC_ONLY,
        "pos": list(ALLOWED_POS), "delim": LINE_DELIM,
        "query_col": QUERY_COL, "freq_col": FREQ_COL,
        "mark_filtered": MARK_FILTERED_FORMS,
    }


def links_config():
    """Подпись словаря для этапа 2. Смена = связи надо строить заново."""
    return {"top_lemmas": TOP_LEMMAS, "min_lemma_hits": MIN_LEMMA_HITS}


def save_progress(update_config=False):
    progress["version"] = PROGRESS_VERSION
    progress["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if update_config or not progress.get("config"):
        progress["config"] = scan_config()
    progress["links_config"] = links_config()
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_FILE)


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            with PROGRESS_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ progress.json повреждён: {e}")
    return None


def clear_progress():
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()


# ============================================================
# База
# ============================================================
def connect_db(readonly=False):
    global conn, cursor
    if readonly:
        conn = sqlite3.connect(f"file:{DB_FILE.as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(DB_FILE, timeout=60)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA cache_size = -{int(max(0.05, CACHE_GB) * 1024 * 1024)}")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 1073741824")
    cursor = conn.cursor()
    print(f"🔌 База: {DB_FILE} (кэш {CACHE_GB:.1f} ГБ)")


def disconnect_db():
    if conn:
        try:
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA optimize")
        except Exception:
            pass
        conn.close()
        print("🔌 База закрыта.")


def ensure_column(table, col, decl):
    cols = {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        print(f"   ➕ Добавлена колонка {table}.{col}")


def links_without_rowid():
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='links'").fetchone()
    return bool(row and "WITHOUT ROWID" in (row[0] or "").upper())


def row_cost():
    return 32 if links_without_rowid() else 55


def init_schema():
    cursor.execute("""CREATE TABLE IF NOT EXISTS lemmas(
        word TEXT PRIMARY KEY, lemma TEXT NOT NULL, pos TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS words(
        id INTEGER PRIMARY KEY, word TEXT UNIQUE NOT NULL, pos TEXT,
        count INTEGER DEFAULT 0, hits INTEGER DEFAULT 0)""")

    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='links'").fetchone()
    if row is None:
        cursor.execute(SQL_LINKS_WOR)
        print("   ➕ Таблица links создана (WITHOUT ROWID)")
    elif "WITHOUT ROWID" not in (row[0] or "").upper():
        empty = cursor.execute("SELECT 1 FROM links LIMIT 1").fetchone() is None
        if empty:
            cursor.execute("DROP TABLE links")
            cursor.execute(SQL_LINKS_WOR)
            print("   ♻️ links переведена на WITHOUT ROWID (−40% размера, быстрее upsert)")
        else:
            print("   ⚠️ links в старом формате (rowid) и не пуста — оставляю как есть.")
            print("      Перевод: --links-only --links-restart ... на пустой таблице.")

    ensure_column("words", "hits", "INTEGER DEFAULT 0")
    ensure_column("links", "hits", "INTEGER DEFAULT 0")
    conn.commit()

    if MAKE_HITS_INDEX:
        has = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_words_hits'").fetchone()
        if not has:
            t = time.perf_counter()
            print("   🔧 Создаю индекс words(hits DESC)...", flush=True)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_words_hits ON words(hits DESC)")
            conn.commit()
            print(f"      готово за {fmt(time.perf_counter() - t)}")


def reset_facts():
    print("🗑️ Очистка links и счётчиков (словари сохраняются)...")
    t = time.perf_counter()
    cursor.execute("DELETE FROM links")
    cursor.execute("UPDATE words SET count = 0, hits = 0")
    conn.commit()
    for p in (ID_FILE, WEIGHT_FILE, HITS_FILE, STATS_META_FILE,
              WEIGHT_FILE.with_suffix(".bin.bak"), HITS_FILE.with_suffix(".bin.bak")):
        if p.exists():
            p.unlink()
    print(f"   Готово за {fmt(time.perf_counter() - t)}")


# ============================================================
# Формат строки
# ============================================================
def parse_freq(s):
    if not s:
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        pass
    try:
        t = str(s).strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    except Exception:
        return None
    if not t:
        return None
    try:
        v = int(float(t))
        return v if v >= 0 else None
    except ValueError:
        return None


def detect_format(verbose=True):
    global LINE_DELIM, N_COLS
    if not INPUT_FILE.exists():
        return
    size = INPUT_FILE.stat().st_size
    samples, examples = [], []
    with INPUT_FILE.open("rb") as f:
        for off in (0, size // 3, size // 2, size * 2 // 3):
            f.seek(off)
            if off:
                f.readline()
            data = f.read(256 * 1024)
            if not data:
                continue
            lines = [l.strip() for l in data.decode("utf-8", errors="ignore").split("\n")[:-1] if l.strip()]
            if not examples:
                examples = lines[:3]
            samples.extend(lines[:2000])
    if not samples:
        print("⚠️ Не удалось прочитать примеры строк")
        return

    best = (0.0, "\t", 1)
    for delim in ("\t", ";", "|", ","):
        colcount = Counter()
        ok = 0
        for l in samples:
            parts = l.split(delim)
            colcount[len(parts)] += 1
            if len(parts) >= 2:
                try:
                    fs = parts[FREQ_COL]
                except IndexError:
                    continue
                if parse_freq(fs) is not None:
                    ok += 1
        modal, modal_n = colcount.most_common(1)[0]
        score = (ok / len(samples)) * (modal_n / len(samples))
        if score > best[0]:
            best = (score, delim, modal)

    LINE_DELIM, N_COLS = best[1], best[2]
    if verbose:
        print("\n" + "-" * 70)
        print("🧭 ФОРМАТ СТРОКИ (автоопределение)")
        print("-" * 70)
        print(f"   Разделитель: {LINE_DELIM!r} | колонок: {N_COLS} | уверенность {best[0] * 100:.1f}%")
        qn = QUERY_COL + 1
        fn = N_COLS if FREQ_COL == -1 else FREQ_COL + 1
        print(f"   Запрос = колонка {qn} | вес = колонка {fn}")
        for l in examples:
            parts = l.split(LINE_DELIM)
            print(f"   {l[:80]!r}")
            print(f"      колонок {len(parts)}: {parts[:6]}")
            try:
                print(f"      -> запрос={parts[QUERY_COL]!r}  вес={parse_freq(parts[FREQ_COL])}")
            except IndexError:
                print("      -> строка не разбирается")


# ============================================================
# Обработчик строки
# ============================================================
def make_processor(min_freq, max_words, collect_unknown=True, funnel=None, unk_counter=None):
    raw_get = raw_to_id.get
    delim = LINE_DELIM
    qcol = QUERY_COL
    fcol = FREQ_COL
    unk = unknown_words if unk_counter is None else unk_counter
    cap = UNKNOWN_CAP
    fn = funnel

    n_unknown = n_filtered = n_kept = 0
    since = 0

    def sync():
        nonlocal n_unknown, n_filtered, n_kept, since
        stats["tokens_unknown"] += n_unknown
        stats["tokens_filtered"] += n_filtered
        stats["tokens_kept"] += n_kept
        n_unknown = n_filtered = n_kept = 0
        since = 0

    def process(line):
        nonlocal n_unknown, n_filtered, n_kept, since
        since += 1
        if since >= 65536:
            sync()

        if fn is not None:
            fn["lines"] += 1
        if not line:
            return None

        parts = line.split(delim)
        if len(parts) < 2:
            q, fs = parts[0], ""
        else:
            try:
                q = parts[qcol]
                fs = parts[fcol]
            except IndexError:
                return None

        try:
            freq = int(fs)
        except (ValueError, TypeError):
            freq = parse_freq(fs)

        if fn is not None:
            if freq is None:
                fn["freq_bad"] += 1
            else:
                fn["freq_ok"] += 1
            fv = freq if freq is not None else 1
            fn["hist"]["0" if fv == 0 else
                       "1" if fv == 1 else
                       "2-4" if fv <= 4 else
                       "5-9" if fv <= 9 else
                       "10-99" if fv <= 99 else
                       "100-9999" if fv <= 9999 else
                       "10000+"] += 1

        if freq is None:
            freq = 1
        if freq < min_freq:
            return None
        if fn is not None:
            fn["freq_pass"] += 1

        tokens = q.split()
        if len(tokens) < 2:
            return None
        if fn is not None:
            fn["tokens2"] += 1

        ids = set()
        add = ids.add
        for tok in tokens:
            wid = raw_get(tok)
            if wid is None:
                n_unknown += 1
                if collect_unknown and len(unk) < cap and tok.isalpha():
                    unk[tok] += 1
            elif wid == 0:
                n_filtered += 1
            else:
                add(wid)

        if len(ids) < 2:
            return None
        if fn is not None:
            fn["ids2"] += 1

        if len(ids) > max_words:
            ids = set(sorted(ids)[:max_words])
        n_kept += len(ids)
        return sorted(ids), freq

    process.sync = sync
    return process


# ============================================================
# DRY RUN
# ============================================================
def dry_run():
    print("\n" + "=" * 70)
    print("🔎 DRY RUN: реальные фильтры на выборке по всему файлу")
    print("=" * 70)
    if not INPUT_FILE.exists():
        print(f"❌ Нет файла: {INPUT_FILE}")
        return

    size = INPUT_FILE.stat().st_size
    step = max(1, size // SAMPLE_CHUNKS)
    per_chunk = max(1, SAMPLE_MAX_RECORDS // SAMPLE_CHUNKS)

    funnel = Counter()
    funnel["hist"] = Counter()
    unk = Counter()
    process = make_processor(MIN_QUERY_FREQ, MAX_WORDS_PER_QUERY,
                             collect_unknown=True, funnel=funnel, unk_counter=unk)

    lines = kept = 0
    bytes_read = 0
    recs = []
    lemma_df = Counter()
    lemma_w = Counter()
    t0 = time.perf_counter()

    with INPUT_FILE.open("rb") as f:
        for i in range(SAMPLE_CHUNKS):
            f.seek(i * step)
            if i:
                f.readline()
            data = f.read(SAMPLE_CHUNK_SIZE)
            if not data:
                continue
            cut = data.rfind(b"\n")
            if cut >= 0:
                data = data[:cut + 1]
            bytes_read += len(data)
            got = 0
            for line in data.decode("utf-8", errors="ignore").split("\n"):
                line = line.strip()
                if not line:
                    continue
                lines += 1
                r = process(line)
                if r is None:
                    continue
                kept += 1
                ids, freq = r
                for w in ids:
                    lemma_df[w] += 1
                    lemma_w[w] += freq
                if got < per_chunk:
                    recs.append((ids, freq))
                    got += 1

    process.sync()
    dt = time.perf_counter() - t0
    print(f"\nПрочитано {human(bytes_read)} за {dt:.1f} сек, строк: {lines:,}")
    print("-" * 70)
    print("ВОРОНКА ФИЛЬТРОВ:")
    for key, label in [("lines", "строк прочитано"), ("freq_ok", "вес распознан"),
                       ("freq_pass", f"вес >= {MIN_QUERY_FREQ}"),
                       ("tokens2", "токенов в запросе >= 2"), ("ids2", "найдено >= 2 ID")]:
        v = funnel[key]
        mark = "   ← ЗДЕСЬ ОБРЫВ" if v == 0 else ""
        print(f"   {label:<28} {v:>12,}  ({v / max(1, lines) * 100:5.1f}%){mark}")

    order = ["0", "1", "2-4", "5-9", "10-99", "100-9999", "10000+"]
    mins = [0, 1, 2, 5, 10, 100, 10000]
    print("Распределение веса строки:")
    for k in order:
        if funnel["hist"][k]:
            note = "   ← фантомные запросы (точных показов нет)" if k == "0" else ""
            print(f"   вес {k:>9}: {funnel['hist'][k] / max(1, lines) * 100:5.1f}%{note}")

    print(f"Токенов: взято {stats['tokens_kept']:,}, "
          f"отфильтровано {stats['tokens_filtered']:,}, неизвестно {stats['tokens_unknown']:,}")

    if not lines or not recs:
        print("\n❌ ДАННЫХ ПОСЛЕ ФИЛЬТРОВ НЕТ. Смотри воронку выше:")
        if funnel["freq_ok"] == 0:
            print("   • вес не распознаётся → проверь --query-col / --freq-col")
        elif funnel["freq_pass"] == 0:
            print(f"   • все веса < {MIN_QUERY_FREQ} → поставь --min-freq 1")
        elif funnel["tokens2"] == 0:
            print("   • в колонке запроса меньше 2 слов → проверь --query-col")
        else:
            print("   • словарь не находит слова → --all-pos / --allow-latin")
        if unk:
            print("\n   Топ-15 нераспознанных токенов:")
            for w, c in unk.most_common(15):
                print(f"      {w:<25} {c:,}")
        return

    total_lines = size / (bytes_read / lines)
    keep_share = kept / lines
    total_records = total_lines * keep_share
    n = len(recs)
    avg_words = sum(len(i) for i, _ in recs) / n
    avg_rec_bytes = 2 + (avg_words + 1) * 4

    total_pairs = 0
    full = set()
    hset = set()
    for idx, (ids, freq) in enumerate(recs):
        for a, b in combinations(ids, 2):
            total_pairs += 1
            key = (a << 32) | b
            full.add(key)
            if idx & 1:
                hset.add(key)

    u_full, u_half = len(full), len(hset)
    beta = 1.0
    if u_half > 100 and u_full > u_half:
        beta = min(1.0, max(0.5, math.log(u_full / u_half) / math.log(2)))

    scale = total_records / n
    proj_pairs = total_pairs * scale
    proj_unique = u_full * (scale ** beta)
    rc = row_cost()

    print("-" * 70)
    print(f"Всего строк в файле (оценка):   {big(total_lines)}")
    print(f"Прошло фильтры:                 {kept:,} ({keep_share * 100:.1f}%)")
    print(f"Записей в выборке для пар:      {n:,} (квота {per_chunk:,} на чанк)")
    print(f"Слов на запрос после фильтра:   {avg_words:.2f}")
    print(f"Пар на запрос:                  {total_pairs / n:.2f}")
    print(f"Уникальных лемм в выборке:      {len(lemma_df):,}")
    print("-" * 70)
    print("ПРОГНОЗ НА ВЕСЬ ФАЙЛ (без ограничения словаря):")
    print(f"   Записей в ID-файле:          {big(total_records)}")
    print(f"   Размер ID-файла:             {human(total_records * avg_rec_bytes)}")
    print(f"   Всего пар:                   {big(proj_pairs)}")
    print(f"   Строк в links (β={beta:.2f}):    {big(proj_unique)}")
    print(f"   Размер таблицы:              {human(proj_unique * rc * 0.85)} … {human(proj_unique * rc * 1.3)}")
    print(f"   Время этапа 1 (оценка):      {fmt(total_lines / max(1, lines / dt))}")
    if beta >= 0.99:
        print("   ⚠️ β=1.00 — выборка слишком мала, повторов почти нет.")
        print("      Это ВЕРХНЯЯ граница, а не прогноз. Точную цифру даст --report после скана.")
    print("-" * 70)
    print("ЭФФЕКТ --min-freq (по гистограмме весов, с учётом нулевых):")
    cond = funnel["ids2"] / max(1, funnel["freq_pass"])
    for thr in (1, 2, 5, 10, 100):
        share = sum(funnel["hist"][k] for k, m in zip(order, mins) if m >= thr) / max(1, lines)
        rec = total_lines * share * cond
        mark = "   ← текущий" if thr == MIN_QUERY_FREQ else ""
        print(f"   --min-freq {thr:>4}: строк {share * 100:5.1f}% → записей {big(rec)}, "
              f"пар {big(rec * total_pairs / n)}{mark}")
    print("   ⚠️ Менять --min-freq после скана НЕЛЬЗЯ: это пересчёт с нуля.")
    print("-" * 70)
    df_scale = total_records / max(1, kept)
    print("DF лемм В ВЫБОРКЕ (в скольких запросах встретилась):")
    dfh = Counter()
    for w, c in lemma_df.items():
        dfh["1" if c == 1 else "2-4" if c <= 4 else "5-19" if c <= 19
            else "20-99" if c <= 99 else "100+"] += 1
    for k in ("1", "2-4", "5-19", "20-99", "100+"):
        print(f"   DF {k:>6}: {dfh[k]:,} лемм")
    print(f"   Масштаб выборки ×{df_scale:,.0f} — корпусный DF ориентировочно во столько же раз больше.")
    print("   ➜ Пороги по DF подбираются ТОЧНО командой --report после этапа 1.")

    print("\nТоп-20 лемм по DF (числу запросов) в выборке:")
    for wid, c in lemma_df.most_common(20):
        row = cursor.execute("SELECT word FROM words WHERE id=?", (wid,)).fetchone()
        print(f"   {(row[0] if row else wid):<25} запросов {c:>8,}   вес {lemma_w[wid]:,}")

    if unk:
        print("\nТоп-10 нераспознанных токенов:")
        for w, c in unk.most_common(10):
            print(f"   {w:<25} {c:,}")
    print("=" * 70)
    print("Порядок: scan → --report → --links-only --top-lemmas N → --df-hist → --prune-only")


# ============================================================
# Массивы статистики
# ============================================================
def init_stats_arrays(load_existing=False):
    global word_weight, word_hits, stats_size
    stats_size = max_word_id + 1
    word_weight = array('q', [0]) * stats_size
    word_hits = array('i', [0]) * stats_size
    ok = (load_existing and WEIGHT_FILE.exists() and HITS_FILE.exists()
          and WEIGHT_FILE.stat().st_size == stats_size * 8
          and HITS_FILE.stat().st_size == stats_size * 4)
    if ok:
        w = array('q')
        with WEIGHT_FILE.open("rb") as f:
            w.fromfile(f, stats_size)
        h = array('i')
        with HITS_FILE.open("rb") as f:
            h.fromfile(f, stats_size)
        word_weight, word_hits = w, h
        print(f"▶️ Статистика загружена из weights.bin/hits.bin ({human(stats_size * 12)})")
    else:
        for p in (WEIGHT_FILE, HITS_FILE, STATS_META_FILE):
            if p.exists():
                p.unlink()
        print(f"🧮 Массивы статистики: {stats_size:,} ячеек ({human(stats_size * 12)})")


def save_stats_arrays(meta=None):
    tw = WEIGHT_FILE.with_suffix(".tmp")
    with tw.open("wb") as f:
        word_weight.tofile(f)
    os.replace(tw, WEIGHT_FILE)
    th = HITS_FILE.with_suffix(".tmp")
    with th.open("wb") as f:
        word_hits.tofile(f)
    os.replace(th, HITS_FILE)
    if meta is not None:
        tm = STATS_META_FILE.with_suffix(".tmp")
        with tm.open("w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tm, STATS_META_FILE)


def load_stats_meta():
    if STATS_META_FILE.exists():
        try:
            with STATS_META_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def import_stats_arrays():
    """Идемпотентно: SET, не +=. Повтор не удваивает."""
    if word_weight is None:
        return
    print("\n💾 Импорт веса и DF в таблицу words...")
    t = time.perf_counter()
    rows = []
    total = 0
    for wid in range(stats_size):
        h = word_hits[wid]
        if h:
            rows.append((int(word_weight[wid]), int(h), wid))
        if len(rows) >= STATS_IMPORT_CHUNK:
            cursor.executemany("UPDATE words SET count = ?, hits = ? WHERE id = ?", rows)
            conn.commit()
            total += len(rows)
            rows.clear()
            stats["commits"] += 1
            print(f"\r   записано {total:,}...", end="", flush=True)
    if rows:
        cursor.executemany("UPDATE words SET count = ?, hits = ? WHERE id = ?", rows)
        conn.commit()
        total += len(rows)
        stats["commits"] += 1
    print(f"\r   ✅ Записано {total:,} лемм за {fmt(time.perf_counter() - t)}")

    # v10: НЕ удаляем — оставляем страховку от потери базы (174 МБ).
    for p in (WEIGHT_FILE, HITS_FILE):
        if p.exists():
            bak = p.with_suffix(".bin.bak")
            try:
                os.replace(p, bak)
            except Exception:
                pass
    print(f"   🛡️ Копии сохранены: {WEIGHT_FILE.name}.bak / {HITS_FILE.name}.bak")


def load_node_filter():
    global node_ok
    n = (cursor.execute("SELECT MAX(id) FROM words").fetchone()[0] or 0) + 1
    top = cursor.execute("SELECT MAX(hits) FROM words").fetchone()[0] or 0
    if top == 0:
        node_ok = bytearray(b"\x01") * n
        print("⚠️ В words все hits=0 (скан не импортирован) — фильтр лемм выключен")
        return
    if TOP_LEMMAS > 0:
        node_ok = bytearray(n)
        kept = 0
        t = time.perf_counter()
        for (wid,) in cursor.execute(
                "SELECT id FROM words WHERE hits > 0 ORDER BY hits DESC LIMIT ?", (TOP_LEMMAS,)):
            if wid < n:
                node_ok[wid] = 1
                kept += 1
        print(f"✂️ Топ-{TOP_LEMMAS:,} лемм по DF: оставлено {kept:,} за {fmt(time.perf_counter() - t)}")
    elif MIN_LEMMA_HITS > 0:
        node_ok = bytearray(n)
        kept = 0
        for (wid,) in cursor.execute("SELECT id FROM words WHERE hits >= ?", (MIN_LEMMA_HITS,)):
            if wid < n:
                node_ok[wid] = 1
                kept += 1
        print(f"✂️ Лемм с DF >= {MIN_LEMMA_HITS}: {kept:,}")
    else:
        node_ok = bytearray(b"\x01") * n
        print("🔓 Фильтр по словарю выключен (все леммы)")


# ============================================================
# REPORT
# ============================================================
def report():
    print("\n" + "=" * 70)
    print("📐 REPORT: подбор размера словаря по готовому ID-файлу")
    print("=" * 70)
    if not ID_FILE.exists():
        print("❌ ID-файл не найден. Сначала выполни этап 1 (scan).")
        return
    if (cursor.execute("SELECT MAX(hits) FROM words").fetchone()[0] or 0) == 0:
        print("❌ В words все hits=0. Сначала выполни этап 1 (scan).")
        return

    t0 = time.perf_counter()
    ids_desc = array('i')
    hits_desc = array('i')
    for wid, h in cursor.execute("SELECT id, hits FROM words WHERE hits > 0 ORDER BY hits DESC"):
        ids_desc.append(wid)
        hits_desc.append(h)
    n_lem = len(ids_desc)
    if not n_lem:
        print("❌ Нет лемм с DF > 0")
        return
    print(f"Лемм с DF > 0: {n_lem:,} (загрузка {fmt(time.perf_counter() - t0)})")

    max_id = cursor.execute("SELECT MAX(id) FROM words").fetchone()[0] or 0
    rank = array('i', [BIG_RANK]) * (max_id + 1)
    for i, wid in enumerate(ids_desc):
        rank[wid] = i

    hits_asc = array('i', reversed(hits_desc))
    prefix = array('q', [0]) * (n_lem + 1)
    s = 0
    for i, h in enumerate(hits_asc):
        s += h
        prefix[i + 1] = s
    total_df = s

    print("-" * 70)
    print("Распределение DF лемм (в скольких запросах встретилась):")
    for thr in (1, 2, 3, 5, 10, 50, 100, 1000, 10000):
        idx = bisect.bisect_left(hits_asc, thr)
        cnt = n_lem - idx
        cov = (total_df - prefix[idx]) / max(1, total_df)
        print(f"   DF >= {thr:>6}: {cnt:>10,} лемм, покрывают {cov * 100:5.1f}% вхождений")

    ns = sorted(set(list(REPORT_TOPN) + [n_lem]))
    ns = [x for x in ns if x <= n_lem]
    if not ns or ns[-1] != n_lem:
        ns.append(n_lem)

    file_size = ID_FILE.stat().st_size
    step = max(1, file_size // REPORT_CHUNKS)
    packers = _PACKERS
    unpack_from = struct.unpack_from
    pair_max = {}
    total_by_bucket = [0] * (len(ns) + 1)
    half_pairs = set()
    seen_records = 0
    bytes_read = 0
    br = bisect.bisect_right

    print("-" * 70)
    print(f"Читаю выборку ID-файла: {REPORT_CHUNKS} × {human(REPORT_CHUNK_SIZE)}...")
    with ID_FILE.open("rb") as f:
        header = f.read(8)
        if len(header) < 8:
            print("❌ Нет заголовка")
            return
        magic, version = ID_STRUCT_HEADER.unpack(header)
        if magic != ID_FILE_MAGIC or version != ID_FILE_VERSION:
            print(f"❌ Формат ID-файла: {magic} v{version}")
            return

        for ci in range(REPORT_CHUNKS):
            start = 8 + ci * step
            if start >= file_size:
                break
            f.seek(start)
            if ci:
                while True:
                    b = f.read(2)
                    if len(b) < 2:
                        break
                    nw = b[0] | (b[1] << 8)
                    if nw > 64:
                        continue
                    if f.read((nw + 1) * 4):
                        break
            data = f.read(REPORT_CHUNK_SIZE)
            if not data:
                continue
            bytes_read += len(data)
            n = len(data)
            pos = 0
            while True:
                if pos + 2 > n:
                    break
                nw = data[pos] | (data[pos + 1] << 8)
                end = pos + 2 + (nw + 1) * 4
                if end > n or nw < 2 or nw > 64:
                    break
                k = nw + 1
                vals = packers[k].unpack_from(data, pos + 2)
                ids = vals[:-1]
                even = (seen_records & 1) == 0
                for a, b in combinations(ids, 2):
                    if a > b:
                        a, b = b, a
                    if a >= len(rank) or b >= len(rank):
                        continue
                    ra = rank[a]
                    rb = rank[b]
                    m = ra if ra > rb else rb
                    total_by_bucket[br(ns, m)] += 1
                    key = (a << 32) | b
                    prev = pair_max.get(key)
                    if prev is None:
                        pair_max[key] = (m << CNT_BITS) | 1
                    else:
                        pm = prev >> CNT_BITS
                        if m < pm:
                            pm = m
                        c = (prev & CNT_MASK) + 1
                        if c > CNT_MASK:
                            c = CNT_MASK
                        pair_max[key] = (pm << CNT_BITS) | c
                    if even and len(half_pairs) < HALF_PAIRS_CAP:
                        half_pairs.add(key)
                seen_records += 1
                pos = end

    if not seen_records:
        print("❌ Не удалось прочитать записи")
        return

    total_pairs = sum(total_by_bucket)
    u_full = len(pair_max)
    u_half = len(half_pairs)
    beta = 1.0
    if u_half > 100 and u_full > u_half:
        beta = min(1.0, max(0.5, math.log(u_full / u_half) / math.log(2)))

    uniq_by_bucket = [0] * (len(ns) + 1)
    cnt_hist = Counter()
    for v in pair_max.values():
        uniq_by_bucket[br(ns, v >> CNT_BITS)] += 1
        c = v & CNT_MASK
        cnt_hist["1" if c == 1 else "2" if c == 2 else "3-4" if c <= 4 else "5+"] += 1

    frac = bytes_read / file_size
    scale = 1.0 / max(frac, 1e-12)
    total_records_est = seen_records * scale
    rc = row_cost()

    print(f"   Записей в выборке: {seen_records:,} ({frac * 100:.2f}% ID-файла)")
    print(f"   Пар в выборке: {total_pairs:,} | уникальных: {u_full:,} | β={beta:.3f}")
    print(f"   Всего записей в ID-файле (оценка): {big(total_records_est)}")
    print(f"   Формат links: {'WITHOUT ROWID' if rc == 32 else 'rowid'} (~{rc} Б/строка)")
    print("-" * 70)
    print("ПРОГНОЗ РАЗМЕРА ГРАФА ПРИ РАЗНЫХ СЛОВАРЯХ (без отсечения по DF пары):")
    print(f"{'топ-N лемм':>14} {'пар %':>7} {'уник %':>7} {'рёбер (оценка)':>16} {'размер':>12}")
    cum_total = 0
    cum_uniq = 0
    totals_cum = []
    uniqs_cum = []
    for i in range(len(ns) + 1):
        cum_total += total_by_bucket[i]
        cum_uniq += uniq_by_bucket[i]
        totals_cum.append(cum_total)
        uniqs_cum.append(cum_uniq)
    for i, N in enumerate(ns):
        tp = totals_cum[i]
        up = uniqs_cum[i]
        proj = up * (scale ** beta)
        label = f"{N:,}" if N != n_lem else f"ВСЕ ({N:,})"
        print(f"{label:>14} {tp / max(1, total_pairs) * 100:6.1f}% "
              f"{up / max(1, u_full) * 100:6.1f}% {big(proj):>16} {human(proj * rc):>12}")

    print("-" * 70)
    print("Повторяемость пар ВНУТРИ выборки (не корпусный DF!):")
    for k in ("1", "2", "3-4", "5+"):
        print(f"   встретилась {k:>3} раз: {cnt_hist[k]:,} уникальных пар")
    rep = 1.0 - u_full / max(1, total_pairs)
    print(f"   Доля повторов: {rep * 100:.1f}%")
    print(f"   ⚠️ Выборка = {frac * 100:.2f}% корпуса, поэтому DF пары по ней НЕ extrapolируется.")
    print("   Реальный порог подбирай так: links без prune (--no-prune) → --df-hist → --prune-only.")
    print("-" * 70)
    print(f"💽 Свободно на диске: {human(free_space())}")
    print("\nЗапуск с выбранным словарём:")
    print("   python scripts/graph_builder.py --links-only --links-restart "
          "--top-lemmas 200000 --no-prune")
    print("=" * 70)


# ============================================================
# Неизвестные слова
# ============================================================
_wmorph = None


def _init_worker():
    global _wmorph
    import pymorphy3
    _wmorph = pymorphy3.MorphAnalyzer()


def _lemm_chunk(words):
    global _wmorph
    if _wmorph is None:
        _init_worker()
    out = []
    for w in words:
        try:
            p = _wmorph.parse(w)[0]
            tag = p.tag.POS
            out.append((w, p.normal_form, POS_RU.get(tag, "неизвестно") if tag else "неизвестно"))
        except Exception:
            out.append((w, w, "неизвестно"))
    return out


def dump_unknown():
    if not unknown_words:
        return
    top = unknown_words.most_common(UNKNOWN_DUMP_LIMIT)
    with UNKNOWN_FILE.open("w", encoding="utf-8") as f:
        for w, c in top:
            f.write(f"{w}\t{c}\n")
    print(f"📝 Неизвестные слова выгружены: {len(top):,} → {UNKNOWN_FILE}")


def lemmatize_unknown():
    global next_word_id
    if not unknown_words:
        return
    words = [w for w, _ in unknown_words.most_common(UNKNOWN_DUMP_LIMIT)]
    print(f"\n🔤 Лемматизация {len(words):,} неизвестных слов ({NUM_WORKERS} процессов)...")
    t = time.perf_counter()
    from concurrent.futures import ProcessPoolExecutor
    chunks = [words[i:i + 5000] for i in range(0, len(words), 5000)]
    results = []
    with ProcessPoolExecutor(max_workers=NUM_WORKERS, initializer=_init_worker) as ex:
        for part in ex.map(_lemm_chunk, chunks):
            results.extend(part)
    lemma_rows, word_rows = [], []
    for raw, lemma, pos in results:
        lemma_rows.append((raw, lemma, pos))
        if (lemma not in allowed_lemmas and pos in ALLOWED_POS
                and len(lemma) >= MIN_LEMMA_LEN
                and (not CYRILLIC_ONLY or _CYR.match(lemma))):
            wid = next_word_id
            next_word_id += 1
            allowed_lemmas[lemma] = wid
            word_rows.append((wid, lemma, pos))
    cursor.executemany("INSERT OR IGNORE INTO lemmas(word, lemma, pos) VALUES(?,?,?)", lemma_rows)
    if word_rows:
        cursor.executemany("INSERT OR IGNORE INTO words(id, word, pos, count, hits) VALUES(?,?,?,0,0)",
                           word_rows)
    conn.commit()
    print(f"✅ За {fmt(time.perf_counter() - t)}: форм {len(lemma_rows):,}, "
          f"новых полезных лемм {len(word_rows):,}")


# ============================================================
# Загрузка словаря
# ============================================================
def load_minus():
    global minus_words
    minus_words = set()
    from_file = 0
    if MINUS_FILE.exists():
        with MINUS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                w = line.strip().lower()
                if w:
                    minus_words.add(w)
        from_file = len(minus_words)
    added = 0
    try:
        ph = ",".join("?" * len(STOP_POS))
        for (w,) in cursor.execute(f"SELECT word FROM words WHERE pos IN ({ph})", STOP_POS):
            if w and w not in minus_words:
                minus_words.add(w)
                added += 1
    except Exception as e:
        print(f"⚠️ Не удалось прочитать служебные POS: {e}")
    print(f"🚫 Минус-слов: {from_file:,} из файла + {added:,} служебных из БД = {len(minus_words):,}")


def load_dictionaries(show_pos=True):
    global next_word_id, max_word_id
    t = time.perf_counter()
    max_word_id = cursor.execute("SELECT MAX(id) FROM words").fetchone()[0] or 0
    next_word_id = max_word_id + 1

    if show_pos:
        print("📊 Распределение POS в таблице words:")
        try:
            for pos, cnt in cursor.execute(
                    "SELECT pos, COUNT(*) c FROM words GROUP BY pos ORDER BY c DESC LIMIT 20"):
                if pos in ALLOWED_POS:
                    mark = "✅"
                elif pos in STOP_POS:
                    mark = "🚫"
                else:
                    mark = "➖"
                print(f"   {mark} {str(pos):<24} {cnt:>12,}")
        except Exception as e:
            print(f"   (не удалось: {e})")

    ph = ",".join("?" * len(ALLOWED_POS))
    kept = skipped = blocked_lemmas = 0
    for wid, lemma in cursor.execute(f"SELECT id, word FROM words WHERE pos IN ({ph})", ALLOWED_POS):
        if lemma in minus_words:
            blocked_lemmas += 1
            continue
        if len(lemma) < MIN_LEMMA_LEN:
            skipped += 1
            continue
        if CYRILLIC_ONLY and not _CYR.match(lemma):
            skipped += 1
            continue
        allowed_lemmas[lemma] = wid
        raw_to_id[lemma] = wid
        kept += 1
    stats["dict_lemmas"] = kept
    print(f"📖 Разрешённых лемм: {kept:,} (коротких/не-кириллица {skipped:,}, "
          f"чёрный список {blocked_lemmas:,}) за {fmt(time.perf_counter() - t)}")

    t = time.perf_counter()
    forms = zeros = blocked_forms = 0
    read_cur = conn.cursor()
    for raw, lemma in read_cur.execute("SELECT word, lemma FROM lemmas"):
        if lemma in minus_words:
            raw_to_id[raw] = 0
            blocked_forms += 1
            continue
        wid = allowed_lemmas.get(lemma)
        if wid is not None:
            raw_to_id[raw] = wid
            forms += 1
        elif MARK_FILTERED_FORMS and raw not in raw_to_id:
            raw_to_id[raw] = 0
            zeros += 1
    read_cur.close()
    stats["dict_forms"] = forms
    stats["dict_zero"] = zeros
    print(f"📖 Словоформ к разрешённым леммам: {forms:,} за {fmt(time.perf_counter() - t)}")
    print(f"   Заблокировано форм по чёрному списку: {blocked_forms:,}")
    if zeros:
        print(f"   Помечено как отфильтрованные: {zeros:,}")
    for w in minus_words:
        raw_to_id[w] = 0
    print(f"⚡ Словарь в памяти: {len(raw_to_id):,} ключей{rss()}")


# ============================================================
# ЭТАП 1: скан
# ============================================================
def stage_scan(resume_offset=0, resume_id_offset=0, resume_records=0, resume_lines=0):
    global interrupted
    print("\n" + "=" * 70)
    print(f"🔢 ЭТАП 1: скан (вес>={MIN_QUERY_FREQ}, запрос=колонка {QUERY_COL + 1})")
    print("=" * 70)
    stats["scan_start"] = time.perf_counter()
    total_size = INPUT_FILE.stat().st_size
    print(f"📄 Файл: {human(total_size)}")

    if resume_offset > 0 and ID_FILE.exists() and resume_id_offset >= 8:
        meta = load_stats_meta()
        if meta and meta.get("byte_offset", 0) <= resume_offset:
            if meta.get("byte_offset") != resume_offset:
                print(f"♻️ Откат к согласованному чекпоинту статистики: "
                      f"{meta['byte_offset']:,} (прогресс был {resume_offset:,})")
            resume_offset = meta.get("byte_offset", resume_offset)
            resume_id_offset = meta.get("id_file_offset", resume_id_offset)
            resume_records = meta.get("records", resume_records)
            resume_lines = meta.get("lines", resume_lines)
        else:
            print("⚠️ stats_meta.json нет (прогресс v9): возможен небольшой двойной учёт")
            print("   веса/DF на хвосте до 120 сек работы. На связи это не влияет.")
        with ID_FILE.open("r+b") as t:
            t.truncate(resume_id_offset)
        id_f = ID_FILE.open("ab")
        print(f"▶️ Продолжение: байт {resume_offset:,}, ID-файл {resume_id_offset:,}")
    else:
        id_f = ID_FILE.open("wb")
        id_f.write(ID_STRUCT_HEADER.pack(ID_FILE_MAGIC, ID_FILE_VERSION))
        resume_offset = 0
        resume_records = 0
        resume_lines = 0
        for p in (STATS_META_FILE,):
            if p.exists():
                p.unlink()
        print("🆕 Новый ID-файл")

    process = make_processor(MIN_QUERY_FREQ, MAX_WORDS_PER_QUERY)
    buffer = bytearray()
    extend = buffer.extend
    packers = _PACKERS
    count_pack = ID_STRUCT_COUNT.pack
    weights = word_weight
    hits = word_hits

    records = resume_records
    offset = resume_offset
    lines = resume_lines
    kept = 0
    tail = b""
    last_status = 0.0
    last_ckpt = time.perf_counter()
    start_time = time.perf_counter()

    def write_buffer():
        if buffer:
            id_f.write(buffer)
            buffer.clear()

    def checkpoint():
        nonlocal last_ckpt
        process.sync()
        write_buffer()
        id_f.flush()
        idpos = id_f.tell()
        # сначала массивы + мета, потом прогресс: при сбое откатываемся к мете
        save_stats_arrays({"byte_offset": offset, "id_file_offset": idpos,
                           "records": records, "lines": lines})
        progress["stage"] = "scan"
        progress["byte_offset"] = offset
        progress["id_file_offset"] = idpos
        progress["records_written"] = records
        progress["lines"] = lines
        save_progress(update_config=True)
        last_ckpt = time.perf_counter()

    try:
        with INPUT_FILE.open("rb") as f:
            f.seek(offset)
            while not interrupted:
                chunk = f.read(READ_CHUNK)
                if not chunk:
                    break
                data = tail + chunk if tail else chunk
                cut = data.rfind(b"\n")
                if cut < 0:
                    tail = data
                    continue
                block = data[:cut + 1]
                tail = data[cut + 1:]

                for line in block.decode("utf-8", errors="ignore").split("\n"):
                    if not line:
                        continue
                    lines += 1
                    r = process(line)
                    if r is None:
                        continue
                    ids, freq = r
                    kept += 1
                    for wid in ids:
                        weights[wid] += freq
                        hits[wid] += 1
                    nw = len(ids)
                    extend(count_pack(nw))
                    k = nw + 1
                    if k < 65:
                        extend(packers[k].pack(*ids, freq))
                    else:
                        extend(struct.pack(f"<{k}I", *ids, freq))
                    records += 1

                offset += len(block)
                if len(buffer) >= ID_FILE_BUFFER_SIZE:
                    write_buffer()

                now = time.perf_counter()
                if now - last_ckpt >= CHECKPOINT_SEC:
                    checkpoint()
                if PROGRESS_EVERY > 0 and now - last_status >= PROGRESS_EVERY:
                    last_status = now
                    run = now - start_time
                    done_lines = lines - resume_lines
                    speed = done_lines / run if run > 0 else 0
                    eta = (total_size - offset) / (offset - resume_offset) * run \
                        if offset > resume_offset else 0
                    status_line(f"[{fmt(run)}] {offset / total_size * 100:.1f}% | "
                                f"{short(speed)} стр/с | ост {fmt(eta)[:5]} | "
                                f"стр {short(done_lines)} | зап {short(records)}{rss_s()}")

        checkpoint()
        import_stats_arrays()
    finally:
        status_end()
        id_f.close()
        process.sync()
        stats["lines"] = lines
        stats["lines_kept"] = kept
        stats["records"] = records
        stats["scan_end"] = time.perf_counter()

    if interrupted:
        print(f"\n⏸️ Прервано на байте {offset:,}. Прогресс сохранён.")
        return False

    print(f"\n✅ Этап 1 за {fmt(stats['scan_end'] - stats['scan_start'])}")
    print(f"   Строк: {lines:,} | прошло: {kept:,} ({kept / max(1, lines) * 100:.1f}%)")
    print(f"   Записей ID: {records:,} | размер: {human(ID_FILE.stat().st_size)}")
    print(f"   Токенов: взято {stats['tokens_kept']:,}, "
          f"неизвестно {stats['tokens_unknown']:,}, отфильтровано {stats['tokens_filtered']:,}")

    progress["stage"] = "links"
    progress["id_file_offset"] = 8
    progress["links_records"] = 0
    save_progress(update_config=True)
    return True


# ============================================================
# ЭТАП 2: связи
# ============================================================
def flush_links():
    if not links_buffer:
        return
    t = time.perf_counter()
    n = len(links_buffer)
    buf = links_buffer

    def gen():
        for k, v in buf.items():
            yield (k >> 32, k & 0xFFFFFFFF, v >> HIT_BITS, v & HIT_MASK)

    cursor.executemany(SQL_UPSERT_LINKS, gen())
    conn.commit()
    buf.clear()
    stats["links_saved"] += n
    stats["commits"] += 1
    print(f"\n💾 Связей сброшено: {n:,} за {time.perf_counter() - t:.1f} сек{rss()}")


def stage_links(resume_offset=8, resume_records=0):
    global interrupted
    print("\n" + "=" * 70)
    print("🔗 ЭТАП 2: построение связей")
    print("=" * 70)
    stats["links_start"] = time.perf_counter()
    if not ID_FILE.exists():
        print("❌ ID-файл не найден")
        return False

    load_node_filter()
    file_size = ID_FILE.stat().st_size
    print(f"📄 ID-файл: {human(file_size)}")
    if not check_disk(max(2 * 1024 ** 3, file_size * 0.6), "таблица links"):
        return False

    buf = links_buffer
    ok = node_ok
    n_ok = len(ok)
    packers = _PACKERS
    unpack_from = struct.unpack_from

    records = resume_records
    pairs_total = pairs_kept = 0
    base = 8
    tail = b""
    last_status = 0.0
    t_start = time.perf_counter()
    last_ckpt = t_start

    with ID_FILE.open("rb") as f:
        header = f.read(8)
        if len(header) < 8:
            print("❌ Нет заголовка")
            return False
        magic, version = ID_STRUCT_HEADER.unpack(header)
        if magic != ID_FILE_MAGIC or version != ID_FILE_VERSION:
            print(f"❌ Формат: magic={magic}, version={version}")
            return False
        if 8 <= resume_offset <= file_size:
            base = resume_offset
            if base > 8:
                print(f"▶️ Продолжение с байта {base:,}, записей {records:,}")
        f.seek(base)

        while not interrupted:
            chunk = f.read(READ_CHUNK)
            if not chunk:
                break
            data = tail + chunk if tail else chunk
            n = len(data)
            pos = 0
            since = 0

            while True:
                if pos + 2 > n:
                    break
                nw = data[pos] | (data[pos + 1] << 8)
                end = pos + 2 + (nw + 1) * 4
                if end > n or nw < 2 or nw > 64:
                    break
                k = nw + 1
                if k < 65:
                    vals = packers[k].unpack_from(data, pos + 2)
                else:
                    vals = unpack_from(f"<{k}I", data, pos + 2)
                freq = vals[-1]
                inc = (freq << HIT_BITS) + 1

                if nw == 2:
                    a = vals[0]
                    b = vals[1]
                    pairs_total += 1
                    if a < n_ok and b < n_ok and ok[a] and ok[b] and a != b:
                        if a > b:
                            a, b = b, a
                        buf[(a << 32) | b] += inc
                        pairs_kept += 1
                else:
                    ids = [x for x in vals[:-1] if x < n_ok and ok[x]]
                    m = len(ids)
                    pairs_total += nw * (nw - 1) // 2
                    if m >= 2:
                        for a, b in combinations(ids, 2):
                            buf[(a << 32) | b] += inc
                        pairs_kept += m * (m - 1) // 2

                records += 1
                pos = end
                since += 1

                if since >= 50_000:
                    since = 0
                    now = time.perf_counter()
                    if buf and (len(buf) >= LINKS_BUFFER_LIMIT or now - last_ckpt >= CHECKPOINT_SEC):
                        flush_links()
                        progress["stage"] = "links"
                        progress["id_file_offset"] = base + pos
                        progress["links_records"] = records
                        save_progress()
                        last_ckpt = time.perf_counter()
                    now = time.perf_counter()
                    if now - last_status >= STATUS_UPDATE_EVERY_SEC:
                        last_status = now
                        run = now - t_start
                        done = base + pos
                        speed = (records - resume_records) / run if run > 0 else 0
                        eta = (file_size - done) / ((done - 8) / run) if done > 8 and run > 0 else 0
                        print(f"\r[{fmt(run)}] записей {records:,} ({done / file_size * 100:.1f}%) | "
                              f"{speed:,.0f} зап/сек | буфер {len(buf):,} | "
                              f"ETA: {fmt(eta)}{rss()}   ", end="", flush=True)
                    if interrupted:
                        break

            base += pos
            tail = data[pos:]

    flush_links()
    progress["stage"] = "links"
    progress["id_file_offset"] = base
    progress["links_records"] = records
    save_progress()
    stats["pairs_total"] = pairs_total
    stats["pairs_kept"] = pairs_kept
    stats["links_end"] = time.perf_counter()

    if interrupted:
        print(f"\n⏸️ Прервано на записи {records:,}. Прогресс сохранён.")
        return False

    print(f"\n✅ Этап 2 за {fmt(stats['links_end'] - stats['links_start'])}")
    print(f"   Записей: {records:,} | пар всего: {pairs_total:,} | "
          f"после фильтра словаря: {pairs_kept:,}")
    print(f"   Размер базы: {human(DB_FILE.stat().st_size)}")
    return True


# ============================================================
# Гистограмма DF пар / ЭТАП 3
# ============================================================
def df_histogram():
    print("\n" + "=" * 70)
    print("📊 Распределение DF пары (links.hits) — один полный проход по таблице")
    print("=" * 70)
    has = cursor.execute("SELECT 1 FROM links LIMIT 1").fetchone()
    if has is None:
        print("❌ Таблица links пуста")
        return None
    thr = (1, 2, 3, 5, 10, 20, 50, 100, 1000)
    cols = ", ".join(f"SUM(CASE WHEN hits >= {t} THEN 1 ELSE 0 END)" for t in thr)
    t0 = time.perf_counter()
    print("   ⏳читаю... (на большой таблице это минуты)", flush=True)
    row = cursor.execute(f"SELECT COUNT(*), {cols} FROM links").fetchone()
    total = row[0] or 0
    rc = row_cost()
    print(f"   Всего рёбер: {total:,} (за {fmt(time.perf_counter() - t0)})")
    print(f"{'DF пары >=':>12} {'рёбер':>16} {'доля':>8} {'размер':>12}")
    for i, t in enumerate(thr):
        c = row[i + 1] or 0
        print(f"{t:>12} {c:>16,} {c / max(1, total) * 100:7.1f}% {human(c * rc):>12}")
    print("\n   ➜ Выбери порог и запусти: --prune-only --min-pair-hits N")
    print("   ⚠️ Prune необратим: понижение порога потребует пересчёта связей (--links-restart).")
    return total


def stage_prune():
    print("\n" + "=" * 70)
    print(f"✂️ ЭТАП 3: отсечение (DF пары >= {MIN_PAIR_HITS}, вес >= {MIN_PAIR_COUNT})")
    print("=" * 70)
    df_histogram()
    t = time.perf_counter()
    before = cursor.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    print(f"\n   Строк до: {before:,}")
    cursor.execute("DELETE FROM links WHERE hits < ? OR count < ?",
                   (MIN_PAIR_HITS, MIN_PAIR_COUNT))
    conn.commit()
    after = cursor.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    print(f"   Строк после: {after:,} (убрано {before - after:,}) за {fmt(time.perf_counter() - t)}")

    if TOP_EDGES_PER_NODE > 0:
        print(f"   Топ-{TOP_EDGES_PER_NODE} рёбер на узел (по word1_id)...")
        t = time.perf_counter()
        cursor.execute("DROP TABLE IF EXISTS temp._prune_keys")
        cursor.execute("""CREATE TEMP TABLE _prune_keys(
            w1 INTEGER NOT NULL, w2 INTEGER NOT NULL, PRIMARY KEY(w1, w2)) WITHOUT ROWID""")
        cursor.execute("""INSERT INTO _prune_keys(w1, w2)
            SELECT word1_id, word2_id FROM (
                SELECT word1_id, word2_id,
                       ROW_NUMBER() OVER (PARTITION BY word1_id ORDER BY hits DESC, count DESC) rn
                FROM links) WHERE rn > ?""", (TOP_EDGES_PER_NODE,))
        cursor.execute("""DELETE FROM links WHERE EXISTS(
            SELECT 1 FROM _prune_keys k WHERE k.w1 = links.word1_id AND k.w2 = links.word2_id)""")
        cursor.execute("DROP TABLE IF EXISTS temp._prune_keys")
        conn.commit()
        after = cursor.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        print(f"   Осталось: {after:,} за {fmt(time.perf_counter() - t)}")

    nodes = cursor.execute(
        "SELECT COUNT(*) FROM (SELECT word1_id FROM links UNION SELECT word2_id FROM links)"
    ).fetchone()[0]
    stats["links_final"] = after
    print(f"   🎯 ФИНАЛЬНЫЙ ГРАФ: узлов {nodes:,}, рёбер {after:,}")
    print(f"   Размер базы: {human(DB_FILE.stat().st_size)}")
    cursor.execute("ANALYZE")
    conn.commit()
    if VACUUM_AFTER_PRUNE:
        print("   VACUUM...")
        conn.execute("VACUUM")
        print(f"   Размер после VACUUM: {human(DB_FILE.stat().st_size)}")


# ============================================================
# Статус / итог
# ============================================================
def print_status():
    print("\n" + "=" * 70)
    print("📋 СТАТУС")
    print("=" * 70)
    p = load_progress()
    if p:
        print(f"   Прогресс: стадия={p.get('stage')} | версия={p.get('version')} "
              f"| {p.get('timestamp')}")
        print(f"   scan: байт {p.get('byte_offset', 0):,} | записей {p.get('records_written', 0):,} "
              f"| строк {p.get('lines', 0):,}")
        print(f"   links: байт ID {p.get('id_file_offset', 0):,} | записей {p.get('links_records', 0):,}")
        print(f"   словарь: {p.get('links_config')}")
    else:
        print("   Прогресса нет (progress.json отсутствует)")
    for f in (INPUT_FILE, ID_FILE, WEIGHT_FILE, HITS_FILE, DB_FILE):
        print(f"   {f.name:<20} {human(f.stat().st_size) if f.exists() else '—'}")
    print(f"   💽 Свободно: {human(free_space())}")
    try:
        total = cursor.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        withdf = cursor.execute("SELECT COUNT(*) FROM words WHERE hits > 0").fetchone()[0]
        print(f"   words: {total:,} всего, {withdf:,} с DF > 0")
        print(f"   links: {'WITHOUT ROWID' if links_without_rowid() else 'rowid'}, "
              f"{'пуста' if cursor.execute('SELECT 1 FROM links LIMIT 1').fetchone() is None else 'есть данные'}")
    except Exception as e:
        print(f"   (БД: {e})")
    print("=" * 70)


def print_stats():
    total = time.perf_counter() - stats["start"]
    print("\n" + "=" * 70)
    print("⏸️ ПРЕРВАНО" if interrupted else "✅ ЗАВЕРШЕНО")
    print("-" * 70)
    print(f"Всего времени:        {fmt(total)}")
    print(f"  Инициализация:      {fmt(stats['init_end'] - stats['start'])}")
    if stats["scan_end"]:
        print(f"  Этап 1 (скан):      {fmt(stats['scan_end'] - stats['scan_start'])}")
    if stats["links_end"]:
        print(f"  Этап 2 (связи):     {fmt(stats['links_end'] - stats['links_start'])}")
    print("-" * 70)
    print(f"Формат: разделитель={LINE_DELIM!r}, колонок={N_COLS}, "
          f"запрос=кол.{QUERY_COL + 1}, вес=кол.{N_COLS if FREQ_COL == -1 else FREQ_COL + 1}")
    if stats["dict_lemmas"]:
        print(f"Словарь: лемм {stats['dict_lemmas']:,}, форм {stats['dict_forms']:,}")
    print(f"Строк прочитано:      {stats['lines']:,}")
    print(f"Прошло фильтры:       {stats['lines_kept']:,}")
    print(f"Записей ID:           {stats['records']:,}")
    print(f"Токенов взято:        {stats['tokens_kept']:,}")
    print(f"Токенов неизвестно:   {stats['tokens_unknown']:,}")
    print(f"Токенов отфильтр.:    {stats['tokens_filtered']:,}")
    print(f"Пар всего:            {stats['pairs_total']:,}")
    print(f"Пар после словаря:    {stats['pairs_kept']:,}")
    print(f"Рёбер в финале:       {stats['links_final']:,}")
    print(f"Коммитов:             {stats['commits']:,}")
    if stats["scan_end"] and stats["lines"]:
        sec = stats["scan_end"] - stats["scan_start"]
        if sec > 0:
            print(f"Скорость скана:       {stats['lines'] / sec:,.0f} строк/сек")
    print("=" * 70)


def signal_handler(sig, frame):
    global interrupted
    if interrupted:
        print("\n⚠️ Повторное прерывание. Принудительный выход...")
        sys.exit(1)
    interrupted = True
    print("\n⏸️ Прерывание: дохожу до конца блока и сохраняю чекпоинт...")


# ============================================================
# main
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(description="Граф связей v10")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report", action="store_true", help="подбор словаря по готовому ID-файлу")
    p.add_argument("--status", action="store_true", help="быстрый статус, без загрузки словаря")
    p.add_argument("--df-hist", action="store_true", help="распределение DF пары в links")
    p.add_argument("--restart", action="store_true", help="ПОЛНЫЙ пересчёт с нуля (удалит ID-файл!)")
    p.add_argument("--links-only", action="store_true")
    p.add_argument("--links-restart", action="store_true", help="связи с начала + очистка links")
    p.add_argument("--scan-only", action="store_true", help="остановиться после этапа 1")
    p.add_argument("--prune-only", action="store_true")
    p.add_argument("--no-prune", action="store_true", help="не резать links после этапа 2")
    p.add_argument("--all-lemmas", action="store_true", help="разрешить граф по ВСЕМ леммам")
    p.add_argument("--min-freq", type=int, default=MIN_QUERY_FREQ)
    p.add_argument("--min-pair-hits", type=int, default=MIN_PAIR_HITS)
    p.add_argument("--min-pair", type=int, default=MIN_PAIR_COUNT)
    p.add_argument("--top-lemmas", type=int, default=TOP_LEMMAS)
    p.add_argument("--min-lemma-hits", type=int, default=MIN_LEMMA_HITS)
    p.add_argument("--max-words", type=int, default=MAX_WORDS_PER_QUERY)
    p.add_argument("--top-edges", type=int, default=TOP_EDGES_PER_NODE)
    p.add_argument("--query-col", type=int, default=1, help="номер колонки запроса (1-based)")
    p.add_argument("--freq-col", default="last", help='номер колонки веса (1-based) или "last"')
    p.add_argument("--delim", default=None, help='";", "tab", "|"')
    p.add_argument("--all-pos", action="store_true")
    p.add_argument("--allow-latin", action="store_true")
    p.add_argument("--lean-dict", action="store_true")
    p.add_argument("--lemmatize-new", action="store_true")
    p.add_argument("--workers", type=int, default=NUM_WORKERS)
    p.add_argument("--vacuum", action="store_true")
    p.add_argument("--cache-gb", type=float, default=CACHE_GB, help="кэш SQLite в ГБ")
    p.add_argument("--links-buffer", type=float, default=LINKS_BUFFER_LIMIT / 1e6,
                   help="буфер связей, млн пар")
    p.add_argument("--report-chunks", type=int, default=REPORT_CHUNKS)
    p.add_argument("--report-chunk-mb", type=float, default=REPORT_CHUNK_SIZE / 1024 / 1024)
    p.add_argument("--no-index", action="store_true", help="не создавать индекс words(hits)")
    p.add_argument("--progress-every", type=float, default=-1, help="секунды между строками прогресса; 0 = выключить")
    p.add_argument("--quiet", action="store_true", help="минимум вывода")
    return p.parse_args()


def main():
    global interrupted, MIN_QUERY_FREQ, MIN_PAIR_HITS, MIN_PAIR_COUNT, TOP_LEMMAS
    global MIN_LEMMA_HITS, MAX_WORDS_PER_QUERY, TOP_EDGES_PER_NODE, ALLOWED_POS
    global CYRILLIC_ONLY, LEMMATIZE_UNKNOWN, NUM_WORKERS, VACUUM_AFTER_PRUNE
    global MARK_FILTERED_FORMS, LINE_DELIM, QUERY_COL, FREQ_COL
    global CACHE_GB, LINKS_BUFFER_LIMIT, REPORT_CHUNKS, REPORT_CHUNK_SIZE, MAKE_HITS_INDEX
    global max_word_id, PROGRESS_EVERY, QUIET

    a = parse_args()
    MIN_QUERY_FREQ = a.min_freq
    MIN_PAIR_HITS = a.min_pair_hits
    MIN_PAIR_COUNT = a.min_pair
    TOP_LEMMAS = a.top_lemmas
    MIN_LEMMA_HITS = a.min_lemma_hits
    MAX_WORDS_PER_QUERY = a.max_words
    TOP_EDGES_PER_NODE = a.top_edges
    CYRILLIC_ONLY = not a.allow_latin
    LEMMATIZE_UNKNOWN = a.lemmatize_new
    MARK_FILTERED_FORMS = not a.lean_dict
    NUM_WORKERS = max(1, a.workers)
    VACUUM_AFTER_PRUNE = a.vacuum
    CACHE_GB = max(0.05, a.cache_gb)
    LINKS_BUFFER_LIMIT = max(200_000, int(a.links_buffer * 1_000_000))
    REPORT_CHUNKS = max(2, a.report_chunks)
    REPORT_CHUNK_SIZE = max(64 * 1024, int(a.report_chunk_mb * 1024 * 1024))
    MAKE_HITS_INDEX = not a.no_index
    PROGRESS_EVERY = a.progress_every
    QUIET = a.quiet
    init_console()
    QUERY_COL = max(0, a.query_col - 1)
    FREQ_COL = -1 if str(a.freq_col).lower() == "last" else max(0, int(a.freq_col) - 1)
    if a.all_pos:
        ALLOWED_POS = tuple(POS_RU.values()) + ("неизвестно",)

    signal.signal(signal.SIGINT, signal_handler)
    stats["start"] = time.perf_counter()

    print("=" * 70)
    print("🚀 Граф связей v10")
    print(f"   вес>={MIN_QUERY_FREQ} | DF леммы>={MIN_LEMMA_HITS} | топ-лемм={TOP_LEMMAS or 'ВСЕ'} | "
          f"DF пары>={MIN_PAIR_HITS} | слов/запрос<={MAX_WORDS_PER_QUERY}")
    if not QUIET:
        print(f"   POS: {len(ALLOWED_POS)} разрешённых | кириллица: {'да' if CYRILLIC_ONLY else 'нет'} | "
              f"буфер {LINKS_BUFFER_LIMIT:,} | кэш {CACHE_GB:.1f} ГБ")
    print(f"   Прогресс: каждые {PROGRESS_EVERY:.0f} сек, режим {'терминал' if IS_TTY else 'лог'}")
    print("=" * 70)

    light = a.report or a.status or a.df_hist or a.prune_only or a.links_only
    readonly = a.dry_run or a.report or a.status or a.df_hist

    if not light and not INPUT_FILE.exists():
        print(f"❌ Нет файла: {INPUT_FILE}")
        return
    if not light:
        detect_format(verbose=True)
        if a.delim is not None:
            LINE_DELIM = "\t" if a.delim.lower() in ("tab", "\\t", "t") else a.delim
            print(f"   ⚙️ Разделитель задан вручную: {LINE_DELIM!r}")

    connect_db(readonly=readonly)
    if not readonly:
        init_schema()

    if a.status:
        print_status()
        disconnect_db()
        return
    if a.df_hist:
        df_histogram()
        disconnect_db()
        return

    saved = load_progress()
    if saved and saved.get("config"):
        progress["config"] = saved["config"]

    stage = "scan"
    off = recs = lrecs = rlines = 0
    lidoff = 8

    if a.prune_only:
        stage = "prune"
    elif a.links_only:
        stage = "links"
        same_dict = bool(saved and saved.get("links_config") == links_config())
        if (saved and saved.get("stage") == "links" and same_dict
                and not a.links_restart):
            lidoff = saved.get("id_file_offset", 8) or 8
            lrecs = saved.get("links_records", 0)
        else:
            if saved and saved.get("stage") == "links" and not same_dict:
                print("⚠️ Словарь изменился с прошлого запуска связей — начинаю с начала.")
            lidoff = 8
            lrecs = 0
        if lidoff <= 8:
            print("🗑️ Связи с начала: очищаю links (защита от удвоения)")
            cursor.execute("DELETE FROM links")
            conn.commit()
            progress["id_file_offset"] = 8
            progress["links_records"] = 0
            progress["stage"] = "links"
            save_progress()
    elif a.restart:
        reset_facts()
        clear_progress()
    elif (saved and saved.get("version") in COMPAT_VERSIONS
          and saved.get("config") == scan_config()):
        stage = saved.get("stage", "scan")
        off = saved.get("byte_offset", 0)
        lidoff = saved.get("id_file_offset", 8) or 8
        recs = saved.get("records_written", 0)
        lrecs = saved.get("links_records", 0)
        rlines = saved.get("lines", 0)
        print(f"📌 Продолжение: стадия={stage}, байт {off:,} от {saved.get('timestamp')}")
        if stage == "links" and saved.get("links_config") not in (None, links_config()):
            print("⚠️ Словарь в прогрессе отличается от заданного — связи с начала.")
            lidoff, lrecs = 8, 0
            cursor.execute("DELETE FROM links")
            conn.commit()
    else:
        if saved:
            print("⚠️ Прогресс от другой версии/настроек — пересчёт фактов")
        elif cursor.execute("SELECT 1 FROM links LIMIT 1").fetchone() or ID_FILE.exists():
            print("⚠️ Прогресса нет, но данные есть — пересчёт фактов")
        reset_facts()
        clear_progress()

    need_dict = a.dry_run or stage == "scan"
    if need_dict:
        load_minus()
        load_dictionaries(show_pos=True)
    else:
        max_word_id = cursor.execute("SELECT MAX(id) FROM words").fetchone()[0] or 0
        print("⚡ Лёгкий режим: словарь словоформ не грузится (этап работает по ID)")

    stats["init_end"] = time.perf_counter()
    print(f"⏱️ Инициализация: {fmt(stats['init_end'] - stats['start'])}")

    if a.dry_run:
        dry_run()
        disconnect_db()
        return
    if a.report:
        report()
        disconnect_db()
        return

    if stage == "scan":
        init_stats_arrays(load_existing=(off > 0))
        if not check_disk(max(5 * 1024 ** 3, INPUT_FILE.stat().st_size * 0.25), "ID-файл"):
            disconnect_db()
            return

    try:
        if stage == "scan":
            if not stage_scan(off, lidoff, recs, rlines):
                return
            stage = "links"
            lidoff, lrecs = 8, 0
            dump_unknown()
            if LEMMATIZE_UNKNOWN:
                lemmatize_unknown()
            raw_to_id.clear()
            allowed_lemmas.clear()
            print("\n💡 Теперь подбери словарь: python scripts/graph_builder.py --report")
            if a.scan_only:
                print("⏹️ --scan-only: останавливаюсь после этапа 1.")
                return

        if stage == "links":
            if not (TOP_LEMMAS > 0 or MIN_LEMMA_HITS > 0 or a.all_lemmas):
                print("\n" + "⛔" * 35)
                print("⛔ Этап 2 БЕЗ ограничения словаря отключён в v10.")
                print("   По всем леммам это миллиарды пар, десятки часов и база на 50-100 ГБ.")
                print("   Порядок:")
                print("     1) python scripts/graph_builder.py --report")
                print("     2) python scripts/graph_builder.py --links-only --links-restart "
                      "--top-lemmas 200000 --no-prune")
                print("   Если действительно нужен полный граф — добавь --all-lemmas")
                print("⛔" * 35)
                progress["stage"] = "links"
                save_progress()
                return
            if not stage_links(lidoff, lrecs):
                return
            stage = "prune"
            progress["stage"] = "prune"
            save_progress()
            if a.no_prune:
                print("\n⏹️ --no-prune: связи построены, отсечение пропущено.")
                print("   Далее: --df-hist, затем --prune-only --min-pair-hits N")
                return

        if stage == "prune":
            stage_prune()

        progress["stage"] = "done"
        save_progress()
        clear_progress()
        if ID_FILE.exists():
            print(f"\n💾 ID-файл сохранён ({human(ID_FILE.stat().st_size)}): {ID_FILE}")
            print("   Меняешь словарь/пороги? --report и --links-only, исходник перечитывать не надо.")
    finally:
        print_stats()
        disconnect_db()


if __name__ == "__main__":
    main()