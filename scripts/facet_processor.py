
# ============================================================
# КОНСОЛЬНЫЙ СТАТУС
# ============================================================
# Текущая операция обновляется в одной строке.
# Завершенные этапы печатаются отдельными строками.
def console_status(text):
    """
    Обновляет текущий статус выполнения программы
    в одной строке консоли.

    Используется для отображения операций,
    которые выполняются последовательно и должны
    заменять предыдущий статус, а не создавать
    новую строку. Каждый вызов перезаписывает
    текущую строку, а не печатает новую.

    Реализация:
        печать с возвратом каретки (\r) без перевода
        строки и с принудительным сбросом буфера,
        чтобы статус отображался сразу.

    Параметры:
        text — текст текущего статуса.
    """
    print(f"\r{text:<80}", end="", flush=True)

def console_line(text=""):
    """
    Выводит отдельную строку в консоль.

    Используется для сообщений, которые должны
    сохраниться в журнале выполнения и не заменяться
    последующими статусами (в отличие от console_status,
    который перезаписывает текущую строку).

    Параметры:
        text — текст сообщения. По умолчанию — пустая строка,
               что выводит пустую строку (перенос строки).
    """
    print(text)

############################################################
#
# Исследование тематической области.
#
# На вход подается список стартовых слов.
#
# Программа:
#
#     • находит связанные слова;
#     • формирует тематический словарь;
#     • передает его AI;
#     • получает фасеты темы;
#     • сохраняет фасеты в базе данных.
#
# Главный результат программы —
# сохраненные в базе данных фасеты темы,
# построенные по группе связанных слов.
#
############################################################
import sqlite3
from collections import defaultdict
from colorama import Fore, Style
import json
import pickle
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
from collections import Counter
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import threading
load_dotenv()
from facet_analyzer import FacetAnalyzer

# ============================================================
# НАСТРОЙКИ ЭКСПЕРИМЕНТА
# ============================================================

####################################################
# Параметры исследования
####################################################
# Глубина поиска по соседям
MAX_DEPTH = 1
# количество слов передаваемых ИИ
TARGET_GRAPH_SIZE = 100
# Сколько слов отправлять одной модели за один запрос
AI_BATCH_SIZE = 30
AI_SECOND_BATCH_SIZE = 30

# --- Поиск слов ---
# Начальный порог силы связи
START_LINK_WEIGHT = 100
# Если True — слова из start_words.txt
# сразу передаются на классификацию AI,
# без поиска связанных слов.
CLASSIFY_START_WORDS_ONLY = False

# --- Голосование ---
# количество судей необходимых для перехода слова на следующий этап
MIN_VOTES = 3
GROUP_TYPE_NORMAL = 1
GROUP_TYPE_STUDY = 2
GROUP_TYPE_FACET = 3

# Разрешенные части речи
ALLOWED_POS = {
    "существительное",
    "глагол",
    "прилагательное",
    "краткое прилагательное",
    "неизвестно"
}
"""
глагол
деепричастие
инфинитив
краткое прилагательное
краткое причастие
междометие
местоимение
наречие
неизвестно
предикатив
предлог
прилагательное
причастие
союз
сравнительная степень
существительное
частица
числительное
"""

NO_FILTER_GROUPS = {
    2,   # Существительные
    4,   # Прилагательное
    5,   # Краткое прилагательное
    6,   # Сравнительная степень
    8,   # Глагол
    9,   # Инфинитив
    10,  # Причастие
    11,  # Краткое причастие
    12,  # Деепричастие
    13,  # Наречия
    14,  # Местоимения
    15,  # Числительные
    16,  # Предлоги
    17,  # Союзы
    18,  # Частицы
    19,  # Междометия
    20,  # Предикативы

}

# Группы частей речи, исключаемые из тематических групп
# при построении кэша известных слов.
# Дублирует NO_FILTER_GROUPS, но хранится как сортированный
# кортеж — подходит для подстановки прямо в SQL.
POS_GROUP_IDS = tuple(sorted(NO_FILTER_GROUPS))

CONFIRM_FILES = [
    "output/last_facets.txt",
    "output/level1.txt",
]

