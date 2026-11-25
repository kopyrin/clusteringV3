import sqlite3
import requests
import json
import re
import time

class Hypothesis:

    def __init__(self):
        self.conn = None
        self.cur = None

        self.session = requests.Session()
        self.session.trust_env = False
        self.ai_url = "http://localhost:5678/webhook/ai"

        self.ai_headers = {
            "Authorization": "Bearer XXXXX"
        }

        self.ai_model = "kimi-k2"

    def connect(self):
        self.conn = sqlite3.connect(str(PROJECT_ROOT / "data" / "output" / "semantics.db"))
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

    def load_words(self):
        # self.cur.execute("""
        #                  SELECT w.id, w.word, w.count
        #                     FROM words w
        #                     WHERE w.id IN (
        #                         SELECT word_id
        #                         FROM group_words
        #                         WHERE group_id = 2
        #                     )
        #                     AND w.id NOT IN (
        #                         SELECT word_id
        #                         FROM group_words
        #                         WHERE group_id <> 2
        #                     )
        #                     ORDER BY w.count DESC
        #                     LIMIT 5000
        #                  """)
        self.cur.execute("""
SELECT
    w.id,
    w.word,
    w.count
FROM words w
JOIN group_words gw
    ON gw.word_id = w.id
GROUP BY
    w.id,
    w.word,
    w.count
HAVING
    COUNT(*) = 1
    AND MAX(gw.group_id) = 2
ORDER BY w.count DESC
LIMIT 500;
                         """)
        return self.cur.fetchall()

    def clean_json(self,text):
        text = text.strip()

        if text.startswith("```"):
            lines = text.splitlines()

            # убрать первую строку ```json
            lines = lines[1:]

            # убрать последнюю ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            text = "\n".join(lines).strip()

        return text

    def process_word(self, row):
        word = row["word"]

        prompt = f"""
Ты классификатор слов.
Слово:
{word}
Если слово является общеизвестным объектом, брендом, сервисом,
организацией, технологией или термином — верни наиболее подходящую
тематическую группу.
Слова могут содержать опечатки, ошибки OCR, транслитерацию,
неправильную раскладку клавиатуры и фонетическое написание.

Если очевидно, что слово является искаженным написанием
известного бренда, сервиса, лекарства, города, компании
или термина, классифицируй его по исходному объекту.

Не исправляй слово, если совпадение неочевидно.

Если слово действительно неизвестно или случайный набор символов —
верни "Неизвестно".

Ответ только JSON:

{{
  "group": "Неизвестно"
}}

"""
        payload = {
            "model": "cerebras/gemma-4-31b",
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0,
            "stream": False
        }

        # print("=" * 80)
        # print(payload)
        # print("=" * 80)
        for attempt in range(5):
            response = self.session.post(
                "http://localhost:20128/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-199d92405b9769bf-81a6e8-3a0f8185",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=600
            )
        # response = self.session.post(
        #     "http://localhost:20128/v1/chat/completions",
        #     headers={
        #         "Authorization": "Bearer sk-199d92405b9769bf-81a6e8-3a0f8185",
        #         "Content-Type": "application/json"
        #     },
        #     json={
        #         "model": "ddgw/gpt-5-mini",  # позже уточним название модели/комбо
        #         "messages": [
        #             {
        #                 "role": "user",
        #                 "content": prompt
        #             }
        #         ],
        #         "temperature": 0,
        #         "stream": False
        #     },
        #     timeout=600
        # )
            if response.status_code == 429:
                print("HTTP 429")
                print(response.text)

                try:
                    data = response.json()
                    msg = data["error"]["message"]

                    m = re.search(r"reset after (\d+)s", msg)
                    wait = int(m.group(1)) if m else 5

                except Exception:
                    wait = 5

                print(f"Жду {wait} сек...")
                time.sleep(wait)
                continue

            if response.status_code != 200:
                print("HTTP", response.status_code)
                print(response.text)
                return None

            data = response.json()

            if "error" in data:
                print(data)
                return None

            break
        else:
            print("Не удалось получить ответ AI")
            return None

        data = response.json()

        if "error" in data:
            print(data)
            return None



        if "choices" not in data:
            print(data)
            raise Exception("Некорректный ответ AI")

        message = data["choices"][0]["message"]

        content = message.get("content", "")
        content = self.clean_json(content)

        # print(content)

        result = json.loads(content)

        return {
            "word_id": row["id"],
            "word": row["word"],
            "group": result["group"]
        }



    def get_group_id(self, group_name):

        self.cur.execute(
            "SELECT id FROM groups WHERE name = ?",
            (group_name,)
        )

        row = self.cur.fetchone()

        if row:
            return row["id"]

        self.cur.execute(
            "INSERT INTO groups(name) VALUES(?)",
            (group_name,)
        )

        self.conn.commit()

        return self.cur.lastrowid

    def add_word_to_group(self, word_id, group_id):

        self.cur.execute("""
                         SELECT 1
                         FROM group_words
                         WHERE word_id = ?
                           AND group_id = ?
                         """, (word_id, group_id))

        if self.cur.fetchone():
            return

        self.cur.execute("""
                         INSERT INTO group_words(word_id, group_id)
                         VALUES (?, ?)
                         """, (word_id, group_id))

        self.conn.commit()

    def save_result(self, result):

        group_id = self.get_group_id(result["group"])

        self.add_word_to_group(
            result["word_id"],
            group_id
        )

        print(f'{result["word"]} -> {result["group"]}')

    def get_group_id(self, group_name):

        self.cur.execute(
            "SELECT id FROM groups WHERE name = ?",
            (group_name,)
        )

        row = self.cur.fetchone()

        if row:
            return row["id"]

        self.cur.execute(
            "INSERT INTO groups(name) VALUES(?)",
            (group_name,)
        )

        self.conn.commit()

        return self.cur.lastrowid

    def add_word_to_group(self, word_id, group_id):

        self.cur.execute("""
                         INSERT
                         OR IGNORE INTO group_words(word_id, group_id)
            VALUES (?, ?)
                         """, (word_id, group_id))

        self.conn.commit()

    def run(self):

        self.connect()

        words = self.load_words()

        for row in words:
            try:
                result = self.process_word(row)

                if result is None:
                    print(f'Пропускаю "{row["word"]}" из-за ошибки AI')
                    continue

                self.save_result(result)
            except Exception as e:
                print(f'Ошибка для слова "{row["word"]}": {e}')

if __name__ == "__main__":
    Hypothesis().run()