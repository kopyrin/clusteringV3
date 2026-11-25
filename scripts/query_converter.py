from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sqlite3
import json
import pymorphy3
import requests
from urllib.parse import quote_plus

DATABASE_FILE = PROJECT_ROOT / "data" / "output" / "semantics.db"

morph = pymorphy3.MorphAnalyzer()

POS_RU = {
    "NOUN": "существительное",
    "ADJF": "прилагательное",
    "ADJS": "краткое прилагательное",
    "COMP": "сравнительная степень",
    "VERB": "глагол",
    "INFN": "инфинитив",
    "PRTF": "причастие",
    "PRTS": "краткое причастие",
    "GRND": "деепричастие",
    "NUMR": "числительное",
    "ADVB": "наречие",
    "NPRO": "местоимение",
    "PRED": "предикатив",
    "PREP": "предлог",
    "CONJ": "союз",
    "PRCL": "частица",
    "INTJ": "междометие",
}

DEBUG = True
# -------------------------------------------------
# работа со словарями минус слов
# -------------------------------------------------
DICT_CODES = {
    # служебные
    -1:  {"file": "stop.txt",          "exclude": True},

    # именованные сущности
    -3:  {"file": "brands.txt",        "exclude": False},
    -4:  {"file": "geo.txt",           "exclude": False},
    -5:  {"file": "marketplaces.txt",  "exclude": False},

    # свойства товара
    -6:  {"file": "colors.txt",        "exclude": False},
    -7:  {"file": "materials.txt",     "exclude": False},
    -8:  {"file": "sizes.txt",         "exclude": False},

    # типы запросов
    -9:  {"file": "questions.txt",     "exclude": False},
    -10: {"file": "codes.txt",         "exclude": False},

    # категории товаров
    -11: {"file": "products.txt"},   # шкаф, диван, стол...
    -12: {"file": "appliances.txt",    "exclude": False},   # духовой, сушильный...

    # назначение
    -13: {"file": "purpose.txt",       "exclude": False},   # кухонный, медицинский...

    # конструкция
    -14: {"file": "construction.txt",  "exclude": False},   # купе, угловой...

    # характеристики
    -15: {"file": "features.txt",      "exclude": False},   # металлический, стеклянный...

    # действия
    -16: {"file": "actions.txt",       "exclude": False},   # купить, собрать...

    # информационные запросы
    -17: {"file": "content.txt",       "exclude": False},   # инструкция, фото...

    # коммерция
    -18: {"file": "commerce.txt",      "exclude": False},   # недорого, бу...

    # помещения
    -19: {"file": "rooms.txt",         "exclude": False},   # кухня, прихожая...

    # стиль / дизайн
    -20: {"file": "styles.txt",        "exclude": False},   # лофт, современный...

    # полисемантические слова хз куда
    -21: {"file": "polysemy.txt",      "exclude": False},

    # медиа
    -22: {
        "file": "media.txt",
        "exclude": False
    }
}

# -------------------------------------------------
# программа переноса слов в словари минус слов
# -------------------------------------------------
def cmd_dict_update(filename):

    with open(filename, encoding="utf-8") as f:
        data = json.load(f)

    result = {}

    for cfg in DICT_CODES.values():
        result[cfg["file"]] = set()

    def walk(node):

        if not isinstance(node, dict):
            return

        for word, value in node.items():

            if isinstance(value, dict):
                walk(value)

            elif isinstance(value, int) and value < 0:

                cfg = DICT_CODES.get(value)

                if cfg:
                    result[cfg["file"]].add(word)

    walk(data)

    Path("dict").mkdir(exist_ok=True)

    for file, words in result.items():

        path = Path("dict") / file

        old = set()

        if path.exists():

            with path.open(encoding="utf-8") as f:

                for line in f:

                    line = line.strip()

                    if line:
                        old.add(line)

        words |= old

        with path.open("w", encoding="utf-8") as f:

            for word in sorted(words):
                f.write(word + "\n")

        print(file, len(words))

# -------------------------------------------------
# Database
# -------------------------------------------------
class Database:

    def __init__(self, filename):

        self.connection = sqlite3.connect(Path(filename))
        self.connection.row_factory = sqlite3.Row

        self.exclude = set()

        self.words = Words(self)
        self.links = Links(self)
        self.lemmas = Lemmas(self)

    def query(self, sql, *params):

        cursor = self.connection.execute(sql, params)

        return cursor.fetchall()

    def query_one(self, sql, *params):

        cursor = self.connection.execute(sql, params)

        return cursor.fetchone()

    def close(self):

        self.connection.close()

def load_exclude_words():

    exclude = set()
    dict_dir = Path("dict")

    if not dict_dir.exists():
        return exclude

    for cfg in DICT_CODES.values():

        if not cfg.get("exclude", False):
            continue

        path = dict_dir / cfg["file"]

        if not path.exists():
            continue

        with path.open(encoding="utf-8") as f:
            for line in f:
                word = line.strip()
                if word:
                    exclude.add(word)

    print(f"Загружено исключений: {len(exclude):,}")

    return exclude

