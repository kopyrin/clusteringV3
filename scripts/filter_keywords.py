"""
Фильтрация keywords.txt — оставляет только строки с частотой > N.
ОПТИМИЗИРОВАНО для огромных файлов (потоковая запись, 0 нагрузки на RAM).

Запуск:
  python scripts/filter_keywords.py              # предпросмотр (быстрый проход, БЕЗОПАСНО)
  python scripts/filter_keywords.py --apply      # реально заменить файл
  python scripts/filter_keywords.py --threshold 5 --apply  # порог > 5
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

# ============================================================
# ПУТИ
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_FILE = PROJECT_ROOT / "data" / "input" / "keywords.txt"
BACKUP_FILE = PROJECT_ROOT / "data" / "input" / "keywords.txt.bak"
TEMP_FILE = PROJECT_ROOT / "data" / "input" / "keywords.txt.tmp"


# ============================================================
# АРГУМЕНТЫ
# ============================================================
def parse_args():
    parser = argparse.ArgumentParser(description="Фильтрация keywords.txt по частоте")
    parser.add_argument("--apply", action="store_true",
                        help="Реально заменить файл (по умолчанию — только предпросмотр)")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="Минимальная частота для сохранения (по умолчанию > 1.0)")
    return parser.parse_args()


# ============================================================
# БЕЗОПАСНАЯ ПОТОКОВАЯ ОБРАБОТКА
# ============================================================
def process_file_streaming(input_path: Path, output_path: Path | None, threshold: float, show_progress: bool = True):
    """
    Читает и фильтрует файл построчно.
    Если output_path=None — только считает (dry-run, безопасно).
    Если output_path указан — сразу записывает результат.
    """
    kept = 0
    dropped = 0
    total = 0
    
    last_update_time = time.perf_counter()
    last_total = 0
    
    # Примеры для отчёта
    sample_kept = []
    sample_dropped = []

    # Открываем файл на запись ТОЛЬКО если output_path указан
    if output_path:
        fout = open(output_path, "w", encoding="utf-8")
    else:
        fout = None

    try:
        with open(input_path, "r", encoding="utf-8") as fin:
            for line in fin:
                total += 1
                
                # Быстрая проверка: разбиваем только по последнему пробелу/табуляции/запятой
                stripped = line.rstrip("\n\r")
                if not stripped:
                    dropped += 1
                    continue
                
                # Пробуем найти число в конце строки
                parts = stripped.rsplit(maxsplit=1)
                if len(parts) == 2:
                    word_part, freq_part = parts
                    freq_str = freq_part.replace(",", ".").strip()
                    try:
                        freq = float(freq_str)
                        if freq > threshold:
                            if fout:
                                fout.write(line)
                            kept += 1
                            if len(sample_kept) < 3 and show_progress:
                                sample_kept.append(f"  {word_part.strip()!r} → {freq}")
                        else:
                            dropped += 1
                            if len(sample_dropped) < 3 and show_progress:
                                sample_dropped.append(f"  {word_part.strip()!r} → {freq}")
                    except ValueError:
                        dropped += 1
                else:
                    dropped += 1

                # Обновляем прогресс-бар каждые 100 000 строк или каждые 0.5 секунды
                if show_progress and (total % 100_000 == 0):
                    now = time.perf_counter()
                    if now - last_update_time >= 0.5:
                        elapsed = now - last_update_time
                        speed = (total - last_total) / elapsed if elapsed > 0 else 0
                        
                        speed_str = f"{speed/1000:.1f}K строк/с" if speed >= 1000 else f"{speed:.0f} строк/с"
                        
                        line_out = (
                            f"\r📖 Обработано: {total:,} строк | "
                            f"✅ Оставлено: {kept:,} | "
                            f"❌ Отброшено: {dropped:,} | "
                            f"Скорость: {speed_str}"
                        )
                        sys.stdout.write(line_out)
                        sys.stdout.flush()
                        
                        last_update_time = now
                        last_total = total

        if show_progress:
            sys.stdout.write("\n")
            sys.stdout.flush()

    finally:
        if fout:
            fout.close()

    return total, kept, dropped, sample_kept, sample_dropped


# ============================================================
# ОТЧЁТ
# ============================================================
def print_report(total: int, kept: int, dropped: int, sample_kept: list, sample_dropped: list, dry_run: bool):
    print("\n" + "=" * 70)
    mode = "🔍 ПРЕДПРОСМОТР (dry-run, файл НЕ изменён)" if dry_run else "✅ ПРИМЕНЕНИЕ"
    print(f"{mode}")
    print("=" * 70)
    print(f"Всего строк:            {total:>10,}")
    print(f"  ✅ Оставлено:         {kept:>10,}  ({kept/max(total,1)*100:.1f}%)")
    print(f"  ❌ Отброшено:         {dropped:>10,}")
    
    if sample_kept:
        print("\nПримеры оставленных:")
        for s in sample_kept:
            print(s)
    if sample_dropped:
        print("\nПримеры отброшенных:")
        for s in sample_dropped:
            print(s)
    print("=" * 70)


# ============================================================
# ТОЧКА ВХОДА
# ============================================================
def main():
    args = parse_args()
    input_path = KEYWORDS_FILE
    backup_path = BACKUP_FILE
    temp_path = TEMP_FILE

    if not input_path.exists():
        print(f"❌ Файл не найден: {input_path}")
        return

    file_size_gb = input_path.stat().st_size / (1024**3)
    print(f"📂 Входной файл: {input_path.relative_to(PROJECT_ROOT)}")
    print(f"📏 Размер файла: {file_size_gb:.2f} ГБ")
    print(f"🎯 Порог частоты: > {args.threshold}")
    print()

    t0 = time.perf_counter()
    
    # БЕЗОПАСНОСТЬ: в dry-run режиме output_path=None (ничего не пишем)
    # В apply режиме пишем во временный файл
    target_path = temp_path if args.apply else None
    
    total, kept, dropped, sample_kept, sample_dropped = process_file_streaming(
        input_path, target_path, args.threshold, show_progress=True
    )
    
    elapsed = time.perf_counter() - t0
    print_report(total, kept, dropped, sample_kept, sample_dropped, dry_run=not args.apply)
    print(f"⏱  Время обработки: {elapsed:.1f}с")

    if not args.apply:
        print("\n💡 Это предпросмотр. Чтобы реально заменить файл, добавь флаг --apply")
        return

    # Подтверждение перед заменой
    print(f"\n⚠️  Оригинальный файл будет заменён отфильтрованной версией.")
    print(f"   Бэкап сохранится в: {backup_path.name}")
    try:
        answer = input("   Продолжить? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n❌ Отменено.")
        return
    
    if answer not in ("y", "yes", "д", "да"):
        print("❌ Отменено.")
        # Чистим временный файл
        if temp_path.exists():
            temp_path.unlink()
        return

    print("\n🔄 Замена файлов...")
    # 1. Делаем бэкап оригинала
    shutil.copy2(input_path, backup_path)
    
    # 2. Заменяем оригинал временным файлом
    shutil.move(temp_path, input_path)
    
    new_size_gb = input_path.stat().st_size / (1024**3)
    reduction = (1 - new_size_gb / file_size_gb) * 100
    
    print(f"💾 Бэкап создан: {backup_path.relative_to(PROJECT_ROOT)}")
    print(f"📉 Размер файла: {file_size_gb:.2f} ГБ → {new_size_gb:.2f} ГБ (−{reduction:.1f}%)")
    print(f"✅ Файл успешно обновлён!")


if __name__ == "__main__":
    main()