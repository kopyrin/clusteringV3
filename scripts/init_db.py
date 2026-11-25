"""
Инициализация пустой базы данных semantics.db
Создаёт структуру таблиц, индексы и справочник фасетных групп.
Схема соответствует V2 (clusteringV2).
Запуск:  python scripts/init_db.py
"""
import sqlite3
from pathlib import Path


def find_root() -> Path:
    current = Path(__file__).resolve()
    for parent in [current.parent] + list(current.parents):
        if (parent / "data").exists() or (parent / "requirements.txt").exists():
            return parent
    return current.parent.parent


ROOT = find_root()
DB_PATH = ROOT / "data" / "output" / "semantics.db"

GROUP_RULES = {
    1:  "Лингвистика", 2:  "Существительные", 4:  "Прилагательное",
    5:  "Краткое прилагательное", 6:  "Сравнительная степень",
    8:  "Глагол", 9:  "Инфинитив", 10: "Причастие", 11: "Краткое причастие",
    12: "Деепричастие", 13: "Наречия", 14: "Местоимения", 15: "Числительные",
    16: "Предлоги", 17: "Союзы", 18: "Частицы", 19: "Междометия",
    20: "Предикативы", 21: "Предметные области", 22: "Свойства товара",
    23: "Тематики", 24: "Человек", 35: "Мебель", 36: "Именованные объекты",
    37: "Бренды", 38: "Компании", 40: "Программы", 45: "Валюты",
    46: "Географические объекты", 47: "Организации", 48: "Технологии",
    50: "Английские служебные слова", 53: "Маркетплейсы",
    55: "Автомобильные бренды", 56: "Видео и стриминг",
    57: "Искусственный интеллект", 58: "Английские слова", 64: "Отели",
    65: "Банки", 66: "Операторы связи", 67: "Криптовалюты",
    70: "Фильмы и сериалы", 71: "Персонажи", 72: "Контент 18+",
    73: "Посуда", 74: "Текстиль", 75: "Крепеж", 76: "Боли", 77: "Возражения",
    79: "Освещение", 80: "Медиа", 81: "Форматы файлов",
    82: "Свойства материалов", 83: "Строительные материалы", 84: "Помещения",
    85: "Модели", 86: "Проблемы", 87: "Производители", 88: "Профессии",
    89: "Эконом-сегмент", 90: "Цена", 91: "Целевая аудитория",
    92: "Характеристики", 93: "Вес", 94: "Объем", 95: "Напряжение",
    96: "IT", 132: "Документы", 176: "Общество", 177: "Животные",
    181: "Спорт", 182: "Политика", 184: "Наука", 185: "Мероприятия",
    187: "Социальные сети", 189: "Продукты питания", 195: "Безопасность",
    199: "Финансы", 201: "Медицина", 205: "Кино", 208: "История",
    214: "Музыка", 220: "Растения", 221: "Образование", 227: "Игрушки",
    231: "Косметика", 235: "Страхование", 236: "Туризм", 242: "Сленг",
    250: "Микрозаймы", 251: "Еда", 254: "Религия", 256: "Купоны и скидки",
    260: "Интернет", 262: "Психология", 263: "Авиация", 267: "Экономика",
    270: "Игры", 271: "Военное дело", 275: "Города", 279: "Топливо",
    284: "Напитки", 286: "Развлечения", 292: "Автомобили",
    293: "Электроника", 294: "Бизнес", 313: "Искусство",
    320: "Поисковые системы", 321: "Сантехника", 328: "Химия",
    329: "Законодательство", 363: "Строительство и ремонт", 400: "Качество",
    402: "Литература", 410: "Инструменты", 413: "Одежда", 417: "Люди",
    424: "Термин", 437: "Сельское хозяйство", 451: "Транспорт",
    492: "Бытовая техника и оборудование",
    493: "Промышленное и техническое оборудование",
    496: "Мечта, желание, цель", 497: "Психология и саморазвитие",
    501: "Места продаж", 502: "Материалы", 503: "Комплектующие",
    504: "Услуги", 505: "Цвета", 506: "Стиль", 507: "Размеры",
    508: "Форма", 509: "Вместимость", 510: "Покрытие и отделка",
    511: "Ткани", 512: "Назначение", 513: "Конструкция", 514: "Артикулы",
    515: "Виды товаров", 516: "Торговое оборудование", 517: "Возраст",
    518: "Пол", 519: "Доход", 520: "Семейное положение", 521: "Интересы",
    522: "Хобби", 523: "Образ жизни", 524: "Ценности", 525: "Потребности",
    526: "Мотивация", 527: "Страхи", 528: "Эмоции", 529: "Привычки",
}


def create_database():
    if DB_PATH.exists():
        print(f"База уже существует: {DB_PATH}")
        answer = input("Удалить и создать заново? (y/N): ").strip().lower()
        if answer != "y":
            print("Отменено.")
            return
        DB_PATH.unlink()
        print("Старая база удалена.")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"Создаю базу: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL")
    # FOREIGN KEY отключены — ускоряют вставку в 2-3 раза
    cursor.execute("PRAGMA foreign_keys = OFF")

    # === ТАБЛИЦЫ (без FOREIGN KEY, как в V2) ===
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            word  TEXT    NOT NULL UNIQUE,
            pos   TEXT,
            count INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS links (
            word1_id INTEGER NOT NULL,
            word2_id INTEGER NOT NULL,
            count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (word1_id, word2_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lemmas (
            word  TEXT PRIMARY KEY,
            lemma TEXT NOT NULL,
            pos   TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY,
            parent_id   INTEGER,
            name        TEXT NOT NULL UNIQUE,
            description TEXT,
            type        INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS group_words (
            word_id  INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            PRIMARY KEY (word_id, group_id)
        )
    """)

    # # === ИНДЕКСЫ (как в V2) ===
    # # links: покрывающие индексы для ORDER BY count DESC
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_word1 ON links(word1_id)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_word2 ON links(word2_id)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_count ON links(count)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_word1_count ON links(word1_id, count)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_word1_count2 ON links(count, word1_id)")
    # # groups: иерархия и фильтрация
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_name ON groups(name)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_type ON groups(type)")
    # # group_words
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_words ON group_words(word_id, group_id)")
    # cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_words_group ON group_words(group_id)")

    # # Справочник групп
    # cursor.executemany(
        # "INSERT OR IGNORE INTO groups (id, name) VALUES (?, ?)",
        # list(GROUP_RULES.items())
    # )

    conn.commit()
    conn.close()

    print(f"✅ База создана. Групп: {len(GROUP_RULES)}")
    print(f"   Размер: {DB_PATH.stat().st_size:,} байт")


if __name__ == "__main__":
    create_database()