class Query:

    def __init__(self):

        self.table = "words"
        self.conditions = {}
        self.order = None
        self.limit = None
        self.graph_depth = 1
        self.min_link = 1
        self.top = 100
        self.exclude = set()

        dict_dir = Path("dict")

        if dict_dir.exists():

            for file in dict_dir.glob("*.txt"):

                with file.open(encoding="utf-8") as f:

                    for line in f:

                        word = line.strip()

                        if word:
                            self.exclude.add(word)

    def select(self, table):

        self.table = table

    def add(self, command, value):

        if command == "order":
            self.order = value
            return

        if command == "limit":
            self.limit = int(value)
            return

        if command == "depth":
            self.graph_depth = int(value)
            return

        if command == "min_link":
            self.min_link = int(value)
            return

        if command == "top":
            self.top = int(value)
            return

        if command == "file":
            with open(value, encoding="utf-8") as f:
                for line in f:
                    word = line.strip()
                    if word:
                        self.add("word", word)
            print(self.conditions)
            return

        self.conditions.setdefault(command, []).append(value)

    def clear(self):

        self.table = None
        self.where.clear()

    def build_sql(self):

        sql = f"SELECT * FROM {self.table}"
        params = []

        if self.conditions:

            sql += " WHERE "

            parts = []

            for field, values in self.conditions.items():

                if field == "word":

                    parts.append(
                        "word IN (" + ",".join(["?"] * len(values)) + ")"
                    )
                    params.extend(values)

                elif field == "pos":

                    parts.append("(" + " OR ".join(["pos = ?"] * len(values)) + ")")
                    params.extend(values)

                elif field == "count":

                    for value in values:

                        if value.startswith(">"):
                            parts.append("count > ?")
                            params.append(int(value[1:]))

                        elif value.startswith("<"):
                            parts.append("count < ?")
                            params.append(int(value[1:]))

                        elif value.startswith("="):
                            parts.append("count = ?")
                            params.append(int(value[1:]))

                else:
                    raise ValueError(f"Неизвестная команда: {field}")

            sql += " AND ".join(parts)

        if self.order:
            sql += " ORDER BY " + self.order

        if self.limit is not None:
            sql += f" LIMIT {self.limit}"

        return sql, params
# -------------------------------------------------
# Words
# -------------------------------------------------
class Words:

    def __init__(self, db):

        self.db = db

    def by_ids(self, ids):

        if not ids:
            return {}

        placeholders = ",".join("?" * len(ids))

        rows = self.db.query(
            f"""
            SELECT id, word, pos, count
            FROM words
            WHERE id IN ({placeholders})
            """,
            *ids
        )

        return {
            row["id"]: row
            for row in rows
        }

    def word(self, word_id):

        row = self.db.query_one(
            """
            SELECT word
            FROM words
            WHERE id = ?
            """,
            word_id
        )

        if row is None:
            return None

        return row["word"]

    def execute(self, query):

        sql, params = query.build_sql()
        print(sql)
        print(params)
        rows = self.db.query(sql, *params)
        print("До фильтра:", len(rows))
        result = []

        for row in rows:

            if row["word"] in self.db.exclude:
                continue

            result.append(row)

        return result

    def get_part_of_speech(self, word):

        parse = morph.parse(word)[0]

        return POS_RU.get(parse.tag.POS, "неизвестно")

    # ---------------------------------------------------
    # сохранение слов в SQLite
    # ---------------------------------------------------
    def save_words_sqlite(self, dictionary):

        word_to_id = self.load_words()
        for word in sorted(dictionary):

            if word in word_to_id:
                continue

            cursor = self.db.connection.execute("""
                           INSERT INTO words (word, pos, count)
                           VALUES (?, ?, ?)
                           """, (
                               word,
                               self.get_part_of_speech(word),
                               dictionary[word]
                           ))

            word_to_id[word] = cursor.lastrowid

        print(f"Записано слов: {len(word_to_id):,}")

        return word_to_id

    # ---------------------------------------------------
    # Загрузка словаря слов
    # ---------------------------------------------------
    def load_words(self):

        word_to_id = {}

        rows = self.db.query("""
                             SELECT id, word
                             FROM words
                             """)

        for row in rows:
            word_to_id[row["word"]] = row["id"]

        print(f"Загружено слов: {len(word_to_id):,}")

        return word_to_id
# -------------------------------------------------
# Links
# -------------------------------------------------

