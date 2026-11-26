![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)
![License MIT](https://img.shields.io/badge/License-MIT-green.svg)

# clusteringV3

Инструмент для анализа поисковых запросов с помощью графовых алгоритмов и ИИ.

## Возможности

- **Сборка семантического ядра** — графовый алгоритм выделения ключевых слов от стартовых слов
- **Фасетная классификация** — группировка слов через ансамбль LLM-моделей (13 провайдеров)
- **Поиск гипотез** — обнаружение скрытых связей через BFS глубиной 2
- **Экспорт в XLSX** — дерево связанных запросов для Яндекс.Директ
- **Гибкие параметры** — 15+ настраиваемых параметров, пресеты, чекпоинты
- **Сжатие архивов** — перенос данных между компьютерами через многотомные 7z

## Требования

- Python 3.10+
- 7-Zip (для сжатия архивов при экспорте)
- API-ключ хотя бы одного LLM-провайдера (для фасетной классификации)

## Установка


```bash
git clone https://github.com/kopyrin/clusteringV3.git
cd clusteringV3
python launcher_V2.py
```

Лаунчер автоматически создаст виртуальное окружение и установит зависимости.


Лаунчер автоматически создаст виртуальное окружение `.venv` и установит зависимости из `requirements.txt`.

## Быстрый старт

1. **Инициализация проекта** — создаст БД и распакует seed-данные из архивов `data/seed/*.csv.gz`
2. **Положите файлы в `data/input/`:**
   - `start_words.txt` — стартовые слова для сборки ядра
3. **Соберите ядро** — все параметры, пресеты, режимы запуска доступны в подменю


## Структура проекта


## Структура проекта
```
clusteringV3/
├── scripts/
│ ├── launcher_v2.py # Точка входа, главное меню
│ ├── common.py # Общая библиотека (пути, UI, БД, YAML)
│ ├── data_tools.py # Утилиты данных (init, import, export, filter)
│ ├── graph_builder.py # Построение графа из keywords.txt (v10)
│ ├── core_builder.py # Сборка семантического ядра
│ ├── hypothesis_finder.py # Поиск гипотез + экспорт XLSX (--xlsx)
│ ├── facet_processor.py # Фасетная классификация (ИИ)
│ └── cleanup_orphan_words.py # Очистка слов без связей
├── config/
│ ├── core_builder.yaml # Параметры сборки ядра
│ └── hypothesis_finder.yaml # Параметры поиска гипотез
├── data/
│ ├── input/ # keywords.txt, start_words.txt
│ ├── output/ # semantics.db, core_words.csv, ...
│ └── seed/ # Архивы seed-данных (*.csv.gz, .part)
├── dict/ # Словари для меток (brands.txt, geo.txt, ...)
├── .env.example # Шаблон API-ключей
├── requirements.txt # Зависимости Python
└── readme.md
```

## Основные команды (без лаунчера)

```bash
# Инициализация проекта
python scripts/data_tools.py init

# Импорт seed-CSV в БД
python scripts/data_tools.py import

# Экспорт БД в CSV + 7z
python scripts/data_tools.py export

# Фильтрация keywords.txt (предпросмотр)
python scripts/data_tools.py filter --threshold 100

# Фильтрация с применением
python scripts/data_tools.py filter --threshold 100 --apply

# Построение графа (dry-run — оценка масштаба)
python scripts/graph_builder.py --dry-run

# Построение графа (полный цикл)
python scripts/graph_builder.py --links-only --top-lemmas 200000 --no-prune

# Сборка ядра 
python scripts/core_builder.py

# Поиск гипотез для бизнеса ( слова которые не встречаются в запросах но которые часто запрашивают)
python scripts/hypothesis_finder.py

# Экспорт дерева в XLSX для Яндекс.Директ
python scripts/hypothesis_finder.py --xlsx

# Фасетная классификация (ИИ)
python scripts/facet_processor.py

# Очистка от слов без связей (предпросмотр)
python scripts/cleanup_orphan_words.py

# Очистка от слов без связей 
python scripts/cleanup_orphan_words.py --apply
```
## Seed-данные
```
Архивы data/seed/*.csv.gz уже содержат полный набор данных для инициализации:
words.csv.gz — слова и частоты
links.csv.gz — связи между словами
groups.csv.gz — названия групп
group_words.csv.gz — слова в группах
lemmas.csv.gz — словарь лемм
При первом запуске лаунчер автоматически распакует архивы и загрузит данные в БД.
```

## Лицензия

MIT License