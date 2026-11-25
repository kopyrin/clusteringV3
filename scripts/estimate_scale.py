"""
scripts/estimate_scale.py — оценка масштаба графа по выборке из keywords.txt.
НИЧЕГО не изменяет: файл и база открываются только на чтение.
Запуск: python scripts/estimate_scale.py
"""
from pathlib import Path
import ctypes
import math
import shutil
import sqlite3
import time
from collections import Counter

SAMPLE_CHUNKS = 32                  # сколько кусков читать равномерно по файлу
CHUNK_SIZE = 4 * 1024 * 1024        # размер куска
MAX_RECORDS = 300_000               # сколько запросов анализировать на пары
TOP_K_LIST = (100_000, 300_000, 1_000_000)


def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
INPUT = ROOT / "data" / "input" / "keywords.txt"
DB = ROOT / "data" / "output" / "semantics.db"


def human(n):
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
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


def ram_info():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys, st.ullAvailPhys
    except Exception:
        return 0, 0


def sample_file(path, size):
    per_chunk = max(1, MAX_RECORDS // SAMPLE_CHUNKS)
    records = []
    lines = 0
    bytes_read = 0
    tokens_total = 0
    freq_hist = Counter()
    len_hist = Counter()
    step = max(1, size // SAMPLE_CHUNKS)
    t0 = time.perf_counter()

    with path.open("rb") as f:
        for i in range(SAMPLE_CHUNKS):
            start = i * step
            f.seek(start)
            if start:
                f.readline()                      # выбрасываем огрызок строки
            data = f.read(CHUNK_SIZE)
            if not data:
                continue
            bytes_read += len(data)
            text = data.decode("utf-8", errors="ignore")
            chunk_lines = text.split("\n")
            if not data.endswith(b"\n"):
                chunk_lines.pop()                 # хвост-огрызок
            kept = 0
            for line in chunk_lines:
                line = line.strip().lstrip("\ufeff")
                if not line:
                    continue
                lines += 1
                if "\t" in line:
                    line = line.replace("\t", ";")
                q, sep, fs = line.rpartition(";")
                if not sep:
                    q, fs = line, "1"
                try:
                    freq = int(fs.strip())
                except ValueError:
                    freq = 1
                if freq < 1:
                    freq = 1
                toks = q.split()
                n = len(toks)
                tokens_total += n
                len_hist[min(n, 15)] += 1
                if freq == 1:
                    freq_hist["1"] += 1
                elif freq <= 4:
                    freq_hist["2-4"] += 1
                elif freq <= 9:
                    freq_hist["5-9"] += 1
                elif freq <= 99:
                    freq_hist["10-99"] += 1
                else:
                    freq_hist["100+"] += 1
                if kept < per_chunk and n >= 2:
                    records.append((toks, freq))
                    kept += 1
    return records, lines, bytes_read, tokens_total, freq_hist, len_hist, time.perf_counter() - t0


def fetch_map(conn, table, val_col, keys):
    out = {}
    keys = list(keys)
    CH = 900
    for i in range(0, len(keys), CH):
        part = keys[i:i + CH]
        ph = ",".join("?" * len(part))
        sql = f"SELECT word, {val_col} FROM {table} WHERE word IN ({ph})"
        for w, v in conn.execute(sql, part):
            out[w] = v
    return out


def main():
    print("=" * 70)
    print("🔎 ОЦЕНКА МАСШТАБА (только чтение)")
    print("=" * 70)

    if not INPUT.exists():
        print(f"❌ Не найден файл: {INPUT}")
        return

    size = INPUT.stat().st_size
    total_ram, avail_ram = ram_info()
    usage = shutil.disk_usage(ROOT)
    print(f"📄 keywords.txt: {human(size)}")
    print(f"🧠 RAM: всего {human(total_ram)}, свободно {human(avail_ram)}")
    print(f"💾 Диск {ROOT.drive or '/'}: свободно {human(usage.free)} из {human(usage.total)}")

    print(f"\n⏳ Читаю {SAMPLE_CHUNKS} × {human(CHUNK_SIZE)} равномерно по файлу...")
    records, lines, bytes_read, tokens_total, freq_hist, len_hist, dt = sample_file(INPUT, size)
    if lines == 0:
        print("❌ Не удалось прочитать ни одной строки")
        return

    avg_line = bytes_read / lines
    est_lines = size / avg_line
    avg_tokens = tokens_total / lines
    print(f"   Прочитано {human(bytes_read)} за {dt:.1f} сек, строк в выборке: {lines:,}")

    print("\n" + "-" * 70)
    print("📏 СТРУКТУРА ФАЙЛА")
    print("-" * 70)
    print(f"Средняя длина строки:   {avg_line:.1f} байт")
    print(f"Всего строк (оценка):   {big(est_lines)}")
    print(f"Слов в запросе (сред.): {avg_tokens:.2f}")
    print("Распределение длины запроса:")
    for n in sorted(len_hist):
        label = f"{n} слов" if n < 15 else "15+ слов"
        print(f"   {label:>10}: {len_hist[n] / lines * 100:5.1f}%")
    print("Распределение частотности запроса:")
    for k in ("1", "2-4", "5-9", "10-99", "100+"):
        if freq_hist[k]:
            print(f"   freq {k:>6}: {freq_hist[k] / lines * 100:5.1f}%")

    # --- словарь из БД ---
    form2lemma = {}
    db_ok = False
    uniq_tokens = {t for toks, _ in records for t in toks}
    if DB.exists():
        try:
            uri = f"file:{DB.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            t0 = time.perf_counter()
            form2lemma = fetch_map(conn, "lemmas", "lemma", uniq_tokens)
            n_forms = conn.execute("SELECT COUNT(*) FROM lemmas").fetchone()[0]
            n_words = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
            conn.close()
            db_ok = True
            print("\n" + "-" * 70)
            print("📖 СЛОВАРЬ В БД")
            print("-" * 70)
            print(f"lemmas (словоформ): {n_forms:,}")
            print(f"words  (лемм):      {n_words:,}")
            print(f"Форм на лемму:      {n_forms / max(1, n_words):.2f}  "
                  f"(для русского нормально 3-5; ~1 = словарь забит не-словами)")
            print(f"Проверка {len(uniq_tokens):,} уникальных токенов за {time.perf_counter() - t0:.1f} сек")
        except Exception as e:
            print(f"\n⚠️ База недоступна ({e}).")
            print("   Останови graph_builder.py (Ctrl+C) и повтори — иначе оценка по леммам невозможна.")
    if not db_ok:
        print("\n⚠️ Работаю по сырым словоформам (без лемматизации) — оценки сверху.")

    # --- леммы и их частоты в выборке ---
    lemma_count = Counter()
    hit = miss = 0
    for toks, _ in records:
        for t in toks:
            lm = form2lemma.get(t)
            if lm is None:
                if db_ok:
                    miss += 1
                lm = t
            else:
                hit += 1
            lemma_count[lm] += 1
    if db_ok and hit + miss:
        print(f"Покрытие словарём:    {hit / (hit + miss) * 100:.2f}% "
              f"(не найдено {miss:,} токенов)")

    ranked = lemma_count.most_common()
    total_occ = sum(lemma_count.values())
    print(f"Уникальных лемм в выборке: {len(ranked):,}")
    cum = 0
    marks = {0.90: None, 0.95: None, 0.99: None}
    for i, (_, c) in enumerate(ranked, 1):
        cum += c
        for thr in marks:
            if marks[thr] is None and cum / total_occ >= thr:
                marks[thr] = i
    print("Сколько лемм покрывают вхождения:")
    for thr in (0.90, 0.95, 0.99):
        if marks[thr]:
            print(f"   {int(thr * 100)}%: топ-{marks[thr]:,} лемм")

    # --- индексация лемм ---
    lidx = {lm: i for i, (lm, _) in enumerate(ranked)}
    L = max(1, len(lidx))
    rec_ids = []
    for toks, freq in records:
        ids = sorted({lidx[form2lemma.get(t, t)] for t in toks})
        if len(ids) >= 2:
            rec_ids.append((ids, freq))

    def pairs_stat(recs, allowed=None):
        total = 0
        uniq_full = set()
        half = len(recs) // 2
        uniq_half = set()
        for i, (ids, freq) in enumerate(recs):
            if allowed is not None:
                ids = [x for x in ids if x < allowed]
                if len(ids) < 2:
                    continue
            for a in range(len(ids)):
                x = ids[a] * L
                for b in range(a + 1, len(ids)):
                    key = x + ids[b]
                    total += 1
                    uniq_full.add(key)
                    if i < half:
                        uniq_half.add(key)
        return total, len(uniq_full), len(uniq_half), half

    print("\n" + "-" * 70)
    print("🔗 ПАРЫ И РАЗМЕР ТАБЛИЦЫ links")
    print("-" * 70)
    t0 = time.perf_counter()
    total_pairs, u_full, u_half, half = pairs_stat(rec_ids)
    n_rec = len(rec_ids)
    print(f"Запросов с 2+ словами:  {n_rec:,} ({n_rec / max(1, len(records)) * 100:.1f}%)")
    print(f"Пар в выборке:          {total_pairs:,} ({total_pairs / max(1, n_rec):.2f} на запрос)")
    print(f"Уникальных пар:         {u_full:,}")

    beta = 0.85
    if u_half > 100 and u_full > u_half:
        beta = math.log(u_full / u_half) / math.log(2)
        beta = min(1.0, max(0.3, beta))
    est_records = est_lines * (n_rec / max(1, lines))
    scale = est_records / max(1, n_rec)
    est_unique = u_full * (scale ** beta)
    avg_rec_bytes = 2 + (sum(len(i) for i, _ in rec_ids) / max(1, n_rec) + 1) * 4
    print(f"Коэффициент роста уникальных пар (Heaps β): {beta:.3f}")
    print(f"\nПРОГНОЗ НА ВЕСЬ ФАЙЛ:")
    print(f"   Записей в ID-файле:  {big(est_records)}")
    print(f"   Размер ID-файла:     {human(est_records * avg_rec_bytes)}")
    print(f"   Всего пар:           {big(total_pairs * scale)}")
    print(f"   Строк в links:       {big(est_unique)}  (диапазон "
          f"{big(u_full * scale ** max(0.3, beta - 0.07))} … {big(u_full * scale ** min(1.0, beta + 0.07))})")
    print(f"   Размер таблицы:      {human(est_unique * 40)} … {human(est_unique * 60)}")

    print("\n" + "-" * 70)
    print("✂️ ЭФФЕКТ ФИЛЬТРА ПО ТОП-ЛЕММАМ")
    print("-" * 70)
    for k in TOP_K_LIST:
        if k >= len(ranked):
            continue
        tp, uf, _, _ = pairs_stat(rec_ids, allowed=k)
        if total_pairs:
            print(f"топ-{k:>9,} лемм: пар {tp / total_pairs * 100:5.1f}% от всех, "
                  f"уникальных {uf / max(1, u_full) * 100:5.1f}% → "
                  f"links ≈ {big(est_unique * uf / max(1, u_full))} строк, "
                  f"{human(est_unique * uf / max(1, u_full) * 50)}")

    share1 = freq_hist["1"] / lines
    print(f"\nОтсечение строк с freq=1 уберёт {share1 * 100:.1f}% строк.")
    print(f"Расчёт статистики занял {time.perf_counter() - t0:.1f} сек")

    print("\n" + "-" * 70)
    print("🧠 ПАМЯТЬ ПОД ПАРАЛЛЕЛЬНЫЙ РЕЖИМ")
    print("-" * 70)
    if db_ok:
        per_worker = n_forms * 95
        print(f"Словарь на 1 процесс (bytes-ключи): ≈ {human(per_worker)}")
        for w in (4, 6, 8):
            need = per_worker * w + 2 * 1024 ** 3
            verdict = "✅ влезет" if total_ram and need < total_ram * 0.8 else "❌ мало RAM"
            print(f"   {w} воркеров: ≈ {human(need)}  {verdict}")
    print("=" * 70)


if __name__ == "__main__":
    main()