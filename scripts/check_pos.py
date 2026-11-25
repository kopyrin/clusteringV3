"""
scripts/check_pos.py — что реально лежит под частями речи в таблице words.
Только чтение. Запуск: python scripts/check_pos.py
"""
from pathlib import Path
import sqlite3
import time
from collections import Counter, defaultdict

import pymorphy3

SAMPLE = 20
POS_RU = {
    "NOUN": "существительное", "ADJF": "прилагательное", "ADJS": "краткое прилагательное",
    "COMP": "сравнительная степень", "VERB": "глагол", "INFN": "инфинитив",
    "PRTF": "причастие", "PRTS": "краткое причастие", "GRND": "деепричастие",
    "NUMR": "числительное", "ADVB": "наречие", "NPRO": "местоимение",
    "PRED": "предикатив", "PREP": "предлог", "CONJ": "союз",
    "PRCL": "частица", "INTJ": "междометие",
}
ORDER = [
    "существительное", "прилагательное", "глагол", "инфинитив", "наречие",
    "сравнительная степень", "краткое прилагательное", "деепричастие",
    "причастие", "краткое причастие",
    "числительное", "местоимение", "предлог", "союз", "частица",
    "предикатив", "междометие", "неизвестно",
]
CLOSED = {"предлог", "союз", "частица", "местоимение", "числительное",
          "предикатив", "междометие"}
FORM_POS = {"краткое прилагательное", "деепричастие", "краткое причастие",
            "сравнительная степень"}

MIN_LEMMA_LEN = 2   # было 3 — иначе теряем вк, вб, тг, ру, ок, ai, tv
STOP_POS = ("предлог", "союз", "частица", "местоимение", "предикатив", "междометие")

def find_root() -> Path:
    cur = Path(__file__).resolve()
    for p in [cur.parent] + list(cur.parents):
        if (p / "data").exists() or (p / "requirements.txt").exists():
            return p
    return cur.parent.parent


DB = find_root() / "data" / "output" / "semantics.db"


def main():
    print("=" * 78)
    print("🔬 ЧТО ЛЕЖИТ ПОД ЧАСТЯМИ РЕЧИ В words")
    print("=" * 78)
    if not DB.exists():
        print(f"❌ Нет базы: {DB}")
        return

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=30)
    counts = Counter()
    samples = defaultdict(list)
    t0 = time.perf_counter()
    total = 0
    print("Читаю таблицу words...")
    for word, pos in conn.execute("SELECT word, pos FROM words"):
        counts[pos] += 1
        total += 1
        lst = samples[pos]
        if len(lst) < SAMPLE:
            lst.append(word)
        if total % 3_000_000 == 0:
            print(f"   {total:,}...")
    conn.close()
    print(f"Всего лемм: {total:,} за {time.perf_counter() - t0:.0f} сек\n")

    print("=" * 78)
    print("ПОЛНОЕ РАСПРЕДЕЛЕНИЕ POS")
    print("=" * 78)
    for pos, c in counts.most_common():
        tag = "❌ служебное" if pos in CLOSED else ("⚠️ форма" if pos in FORM_POS else "")
        print(f"   {str(pos):<26} {c:>12,}  {tag}")

    morph = pymorphy3.MorphAnalyzer()
    print("\n" + "=" * 78)
    print("ПРОВЕРКА: какой POS у САМОЙ ЛЕММЫ (а не у первой формы)")
    print("=" * 78)
    rescue = 0
    for pos in ORDER:
        if pos not in samples:
            continue
        print(f"\n--- {pos}  ({counts[pos]:,} лемм) ---")
        mismatch = 0
        shown = 0
        for lemma in samples[pos]:
            p = morph.parse(lemma)[0]
            tag = p.tag.POS
            real = POS_RU.get(tag, "неизвестно") if tag else "неизвестно"
            flag = ""
            if real != pos:
                mismatch += 1
                flag = f"   ← на самом деле {real}"
            if shown < 10:
                print(f"   {lemma:<24} {real:<24}{flag}")
                shown += 1
        print(f"   Расхождений в выборке {SAMPLE}: {mismatch}")
        if pos in FORM_POS and mismatch > SAMPLE * 0.5:
            rescue += counts[pos]

    print("\n" + "=" * 78)
    print("ВЫВОД")
    print("=" * 78)
    print(f"Служебные классы (исключать): "
          f"{sum(counts[p] for p in CLOSED if p in counts):,} лемм")
    print(f"Под «формами» спрятано нормальных слов: ~{rescue:,} лемм")
    print(f"Причастия (реальные слова, решать отдельно): "
          f"{counts.get('причастие', 0) + counts.get('краткое причастие', 0):,}")
    print("\nРекомендуемый ALLOWED_POS:")
    print('ALLOWED_POS = ("существительное", "прилагательное", "краткое прилагательное",')
    print('               "сравнительная степень", "глагол", "инфинитив",')
    print('               "причастие", "краткое причастие", "деепричастие", "наречие")')
    print("=" * 78)


if __name__ == "__main__":
    main()