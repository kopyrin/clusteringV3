"""
Распаковка .gz архивов из data/seed/ в CSV-файлы
Запуск: python scripts/extract_seed.py
"""
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from progress import Progress


def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
SEED_DIR = ROOT / "data" / "seed"
CHUNK = 1024 * 1024  # 1 MB


def main():
    if not SEED_DIR.exists():
        print(f"❌ Папка не найдена: {SEED_DIR}")
        return

    gz_files = list(SEED_DIR.glob("*.csv.gz"))
    if not gz_files:
        print("✅ Архивов *.csv.gz не найдено.")
        return

    print(f"Найдено архивов: {len(gz_files)}")
    for gz in gz_files:
        csv_path = gz.with_suffix("")  # links.csv.gz -> links.csv
        prog = Progress(gz.name, unit="MB")
        with gzip.open(gz, "rb") as fin, open(csv_path, "wb") as fout:
            while chunk := fin.read(CHUNK):
                fout.write(chunk)
                prog.update(len(chunk) / CHUNK)
        prog.finish()

    print("\n✅ Распаковка завершена.")


if __name__ == "__main__":
    main()