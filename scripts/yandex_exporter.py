"""
========================================================

WordsToYandexDirect

Построение дерева связанных запросов
для формирования структуры рекламной кампании
Яндекс.Директ.

Алгоритм:

1. Читаем стартовые слова.
2. Для каждого слова запускаем BFS.
3. Находим все вершины до MAX_DEPTH.
4. Строим дерево.
5. Экспортируем дерево в CSV.

========================================================
"""
import sqlite3
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
from collections import defaultdict
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
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
     новую строку.

     Параметры:
         text — текст текущего статуса.
     """
    print(f"\r{text:<80}", end="", flush=True)

def console_line(text=""):
    """
    Выводит отдельную строку в консоль.

    Используется для сообщений, которые должны
    сохраниться в журнале выполнения и не заменяться
    последующими статусами.

    Параметры:
        text — текст сообщения.
    """
    print(text)

class HypothesisFinder:
    # ============================================================
    # Настройки
    # ============================================================

    # ============================================================
    # Пути
    # ============================================================

    OUTPUT_XLSX = "TreeToYandexDirect.xlsx"
    START_WORDS_FILE = "start_words.txt"

    # ============================================================
    # Параметры алгоритма
    # ============================================================

    MAX_DEPTH = 3
    MIN_LINK_WEIGHT = 1000
    INITIAL_WEAKEST = 2147483647

    # ============================================================
    # Инициализация
    # ============================================================
    def __init__(self, db_path):
        """
        Инициализирует анализатор связанных слов.

        Получает:
            db_path — путь к базе данных.

        Создаёт состояние анализатора:

            db_path
                путь к базе данных;

            conn
                соединение с базой данных;

            all_tree_rows
                накопитель полных строк всех деревьев,
                из которого после обработки всех слов
                строится итоговый XLSX.

        После создания состояния подключается
        к базе данных через connect().

        Метод не выполняет поиск и не создаёт
        файл результата.
        """
        self.db_path = db_path
        self.conn = None

        self.connect()

        self.all_tree_rows = []

    def connect(self):
        """
        Подключается к базе данных.

        Создаёт соединение с базой данных,
        указанной при инициализации объекта.

        После подключения создаёт курсор,
        используемый остальными методами анализатора.

        Метод не выполняет поиск и не подготавливает
        конкретный запуск BFS.
        """

        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()

        self._create_search_tables()

    def _create_search_tables(self):
        """
        Создаёт рабочие таблицы BFS.

        Таблицы используются для текущего поиска:

            wave
                текущая волна;

            wave_next
                следующая волна;

            visited
                все найденные вершины.

        Метод вызывается один раз после подключения
        к базе данных.
        """

        self.cursor.executescript("""
                                  CREATE TABLE IF NOT EXISTS wave
                                  (
                                      word_id   INTEGER PRIMARY KEY,
                                      parent_id INTEGER,
                                      depth     INTEGER,
                                      weakest   INTEGER
                                  );

                                  CREATE TABLE IF NOT EXISTS wave_next
                                  (
                                      word_id   INTEGER PRIMARY KEY,
                                      parent_id INTEGER,
                                      depth     INTEGER,
                                      weakest   INTEGER
                                  );

                                  CREATE TABLE IF NOT EXISTS visited
                                  (
                                      word_id   INTEGER PRIMARY KEY,
                                      parent_id INTEGER,
                                      depth     INTEGER,
                                      weakest   INTEGER
                                  );
                                  """)

        self.conn.commit()
    # ============================================================
    # Запуск программы
    # ============================================================
    def run(self, start_word=None):
        """
        Запускает обработку одного стартового слова.

        Получает:
            start_word — слово, с которого начинается поиск.

        Выполняет:
            1. Находит идентификатор стартового слова в базе.
            2. Проверяет, что слово существует.
            3. Передаёт найденный идентификатор
               в process_start_word().

        Если слово отсутствует в базе,
        обработка этого стартового слова прекращается.

        Сам поиск связанных слов метод не выполняет.
        Он является точкой входа для обработки
        одного стартового слова.
        """
        if start_word is not None:

            start_word = start_word.strip()

            start = self.find_word_id(start_word)

            if start is None:
                print(f"Слово '{start_word}' не найдено.")
                return

            console_line(
                f"Стартовое слово: {start_word} "
                f"(id: {start})"
            )

            self.process_start_word(start)
            return
    # ============================================================
    # Обработка стартового слова
    # ============================================================
    def _reset_search(self):
        """
        Очищает рабочие таблицы перед новым поиском.
        """

        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("DELETE FROM wave_next")
        self.cursor.execute("DELETE FROM visited")

        self.conn.commit()

    def _run_bfs(self):
        """
        Выполняет поиск связанных слов методом BFS.

        Последовательно строит волны поиска от первой
        до максимальной глубины MAX_DEPTH.

        После каждой волны обновляет отображаемый
        прогресс и количество найденных вершин.

        Возвращает:
            количество найденных вершин в таблице visited.
        """

        for depth in range(1, self.MAX_DEPTH + 1):

            new_rows = self.build_next_wave()

            rows = self.cursor.execute("""
                                       SELECT COUNT(*)
                                       FROM visited
                                       """).fetchone()[0]

            console_status(
                f"Поиск связанных слов: "
                f"уровень {depth}/{self.MAX_DEPTH} | "
                f"найдено: {rows:,}"
                .replace(",", " ")
            )

            if new_rows == 0:
                break

        print()

        rows = self.cursor.execute("""
                                   SELECT COUNT(*)
                                   FROM visited
                                   """).fetchone()[0]

        console_line(
            f"Поиск завершён: найдено {rows:,} слов"
            .replace(",", " ")
        )

        return rows

    def _initialize_start_node(self, start):
        """
        Добавляет стартовое слово в первую волну BFS.

        Стартовая вершина является собственным родителем,
        имеет глубину 0 и начальное значение weakest.

        Параметры:
            start — идентификатор стартового слова.
        """

        self.cursor.execute("""
                            INSERT INTO wave
                            (word_id,
                             parent_id,
                             depth,
                             weakest)
                            VALUES (?, ?, ?, ?)
                            """, (
                                start,
                                start,
                                0,
                                self.INITIAL_WEAKEST
                            ))

    def process_start_word(self, start):
        """
            Обрабатывает одно стартовое слово.

            Получает:
                start — идентификатор стартового слова
                        в таблице words.

            Выполняет:
                1. Очищает рабочие таблицы BFS.
                2. Добавляет стартовую вершину в первую волну.
                3. Последовательно строит волны поиска
                   до максимальной глубины MAX_DEPTH.
                4. Сохраняет найденные вершины в таблице visited.
                5. Передаёт найденное дерево в export_tree()
                   для формирования результата.

            Во время поиска для каждой вершины сохраняется:
                - родительская вершина;
                - глубина;
                - минимальный вес связи на всём пути
                  от стартового слова.

            Метод не ищет идентификатор слова по его названию.
            Идентификатор должен быть получен заранее
            методом find_word_id().
            """

        # -------------------------------------------------
        # Подготовка рабочей области
        # -------------------------------------------------
        self._reset_search()

        # -------------------------------------------------
        # Добавляем стартовую вершину
        # -------------------------------------------------
        self._initialize_start_node(start)

        self._run_bfs()

        # -------------------------------------------------
        # Экспорт результата
        # -------------------------------------------------
        console_status(
            "Формирование дерева..."
        )
        self.export_tree(start)
    # ============================================================
    # Поиск связанных слов
    # ============================================================
    def build_next_wave(self):
        """
        Строит следующую волну BFS.

        Если вершина достигается несколькими путями в текущей
        волне, выбирается путь с максимальным weakest.
        """
        self.cursor.execute("DELETE FROM wave_next")

        self.cursor.execute("""
            INSERT INTO wave_next
            (
                word_id,
                parent_id,
                depth,
                weakest
            )
            SELECT
                word_id,
                parent_id,
                depth,
                weakest
            FROM (
                SELECT
                    l.word2_id AS word_id,
                    w.word_id AS parent_id,
                    w.depth + 1 AS depth,
                    CASE
                        WHEN w.weakest < l.count
                            THEN w.weakest
                        ELSE l.count
                    END AS weakest,
                    ROW_NUMBER() OVER (
                        PARTITION BY l.word2_id
                        ORDER BY
                            CASE
                                WHEN w.weakest < l.count
                                    THEN w.weakest
                                ELSE l.count
                            END DESC,
                            w.word_id ASC
                    ) AS rn
                FROM wave w
                JOIN links l
                    ON l.word1_id = w.word_id
                LEFT JOIN visited v
                    ON v.word_id = l.word2_id
                WHERE l.count >= ?
                  AND v.word_id IS NULL
            )
            WHERE rn = 1
        """, (self.MIN_LINK_WEIGHT,))

        rows = self.cursor.execute(
            "SELECT COUNT(*) FROM wave_next"
        ).fetchone()[0]

        self.cursor.execute("""
            INSERT INTO visited
            SELECT *
            FROM wave_next
        """)

        self.cursor.execute("DELETE FROM wave")

        self.cursor.execute("""
            INSERT INTO wave
            (
                word_id,
                parent_id,
                depth,
                weakest
            )
            SELECT
                word_id,
                parent_id,
                depth,
                weakest
            FROM wave_next
            ORDER BY weakest DESC, word_id ASC
        """)

        return rows

    def _prepare_tree_rows(self):
        """
        Формирует рабочие записи дерева одним SQL-запросом.
        """
        rows = self.cursor.execute("""
            SELECT
                v.word_id,
                v.parent_id,
                v.depth,
                v.weakest,
                w.word AS word,
                COALESCE(GROUP_CONCAT(DISTINCT g.name), '') AS group_name
            FROM visited v
            JOIN words w
                ON w.id = v.word_id
            LEFT JOIN group_words gw
                ON gw.word_id = v.word_id
            LEFT JOIN groups g
                ON g.id = gw.group_id
               AND (
                    g.parent_id IS NULL
                    OR g.parent_id != 1
               )
            GROUP BY
                v.word_id,
                v.parent_id,
                v.depth,
                v.weakest,
                w.word
            ORDER BY
                v.depth,
                v.weakest DESC
        """).fetchall()

        return [
            {
                "word_id": word_id,
                "parent_id": parent_id,
                "depth": depth,
                "weakest": weakest,
                "word": word,
                "group": group_name
            }
            for (
                word_id,
                parent_id,
                depth,
                weakest,
                word,
                group_name
            ) in rows
        ]

    # ============================================================
    # Формирование результата
    # ============================================================
    def export_tree(self, start):
        """
        Формирует CSV и XLSX одним проходом по дереву.

        XLSX больше не пересоздаётся из CSV, поэтому
        outlineLevel и сворачивание сохраняются.

        Здесь дерево только обходится: плоские строки
        накапливаются в self.all_tree_rows, а единая
        книга со всеми листами строится позже
        методом build_workbook().
        """
        tree_rows = self._prepare_tree_rows()

        tree = self.build_parent_index(tree_rows)
        start_word = self.get_word(start)

        flat_rows = []

        self.walk_tree(
            tree,
            start,
            0,
            [],
            start_word,
            self.MAX_DEPTH,
            flat_rows
        )

        self.all_tree_rows.extend(flat_rows)

        console_line(
            f"Дерево сформировано: "
            f"{len(flat_rows):,} записей"
            .replace(",", " ")
        )

    def _make_header(self, max_depth):
        """
        Формирует заголовок таблицы дерева.
        """
        header = ["Стартовое слово"]

        header.extend(
            f"Уровень {level}"
            for level in range(1, max_depth + 1)
        )

        header.append("Группа")

        return header

    def _fill_worksheet(self, worksheet, flat_rows, max_depth, hide_prefix=False):
        """
        Заполняет лист плоскими строками дерева.

        flat_rows — список записей с ключом "row",
        содержащим готовую строку для записи.

        При hide_prefix=True совпадающее с предыдущей
        строкой начало скрывается (используется на
        листах отдельных слов для компактного вида).
        """
        header = self._make_header(max_depth)

        worksheet.append(header)

        prev_row = []

        for flat_row in flat_rows:

            row = flat_row["row"]

            if hide_prefix:
                out = row.copy()

                i = 0

                while (
                        i < len(out)
                        and i < len(prev_row)
                        and out[i] == prev_row[i]
                ):
                    out[i] = ""
                    i += 1

                prev_row = row.copy()
            else:
                out = row

            worksheet.append(out)

        if worksheet.max_row >= 1:
            worksheet.auto_filter.ref = worksheet.dimensions

        worksheet.freeze_panes = "A2"

        for column_cells in worksheet.columns:
            max_length = 0

            for cell in column_cells:
                value = (
                    ""
                    if cell.value is None
                    else str(cell.value)
                )
                max_length = max(max_length, len(value))

            column_letter = get_column_letter(
                column_cells[0].column
            )
            worksheet.column_dimensions[
                column_letter
            ].width = min(max_length + 2, 60)

    def _fill_outline(self, worksheet, flat_rows):
        """
        Создаёт в листе сворачиваемую структуру
        по уровням дерева.

        Уровень каждой строки берётся из записи
        ("level"), а не вычисляется по содержимому.

        Включает отображение контуров, чтобы в Excel
        появлялись кнопки сворачивания строк.
        """
        worksheet.sheet_properties.outlinePr.summaryBelow = True
        worksheet.sheet_properties.outlinePr.summaryRight = True
        worksheet.sheet_properties.outlinePr.showOutlineSymbols = True
        worksheet.sheet_view.showOutlineSymbols = True

        for excel_row, flat_row in enumerate(
                flat_rows,
                start=2
        ):
            level = flat_row["level"]

            worksheet.row_dimensions[
                excel_row
            ].outlineLevel = level

            if level > 0:
                worksheet.row_dimensions.group(
                    excel_row,
                    excel_row,
                    outline_level=level,
                    hidden=False
                )

        max_outline_level = max(
            (
                dimension.outlineLevel
                for dimension in worksheet.row_dimensions.values()
            ),
            default=0
        )

        worksheet.sheet_format.outlineLevelRow = max_outline_level

        return max_outline_level

    def _sort_global(self):
        """
        Сортирует строки сводного листа по общим правилам:

            1. группа;
            2. стартовое слово;
            3. уровень 1;
            4. уровень 2;
            5. уровень 3.
        """
        return sorted(
            self.all_tree_rows,
            key=lambda entry: (
                entry["row"][-1],
                entry["row"][0],
                entry["row"][1],
                entry["row"][2],
                entry["row"][3]
            )
        )

    def build_workbook(self):
        """
        Строит единый XLSX-файл со всеми деревьями.

        Первый лист «Все значения» содержит плоские
        строки всех стартовых слов подряд. Каждому
        стартовому слову соответствует отдельный лист
        с его деревом.

        Возвращает:
            максимальный уровень сворачивания (для отчёта).
        """
        workbook = Workbook()
        workbook.remove(workbook.active)

        worksheet = workbook.create_sheet("Все значения")
        sorted_rows = self._sort_global()
        self._fill_worksheet(worksheet, sorted_rows, self.MAX_DEPTH)

        max_outline_level = self._fill_outline(
            worksheet,
            sorted_rows
        )

        for start_word in self._ordered_start_words():

            flat_rows = [
                row
                for row in self.all_tree_rows
                if row["start_word"] == start_word
            ]

            if not flat_rows:
                continue

            worksheet = workbook.create_sheet(start_word[:31])
            self._fill_worksheet(
                worksheet,
                flat_rows,
                self.MAX_DEPTH,
                hide_prefix=True
            )

            max_outline_level = max(
                max_outline_level,
                self._fill_outline(worksheet, flat_rows)
            )

        workbook.save(self.OUTPUT_XLSX)

        return max_outline_level

    def _ordered_start_words(self):
        """
        Возвращает уникальные стартовые слова
        в порядке их появления в self.all_tree_rows.
        """
        seen = []
        seen_set = set()

        for row in self.all_tree_rows:
            if row["start_word"] not in seen_set:
                seen_set.add(row["start_word"])
                seen.append(row["start_word"])

        return seen

    def build_parent_index(self, csv_rows):
        """
        Строит индекс дерева по родительским вершинам.

        Получает:
            csv_rows — временный список найденных вершин,
                       подготовленный методом export_tree().

        Формирует структуру:

            parent_id → список дочерних вершин

        Возвращает:
            defaultdict(list) с дочерними вершинами
            для каждого parent_id.

        Метод не изменяет базу данных.
        """
        tree = defaultdict(list)

        for row in csv_rows:
            tree[row["parent_id"]].append({
                "word_id": row["word_id"],
                "parent_id": row["parent_id"],
                "depth": row["depth"],
                "weakest": row["weakest"],
                "word": row["word"],
                "group": row["group"]
            })

        for children in tree.values():
            children.sort(
                key=lambda child: (
                    child["group"].lower(),
                    child["weakest"],
                    child["word"]
                )
            )

        return tree

    def _build_tree_row(self, row, child, level):
        """
        Формирует полный путь дерева для текущей вершины.

        Получает:
            row   — путь от стартового слова до родителя;
            child — текущая дочерняя вершина;
            level — уровень текущей вершины.

        Возвращает:
            список значений с добавленным на текущем
            уровне словом child.
        """

        new_row = row.copy()

        while len(new_row) <= level:
            new_row.append("")

        new_row[level] = child["word"]

        return new_row

    def walk_tree(
            self,
            tree,
            parent_id,
            level,
            row,
            start_word,
            max_depth,
            flat_rows
    ):
        """
           Рекурсивно обходит дерево от указанной вершины.

           Для каждой дочерней вершины:

               1. Формирует полный путь дерева.
               2. Добавляет в flat_rows запись с полной
                  строкой и уровнем вершины.
               3. Продолжает обход дочерних вершин.

           Параметры:
               tree   — индекс parent_id → дочерние вершины;
               parent_id — текущая родительская вершина;
               level  — текущий уровень дерева;
               row    — полный путь до текущей вершины;
               start_word — стартовое слово дерева;
               max_depth — максимальная глубина дерева;
               flat_rows — список, куда накапливаются
                           полные строки с уровнем.
           """
        # -------------------------------------------------
        # Получаем дочерние вершины
        # -------------------------------------------------
        children = tree.get(parent_id, [])

        # -------------------------------------------------
        # Обходим все дочерние вершины
        # -------------------------------------------------
        for child in children:

            new_row = self._build_tree_row(
                row,
                child,
                level
            )

            out = new_row.copy()

            out.insert(
                0,
                start_word
            )

            # -------------------------------------------------
            # Дополняем строку до максимальной глубины дерева
            # -------------------------------------------------
            while len(out) < max_depth + 1:
                out.append("")

            # -------------------------------------------------
            # Группа всегда является последней колонкой
            # -------------------------------------------------
            out.append(
                child["group"]
            )

            flat_rows.append({
                "start_word": start_word,
                "level": level,
                "row": out
            })

            # -------------------------------------------------
            # Переходим к следующему уровню дерева
            # -------------------------------------------------
            self.walk_tree(
                tree,
                child["word_id"],
                level + 1,
                new_row,
                start_word,
                max_depth,
                flat_rows
            )

    # ============================================================
    # Работа со словами
    # ============================================================
    def find_word_id(self, word):
        """
            Находит идентификатор слова в таблице words.

            Получает:
                word — точное написание слова,
                       которое необходимо найти.

            Выполняет:
                1. Ищет слово в таблице words.
                2. Получает его идентификатор.
                3. Возвращает найденный идентификатор.

            Возвращает:
                int — идентификатор слова, если слово найдено;
                None — если такого слова нет в базе.

            Метод не изменяет базу данных.
            """

        self.cursor.execute(
            """
            SELECT id
            FROM words
            WHERE word = ?
            """,
            (word,)
        )

        row = self.cursor.fetchone()

        if row is None:
            return None

        return row[0]

    def get_word(self, word_id):
        """
            Возвращает текст слова по его идентификатору.

            Получает:
                word_id — идентификатор слова в таблице words.

            Выполняет:
                Ищет запись с указанным идентификатором
                и извлекает значение поля word.

            Возвращает:
                str — текст слова, если запись найдена;
                None — если слова с таким идентификатором нет.

            Метод используется там, где алгоритм уже работает
            с идентификаторами слов, но для вывода необходимо
            получить их текстовое представление.

            Метод не изменяет базу данных.
            """
        self.cursor.execute(
            "SELECT word FROM words WHERE id = ?",
            (word_id,)
        )

        row = self.cursor.fetchone()

        return row[0] if row else None

def main():
    # -------------------------------------------------
    # Открываем базу данных
    # -------------------------------------------------
    DB_PATH = PROJECT_ROOT / "data" / "output" / "semantics.db"

    finder = HypothesisFinder(DB_PATH)

    # -------------------------------------------------
    # Загружаем стартовые слова
    # -------------------------------------------------
    start_words = [
        line.strip()
        for line in Path(finder.START_WORDS_FILE).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]

    try:
        # -------------------------------------------------
        # Обрабатываем каждое стартовое слово
        # -------------------------------------------------
        for word in start_words:
            try:
                finder.run(word)
            except Exception as exc:
                console_line(
                    f"Ошибка при обработке слова '{word}': {exc}"
                )

        # -------------------------------------------------
        # Строим единый XLSX со всеми деревьями
        # -------------------------------------------------
        max_outline_level = finder.build_workbook()

        print()

        console_line(
            f"Максимальный outlineLevel по строкам: "
            f"{max_outline_level}"
        )
        console_line(
            f"XLSX создан: {finder.OUTPUT_XLSX}"
        )
        console_line(
            f"Всего записей: "
            f"{len(finder.all_tree_rows):,}"
            .replace(",", " ")
        )

    # -------------------------------------------------
    # Закрываем файлы и соединение с базой
    # -------------------------------------------------
    finally:

        if finder.conn is not None:
            finder.conn.close()

            print()

            print("Соединение с базой закрыто.")

if __name__ == "__main__":

    main()
