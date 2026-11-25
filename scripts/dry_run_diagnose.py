"""
scripts/dry_run_diagnose.py — воронка фильтров v7 на выборке из keywords.txt.
Ничего не изменяет. Запуск: python scripts/dry_run_diagnose.py
"""
from pathlib import Path
import sqlite3
import time
from collections import Counter

ALLOWED_POS = {"существительное", "прилагательное", "наречие", "глагол", "инфинитив"}
MIN_FREQ = 2
MIN_PAIR = 3
MAX_WORDS = 8
SAMPLE_CHUNKS = 16
CHUNK_SIZE = 2 * 1024 * 1024

CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя-")


def find_root() -> Path:
    cur = Path(__file__).resolve()
    for p in [cur.parent] + list(cur.parents):
        if (p / "data").exists() or (p / "requirements.txt").exists():
            return p
    return cur.parent.parent


ROOT = find_root()
INPUT = ROOT / "data" / "input" / "keywords.txt"
MINUS = ROOT / "data" / "input" / "minus.txt"
DB = ROOT / "data" / "output" / "semantics.db"


def is_cyr(w):
    return bool(w) and all(c in CYR for c in w.lower())


def human(n):
    n = float(n)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


def main():
    print("=" * 70)
    print("🔬 ВОРОНКА ФИЛЬТРОВ v7 (только чтение)")
    print("=" * 70)
    size = INPUT.stat().st_size
    print(f"📄 {INPUT.name}: {human(size)}")

    # --- 1. Сырые строки: что реально лежит в файле ---
    print("\n" + "-" * 70)
    print("1. СЫРЫЕ СТРОКИ (repr)")
    print("-" * 70)
    with INPUT.open("rb") as f:
        for i in range(5):
            print(f"   {f.readline()!r}")
        for off in (size // 3, size // 2, size * 2 // 3):
            f.seek(off)
            f.readline()
            print(f"   @{off:,}: {f.readline()!r}")

    # --- 2. Словарь ---
    print("\n" + "-" * 70)
    print("2. СЛОВАРЬ ИЗ БД")
    print("-" * 70)
    t0 = time.perf_counter()
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
    allowed, id2lemma = {}, {}
    pos_hist = Counter()
    for wid, lemma, pos in conn.execute("SELECT id, word, pos FROM words"):
        pos_hist[pos] += 1
        if pos in ALLOWED_POS and is_cyr(lemma):
            allowed[lemma] = wid
            id2lemma[wid] = lemma
    forms = {}
    for raw, lemma in conn.execute("SELECT word, lemma FROM lemmas"):
        wid = allowed.get(lemma)
        if wid is not None:
            forms[raw] = wid
    for lemma, wid in allowed.items():
        forms.setdefault(lemma, wid)
    conn.close()
    minus = set()
    if MINUS.exists():
        minus = {w.strip().lower() for w in MINUS.read_text(encoding="utf-8").splitlines() if w.strip()}
    print(f"   Разрешённых лемм: {len(allowed):,} | ключей словаря: {len(forms):,} "
          f"| минус: {len(minus):,} | за {time.perf_counter() - t0:.0f} сек")
    print("   POS в таблице words (топ-8):")
    for p, c in pos_hist.most_common(8):
        mark = "✅" if p in ALLOWED_POS else "❌"
        print(f"      {mark} {str(p):>22}: {c:,}")

    # --- 3. Выборка ---
    step = max(1, size // SAMPLE_CHUNKS)
    raw_lines, bytes_read = [], 0
    with INPUT.open("rb") as f:
        for i in range(SAMPLE_CHUNKS):
            f.seek(i * step)
            if i:
                f.readline()
            data = f.read(CHUNK_SIZE)
            if not data:
                continue
            bytes_read += len(data)
            parts = data.decode("utf-8", errors="ignore").split("\n")
            if not data.endswith(b"\n"):
                parts.pop()
            raw_lines.extend(parts)
    frac = bytes_read / size
    print(f"\n   Выборка: {human(bytes_read)} ({frac * 100:.4f}% файла), строк: {len(raw_lines):,}")

    # --- 4. Разбор строки ---
    sep_hist, freq_hist = Counter(), Counter()
    parsed = []
    bad_freq = no_sep = 0
    for line in raw_lines:
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        if "\t" in line:
            line = line.replace("\t", ";")
        sep_hist[";"] += line.count(";")
        q, sep, fs = line.rpartition(";")
        if not sep:
            no_sep += 1
            q, fs = line, "1"
        fs_clean = fs.strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            freq = int(float(fs_clean))
        except ValueError:
            bad_freq += 1
            freq = 1
        if freq < 1:
            freq = 1
        freq_hist["1" if freq == 1 else "2-4" if freq <= 4 else "5-9" if freq <= 9 else "10+"] += 1
        parsed.append((q.split(), freq))

    n = len(parsed)
    print("\n" + "-" * 70)
    print("3. РАЗБОР СТРОКИ")
    print("-" * 70)
    print(f"   Строк разобрано:        {n:,}")
    print(f"   Без разделителя ';':    {no_sep:,} ({no_sep / max(1, n) * 100:.1f}%)")
    print(f"   freq не распарсилась:   {bad_freq:,} ({bad_freq / max(1, n) * 100:.1f}%)  "
          f"{'⚠️ ПОДОЗРИТЕЛЬНО' if bad_freq > n * 0.05 else '✅'}")
    print("   Распределение freq:")
    for k in ("1", "2-4", "5-9", "10+"):
        print(f"      freq {k:>5}: {freq_hist[k]:,} ({freq_hist[k] / max(1, n) * 100:5.1f}%)")

    # --- 5. Воронка ---
    def funnel(min_freq):
        st = Counter()
        pairs = Counter()
        for toks, freq in parsed:
            st["0. строк"] += 1
            if freq < min_freq:
                continue
            st["1. freq пройден"] += 1
            if len(toks) > MAX_WORDS:
                continue
            st["2. длина <= %d" % MAX_WORDS] += 1
            ids = set()
            for t in toks:
                st["токенов всего"] += 1
                if not is_cyr(t):
                    continue
                st["токенов кириллица"] += 1
                if t.lower() in minus:
                    continue
                st["токенов не минус"] += 1
                wid = forms.get(t)
                if wid is None:
                    continue
                st["токенов в словаре"] += 1
                ids.add(wid)
            if len(ids) < 2:
                continue
            st["3. запросов с 2+ ID"] += 1
            s = sorted(ids)
            for a in range(len(s)):
                for b in range(a + 1, len(s)):
                    pairs[(s[a] << 32) | s[b]] += freq
        return st, pairs

    for mf in (MIN_FREQ, 1):
        st, pairs = funnel(mf)
        title = f"4. ВОРОНКА ПРИ freq >= {mf}" + ("  (текущая настройка)" if mf == MIN_FREQ else "  (для сравнения)")
        print("\n" + "-" * 70)
        print(title)
        print("-" * 70)
        for k in ("0. строк", "1. freq пройден", f"2. длина <= {MAX_WORDS}", "3. запросов с 2+ ID"):
            v = st[k]
            print(f"   {k:>24}: {v:>10,}  ({v / max(1, n) * 100:5.1f}% от строк)"
                  f"{'   ← ЗДЕСЬ ОБРЫВ' if v == 0 else ''}")
        for k in ("токенов всего", "токенов кириллица", "токенов не минус", "токенов в словаре"):
            print(f"   {k:>24}: {st[k]:>10,}")
        total_pairs = sum(pairs.values())
        print(f"   {'пар всего':>24}: {total_pairs:,}")
        print(f"   {'пар уникальных':>24}: {len(pairs):,}")
        for thr in (1, 2, 3, 5, 10):
            c = sum(1 for v in pairs.values() if v >= thr)
            flag = "   ← ПОРОГ pair>=%d ОБНУЛЯЕТ ВЫБОРКУ" % thr if c == 0 and thr == MIN_PAIR else ""
            print(f"      пар с count >= {thr:<2}: {c:,}{flag}")
        if mf == MIN_FREQ and pairs:
            print("\n   Топ-10 пар выборки:")
            for key, c in pairs.most_common(10):
                print(f"      {id2lemma.get(key >> 32, '?')} + {id2lemma.get(key & 0xFFFFFFFF, '?')} = {c}")

    print("\n" + "-" * 70)
    print("5. ВЫВОД")
    print("-" * 70)
    print(f"   Выборка = {frac * 100:.4f}% корпуса. Пара с корпусным count=100")
    print(f"   в выборке имеет count ≈ {100 * frac:.3f}, то есть 0-1.")
    print(f"   Порог MIN_PAIR_COUNT={MIN_PAIR} на выборке ≈ порог {MIN_PAIR / max(frac, 1e-9):,.0f} на корпусе.")
    print("   ➜ Порог частоты пары в dry-run применять НЕЛЬЗЯ.")
    print("=" * 70)


if __name__ == "__main__":
    main()