####################################################
# HypothesisFinder
####################################################
class HypothesisFinder:

    # ===================================================
    # ИНИЦИАЛИЗАЦИЯ
    # ===================================================
    def __init__(self, db_path):
        """
        Инициализирует исследователь.

        Выполняется один раз при создании объекта.

        Что делает:

        1. Сохраняет путь к базе данных.
        2. Подготавливает внутренние структуры и кэши.
        3. Подключается к SQLite.
        4. Загружает автоматические группы слов.
        5. Создает временные таблицы для построения графа.
        6. Создает FacetAnalyzer для работы с AI.

        После выполнения объект готов к запуску исследования.
        """
        # ---------------------------------------------------
        # Состояние исследователя
        # ---------------------------------------------------
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.word_info_cache = {}
        # self.report_file = None
        self.auto_words = {}
        self.auto_podgroup = {}
        self.min_link_weight = START_LINK_WEIGHT

        # Подключение к базе данных
        # и подготовка рабочих таблиц.
        self.connect()
        # Анализатор отвечает за все обращения к AI.
        #
        # provider="all" — в пул судей попадают модели
        # всех провайдеров из AI_PROVIDERS. Недоступные
        # и не отвечающие модели программа исключает сама
        # при формировании пула (test) и во время работы.
        self.facet_analyzer = FacetAnalyzer(
            self.report,
            provider="all"
        )
        self.last_error_type = None
        self.last_error = None
        # Максимальное количество слов
        # в одном запросе к AI.
        # Максимальное количество слов
        # в одном запросе к AI на первом этапе.
        self.batch_size = AI_BATCH_SIZE

        # Максимальное количество слов
        # в одном запросе к AI на втором этапе.
        self.second_batch_size = AI_SECOND_BATCH_SIZE

        self.start_words = []
        self.start_words_ids = []
        self.studies = []
        self.first_stage_result = []
        self.judges_raw = []
        # Защищает общий список judges_raw:
        # пакеты второго этапа обрабатываются параллельно,
        # потоки не должны дописывать список одновременно.
        self._judges_raw_lock = threading.Lock()

    def connect(self):
        """
        Подключается к SQLite и подготавливает
        временные таблицы исследования.

        Выполняется:

        1. Подключение к базе данных.
        2. Создание курсора.
        3. Загрузка слов, уже относящихся
           к известным автоматическим группам.
        4. Удаление старых временных таблиц.
        5. Создание таблиц wave, wave_next и visited.

        Таблицы wave, wave_next и visited используются
        только во время текущего построения графа.
        """
        # ---------------------------------------------------
        # Подключение к базе данных
        # ---------------------------------------------------
        self.conn = sqlite3.connect(self.db_path)
        # ---------------------------------------------------
        # Создание курсора
        # ---------------------------------------------------
        self.cursor = self.conn.cursor()

        # ---------------------------------------------------
        # Загрузка уже известных групп
        # ---------------------------------------------------
        self.load_auto_groups()

        # ---------------------------------------------------
        # Загрузка структуры подгрупп
        # ---------------------------------------------------
        self.load_auto_podgroups()
        # ---------------------------------------------------
        # Подготовка временных таблиц графа
        #
        # wave      — текущая волна поиска;
        # wave_next — следующая волна;
        # visited   — все слова, уже найденные в исследовании.
        # ---------------------------------------------------
        self.cursor.executescript("""
                                  DROP TABLE IF EXISTS wave;
                                  DROP TABLE IF EXISTS wave_next;
                                  DROP TABLE IF EXISTS visited;

                                  CREATE TABLE wave
                                  (
                                      word_id   INTEGER PRIMARY KEY,
                                      parent_id INTEGER,
                                      depth     INTEGER,
                                      weakest   INTEGER
                                  );

                                  CREATE TABLE wave_next
                                  (
                                      word_id   INTEGER PRIMARY KEY,
                                      parent_id INTEGER,
                                      depth     INTEGER,
                                      weakest   INTEGER
                                  );

                                  CREATE TABLE visited
                                  (
                                      word_id   INTEGER PRIMARY KEY,
                                      parent_id INTEGER,
                                      depth     INTEGER,
                                      weakest   INTEGER
                                  );
                                  """)


        self.conn.commit()
    # ===================================================
    # ПОДГОТОВКА
    # ===================================================
    # ---------------------------------------------------
    # Загрузка слов, уже известных программе
    # ------------------------------------------------
    def load_auto_groups(self):
        """
        Загружает слова, уже относящиеся
        к известным тематическим группам.

        Источник данных:

            • сначала output/auto_words.pkl;
            • если кэш отсутствует — база данных.

        Из базы выбираются слова групп типа
        GROUP_TYPE_NORMAL, кроме технических
        и стандартных групп частей речи.

        Результат сохраняется в:

            self.auto_words

        При отсутствии кэша после загрузки
        создается output/auto_words.pkl.
        """
        cache_file = PROJECT_ROOT / "data" / "output" / "auto_words.pkl"
        # ===================================================
        # 1. Попытка загрузить готовый кэш
        # ===================================================
        if cache_file.exists():

            try:

                with cache_file.open("rb") as f:
                    self.auto_words = pickle.load(f)

                if not isinstance(self.auto_words, dict):
                    raise ValueError(
                        "Кэш auto_words имеет неправильный формат."
                    )

                console_status(
                    f"Взято из файла кэша: "
                    f"{len(self.auto_words)} записей"
                )

                return

            except (
                    EOFError,
                    pickle.UnpicklingError,
                    ValueError,
                    OSError
            ) as e:

                console_line(
                    f"Кэш auto_words повреждён: "
                    f"{type(e).__name__}: {e}"
                )

                console_line(
                    "Кэш будет пересоздан из базы данных."
                )

                try:
                    cache_file.unlink()
                except OSError:
                    pass
        # ===================================================
        # 2. Загрузка данных из базы
        # ===================================================
        self.auto_words = {}
        # ---------------------------------------------------
        # Выбираем слова из обычных тематических групп.
        # Технические группы и группы частей речи исключаются.
        # ---------------------------------------------------
        placeholders = ",".join(
            "?" for _ in POS_GROUP_IDS
        )

        self.cursor.execute(f"""
                    SELECT w.word,
                           g.name
                    FROM group_words gw
                    JOIN words w ON w.id = gw.word_id
                    JOIN groups g ON g.id = gw.group_id
                    WHERE g.type = ?
                      AND g.id NOT IN ({placeholders})
                            """, (GROUP_TYPE_NORMAL,) + POS_GROUP_IDS)

        rows = self.cursor.fetchall()

        for word, group_name in rows:
            self.auto_words[word] = group_name
        console_line(f"записей в кэше = {len(rows)}")
        # ===================================================
        # 3. Сохранение кэша
        # ===================================================
        temp_file = cache_file.with_suffix(
            ".pkl.tmp"
        )

        try:

            with temp_file.open("wb") as f:

                pickle.dump(
                    self.auto_words,
                    f,
                    protocol=pickle.HIGHEST_PROTOCOL
                )

                f.flush()

            temp_file.replace(cache_file)

            console_line(
                "кэш для фильтра создан "
            )

        except Exception:

            try:
                temp_file.unlink()
            except OSError:
                pass

            raise
    # ---------------------------------------------------
    # Загрузка названий групп и подгрупп для использвании в промте определения слова ИИ
    # ------------------------------------------------
    def load_auto_podgroups(self):
        """
        Загружает структуру родительских групп и их подгрупп.

        Источник данных:

            • сначала output/auto_podgroup.pkl;
            • если кэш отсутствует — база данных.

        Из структуры исключаются группы из
        NO_FILTER_GROUPS.

        Результат:

            self.auto_podgroup

        Формат:

            {
                "Предметные области": [
                    "Мебель",
                    "Электроника",
                    ...
                ],
                "Свойства товара": [
                    "Материалы",
                    "Конструкция",
                    "Размеры",
                    ...
                ]
            }

        Ключ:
            родительская группа.

        Значение:
            список её непосредственных подгрупп.
        """

        cache_file = PROJECT_ROOT / "data" / "output" / "auto_podgroup.pkl"

        # ===================================================
        # 1. Загрузка готового кэша
        # ===================================================

        if cache_file.exists():

            with cache_file.open("rb") as f:
                self.auto_podgroup = pickle.load(f)


            total = sum(
                len(groups)
                for groups in self.auto_podgroup.values()
            )

            console_status(
                f"Взято из файла кэша подгрупп: "
                f"{total} записей"
            )

            return

        # ===================================================
        # 2. Загрузка структуры из базы
        # ===================================================

        self.auto_podgroup = {}

        excluded = tuple(NO_FILTER_GROUPS)

        placeholders = ",".join(
            "?" for _ in excluded
        )

        self.cursor.execute(
            f"""
            SELECT
                parent.id,
                parent.name,
                child.id,
                child.name
            FROM groups AS parent
            JOIN groups AS child
                ON child.parent_id = parent.id
            WHERE parent.id NOT IN ({placeholders})
              AND child.id NOT IN ({placeholders})
            ORDER BY
                parent.id,
                child.id
            """,
            excluded + excluded
        )

        rows = self.cursor.fetchall()

        # ===================================================
        # 3. Формирование словаря
        # ===================================================

        for (
            parent_id,
            parent_name,
            child_id,
            child_name
        ) in rows:

            self.auto_podgroup.setdefault(
                parent_name,
                []
            ).append(
                child_name
            )

        # ===================================================
        # 4. Сохранение кэша
        # ===================================================

        with cache_file.open("wb") as f:

            pickle.dump(
                self.auto_podgroup,
                f,
                protocol=pickle.HIGHEST_PROTOCOL
            )

        total = sum(
            len(groups)
            for groups in self.auto_podgroup.values()
        )

        console_line(
            f"Кэш подгрупп создан: "
            f"{total} записей"
        )
    # ---------------------------------------------------
    # Загрузка стартовых слов
    # ---------------------------------------------------
    def load_start_words(self):
        """
        Загружает все стартовые слова из start_words.txt
        как одно общее исследование.

        Все строки файла объединяются.
        Повторы слов исключаются.

        Результат:

            self.studies = [
                (все слова, все их ID)
            ]
        """

        # ===================================================
        # 1. Чтение файла стартовых слов
        # ===================================================
        path = Path("start_words.txt")

        self.studies = []

        all_words = []
        all_ids = []
        seen_ids = set()

        # ===================================================
        # 2. Все строки объединяются в одно исследование
        # ===================================================
        for line in path.read_text(
                encoding="utf-8"
        ).splitlines():

            line = line.strip()

            if not line:
                continue

            words = line.split()

            # ---------------------------------------------------
            # Преобразование слов в ID базы
            # ---------------------------------------------------
            for word in words:

                word_id = self.find_word_id(word)

                if word_id is None:
                    self.report(
                        f"Не найдено: {word}"
                    )
                    continue

                # ------------------------------------------------
                # Одно слово не должно попасть
                # в общий граф несколько раз.
                # ------------------------------------------------
                if word_id in seen_ids:
                    continue

                seen_ids.add(word_id)

                all_words.append(word)
                all_ids.append(word_id)

        # ===================================================
        # 3. Все стартовые слова = одно исследование
        # ===================================================
        if all_words:
            self.studies.append(
                (all_words, all_ids)
            )
    # ===================================================
    # ЗАПУСК ИССЛЕДОВАНИЯ
    # ===================================================
    def run_study(self):
        """
        Запускает исследование всех наборов стартовых слов.

        Последовательность работы:

        1. Загружает стартовые слова из start_words.txt.
        2. Для каждого исследования:
           • вызывает find_related_words(), который либо
             передаёт стартовые слова судьям напрямую
             (CLASSIFY_START_WORDS_ONLY=True), либо строит
             граф связанных слов и затем передаёт слова AI;
           • если рабочих AI-моделей нет — останавливает
             исследование.

        Все строки start_words.txt объединяются
        в одно исследование.

        Основной результат поиска формируется
        в таблице visited.
        """
        # ---------------------------------------------------
        # Подготовка исследования
        # ---------------------------------------------------
        self.load_start_words()
        # ---------------------------------------------------
        # Последовательная обработка исследований
        # ---------------------------------------------------
        for self.start_words, self.start_words_ids in self.studies:

            self.report()

            if CLASSIFY_START_WORDS_ONLY:
                self.find_related_words(
                    start_words_only=True
                )
            else:
                self.find_related_words()

            if self.facet_analyzer.no_models:
                console_line(
                    "Рабочих AI-моделей нет. "
                    "Исследование остановлено."
                )
                return
    # ===================================================
    # ПОИСК СВЯЗАННЫХ СЛОВ
    # ===================================================
    def find_related_words(
            self,
            start_words_only=False
    ):
        """
        Строит граф связанных слов и передает найденные
        слова на классификацию AI.

        Два режима работы:

        • start_words_only=True — стартовые слова сразу
          формируют список «Связанных слов» и передаются
          судьям без поиска соседей по графу.
        • start_words_only=False — строится граф связанных
          слов с подбором порога силы связи, слова
          фильтруются и передаются судьям.

        Этапы работы (режим поиска):

        1. Строит граф связанных слов.
        2. Подбирает минимальный порог силы связи так,
           чтобы получить подходящий объем данных.
        3. Формирует список слов для AI.
        4. Исключает слова, которые уже известны программе.
        5. Разбивает оставшиеся слова на пакеты.
        6. Формирует prompt для каждого пакета.
        7. Передает пакеты AI.
        8. Анализирует полученные результаты
           и собирает обнаруженные типы фасетов.
        9. Запускает второй этап классификации (подгруппы).

        Параметры:
            start_words_only — если True, поиск связанных
                               слов по графу не выполняется,
                               стартовые слова передаются
                               судьям напрямую.

        Основные данные для поиска находятся
        в таблице visited.

        В текущей реализации запись результата
        исследования в базу данных отключена.
        """
        if start_words_only:

            filtered = [
                {
                    "слово": word
                }
                for word in self.start_words
            ]

            console_line(
                f"Режим без поиска: "
                f"{len(filtered)} готовых слов передано судьям"
            )

            ai_data = {
                "Стартовые слова": self.start_words,
                "Связанные слова": filtered
            }

        else:
            # ===================================================
            # 1. Построение графа и подбор порога связи
            # ===================================================

            self.min_link_weight = START_LINK_WEIGHT

            lower_weight = None
            upper_weight = None

            # ---------------------------------------------------
            # Функция проверки одного порога.
            #
            # Для каждого порога граф строится заново,
            # после чего определяется количество слов,
            # реально пригодных для передачи AI.
            #
            # Возвращает:
            #     ai_data       — данные для AI;
            #     filtered      — слова после фильтрации;
            #     filtered_count — количество таких слов.
            # ---------------------------------------------------

            graph_attempt = 0

            def evaluate_weight(weight):
                """
                Проверяет один порог силы связи.

                Перестраивает граф с указанным порогом,
                собирает данные для AI и фильтрует слова.

                Параметры:
                    weight — минимальная сила связи (порог).

                Возвращает:
                    (ai_data, filtered, filtered_count) —
                    данные для AI, слова после фильтрации
                    и их количество.
                """

                nonlocal graph_attempt

                graph_attempt += 1

                self.min_link_weight = weight

                self._build_graph()

                ai_data = self._build_ai_data()

                filtered = self._filter_for_ai(
                    ai_data["Связанные слова"]
                )

                filtered_count = len(filtered)

                return ai_data, filtered, filtered_count

            # ===================================================
            # 1. Поиск диапазона, в котором находится цель
            # ===================================================

            weight = START_LINK_WEIGHT

            ai_data, filtered, filtered_count = evaluate_weight(
                weight
            )

            # Сохраняем лучший найденный результат.
            best_weight = weight
            best_distance = abs(
                filtered_count - TARGET_GRAPH_SIZE
            )
            best_filtered = filtered

            if filtered_count > TARGET_GRAPH_SIZE:

                # ---------------------------------------------------
                # Слов слишком много.
                #
                # Увеличиваем порог, пока количество слов
                # не станет меньше или равно целевому.
                # ---------------------------------------------------

                lower_weight = weight

                while filtered_count > TARGET_GRAPH_SIZE:

                    weight *= 2

                    ai_data, filtered, filtered_count = evaluate_weight(
                        weight
                    )

                    distance = abs(
                        filtered_count - TARGET_GRAPH_SIZE
                    )

                    if distance < best_distance:
                        best_weight = weight
                        best_distance = distance
                        best_filtered = filtered

                    upper_weight = weight

            elif filtered_count < TARGET_GRAPH_SIZE:

                # ---------------------------------------------------
                # Слов слишком мало.
                #
                # Уменьшаем порог, пока количество слов
                # не станет больше или равно целевому.
                # ---------------------------------------------------

                upper_weight = weight

                while filtered_count < TARGET_GRAPH_SIZE:

                    weight = max(1, weight // 2)

                    if weight == upper_weight:
                        break

                    ai_data, filtered, filtered_count = evaluate_weight(
                        weight
                    )

                    distance = abs(
                        filtered_count - TARGET_GRAPH_SIZE
                    )

                    if distance < best_distance:
                        best_weight = weight
                        best_distance = distance
                        best_filtered = filtered

                    lower_weight = weight

                    if weight == 1:
                        break

            else:

                # ---------------------------------------------------
                # Цель достигнута сразу.
                # ---------------------------------------------------

                best_weight = weight
                best_filtered = filtered

            # ===================================================
            # 2. Двоичный поиск внутри найденного диапазона
            # ===================================================

            if (
                lower_weight is not None
                and upper_weight is not None
            ):

                # ---------------------------------------------------
                # Пока диапазон шире одного шага — сужаем его.
                # ---------------------------------------------------

                while upper_weight - lower_weight > 1:

                    weight = (
                        lower_weight + upper_weight
                    ) // 2

                    ai_data, filtered, filtered_count = evaluate_weight(
                        weight
                    )

                    distance = abs(
                        filtered_count - TARGET_GRAPH_SIZE
                    )

                    if distance < best_distance:
                        best_weight = weight
                        best_distance = distance
                        best_filtered = filtered

                    if filtered_count > TARGET_GRAPH_SIZE:

                        lower_weight = weight

                    elif filtered_count < TARGET_GRAPH_SIZE:

                        upper_weight = weight

                    else:

                        # ---------------------------------------------------
                        # Точное попадание в целевой размер.
                        # ---------------------------------------------------

                        best_weight = weight
                        best_filtered = filtered

                        break

            # ===================================================
            # 3. Фиксация лучшего найденного порога
            # ===================================================

            self.min_link_weight = best_weight
            console_status(f"Граф: {graph_attempt} перестроений | порог {best_weight} | перед AI {len(best_filtered)}")

            # ---------------------------------------------------
            # Если точное значение не найдено,
            # используется ближайший найденный результат.
            # ---------------------------------------------------

            # ВАЖНО:
            # best_filtered уже получен во время подбора порога.
            # Повторно строить тот же граф здесь не нужно.

            filtered = best_filtered
            # ===================================================
            # 2. Подготовка окончательного списка слов
            # ===================================================

        ai_data["Связанные слова"] = filtered
        # ===================================================
        # 3. Разбиение слов на пакеты
        #
        # Каждый пакет передаётся AI отдельным запросом.
        # Размер пакета ограничен self.batch_size.
        # ===================================================
        batches = []

        for i in range(
                0,
                len(ai_data["Связанные слова"]),
                self.batch_size):
            batches.append(
                ai_data["Связанные слова"][
                i:i + self.batch_size
                ]
            )


        # ===================================================
        # 4. Анализ пакетов через AI
        #
        # Для каждого пакета:
        #     • формируется prompt первого этапа;
        #     • ответы MIN_VOTES судей разбираются;
        #     • результаты голосуются;
        #     • победившие пары «слово + группа»
        #       добавляются в self.first_stage_result.
        # ===================================================

        self.first_stage_result = []

        for batch_no, batch in enumerate(
                batches,
                start=1
        ):

            console_status(
                f"ЭТАП 1 | пакет {batch_no}/{len(batches)} | "
                f"подготовка"
            )

            words = [
                item["слово"]
                for item in batch
            ]

            prompt = self._build_first_prompt(
                words
            )


            answers = self.facet_analyzer.ask(
                prompt,
                judges=MIN_VOTES
            )

            if answers is None:
                console_line(
                    "Первый этап остановлен: "
                    "не удалось получить 3 ответа AI."
                )

                return

            # ---------------------------------------------------
            # Сохраняем сырые ответы судей для анализа.
            #
            # Логику голосования не меняем:
            # answers по-прежнему используются только
            # для разбора и последующего голосования.
            # ---------------------------------------------------

            for judge_no, judge in enumerate(
                    answers,
                    start=1
            ):
                with self._judges_raw_lock:
                    self.judges_raw.append({
                        "stage": "first",
                        "batch": batch_no,
                        "judge": judge_no,
                        "model": judge["model"],
                        "words": words,
                        "prompt": prompt,
                        "answer": judge["answer"]
                    })

            # ---------------------------------------------------
            # Разбор ответов судей
            #
            # Каждый ответ преобразуется в структурированный
            # результат. Некорректные ответы пропускаются.
            # ---------------------------------------------------

            judge_results = []

            for judge in answers:
                # print(
                #     f"РАЗБОР СУДЬИ: {judge.get('model')}"
                # )
                result = self._parse_first_result(
                    judge["answer"]
                )

                if result is None:
                    continue

                # Для голосования передаём именно
                # разобранный результат судьи.
                judge_results.append(result)

            # ---------------------------------------------------
            # Если корректных ответов меньше MIN_VOTES,
            # голосовать бессмысленно.
            # ---------------------------------------------------

            if len(judge_results) < MIN_VOTES:
                console_line(
                    "Первый этап остановлен: "
                    "недостаточно корректных ответов AI."
                )

                return

            # ---------------------------------------------------
            # Голосование
            # ---------------------------------------------------

            voted_result = self._vote_first_stage(
                judge_results
            )

            self.first_stage_result.extend(
                voted_result
            )


        # ===================================================
        # Сохранение первого этапа
        #
        # Результаты раскладываются по файлам
        # ai_1_<группа>.txt — по одному на верхнюю группу.
        # ===================================================
        self._save_first_stage_files(
            self.first_stage_result
        )
        # ===================================================
        # Второй этап — определение подгрупп
        # ===================================================

        self._run_second_stage()
        # ===================================================
        # Сбор слов в накопительный last_facets.txt
        # ===================================================
        console_line("Второй этап завершен → last_facets.txt")

        self._append_second_stage_to_last_facets()
        self._save_judges_raw()
        console_line("Готово → last_facets.txt")
        return
    # ===================================================
    # ПОСТРОЕНИЕ ГРАФА
    # ===================================================
    def _build_graph(self):
        """
        Строит граф связанных слов от стартовых слов.

        Алгоритм выполняет поиск волнами до MAX_DEPTH.

        Перед началом:

            • очищаются wave;
            • очищаются wave_next;
            • очищаются visited;
            • стартовые слова помещаются в wave.

        Затем для каждой глубины вызывается
        _find_next_wave(), который строит следующую волну.

        Поиск прекращается:

            • после достижения MAX_DEPTH;
            • если очередная волна не содержит новых слов.

        Возвращает:
            количество слов в таблице visited.
        """
        # ===================================================
        # 1. Очистка предыдущего графа
        # ===================================================
        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("DELETE FROM wave_next")
        self.cursor.execute("DELETE FROM visited")

        # ===================================================
        # 2. Начальная волна
        #
        # Стартовые слова помещаются в wave с глубиной 0.
        # weakest = 2147483647 — максимальный INT, т.е.
        # «бесконечная» сила связи: стартовые слова
        # не ограничены порогом.
        # ===================================================
        for word_id in self.start_words_ids:
            self.cursor.execute("""
                                INSERT INTO wave
                                (word_id,
                                 parent_id,
                                 depth,
                                 weakest)
                                VALUES (?, ?, ?, ?)
                                """,
                                (
                                    word_id,
                                    word_id,
                                    0,
                                    2147483647
                                ))
        # Стартовые слова сразу считаются посещенными.
        self.cursor.execute("""
                            INSERT
                            OR IGNORE INTO visited
                            SELECT *
                            FROM wave
                            """)

        # ===================================================
        # 3. Расширение графа по волнам
        #
        # На каждой глубине строится следующая волна соседей.
        # Если волна пуста — новых слов нет, поиск завершается.
        # ===================================================
        for depth in range(1, MAX_DEPTH + 1):

            rows = self._find_next_wave()

            if rows == 0:
                break
        # ===================================================
        # 4. Подсчет найденных слов
        # ===================================================
        rows = self.cursor.execute("""
                                   SELECT COUNT(*)
                                   FROM visited
                                   """).fetchone()[0]

        return rows
    # ---------------------------------------------------
    # ПОИСК В ГРАФЕ
    # ---------------------------------------------------
    def _find_next_wave(self):
        """
        Строит следующую волну поиска связанных слов.

        На входе:
            таблица wave с текущей волной.

        На выходе:
            таблица wave_next с найденными соседями.

        Для каждого слова текущей волны:

            1. находит связанные слова в links;
            2. отбрасывает связи ниже текущего порога;
            3. исключает слова, уже находящиеся в visited;
            4. рассчитывает минимальную силу связи
               от стартового слова до нового слова;
            5. добавляет новые слова в wave_next.

        После построения следующей волны:

            • найденные слова добавляются в visited;
            • текущая wave очищается;
            • wave_next становится новой текущей wave.

        Возвращает:
            количество слов, найденных в wave_next.
        """
        # ---------------------------------------------------
        # Построение следующей волны BFS.
        #
        # Для каждого слова текущей волны:
        #
        #     ищем соседей;
        #     исключаем уже посещенные;
        #     вычисляем минимальную силу связи
        #     на пути от стартового слова;
        #     помещаем их в wave_next.
        # ---------------------------------------------------
        self.cursor.execute("""
                            INSERT
                            OR IGNORE INTO wave_next
             (
                 word_id,
                 parent_id,
                 depth,
                 weakest
             )
                            SELECT l.word2_id,
                                   w.word_id,
                                   w.depth + 1,
                                   CASE
                                       WHEN w.weakest < l.count THEN w.weakest
                                       ELSE l.count
                                       END
                            FROM wave w
                                     CROSS JOIN links l
                                ON l.word1_id = w.word_id
                                     LEFT JOIN visited v
                                               ON v.word_id = l.word2_id
                            WHERE l.count >= ?
                              AND v.word_id IS NULL;
                            """, (
                                self.min_link_weight,
                            ))
        # ---------------------------------------------------
        # Количество найденных слов
        # ---------------------------------------------------
        self.cursor.execute("""
                            SELECT COUNT(*)
                            FROM wave_next
                            """)

        rows = self.cursor.fetchone()[0]
        # ---------------------------------------------------
        # Сохранение найденных слов
        # ---------------------------------------------------
        self.cursor.execute("""
                             INSERT OR REPLACE INTO visited
                             SELECT *
                             FROM wave_next
                             """)
        # ---------------------------------------------------
        # Подготовка следующей итерации
        # ---------------------------------------------------
        self.cursor.execute("DELETE FROM wave")
        # ---------------------------------------------------
        # Следующая волна становится текущей
        # ---------------------------------------------------

        self.cursor.execute("""
            INSERT INTO wave
            SELECT *
            FROM wave_next
        """)

        self.cursor.execute("DELETE FROM wave_next")

        return rows
    # ---------------------------------------------------
    # РАБОТА СО СТАРТОВЫМИ СЛОВАМИ
    # ---------------------------------------------------
    def _find_word(self, word=None, word_id=None):
        """
        Ищет запись в таблице words по тексту слова
        или по внутреннему ID.

        Общий помощник для find_word_id() и get_word().
        Ровно один из параметров должен быть задан.

        Параметры:
            word    — текст слова (поиск по полю word).
            word_id — внутренний ID слова (поиск по полю id).

        Возвращает:
            строка результата SELECT (row) —
            (id,) при поиске по тексту,
            (word,) при поиске по ID;

            None — если запись не найдена.
        """
        if word_id is not None:
            self.cursor.execute(
                "SELECT word FROM words WHERE id = ?",
                (word_id,)
            )
        else:
            self.cursor.execute(
                "SELECT id FROM words WHERE word = ?",
                (word,)
            )

        return self.cursor.fetchone()

    def find_word_id(self, word):
        """
        Находит внутренний ID слова в таблице words.

        Связи графа хранятся по числовым ID,
        поэтому текстовое слово перед началом поиска
        преобразуется в его идентификатор.

        Параметры:
            word — текст слова.

        Возвращает:
            int  — ID найденного слова;
            None — если слово отсутствует в базе.
        """

        row = self._find_word(word=word)

        if row is None:
            return None

        return row[0]
    # ---------------------------------------------------
    # Получение соседей слова
    # ---------------------------------------------------
    def get_neighbors(self, word_id):
        """
            Возвращает все слова, непосредственно связанные
            с указанным словом в таблице links.

            Для каждой связи определяется ID соседнего слова
            независимо от того, находится ли исходное слово
            в word1_id или word2_id.

            Соседи сортируются по силе связи:
            сначала возвращаются наиболее сильные связи.

            Параметры:
                word_id — внутренний ID исходного слова.

            Возвращает:
                список кортежей вида:

                    (neighbor_id, count)

                где:
                    neighbor_id — ID связанного слова;
                    count       — сила связи.
            """
        self.cursor.execute("""
                            SELECT CASE
                                       WHEN word1_id = ? THEN word2_id
                                       ELSE word1_id
                                       END AS neighbor_id,
                                   count
                            FROM links
                            WHERE word1_id = ?
                               OR word2_id = ?
                            ORDER BY count DESC
                            """, (word_id, word_id, word_id))

        return self.cursor.fetchall()
    # ===================================================
    # Подготовка данных для AI
    # ===================================================
    def _build_ai_data(self):
        """
        Формирует данные исследования для передачи AI.

        Из таблицы visited получает найденные слова
        и преобразует их из word_id в текстовые слова.

        Слова сортируются по силе самой слабой связи
        в порядке убывания.

        Возвращает:

            {
                "Стартовые слова": [...],
                "Связанные слова": [
                    {"слово": "..."},
                    ...
                ]
            }
        """

        ai_data = {
            "Стартовые слова": self.start_words,
            "Связанные слова": []
        }
        # ===================================================
        # 1. Получение найденных слов
        #
        # Слова выбираются из visited в порядке убывания
        # силы самой слабой связи — самые «уверенные»
        # идут первыми.
        # ===================================================
        self.cursor.execute("""
                            SELECT word_id
                            FROM visited
                            ORDER BY weakest DESC
                            """)

        rows = self.cursor.fetchall()
        # ===================================================
        # 2. Преобразование ID в слова
        #
        # Слова, отсутствующие в базе (get_word вернул None),
        # пропускаются.
        # ===================================================
        for (word_id,) in rows:

            word = self.get_word(word_id)

            if word:
                ai_data["Связанные слова"].append({
                    "слово": word
                })

        return ai_data
    # ---------------------------------------------------
    # Фильтрация слов перед отправкой в AI
    # ---------------------------------------------------
    def _filter_for_ai(self, words):
        """
        Фильтрует слова перед отправкой в AI.

        Из исходного списка исключаются:

            • слова, часть речи которых не входит
              в ALLOWED_POS;
            • слова, отсутствующие в базе;
            • технический мусор — смесь букв и цифр
              (например: x86_64, v2ray, abc123, кровать123);
            • слова, уже присутствующие
              в self.auto_words.

        Уже известные слова повторно
        не передаются AI
        Вместо этого они сразу добавляются
        в self.auto_facets.

        Возвращает:
            список слов, которые необходимо
            классифицировать через AI.
        """
        # ===================================================
        # 1. Подготовка результата
        # ===================================================
        result = []


        for item in words:

            word = item["слово"]
            # ===================================================
            # 2. Проверка части речи
            #
            # Слова с недопустимой частью речи (не входящей
            # в ALLOWED_POS) и слова без записи в базе
            # исключаются из классификации.
            # ===================================================
            self.cursor.execute(
                """
                SELECT pos
                FROM words
                WHERE word = ?
                """,
                (word,)
            )

            row = self.cursor.fetchone()

            if not row:
                continue

            if row[0] not in ALLOWED_POS:
                continue

            # ---------------------------------------------------
            # Слово — технический мусор
            #
            # Смесь букв и цифр (например: x86_64, v2ray,
            # abc123, кровать123) — это технические обозначения,
            # а не слова. Моделям их классифицировать нечего,
            # поэтому они исключаются до отправки судьям.
            # ---------------------------------------------------

            if (
                any(c.isalpha() for c in word)
                and any(c.isdigit() for c in word)
            ):
                continue

            # ---------------------------------------------------
            # Слово уже известно программе
            # ---------------------------------------------------

            if word in self.auto_words:
                continue

            # Слово необходимо классифицировать через AI.
            result.append(item)

        return result
    # ===================================================
    # ПЕРВЫЙ ЭТАП — ОПРЕДЕЛЕНИЕ ГРУПП
    # ===================================================
    def _build_first_prompt(self, words):
        """
        Формирует prompt для первого, грубого этапа
        классификации слов.

        AI определяет только верхние группы.

        Для каждого слова AI может выбрать
        одну или две наиболее подходящие группы.

        Новые группы придумывать запрещено.

        Вместо примера ответа в промте указывается только
        формат JSON. Конкретный пример не используется,
        чтобы не смещать ответы модели в сторону одной
        из верхних групп.
        """

        top_groups = list(self.auto_podgroup.keys())

        prompt = []

        prompt.append(
            "Ответь строго одним корректным JSON-массивом, "
            "без пояснений и другого текста."
        )

        prompt.append(
            "Для каждого слова определи наиболее подходящие "
            "верхние группы."
        )

        prompt.append(
            "Используй только названия групп из списка, "
            "ничего нового не добавляй."
        )

        prompt.append(
            "Для каждого слова выбери не более двух групп."
        )

        prompt.append(
            "Если слово уверенно относится только к одной группе, "
            "укажи только её."
        )

        prompt.append(
            "Если слово нельзя уверенно отнести "
            "ни к одной группе, верни пустой список."
        )

        prompt.append("")
        prompt.append("Верхние группы:")

        for group_name in top_groups:
            prompt.append(
                f"- {group_name}"
            )

        prompt.append("")
        prompt.append("Слова для классификации:")

        for word in words:
            prompt.append(word)

        prompt.append("")
        prompt.append("Ответ:")

        prompt.append("""
    Формат ответа — JSON-массив:
    [
      {
        "слово": "слово из списка",
        "группы": ["группа из разрешённых"]
      }
    ]
    """.strip())

        return "\n".join(prompt)

    def _parse_first_result(
            self,
            content,
            expected_words=None,
            report_error=True
    ):
        """
        Разбирает ответ AI первого этапа.

        Ожидаемый формат:

            [
                {
                    "слово": "шкаф",
                    "группы": [
                        "Предметные области"
                    ]
                }
            ]

        Ответ очищается от Markdown-обертки,
        приводится к списку словарей. Для каждого слова:

            • группы фильтруются по списку разрешённых
              верхних групп (self.auto_podgroup);
            • оставляется не более двух групп.

        Если передан expected_words, дополнительно
        проверяется полнота ответа:

            • ответ не пустой;
            • количество результатов совпадает с ожидаемым;
            • нет повторяющихся слов;
            • набор слов совпадает с переданным.

        При любой ошибке разбора ответ сохраняется
        в bad_json.txt через _save_bad_answer().

        Параметры:
            content       — текст ответа AI.
            expected_words — ожидаемый список слов пакета;
                             если None, проверка полноты
                             не выполняется.
            report_error  — если True, сообщения об ошибках
                            выводятся через self.report().

        Возвращает:
            список словарей вида
            [{"слово": ..., "группы": [...]}, ...] —
            при успешном разборе;

            None — если ответ пустой, не является JSON
                   или не проходит проверку полноты.
        """

        if not content or not content.strip():
            self._save_bad_answer(content)
            return None

        content = self._clean_json(content)
        # print("AI JSON:")
        # print(content)
        try:
            result = json.loads(content)

            if isinstance(result, dict):
                result = [result]

            if not isinstance(result, list):
                raise Exception(
                    "Ответ первого этапа должен быть JSON-массивом."
                )

            allowed_groups = set(
                self.auto_podgroup.keys()
            )

            clean_result = []

            for item in result:

                if not isinstance(item, dict):
                    continue

                word = item.get("слово", item.get("word"))
                groups = item.get("группы", item.get("groups"))

                if not isinstance(word, str):
                    continue

                if not isinstance(groups, list):
                    groups = []

                groups = [
                    group
                    for group in groups
                    if group in allowed_groups
                ]

                groups = groups[:2]

                clean_result.append({
                    "слово": word,
                    "группы": groups
                })

            # ---------------------------------------------------
            # Проверка полноты ответа.
            #
            # Модель должна вернуть результат
            # для каждого слова текущего пакета.
            # ---------------------------------------------------

            if expected_words is not None:

                expected = [
                    str(word).strip().lower()
                    for word in expected_words
                    if str(word).strip()
                ]

                actual = [
                    item["слово"].strip().lower()
                    for item in clean_result
                    if item.get("слово")
                ]

                expected_set = set(expected)
                actual_set = set(actual)

                # Пустой результат
                if not clean_result:

                    if report_error:
                        self.report(
                            "AI не вернул ни одного результата "
                            "для пакета."
                        )

                    self._save_bad_answer(content)

                    return None

                # Количество результатов не совпадает
                if len(clean_result) != len(expected):

                    if report_error:
                        self.report(
                            f"AI вернул {len(clean_result)} результатов "
                            f"из {len(expected)} ожидаемых."
                        )

                    self._save_bad_answer(content)

                    return None

                # Есть дубликаты слов
                if len(actual) != len(actual_set):

                    if report_error:
                        self.report(
                            "AI вернул повторяющиеся слова."
                        )

                    self._save_bad_answer(content)

                    return None

                # Набор слов не совпадает
                if actual_set != expected_set:

                    if report_error:
                        self.report(
                            "AI вернул не тот набор слов, "
                            "который был передан в запросе."
                        )

                    self._save_bad_answer(content)

                    return None
            return clean_result

        except json.JSONDecodeError:

            if report_error:
                self.report(
                    "Неверный JSON первого этапа"
                )

            self._save_bad_answer(content)

            return None

    def _vote_first_stage(self, judge_results):
        """
        Объединяет результаты первого этапа MIN_VOTES судей.

        Для каждой пары:
            слово + верхняя группа
        считается количество судей, которые выбрали эту группу.

        Один судья не может проголосовать за одну и ту же
        пару дважды — повторный голос игнорируется.

        В ai_1_* попадает только пара с количеством голосов
        не меньше MIN_VOTES.

        Параметры:
            judge_results — список результатов судей;
                            каждый результат — список
                            словарей вида
                            [{"слово": ..., "группы": [...]}].

        Возвращает:
            список словарей вида
            [{"слово": ..., "группы": [...]}, ...] —
            пары «слово + группа», набравшие не меньше
            MIN_VOTES голосов. Для каждого слова
            оставляется не более двух групп.
        """

        votes = Counter()


        for result in judge_results:

            # Один судья может дать одну и ту же группу только один раз.
            seen = set()

            for item in result:

                word = item.get("слово")



                for group_name in item.get("группы", []):

                    key = (word, group_name)

                    if key in seen:
                        continue

                    seen.add(key)
                    votes[key] += 1

        result_by_word = defaultdict(list)

        for (word, group_name), count in votes.items():

            if count >= MIN_VOTES:
                result_by_word[word].append(group_name)

        voted = []

        for word, groups in result_by_word.items():

            voted.append({
                "слово": word,
                "группы": groups[:2]
            })

        return voted

    def _save_first_stage_files(self, result):
        """
        Раскладывает результат первого этапа
        по отдельным файлам верхних групп.

        Формат файла:
            одно слово на строку.
        Имена файлов:
            output/ai_1_<название группы>.txt
        """

        output_dir = PROJECT_ROOT / "data" / "output"
        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        # ===================================================
        # 1. Очищаем рабочие файлы первого этапа
        # ===================================================

        for group_name in self.auto_podgroup:
            file_path = (
                    output_dir
                    / f"ai_1_{group_name}.txt"
            )

            file_path.write_text(
                "",
                encoding="utf-8"
            )

        # ===================================================
        # 2. Раскладываем слова по группам
        # ===================================================

        files = {}

        try:

            # Каждый элемент результата — пара «слово + группа».
            # Слово может попасть в несколько групп.
            for item in result:

                word = item["слово"]

                for group_name in item["группы"]:

                    # Группы вне self.auto_podgroup игнорируются.
                    if group_name not in self.auto_podgroup:
                        continue

                    # Файл группы открывается один раз
                    # и держится открытым до конца записи.
                    if group_name not in files:
                        file_path = (
                                output_dir
                                / f"ai_1_{group_name}.txt"
                        )

                        files[group_name] = file_path.open(
                            "a",
                            encoding="utf-8"
                        )

                    files[group_name].write(
                        word + "\n"
                    )

        finally:

            for file in files.values():
                file.close()

        # ===================================================
        # 3. Показываем результат
        # ===================================================

        for group_name in self.auto_podgroup:

            file_path = (
                    output_dir
                    / f"ai_1_{group_name}.txt"
            )

            if not file_path.exists():
                continue

            count = sum(
                1
                for line in file_path.open(
                    "r",
                    encoding="utf-8"
                )
                if line.strip()
            )

            if count:
                console_line(
                    f"{group_name}: {count} слов"
                )
    # ===================================================
    # ВТОРОЙ ЭТАП — ОПРЕДЕЛЕНИЕ ПОДГРУПП
    # ===================================================
    def _run_second_stage(self):
        """
        Второй этап классификации — определение подгрупп.

        Берёт каждый файл первого этапа:
            ai_1_<группа>.txt

        Разбивает слова на пакеты.

        Пакеты обрабатываются параллельно:
        каждый пакет независимо передаётся судьям AI,
        время ожидания ответов моделей перекрывается.

        После обработки всей группы
        дописывает результат в файл:
            ai_2_<группа>.txt

        Файл открывается в режиме дописывания ("a"),
        поэтому результаты нескольких запусков
        накапливаются.

        Формат строки:
            группа<TAB>подгруппа<TAB>слово
        """

        output_dir = PROJECT_ROOT / "data" / "output"
        # ===================================================
        # Очистка результатов второго этапа предыдущего
        # запуска НАМЕРЕННО отключена: файлы ai_2_*.txt
        # должны накапливать подтверждения между запусками.
        # ===================================================
        # for old_file in output_dir.glob("ai_2_*.txt"):
        #     old_file.unlink()

        for group_name in self.auto_podgroup:

            input_file = (
                    output_dir
                    / f"ai_1_{group_name}.txt"
            )

            # Пропускаем группы без слов первого этапа.
            if not input_file.exists():
                continue

            words = [
                line.strip()
                for line in input_file.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]

            if not words:
                continue

            # ===================================================
            # Разбиение группы на пакеты
            #
            # Размер пакета ограничен self.second_batch_size.
            # ===================================================

            batches = []

            for i in range(
                    0,
                    len(words),
                    self.second_batch_size
            ):
                batches.append(
                    words[
                    i:i + self.second_batch_size
                    ]
                )

            console_status(
                f"Второй этап: {group_name} | "
                f"{len(words)} слов | {len(batches)} пакетов"
            )

            # ===================================================
            # Обработка пакетов параллельно
            #
            # Каждый пакет независимо передаётся судьям AI,
            # поэтому пакеты запускаются одновременно —
            # время ожидания ответов моделей не суммируется,
            # а перекрывается.
            #
            # FacetAnalyzer рассчитан на параллельные запросы
            # (внутри ask() судьи уже выполняются в потоках),
            # поэтому отдельные вызовы ask() безопасно
            # запускать параллельно.
            #
            # Порядок результатов сохраняется (map):
            # строки в ai_2_*.txt не перемешиваются
            # в зависимости от скорости ответов.
            # ===================================================

            group_results = []

            def classify_one(batch_no_batch):
                batch_no, batch = batch_no_batch
                return self._classify_second_stage_group(
                    group_name,
                    batch,
                    batch_no,
                    len(batches)
                )

            with ThreadPoolExecutor(
                    max_workers=len(batches)
            ) as executor:

                for result in executor.map(
                        classify_one,
                        enumerate(batches, start=1)
                ):

                    group_results.extend(
                        result or []
                    )

            # ===================================================
            # Сохранение результата всей группы
            # ===================================================

            output_file = (
                    output_dir
                    / f"ai_2_{group_name}.txt"
            )

            with output_file.open(
                    "a",
                    encoding="utf-8"
            ) as f:

                for item in group_results:

                    word = item.get("слово")
                    podgroup = item.get("подгруппа")

                    if not word or not podgroup:
                        continue

                    f.write(
                        f"{group_name}\t{podgroup}\t{word}\n"
                    )

    def _classify_second_stage_group(
        self,
        group_name,
        words,
        batch_no=None,
        total_batches=None
    ):
        """
        Передаёт один пакет слов второго этапа
        ровно MIN_VOTES AI-судьям.

        Последовательность работы:

        1. Формирует prompt второго этапа для пакета.
        2. Получает ответы MIN_VOTES судей.
        3. Сохраняет сырые ответы в self.judges_raw.
        4. Разбирает каждый ответ через _parse_second_result().
        5. Некорректные ответы пропускаются; если корректных
           меньше MIN_VOTES — возвращает пустой список.
        6. Результаты всех судей объединяются в один список
           без подсчёта голосов: одинаковые пары
           «слово + подгруппа» остаются повторами строк
           и накапливаются между запусками
           в last_facets.txt.

        ВАЖНО:
            голосование здесь не выполняется.
            Подтверждение происходит позже через promote():
            три одинаковые строки «группа<TAB>подгруппа<TAB>слово»
            дают одну подтверждённую запись.

        Параметры:
            group_name    — верхняя группа, к которой
                            относится пакет слов.
            words         — список слов для классификации.
            batch_no      — номер пакета (для лога и judges_raw).
            total_batches — общее количество пакетов группы.

        Возвращает:
            список словарей вида
            [{"слово": ..., "подгруппа": ...}, ...] —
            объединённые результаты всех судей;

            [] — если пакет пуст, ответы не получены
                 или корректных ответов меньше MIN_VOTES.
        """

        if not words:
            return []

        prompt = self._build_second_prompt(
            words,
            group_name
        )

        answers = self.facet_analyzer.ask(
            prompt,
            judges=MIN_VOTES
        )

        if answers is None:
            console_line(
                f"Не удалось получить ответы всех AI-судей "
                f"для группы: {group_name}"
            )
            return []

        # ---------------------------------------------------
        # Сохраняем сырые ответы второго этапа.
        # ---------------------------------------------------

        for judge_no, judge in enumerate(
                answers,
                start=1
        ):
            with self._judges_raw_lock:
                self.judges_raw.append({
                    "stage": "second",
                    "batch": batch_no,
                    "group": group_name,
                    "judge": judge_no,
                    "model": judge["model"],
                    "words": words,
                    "prompt": prompt,
                    "answer": judge["answer"]
                })

        judge_results = []

        for judge in answers:

            result = self._parse_second_result(
                judge["answer"]
            )


            if result is not None:
                judge_results.append(result)

        if len(judge_results) < MIN_VOTES:
            console_line(
                f"Недостаточно корректных ответов AI-судей "
                f"для группы: {group_name}"
            )
            return []

        result = []

        for judge_result in judge_results:
            for item in judge_result:

                word = item.get("слово")
                podgroup = item.get("подгруппа")

                if not word or not podgroup:
                    continue

                result.append({
                    "слово": word,
                    "подгруппа": podgroup
                })

        return result

    def _build_second_prompt(self, words, group_name):
        """
        Формирует prompt второго этапа классификации.

        На входе:

            • список слов, уже отнесённых первым AI
              к верхней группе;
            • название верхней группы.

        AI должен определить для каждого слова
        конкретную подгруппу из разрешённого списка.

        Новые подгруппы создавать запрещено.

        Вместо примера ответа в промте указывается только
        формат JSON. Пример не используется, потому что
        любое конкретное название подгруппы существует
        лишь у одной верхней группы и провоцирует модель
        выдумывать подгруппы для остальных групп.
        """

        podgroups = self.get_podgroups(
            group_name
        )

        prompt = []

        prompt.append(
            "Ответь строго одним корректным JSON-массивом, "
            "без пояснений и другого текста."
        )

        prompt.append(
            f"Все слова относятся к верхней группе: "
            f"{group_name}"
        )

        prompt.append(
            "Для каждого слова выбери наиболее подходящую "
            "подгруппу из предоставленного списка."
        )

        prompt.append(
            "Используй только названия подгрупп из списка, "
            "ничего нового не добавляй."
        )

        prompt.append(
            "Классифицируй слово по его смысловому значению, "
            "а не по внешнему сходству с названием подгруппы."
        )

        prompt.append(
            "Если слово может относиться к подгруппе только "
            "при дополнительном предположении, не выбирай её."
        )

        prompt.append(
            "Если ни одна подгруппа не подходит достаточно уверенно, "
            "укажи пустую строку."
        )

        prompt.append(
            "Разные написания одного и того же названия "
            "(например: BAXI, baxi, бакси) считай одним объектом, "
            "если они обозначают одно и то же."
        )

        prompt.append("")

        prompt.append("Разрешённые подгруппы:")

        for podgroup in podgroups:
            prompt.append(
                f"- {podgroup}"
            )

        prompt.append("")

        prompt.append("Слова:")

        prompt.extend(words)

        prompt.append("")

        prompt.append("Ответ:")

        prompt.append("""
    Формат ответа — JSON-массив:
    [
      {
        "слово": "слово из списка",
        "подгруппа": "подгруппа из разрешённых"
      }
    ]
    """.strip())

        return "\n".join(prompt)

    def _parse_second_result(self, content):
        """
        Разбирает ответ AI второго этапа.

        Ожидаемый формат:

            [
                {
                    "слово": "угловой",
                    "подгруппа": "Конструкция"
                }
            ]

        Ответ очищается от Markdown-обертки
        и приводится к списку словарей.

        ВАЖНО:
            в отличие от _parse_first_result, этот метод
            не проверяет подгруппу по списку разрешённых
            и не проверяет полноту ответа — значение
            подгруппы принимается как есть.

        При ошибке разбора ответ сохраняется
        в bad_json.txt через _save_bad_answer().

        Параметры:
            content — текст ответа AI.

        Возвращает:
            список словарей вида
            [{"слово": ..., "подгруппа": ...}, ...] —
            при успешном разборе;

            None — если ответ пустой или не является JSON.
        """

        if not content or not content.strip():
            self._save_bad_answer(content)
            return None

        content = self._clean_json(content)

        try:
            result = json.loads(content)

            if isinstance(result, dict):
                result = [result]

            if not isinstance(result, list):
                self.report(
                    "Ответ первого этапа должен быть JSON-массивом."
                )

                self._save_bad_answer(content)

                return None

            clean_result = []

            for item in result:

                if not isinstance(item, dict):
                    continue

                word = item.get("слово")
                podgroup = item.get("подгруппа", "")

                if not isinstance(word, str):
                    continue

                if not isinstance(podgroup, str):
                    podgroup = ""

                clean_result.append({
                    "слово": word,
                    "подгруппа": podgroup
                })

            return clean_result

        except json.JSONDecodeError:

            self.report(
                "Неверный JSON второго этапа"
            )

            self._save_bad_answer(content)

            return None
    # ===================================================
    # СОХРАНЕНИЕ РЕЗУЛЬТАТОВ ИССЛЕДОВАНИЯ
    # ===================================================
    def _append_second_stage_to_last_facets(self):
        """
        Добавляет результаты второго этапа
        в накопительный last_facets.txt.

        Старое содержимое не удаляется.
        """

        output_dir = PROJECT_ROOT / "data" / "output"
        last_file = output_dir / "last_facets.txt"

        added = 0

        # ===================================================
        # Файл last_facets.txt открывается в режиме
        # дописывания, чтобы сохранять подтверждения
        # между запусками.
        # ===================================================

        with last_file.open(
                "a",
                encoding="utf-8"
        ) as out:

            # Каждый файл ai_2_*.txt содержит строки вида:
            # группа<TAB>подгруппа<TAB>слово
            for file_path in sorted(
                    output_dir.glob("ai_2_*.txt")
            ):

                lines = file_path.read_text(
                    encoding="utf-8"
                ).splitlines()

                for line in lines:

                    line = line.strip()

                    # Пустые строки не копируются.
                    if not line:
                        continue

                    out.write(
                        line + "\n"
                    )

                    added += 1

        console_line(
            f"В last_facets.txt добавлено: {added}"
        )

    def _save_judges_raw(self):
        """
        Сохраняет сырые ответы AI-судей
        в output/judges_raw.json.

        В файл записываются результаты всех судей,
        собранные во время первого и последующих этапов
        исследования.

        Сырые ответы сохраняются отдельно от результатов
        голосования, чтобы их можно было использовать
        для последующего анализа работы моделей,
        проверки голосования и поиска ошибок.

        Формат файла:
            JSON-массив self.judges_raw.

        Существующий файл полностью перезаписывается.
        """
        path = PROJECT_ROOT / "data" / "output" / "judges_raw.json"

        # Папка output создаётся, если её нет.
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        # ensure_ascii=False — кириллица сохраняется
        # в читаемом виде.
        path.write_text(
            json.dumps(
                self.judges_raw,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    def print_judge_stats(self):
        """
        Выводит статистику по ответам AI-судей.

        Считается по self.judges_raw, собранному
        за текущий запуск. Данные не требуют новых
        запросов — используется только то,
        что уже сохранено в памяти.

        Метрики:

        1. По этапам:
           • всего ответов судей;
           • ответов с валидным JSON (и доля);
           • ответов с невалидным JSON;
           • полнота ответа — средняя доля возвращённых
             элементов от числа переданных слов
             (для валидных ответов).

        2. По моделям:
           • сколько ответов дала каждая модель;
           • доля валидного JSON по модели.

        Статистика позволяет сравнивать прогоны
        до и после изменения промтов: при тех же
        стартовых словах и том же пуле моделей
        рост доли валидного JSON и полноты говорит
        о том, что промт стал понятнее.
        """

        if not self.judges_raw:
            console_line(
                "Нет данных об ответах судей."
            )
            return

        # ===================================================
        # 1. Разбивка по этапам
        # ===================================================

        console_line()
        console_line("=== Статистика ответов AI ===")

        by_stage = {}

        for entry in self.judges_raw:
            by_stage.setdefault(
                entry.get("stage", "?"),
                []
            ).append(entry)

        for stage in sorted(by_stage):
            entries = by_stage[stage]

            total = len(entries)
            valid = 0
            invalid = 0
            completeness = []

            for entry in entries:
                content = entry.get("answer", "")

                if self._is_valid_json(content):
                    valid += 1
                    n_items = self._json_item_count(content)
                    n_words = len(entry.get("words") or [])

                    if n_words > 0 and n_items is not None:
                        completeness.append(
                            min(1.0, n_items / n_words)
                        )
                else:
                    invalid += 1

            valid_pct = (
                valid * 100 / total
                if total
                else 0
            )

            avg_completeness = (
                sum(completeness) / len(completeness)
                if completeness
                else 0
            )

            console_line(
                f"Этап {stage}: ответов {total} | "
                f"JSON валиден {valid} ({valid_pct:.0f}%) | "
                f"невалиден {invalid} | "
                f"полнота {avg_completeness:.0%}"
            )

        # ===================================================
        # 2. Разбивка по моделям
        # ===================================================

        by_model = {}

        for entry in self.judges_raw:
            model = entry.get("model", "?")
            by_model.setdefault(
                model,
                {"total": 0, "valid": 0}
            )

            by_model[model]["total"] += 1

            if self._is_valid_json(
                    entry.get("answer", "")
            ):
                by_model[model]["valid"] += 1

        console_line()
        console_line("Распределение по моделям:")

        for model, stat in sorted(
                by_model.items(),
                key=lambda kv: -kv[1]["total"]
        ):
            total = stat["total"]
            valid = stat["valid"]

            pct = (
                valid * 100 / total
                if total
                else 0
            )

            console_line(
                f"  {model}: {total} ответов | "
                f"JSON валиден {valid} ({pct:.0f}%)"
            )

    def _is_valid_json(self, content):
        """
        Проверяет, является ли ответ судьи валидным JSON.

        Ответ очищается от Markdown-обертки через
        _clean_json(), после чего пробуется json.loads().

        Параметры:
            content — текст ответа AI.

        Возвращает:
            True — ответ разбирается как JSON;
            False — ответ пустой или не является JSON.
        """

        if not content or not content.strip():
            return False

        try:
            json.loads(self._clean_json(content))
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    def _json_item_count(self, content):
        """
        Определяет количество элементов в JSON-ответе судьи.

        Если ответ — JSON-массив, возвращается длина массива.
        Если ответ — JSON-объект, возвращается 1.
        Иначе — None.

        Параметры:
            content — текст ответа AI.

        Возвращает:
            int  — число элементов;
            None — ответ не является JSON.
        """

        try:
            result = json.loads(
                self._clean_json(content)
            )
        except (json.JSONDecodeError, ValueError):
            return None

        if isinstance(result, list):
            return len(result)

        if isinstance(result, dict):
            return 1

        return None

    # ===================================================
    # РАБОТА С ПОДТВЕРЖДЕННЫМИ РЕЗУЛЬТАТАМИ
    # ===================================================
    def promote(self, input_file, output_file, threshold=3):
        """
        Каскадно переносит подтвержденные результаты
        из одного уровня в следующий.

        Формат строки:

            название фасета<TAB>слово

        Для каждой одинаковой строки подсчитывается
        количество накопленных подтверждений в текущем уровне.

        Каждые threshold подтверждений дают
        одну запись в следующем уровне:

            count // threshold

        Остаток подтверждений:
            count % threshold
        остается в текущем уровне.

        Следующий уровень является накопительным:
        новые подтверждения добавляются к уже существующим
        результатам предыдущих запусков.

        Пример при threshold=3:

            5 подтверждений
                ↓
            1 запись в следующий уровень
            2 подтверждения остаются текущем уровне

        Таким образом, подтверждения проходят
        через каскад уровней постепенно.
        """

        input_path = Path(input_file)
        output_path = Path(output_file)

        if not input_path.exists():
            return

        lines = input_path.read_text(
            encoding="utf-8"
        ).splitlines()

        console_line(
            f"{input_file} строк: {len(lines)}"
        )

        # ---------------------------------------------------
        # Оставляем только строки вида:
        # Название фасета<TAB>слово
        # ---------------------------------------------------

        lines = [
            line.strip()
            for line in lines
            if "\t" in line
            and line.strip()
        ]

        if not lines:
            input_path.write_text(
                "",
                encoding="utf-8"
            )
            return

        # ===================================================
        # Подсчет подтверждений текущего уровня
        #
        # Одинаковые строки считаются одинаковыми
        # подтверждениями одного и того же фасета.
        # ===================================================

        counts = Counter(lines)
        console_line(
            f"Уникальных строк: {len(counts)}"
        )

        repeated = [
            (line, count)
            for line, count in counts.items()
            if count > 1
        ]

        console_line(
            f"Повторяющихся строк: {len(repeated)}"
        )

        # ===================================================
        # Перенос подтвержденных результатов
        #
        # Каждая строка обрабатывается отдельно:
        #
        #     promoted  = count // threshold — сколько записей
        #                 переходит на следующий уровень;
        #     remainder = count % threshold  — сколько остаётся
        #                 на текущем уровне.
        # ===================================================

        remain = []
        added = 0

        with output_path.open(
                "a",
                encoding="utf-8"
        ) as out:

            for line, count in counts.items():

                promoted = count // threshold
                remainder = count % threshold

                # ---------------------------------------------------
                # Подтвержденные результаты
                # ---------------------------------------------------

                if promoted:

                    for _ in range(promoted):
                        out.write(line + "\n")

                    added += promoted

                # ---------------------------------------------------
                # Накопленный остаток текущего уровня
                # ---------------------------------------------------

                if remainder:

                    remain.extend(
                        [line] * remainder
                    )

        # ===================================================
        # Сохранение остатка текущего уровня
        # ===================================================

        if remain:
            input_path.write_text(
                "\n".join(remain) + "\n",
                encoding="utf-8"
            )
        else:
            input_path.write_text(
                "",
                encoding="utf-8"
            )

        console_line(
            f"remain = {len(remain)}"
        )

        console_line(
            f"{input_file} -> {output_file}: "
            f"добавлено {added}"
        )
    # ---------------------------------------------------
    # Импорт подтвержденных результатов в базу
    # ---------------------------------------------------
    def import_level(self, filename):
        """
        Импортирует подтвержденные слова из level1.txt
        в соответствующие подгруппы.

        Формат строки:

            группа<TAB>подгруппа<TAB>слово

        Слово добавляется именно в подгруппу.
        Связь подгруппы с родительской группой
        уже существует через groups.parent_id.

        Строки, которые не удалось импортировать
        (нет группы, подгруппы или слова в базе),
        считаются некорректными ответами моделей
        и отбрасываются. После обработки файл
        level1.txt очищается полностью.
        """

        path = Path(filename)

        if not path.exists():
            return 0

        imported = 0
        skipped = 0

        # ===================================================
        # Каждая строка обрабатывается независимо.
        #
        # Если строку не удалось импортировать
        # (нет группы, подгруппы или слова в базе),
        # она считается кривым ответом моделей
        # и не сохраняется для следующего запуска.
        # ===================================================

        for line in path.read_text(
                encoding="utf-8"
        ).splitlines():

            line = line.strip()

            if not line:
                continue

            try:
                group_name, podgroup_name, word = (
                    line.split("\t", 2)
                )
            except ValueError:
                skipped += 1
                continue

            # ===================================================
            # 1. Найти родительскую группу
            # ===================================================

            self.cursor.execute("""
                                SELECT id
                                FROM groups
                                WHERE name = ?
                                  AND parent_id IS NULL
                                """, (
                                    group_name,
                                ))

            row = self.cursor.fetchone()

            if not row:
                skipped += 1
                continue

            group_id = row[0]

            # ===================================================
            # 2. Найти подгруппу внутри этой группы
            # ===================================================

            self.cursor.execute("""
                                SELECT id
                                FROM groups
                                WHERE name = ?
                                  AND parent_id = ?
                                """, (
                                    podgroup_name,
                                    group_id
                                ))

            row = self.cursor.fetchone()

            if not row:
                skipped += 1
                continue

            podgroup_id = row[0]

            # ===================================================
            # 3. Найти слово
            # ===================================================

            self.cursor.execute("""
                                SELECT id
                                FROM words
                                WHERE word = ?
                                """, (
                                    word,
                                ))

            row = self.cursor.fetchone()

            if not row:
                skipped += 1
                continue

            word_id = row[0]

            # ===================================================
            # 4. Добавить слово именно в подгруппу
            # ===================================================

            self.cursor.execute("""
                                INSERT OR IGNORE INTO group_words
                                (group_id,
                                 word_id)
                                VALUES (?, ?)
                                """, (
                                    podgroup_id,
                                    word_id
                                ))

            imported += 1

        self.conn.commit()

        # ===================================================
        # 5. Очистка файла
        #
        # Неимпортированные строки отбрасываются,
        # поэтому файл полностью очищается.
        # ===================================================

        path.write_text(
            "",
            encoding="utf-8"
        )

        console_line(
            f"импортировано: {imported} | "
            f"отброшено кривых: {skipped}"
        )

        return imported
    # ---------------------------------------------------
    # Пересоздание кеша для уже определенных слов
    # ---------------------------------------------------
    def rebuild_auto_words_cache(self):
        """
        Полностью пересоздает output/auto_words.pkl
        по актуальному состоянию базы.

        Слово может находиться в подгруппе.
        В auto_words сохраняется:

            слово → родительская тематическая группа

        Подгруппа определяется через groups.parent_id.
        """

        cache_file = Path(
            "output/auto_words.pkl"
        )

        auto_words = {}

        placeholders = ",".join(
            "?" for _ in POS_GROUP_IDS
        )

        self.cursor.execute(f"""
                            SELECT w.word,
                                   parent.name
                            FROM group_words gw

                                     JOIN words w
                                          ON w.id = gw.word_id

                                     JOIN groups subgroup
                                          ON subgroup.id = gw.group_id

                                     JOIN groups parent
                                          ON parent.id = subgroup.parent_id

                            WHERE parent.type = ?
                              AND subgroup.id NOT IN ({placeholders})
                            """, (
                                GROUP_TYPE_NORMAL,
                            ) + POS_GROUP_IDS)

        rows = self.cursor.fetchall()

        for word, group_name in rows:
            auto_words[word] = group_name

        # ---------------------------------------------------
        # Перезаписываем кэш
        # ---------------------------------------------------

        with cache_file.open(
                "wb"
        ) as f:
            pickle.dump(
                auto_words,
                f,
                protocol=pickle.HIGHEST_PROTOCOL
            )

        # ---------------------------------------------------
        # Обновляем кэш в памяти
        # ---------------------------------------------------

        self.auto_words = auto_words

        console_line(
            f"auto_words.pkl пересоздан: "
            f"{len(auto_words)} записей"
        )

    # ===================================================
    # СЛУЖЕБНЫЕ ФУНКЦИИ
    # ===================================================
    # ---------------------------------------------------
    # Загрузка подгрупп для использвании в промте определения слова ИИ
    # ------------------------------------------------
    def get_podgroups(self, group_name):
        """
        Возвращает список подгрупп указанной верхней группы.

        Данные берутся из self.auto_podgroup — структуры,
        загруженной методом load_auto_podgroups().

        Параметры:
            group_name — название верхней группы
                         (ключ словаря self.auto_podgroup).

        Возвращает:
            список названий подгрупп данной группы;
            [] — если группа неизвестна.
        """

        return self.auto_podgroup.get(
            group_name,
            []
        )
    # ---------------------------------------------------
    # Получение слова по ID
    # ---------------------------------------------------
    def get_word(self, word_id):
        """
        Возвращает текстовое представление слова
        по его внутреннему ID.

        Параметры:
            word_id — внутренний ID слова.

        Возвращает:
            str  — слово;
            None — если слово отсутствует в базе.
        """

        row = self._find_word(word_id=word_id)
        return row[0] if row else None
    # ---------------------------------------------------
    # Получение информации о слове
    # ---------------------------------------------------
    def get_word_info(self, word_id):
        """
        Возвращает информацию о слове.

        Использует внутренний кэш,
        чтобы повторно не выполнять один и тот же
        запрос к базе данных.

        Возвращает словарь:

            {
                "word": слово,
                "count": частота
            }

        Если слово отсутствует в базе,
        возвращается информация с указанным ID
        и нулевой частотой.
        """
        # Сначала проверяем кэш.
        if word_id in self.word_info_cache:
            return self.word_info_cache[word_id]

        cursor = self.conn.cursor()
        cursor.execute("""
                       SELECT word, count
                       FROM words
                       WHERE id = ?
                       """, (word_id,))

        row = cursor.fetchone()

        if row:
            info = {
                "word": row[0],
                "count": row[1]
            }
        else:
            info = {
                "word": str(word_id),
                "count": 0
            }

        self.word_info_cache[word_id] = info

        return info

    # ---------------------------------------------------
    # Цветной вывод
    # ---------------------------------------------------
    def color(self, text, color=Fore.RED):
        """
        Выделяет текст цветом при выводе в консоль.

        Оборачивает текст в управляющие ANSI-последовательности
        цветового кода и сброса стиля. Цвет действует только
        на указанный текст, после него стиль сбрасывается.

        Параметры:
            text  — текст для выделения.
            color — цвет из colorama (по умолчанию Fore.RED).

        Возвращает:
            строку вида "<цвет><текст><сброс>".
        """
        return f"{color}{text}{Style.RESET_ALL}"
    # ---------------------------------------------------
    # ВЫВОД И ОФОРМЛЕНИЕ
    # ---------------------------------------------------
    def report(self, text="", color=None):
        """
            Выводит сообщение в отчет выполнения программы.

            Если указан цвет, текст предварительно передается
            в self.color() для цветного вывода.

            Параметры:
                text  — текст сообщения.
                color — необязательный цвет вывода.

            Метод используется как единая точка вывода
            информационных сообщений и ошибок исследования.
            """
        if color:
            print(self.color(text, color))
        else:
            print(text)

    # ===================================================
    # ОБЩИЕ МЕТОДЫ AI
    # ===================================================
    def _clean_json(self, text):
        """
        Подготавливает текст ответа AI к разбору JSON.

        Удаляет Markdown-обертку вида ```...```,
        если модель вернула JSON внутри блока кода.

        Затем пытается выделить из ответа:

            1. JSON-массив [...]
            2. JSON-объект {...}

        Если JSON-фрагмент найти не удалось,
        возвращает исходный очищенный текст.

        Возвращает:
            строку, которую можно передать json.loads().
        """

        text = text.strip()
        # ---------------------------------------------------
        # Удаление Markdown-обертки
        # ---------------------------------------------------
        if text.startswith("```"):
            lines = text.splitlines()

            if lines and lines[0].startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]

            text = "\n".join(lines).strip()
        # ---------------------------------------------------
        # Поиск JSON-фрагмента.
        #
        # Сначала ищем массив [...], затем объект {...}.
        # Для каждой пары скобок берётся фрагмент
        # от первой открывающей до последней закрывающей.
        # ---------------------------------------------------
        for open_ch, close_ch in (
                ("[", "]"),
                ("{", "}")
        ):
            p1 = text.find(open_ch)
            p2 = text.rfind(close_ch)

            if p1 != -1 and p2 != -1 and p2 > p1:
                return text[p1:p2 + 1]

        return text

    def _save_bad_answer(self, content):
        """
        Добавляет некорректный ответ AI в bad_json.txt.

        Каждый сохранённый ответ помечается отдельной
        меткой вида "===== A =====". Метки идут
        как номера колонок Excel и никогда не выходят
        за пределы алфавита:

            A, B, ..., Z, AA, ..., AZ, BA, ..., ZZ, AAA, ...

        Файл bad_json.txt создаётся в рабочей папке,
        если его ещё нет.

        Параметры:
            content — текст некорректного ответа AI.
        """

        path = Path("bad_json.txt")

        # ---------------------------------------------------
        # Определение следующей метки по количеству
        # уже сохранённых ответов.
        # ---------------------------------------------------

        if path.exists():
            text = path.read_text(
                encoding="utf8"
            )

            count = text.count("===== ")

        else:
            count = 0

        # ---------------------------------------------------
        # Номер записи переводится в последовательность букв
        # как номера колонок Excel:
        #
        #     0 → A    25 → Z    26 → AA    51 → AZ
        #     52 → BA  77 → BZ   78 → CA    ...
        #
        # Последовательность бесконечная и никогда
        # не выходит за пределы алфавита:
        # после ZZ идёт AAA, а не спецсимвол.
        # ---------------------------------------------------

        n = count + 1
        key = ""

        while n > 0:
            n, rem = divmod(n - 1, 26)
            key = chr(ord("A") + rem) + key

        with path.open(
                "a",
                encoding="utf8"
        ) as f:

            f.write(
                f"\n===== {key} =====\n"
            )

            f.write(content)

            f.write("\n")

####################################################
# ЗАПУСК ПРОГРАММЫ
####################################################
def main():
    """
        Запускает полный цикл исследования.

        Последовательность работы:

        1. Создает HypothesisFinder и подключается к базе данных.
        2. Запускает исследование стартовых слов.
        3. Переносит подтвержденные результаты
           между уровнями через promote().
        4. Импортирует подтвержденные результаты
           в таблицу групп базы данных.
        5. Пересоздает кэш известных слов.
        6. В любом случае закрывает соединение с базой данных.

        Основной сценарий программы выполняется
        внутри блока try/finally, поэтому соединение
        с базой закрывается даже при возникновении ошибки.
        """
    # ---------------------------------------------------
    # Инициализация исследователя
    # ---------------------------------------------------
    db_path = PROJECT_ROOT / "data" / "output" / "semantics.db"
    finder = HypothesisFinder(db_path)

    try:
        # ---------------------------------------------------
        # Основное исследование
        # ---------------------------------------------------
        finder.run_study()
        # ---------------------------------------------------
        # Завершение отчета и подтверждение результатов
        # ---------------------------------------------------

        console_line(
            f"promote {CONFIRM_FILES[0]} -> "
            f"{CONFIRM_FILES[1]}"
        )

        finder.promote(
            CONFIRM_FILES[0],
            CONFIRM_FILES[1]
        )

        imported = finder.import_level(
            CONFIRM_FILES[1]
        )

        console_line(
            f"В группы добавлено: {imported}"
        )

        # ---------------------------------------------------
        # Пересоздаем кэш известных слов
        # ---------------------------------------------------

        finder.rebuild_auto_words_cache()
    finally:
        # ---------------------------------------------------
        # Закрытие ресурсов
        # ---------------------------------------------------
        if finder.conn is not None:
            finder.conn.close()

            console_line()
            console_line("Программа закончила работу штатно")

        # ---------------------------------------------------
        # Статистика по ответам судей
        #
        # Выводится всегда — и при штатном завершении,
        # и если run_study прервался раньше времени.
        # Позволяет сравнивать прогоны после изменения
        # промтов (доля валидного JSON, полнота ответов).
        # ---------------------------------------------------

        finder.print_judge_stats()
        # ---------------------------------------------------
        # Очистка временных AI-файлов НАМЕРЕННО отключена:
        # ai_1_*.txt и ai_2_*.txt используются между
        # запусками для накопления подтверждений.
        # ---------------------------------------------------
        # for file_path in (PROJECT_ROOT / "data" / "output").glob("ai_1_*.txt"):
        #     file_path.unlink(missing_ok=True)
        #
        # for file_path in (PROJECT_ROOT / "data" / "output").glob("ai_2_*.txt"):
        #     file_path.unlink(missing_ok=True)

if __name__ == "__main__":

    main()
    