class Links:
    def __init__(self, db):
        self.db = db


    def by_word(self, word_id):
        return self.db.query(
            """
            SELECT CASE
                       WHEN word1_id = ? THEN word2_id
                       ELSE word1_id
                       END AS id,
                   count
            FROM links
            WHERE word1_id = ?
               OR word2_id = ?
            ORDER BY count DESC
            """,
            word_id,
            word_id,
            word_id
        )

    def neighbors(self, word_id, min_link=1, limit=100):

        sql = """
            SELECT
                w.id,
                w.word,
                w.pos,
                w.count,
                l.count AS link_count
            FROM links l
            JOIN words w
                 ON w.id = CASE
                               WHEN l.word1_id = ? THEN l.word2_id
                               ELSE l.word1_id
                           END
            WHERE (l.word1_id = ?
               OR l.word2_id = ?)
              AND l.count >= ?
            ORDER BY l.count DESC
        """

        params = [word_id, word_id, word_id, min_link]

        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self.db.query(sql, *params)

        result = []

        for row in rows:

            if row["word"] in self.db.exclude:
                continue

            result.append(row)

        return result


# -------------------------------------------------
# Lemmas
# -------------------------------------------------

class Lemmas:
    def __init__(self, db):
        self.db = db
        self.known_words = set()
        self.cache = {}
        self.cache_hit = 0
        self.cache_miss = 0

    def load(self):
        rows = self.db.query("""
                             SELECT word
                             FROM lemmas
                             """)

        self.known_words = {
            row["word"]
            for row in rows
        }

    def lemma(self, word):

        if word in self.cache:
            self.cache_hit += 1
            return self.cache[word]

        self.cache_miss += 1

        row = self.db.query_one(
            """
            SELECT lemma
            FROM lemmas
            WHERE word = ?
            """,
            word
        )

        if row:
            lemma = row["lemma"]
        else:
            lemma = word

        if lemma not in self.known_words:
            lemma = word

        self.cache[word] = lemma

        return lemma


# -------------------------------------------------
# Команды
# -------------------------------------------------


def log(*args):
    if DEBUG:
        print(*args)

def cmd_word(value):

    query.select("words")
    query.add("word", "=", value)


def cmd_words_pos(value):

    query.select("words")
    query.add("pos", "=", value)

def cmd_graph(stream_words, stream_graph):

    stream_graph.clear()

    rows = db.words.execute(query)
    print("Стартовых слов:", len(rows))
    for word in rows:

        stream_graph.append({
            "id": word["id"],
            "word": word["word"],
            "links": rows
        })

    print(f"Размер stream_graph = {len(stream_graph)}")

def cmd_print_graph(stream_graph):

    for vertex in stream_graph:

        print(vertex["word"])

        for row in vertex["links"]:
            print(
                row["word"],
                row["link_count"]
            )

    # -------------------------------------------------
    # Построение графа
    # -------------------------------------------------

def build_graph(start_ids):
    print("build_graph()")
    print("depth =", query.graph_depth)
    graph = {}
    visited = set()
    current = set(start_ids)

    for level in range(query.graph_depth):
        print("level", level, "current", len(current))
        print("depth =", query.graph_depth)
        next_level = set()

        for word_id in current:

            if word_id in visited:
                continue

            visited.add(word_id)

            links = db.links.neighbors(
                word_id,
                query.min_link,
                query.top
            )

            graph[word_id] = links

            for link in links:

                if link["id"] not in visited:
                    next_level.add(link["id"])

        current = next_level

        if not current:
            break

    return graph

def cmd_graph_json(filename):

        rows = db.words.execute(query)

        start_ids = [
            row["id"]
            for row in rows
        ]

        graph = build_graph(start_ids)
        print("После build_graph:", len(graph))

        result = {}

        for node_id, links in graph.items():

            node_word = db.words.word(node_id)

            result[node_word] = {}

            for link in links:
                result[node_word][link["word"]] = link["link_count"]

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"Сохранено: {filename}")

# def cmd_graph_json(filename):
#     print("cmd_graph_json")
#     result = {}
#
#     rows = db.words.execute(query)
#
#     for row in rows:
#
#         result[row["word"]] = {}
#
#         links = db.links.neighbors(
#             row["id"],
#             query.min_link,
#             query.top
#         )
#
#         for link in links:
#
#             result[row["word"]][link["word"]] = link["link_count"]
#
#     with open(filename, "w", encoding="utf-8") as f:
#         json.dump(result, f, ensure_ascii=False, indent=2)
#
#     print(f"Сохранено: {filename}")


def main():

    log("Старт программы")

    with open("query.txt", encoding="utf-8") as f:
        log("Чтение query.txt")
        for line in f:

            log(f"Команда: {line.strip()}")
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = line.split(":", 2)

            source = parts[0]
            command = parts[1]

            if len(parts) == 2:
                value = ""

            elif len(parts) == 3:
                value = parts[2]
            else:
                continue

            # работа со справочником слов
            if source == "words":
                query.add(command, value)

            # обновление словарей минус слов
            elif source == "dict":

                if command == "update":
                    cmd_dict_update(value)

            # Работа с базой связей
            elif source == "graph":

                if command == "json":
                    cmd_graph_json(value)
                else:
                    query.add(command, value)
                    print(command, "=", value)


if __name__ == "__main__":

    db = Database(DATABASE_FILE)

    db.exclude = load_exclude_words()

    query = Query()

    main()