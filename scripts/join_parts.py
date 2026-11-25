"""
Сборка разбитых на части архивов (links.csv.partXX -> links.csv)
Запуск: python scripts/join_parts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from progress import PercentProgress


def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
SEED_DIR = ROOT / "data" / "seed"
CHUNK = 1024 * 1024


def main():
    parts = sorted(SEED_DIR.glob("*.part*"))
    if not parts:
        print("✅ Разбитых частей не найдено.")
        return

    # Группируем по базовому имени: links.csv.part01 -> links.csv
    groups: dict[str, list[Path]] = {}
    for p in parts:
        base = p.name.split(".part")[0]
        groups.setdefault(base, []).append(p)

    for base, files in groups.items():
        out_path = SEED_DIR / base
        total = sum(f.stat().st_size for f in files)
        prog = PercentProgress(base, total=total / CHUNK, unit="MB")
        with open(out_path, "wb") as fout:
            for f in files:
                with open(f, "rb") as fin:
                    while chunk := fin.read(CHUNK):
                        fout.write(chunk)
                        prog.update(len(chunk) / CHUNK)
        prog.finish()
        print(f"    ✅ Файл собран: {out_path.name} "
              f"({out_path.stat().st_size / 1024 / 1024:.1f} MB)")

    print("\nГотово.")


if __name__ == "__main__":
    main()