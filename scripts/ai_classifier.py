import os
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import json
from datetime import datetime
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import atexit

# --- AI ---
# текущий провайдер
AI_PROVIDER = "alibaba"
# ----------------------------------------------------
# AI Provider
# ----------------------------------------------------
AI_PROVIDERS = {
    "omniroute": {
            "url": "http://127.0.0.1:20128/v1/chat/completions",
            "models_url": "http://127.0.0.1:20128/v1/models",
            "key": os.getenv("OMNIROUTE_API_KEY"),

            "models_key": "data",
            "model_field": "id",

            "stream": False,
            "temperature": True,
            "reasoning": True,

            "exclude_prefixes": [
                "auto/"
            ],

            "exclude_contains": [
                "embedding",
                "embed",
                "rerank",
                "tts",
                "whisper",
                "vision",
                "image",
                "moderation",
                "audio"
            ],
            "model_filter": {
                "values": [],
                "prefix": [],
                "suffix": [],
                "contains": [],
                "exclude": []
            }
        },

    "ollama": {
            "url": "http://127.0.0.1:11434/v1/chat/completions",
            "models_url": "http://127.0.0.1:11434/api/tags",
            "key": "",

            "models_key": "models",
            "model_field": "name",

            "stream": False,
            "temperature": True,
            "reasoning": False,

            "exclude_prefixes": [],
            "exclude_contains": [],
            "model_filter": {
                "values": [],
                "prefix": [],
                "suffix": [],
                "contains": [],
                "exclude": []
            }
        },

    "freelllm": {
        "url": "http://host.docker.internal:3001/v1/chat/completions",
        "models_url": "http://127.0.0.1:3001/v1/models",
        "key": os.getenv("FREELLLM_API_KEY"),
        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": True,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],
        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "alibaba": {
        "url": "https://llm-97sm6wobqxie76ta.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions",

        "models_url": "https://dashscope-intl.aliyuncs.com/api/v1/deployments/models",

        "key": os.getenv("ALIBABA_API_KEY"),

        # Список моделей лежит вложенно:
        #     response["output"]["models"]
        "models_key": "output.models",
        "model_field": "model_name",
        # Общее количество моделей (для пагинации):
        #     response["output"]["total"]
        "total_key": "output.total",
        # Пагинация: параметры добавляются к URL списка моделей.
        # Фиксированные параметры передаются как есть.
        "pagination": {
            "param": "page_no",
            "size_param": "page_size",
            "page_size": 100,
            "params": {
                "version": "v1.0",
                "model_source": "base"
            }
        },

        "stream": False,
        "temperature": True,
        "reasoning": True,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],

        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models_url": "https://openrouter.ai/api/v1/models",
        "key": os.getenv("OPENROUTER_API_KEY"),
        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": True,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],
        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": ["free"],
            "contains": [],
            "exclude": []
        }
    },

    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models_url": "https://api.groq.com/openai/v1/models",
        "key": os.getenv("GROQ_API_KEY"),
        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": False,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio",
            "prompt-guard",
            "orpheus"
        ],

        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "huggingface": {
        "url": "https://router.huggingface.co/v1/chat/completions",
        "models_url": "https://router.huggingface.co/v1/models",
        "key": os.getenv("HUGGINGFACE_API_KEY"),
        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": True,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],
        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "tokenrouter": {
        "url": "https://api.tokenrouter.com/v1/chat/completions",
        "models_url": None,
        "models": [
            "moonshotai/kimi-k3-free"
        ],
        "key": os.getenv("TOKENROUTER_API_KEY"),
        "models_key": "data",
        "model_field": "id",
        "stream": False,
        "temperature": True,
        "reasoning": True,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],
        "model_filter": {
            "values": [
        "moonshotai/kimi-k3-free"
    ],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "cloudflare": {
        "url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
        "models_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/models/search",

        "account_id": os.getenv("CLOUDFLARE_ACCOUNT_ID"),
        "key": os.getenv("CLOUDFLARE_API_KEY"),

        "models_key": "result",
        "model_field": "name",

        "stream": False,
        "temperature": True,
        "reasoning": True,

        "task_filter": "Text Generation",

        "exclude_properties": [
            "require_workers_paid"
        ],

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],
        "exclude_tags": [
            "moderation",
            "safety",
            "content-filtering",
            "guardrails"
        ],
        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "sambanova": {
        "url": "https://api.sambanova.ai/v1/chat/completions",
        "models_url": "https://api.sambanova.ai/v1/models",
        "key": os.getenv("SAMBANOVA_API_KEY"),

        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": True,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "audio",
            "vision",
            "image",
            "moderation",
            "safety"
        ],

        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        },
        "lowercase_model_ids": False,
    },

    "mistral": {
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models_url": "https://api.mistral.ai/v1/models",

        "key": os.getenv("MISTRAL_API_KEY"),

        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": False,

        "lowercase_model_ids": False,

        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "moderation",
            "ocr",
            "audio",
            "transcription",
            "tts"
        ],

        "model_filter": {
            "values": [
                "mistral-small-2603",
                "mistral-medium-3-5",
                "mistral-large-2512",
                "ministral-14b-2512",
                "glm-5-2"
            ],
            "prefix": [],
            "suffix": [],
            "contains": [],
            "exclude": []
        }
    },

    "kilo": {
        "url": "https://api.kilo.ai/api/gateway/chat/completions",
        "models_url": "https://api.kilo.ai/api/gateway/models",

        "account_id": "",
        "key": "",

        "models_key": "data",
        "model_field": "id",

        "stream": False,
        "temperature": True,
        "reasoning": False,

        "task_filter": "",

        "exclude_properties": [],
        "exclude_prefixes": [],

        "exclude_contains": [
            "embedding",
            "embed",
            "rerank",
            "tts",
            "whisper",
            "vision",
            "image",
            "moderation",
            "audio"
        ],

        "exclude_tags": [],

        "model_filter": {
            "values": [],
            "prefix": [],
            "suffix": ["free"],
            "contains": [],
            "exclude": []
        }
    },
}
# температура текста
AI_TEMPERATURE = 0
# время на ответ ИИ в нормальном запросе
AI_TIMEOUT = 120
# время на тестовый запрос при формировании пула.
# Короткое: если модель не отвечает быстро — она не для пула.
AI_TEST_TIMEOUT = 30
AI_MAX_ATTEMPTS = 5
# размер пула рабочих моделей; одновременно в голосовании участвует MIN_VOTES
AI_MODEL_POOL_SIZE = 30
# модели тестируем несмотря на срок
RETEST_BAD_MODELS = False
# срок в днях для повторной проверки модели у провайдера
BAD_MODEL_RETEST_DAYS = 3
# --- Голосование ---
# количество судей необходимых для перехода слова на следующий этап
MIN_VOTES = 3
# Ошибки, при которых модель не считается плохой.
# Это временные проблемы соединения или лимиты.
TEMPORARY_AI_ERRORS = {
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectionError",
    "HTTP429_RATELIMIT",
}
####################################################
# FacetAnalyzer
####################################################
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

class FacetAnalyzer:
    # ---------------------------------------------------
    # Настройки провайдера и модели
    # ---------------------------------------------------
    def __init__(self, report, provider=None, model=None):

        self.report = report
        # ---------------------------------------------------
        # Провайдер:
        #   None   — провайдер из глобальной настройки AI_PROVIDER;
        #   "all"  — все доступные провайдеры (общий пул моделей);
        #   <имя>  — конкретный провайдер из AI_PROVIDERS.
        # ---------------------------------------------------
        if provider == "all":
            self.provider = "all"
            provider_names = list(AI_PROVIDERS.keys())
        else:
            self.provider = provider or AI_PROVIDER
            provider_names = [self.provider]

        # ---------------------------------------------------
        # Резолвим конфиги провайдеров.
        #
        # Для каждого провайдера подставляем account_id в URL,
        # формируем заголовки с API-ключом и сохраняем
        # в self._providers. Запросы к моделям идут
        # к провайдеру, которому принадлежит модель.
        # ---------------------------------------------------
        self._providers = {}

        for name in provider_names:

            cfg = dict(AI_PROVIDERS[name])

            url = cfg["url"]
            models_url = cfg["models_url"]

            account_id = cfg.get("account_id")

            if account_id:
                url = url.format(account_id=account_id)

                if models_url:
                    models_url = models_url.format(
                        account_id=account_id
                    )

            headers = {
                "Content-Type": "application/json"
            }

            key = cfg.get("key")

            if key:
                headers["Authorization"] = f"Bearer {key}"

            cfg["url"] = url
            cfg["models_url"] = models_url
            cfg["headers"] = headers

            self._providers[name] = cfg

        # так же с моделью
        self.model = model
        self.no_models = False
        # Параметры запроса к AI.
        self.temperature = AI_TEMPERATURE
        self.timeout = AI_TIMEOUT
        # Таймаут тестового запроса при формировании пула.
        self.test_timeout = AI_TEST_TIMEOUT
        self.max_attempts = AI_MAX_ATTEMPTS
        # ---------------------------------------------------
        # HTTP-сессия
        # ---------------------------------------------------
        self.session = requests.Session()
        self.session.trust_env = False

        # ---------------------------------------------------
        # Состояние моделей
        # ---------------------------------------------------
        # Все модели, полученные от провайдеров
        # после применения фильтров.
        # Элемент списка — пара (provider, model).
        self.models = []
        # Рабочие модели, успешно прошедшие проверку.
        # Используются для автоматического переключения
        # при ошибке текущей модели.
        # Элемент списка — пара (provider, model).
        self.active_models = []
        # Защищает список рабочих моделей: потоки-судьи
        # могут исключать модель (_deactivate_model)
        # одновременно с тем, как ask() читает список.
        self._pool_lock = threading.Lock()
        # Защищает словарь состояний моделей и запись
        # файла bad_models.txt: судьи обновляют состояния
        # параллельно, файл не должен переписываться
        # двумя потоками одновременно.
        self._states_lock = threading.Lock()
        # Информация о последней ошибке запроса.
        # Хранится в thread-local, так как судьи
        # выполняются параллельно в отдельных потоках
        # и не должны перезаписывать ошибки друг друга.
        self._local = threading.local()
        # Загружаем сохраненное состояние моделей
        # из output/bad_models.txt.
        self._load_model_states()

    @property
    def last_error(self):
        return getattr(self._local, "last_error", "")

    @last_error.setter
    def last_error(self, value):
        self._local.last_error = value

    @property
    def last_error_type(self):
        return getattr(self._local, "last_error_type", None)

    @last_error_type.setter
    def last_error_type(self, value):
        self._local.last_error_type = value

    # ===================================================
    # ПУБЛИЧНЫЙ ИНТЕРФЕЙС
    # ===================================================
    def ask(self, prompt, judges=1):
        """
        Отправляет один запрос к AI.

        Одновременно запускает указанное количество судей.
        Если один или несколько судей не ответили,
        недостающие ответы добираются следующими моделями
        из рабочего пула.

        Модель, ответившая постоянной ошибкой, удаляется
        из рабочего пула и сохраняется в bad_models.txt.
        Временные ошибки (соединение, лимиты) не исключают
        модель.

        Параметры:
            prompt — текст запроса к AI.
            judges — необходимое количество ответов.

        Возвращает:
            [
                {
                    "model": "название модели",
                    "answer": "ответ модели"
                },
                ...
            ]

            Если после перебора всего рабочего пула
            не удалось получить необходимое количество
            ответов — возвращает None.

        Метод не анализирует содержимое ответа.
        Ответ может быть обычным текстом, JSON или любым
        другим содержимым.
        """

        if not prompt:
            return None

        if judges < 1:
            return None

        # ---------------------------------------------------
        # Обеспечиваем наличие рабочего пула моделей
        # ---------------------------------------------------

        if not self.active_models:
            self._ensure_model()

        if not self.active_models:
            return None

        # ---------------------------------------------------
        # Запускаем судей партиями.
        #
        # Одновременно работает не более judges моделей.
        # Если какая-то модель не ответила, следующая
        # модель из пула занимает её место.
        #
        # Список моделей для партии берётся снимком,
        # чтобы потоки-судьи могли исключать павшие модели
        # из активного пула без гонок с итерацией.
        # ---------------------------------------------------

        answers = []
        # Модели, уже запущенные в текущем вызове ask():
        # одна модель не может голосовать несколько раз.
        used = set()

        while len(answers) < judges:

            # ---------------------------------------------------
            # Снимок пула под блокировкой.
            # Модели, исключённые судьями в ходе текущего
            # вызова, уже отсутствуют в списке.
            # ---------------------------------------------------

            with self._pool_lock:
                candidates = [
                    model
                    for model in self.active_models
                    if model not in used
                ]

            if not candidates:
                break

            # ---------------------------------------------------
            # Выбираем очередную партию моделей
            # ---------------------------------------------------

            models = candidates[:judges]

            if not models:
                break

            used.update(models)

            # ---------------------------------------------------
            # Параллельно запускаем текущую партию
            # ---------------------------------------------------

            with ThreadPoolExecutor(
                    max_workers=len(models)
            ) as executor:

                futures = {}

                for entry in models:
                    payload = self._build_payload(
                        entry,
                        prompt
                    )

                    _, model = entry

                    console_status(
                        f"AI: запускаю судью | {model}"
                    )

                    future = executor.submit(
                        self._judge_request,
                        entry,
                        payload
                    )

                    futures[future] = entry

                # ---------------------------------------------------
                # Получаем результаты партии
                # ---------------------------------------------------

                for future in as_completed(futures):

                    try:
                        entry, content, error_type = (
                            future.result()
                        )

                    except Exception:
                        continue

                    _, model = entry

                    if content is None:
                        console_status(
                            f"AI: {model} | "
                            f"ответ не получен: {error_type}"
                        )
                        # Постоянная ошибка исключает модель
                        # из рабочего пула.
                        if (
                                error_type
                                and error_type not in TEMPORARY_AI_ERRORS
                        ):
                            self._deactivate_model(
                                entry,
                                error_type
                            )
                        continue

                    answers.append({
                        "model": model,
                        "answer": content
                    })

                    console_status(
                        f"AI: {model} | ответ принят "
                        f"({len(answers)}/{judges})"
                    )

                    if len(answers) >= judges:
                        break

        # ---------------------------------------------------
        # Проверяем, набрали ли необходимое количество
        # ---------------------------------------------------

        if len(answers) < judges:
            return None

        return answers[:judges]

    def _judge_request(self, entry, payload):
        """
        Выполняет один запрос судьи в отдельном потоке.

        Параметры:
            entry — пара (provider, model).

        Использует единый путь HTTP-обработки _send_request,
        общий для теста пула и обычных запросов.

        Возвращает:
            (entry, content, error_type)

        error_type относится только к этому запросу
        и не использует общее состояние self.last_error_type
        (оно thread-local и принадлежит потоку судьи).
        """

        provider, model = entry

        t0 = time.perf_counter()

        content, error_type = self._send_request(
            payload,
            retry_on_429=True,
            timeout=self.timeout,
            provider=provider
        )

        elapsed = time.perf_counter() - t0

        console_status(
            f"AI: {model} | HTTP: {elapsed:.2f} сек"
        )

        return entry, content, error_type

    def _deactivate_model(self, entry, error_type):
        """
        Удаляет модель из рабочего пула и сохраняет
        её состояние в bad_models.txt.

        Параметры:
            entry — пара (provider, model).

        Вызывается для постоянных ошибок.

        Метод выполняется из потоков-судей, поэтому
        изменение пула защищается _pool_lock.
        """

        provider, model = entry

        with self._pool_lock:
            if entry in self.active_models:
                self.active_models.remove(entry)

        self._save_model_state(
            provider,
            model,
            status=error_type,
            parameters=""
        )

        console_status(
            f"AI: {model} | исключена из пула: {error_type}"
        )

    # ===================================================
    # ПОДГОТОВКА МОДЕЛЕЙ
    # ===================================================
    def _ensure_model(self):
        """
        Обеспечивает наличие модели для выполнения запроса.

        Возможны два режима:

        1. Модель передана явно при создании объекта
           вместе с конкретным провайдером.
           В этом случае дополнительный выбор не выполняется.

        2. Модель не задана.
           Тогда:
               • загружается список доступных моделей
                 (одного провайдера или всех — в зависимости
                 от параметра provider);
               • выполняется проверка моделей;
               • формируется список рабочих моделей.

        В режиме provider="all" явная модель не используется:
        пул собирается из моделей всех провайдеров.

        Возвращает:
            True  — модель доступна для работы;
            False — рабочую модель выбрать не удалось.
        """

        # Модель задана явно (только с конкретным провайдером).
        if self.model and self.provider != "all":
            with self._pool_lock:
                if not self.active_models:
                    self.active_models = [
                        (self.provider, self.model)
                    ]
            return True

        # Рабочие модели уже были определены ранее.
        with self._pool_lock:
            has_active = bool(self.active_models)

        if has_active:
            return True

        self._load_models()

        if not self.models:
            self.no_models = True
            self.report(
                "Не найдено ни одной доступной модели."
            )
            return False

        self.active_models = self._find_working_models(
            AI_MODEL_POOL_SIZE
        )

        if not self.active_models:
            self.no_models = True
            self.report(
                "Не найдено ни одной рабочей модели."
            )
            return False

        return True

    def _load_models(self):
        """
        Получает и фильтрует список моделей.

        Перебирает провайдеров из self._providers
        (один или все) и собирает доступные модели.

        Элемент списка self.models — пара (provider, model).

        Конфигурация провайдера описывает формат ответа:

            models_key   — путь к списку моделей в JSON
                           (вложенность через точки,
                           например "output.models");
            model_field  — поле внутри элемента списка
                           с идентификатором модели;
            total_key    — путь к общему количеству моделей
                           (для пагинации, опционально);
            pagination   — настройки пагинации (опционально):
                           параметр номера страницы, параметр
                           размера страницы, размер страницы
                           и фиксированные query-параметры.

        Возвращает:
            список пар (provider, model).

        При ошибке получения списка моделей
        возвращается текущий список self.models.
        """

        # Начинаем формирование списка заново.
        self.models.clear()

        for provider, config in self._providers.items():

            self._load_provider_models(
                provider,
                config
            )

        # Убираем дубли (одинаковые пары provider + model).
        self.models = list(
            dict.fromkeys(self.models)
        )

        console_status(
            f"Получено моделей: {len(self.models)}"
        )

        return self.models

    def _load_provider_models(self, provider, config):
        """
        Получает и фильтрует список моделей одного провайдера.

        Добавляет в self.models пары (provider, model).

        Конфигурация провайдера описывает формат ответа:

            models_key   — путь к списку моделей в JSON
                           (вложенность через точки);
            model_field  — поле с идентификатором модели;
            total_key    — путь к общему количеству моделей;
            pagination   — настройки пагинации.

        При ошибке получения списка моделей
        провайдер пропускается, ошибка сообщается.
        """

        try:

            if not config["models_url"]:

                configured_models = config.get(
                    "models",
                    []
                )

                if configured_models:
                    self.models.extend(
                        (provider, model)
                        for model in configured_models
                    )

                    console_status(
                        f"Модели из конфигурации "
                        f"({provider}): "
                        f"{len(configured_models)}"
                    )

                    return

                console_status(
                    f"У провайдера {provider} нет URL списка "
                    "моделей и не задан список моделей."
                )

                return

            # ---------------------------------------------------
            # Пагинация
            # ---------------------------------------------------
            pagination = config.get("pagination")
            page_param = None
            size_param = None
            page_size = None
            fixed_params = {}

            if pagination:
                page_param = pagination.get("param", "page")
                size_param = pagination.get(
                    "size_param",
                    "page_size"
                )
                page_size = pagination.get("page_size", 100)
                fixed_params = pagination.get("params", {})

            page_no = 1

            while True:

                params = dict(fixed_params)

                if page_param:
                    params[page_param] = page_no

                    if size_param:
                        params[size_param] = page_size

                response = self.session.get(
                    config["models_url"],
                    headers=config["headers"],
                    params=params or None,
                    timeout=self.timeout
                )

                response.raise_for_status()

                data = response.json()

                models_data = self._get_path(
                    data,
                    config["models_key"],
                    []
                )

                for item in models_data:

                    entry = self._extract_model(
                        item,
                        config,
                        provider
                    )

                    if entry is None:
                        continue

                    self.models.append(entry)

                # ---------------------------------------------------
                # Условие окончания пагинации
                # ---------------------------------------------------
                if not page_param:
                    break

                if not models_data:
                    break

                if len(models_data) < page_size:
                    break

                total = self._get_path(
                    data,
                    config.get("total_key"),
                    None
                )

                if total and len(self.models) >= total:
                    break

                page_no += 1

        except Exception as e:

            self.last_error = str(e)

            self.report(
                f"Не удалось получить список моделей "
                f"({provider})."
            )

            self.report(
                f"{type(e).__name__}: {e}"
            )

    @staticmethod
    def _get_path(data, key, default=None):
        """
        Извлекает значение из вложенного словаря по пути
        с точками.

        Примеры:
            "output.models"  → data["output"]["models"]
            "output.total"   → data["output"]["total"]

        Возвращает default, если путь отсутствует.
        """

        if not key:
            return default

        current = data

        for part in key.split("."):

            if not isinstance(current, dict):
                return default

            current = current.get(part)

            if current is None:
                return default

        return current

    def _extract_model(self, item, config, provider):
        """
        Извлекает идентификатор модели из элемента списка
        и применяет общие фильтры:

            • регистр (lowercase_model_ids);
            • тип задачи (task_filter);
            • теги и свойства;
            • технические модели (exclude_prefixes,
              exclude_contains);
            • фильтр моделей провайдера (model_filter);
            • сохраненное состояние (bad_models.txt).

        Возвращает:
            пару (provider, model);
            None — если элемент не подходит.
        """

        if not isinstance(item, dict):
            return None

        model = item.get(
            config["model_field"],
            ""
        )

        if not model:
            return None

        if config.get(
                "lowercase_model_ids",
                True
        ):
            model = model.lower()
        # ---------------------------------------------------
        # Фильтр типа задачи модели
        # ---------------------------------------------------

        task_filter = config.get(
            "task_filter"
        )

        if task_filter:

            task = item.get(
                "task",
                {}
            )

            task_name = task.get(
                "name",
                ""
            )

            if task_name != task_filter:
                return None
            # ---------------------------------------------------
            # Фильтр по тегам модели
            # ---------------------------------------------------

            exclude_tags = config.get(
                "exclude_tags",
                []
            )

            if exclude_tags:

                tags = item.get(
                    "tags",
                    []
                )

                if any(
                    tag in exclude_tags
                    for tag in tags
                ):
                    return None
        # ---------------------------------------------------
        # Фильтр специальных свойств модели
        # ---------------------------------------------------

        exclude_properties = config.get(
            "exclude_properties",
            []
        )

        if exclude_properties:

            properties = item.get(
                "properties",
                []
            )

            property_ids = {
                prop.get("property_id")
                for prop in properties
            }

            if any(
                prop in property_ids
                for prop in exclude_properties
            ):
                return None

        # ---------------------------------------------------
        # Фильтрация технических моделей
        # ---------------------------------------------------

        if any(
                model.startswith(prefix)
                for prefix in config["exclude_prefixes"]
        ):
            return None

        if any(
                text in model
                for text in config["exclude_contains"]
        ):
            return None
        # ---------------------------------------------------
        # Фильтр моделей провайдера
        # ---------------------------------------------------

        model_filter = config.get(
            "model_filter",
            {}
        )

        # Точное совпадение
        values = model_filter.get(
            "values",
            []
        )

        if values and model not in values:
            return None

        # Префикс
        prefixes = model_filter.get(
            "prefix",
            []
        )

        if prefixes and not any(
                model.startswith(prefix)
                for prefix in prefixes
        ):
            return None

        # Суффикс
        suffixes = model_filter.get(
            "suffix",
            []
        )

        if suffixes and not any(
                model.endswith(suffix)
                for suffix in suffixes
        ):
            return None

        # Содержимое
        contains = model_filter.get(
            "contains",
            []
        )

        if contains and not any(
                text in model
                for text in contains
        ):
            return None

        # Дополнительное исключение
        exclude = model_filter.get(
            "exclude",
            []
        )

        if any(
                text in model
                for text in exclude
        ):
            return None

        # ---------------------------------------------------
        # Проверка сохраненного состояния модели
        # ---------------------------------------------------

        key = f"{provider}|{model}"

        state = self.bad_models.get(key)

        if (
                state
                and state["status"] != "WORKING"
                and not RETEST_BAD_MODELS
        ):

            try:

                checked_at = datetime.strptime(
                    state["checked_at"],
                    "%Y-%m-%d %H:%M:%S"
                )

                age_days = (
                        (datetime.now() - checked_at).total_seconds()
                        / 86400
                )

            except Exception:

                age_days = 999

            if age_days < BAD_MODEL_RETEST_DAYS:
                return None

        return (provider, model)

    def _find_working_models(self, count):
        """
        Находит рабочие модели среди доступных.

        Для каждой модели выполняется тестовый запрос.
        Успешно ответившие модели добавляются в active.

        После проверки состояние модели сохраняется
        в bad_models.txt:

            WORKING — модель успешно прошла проверку;
            тип ошибки — модель не прошла проверку.

        Временные ошибки (соединение, лимиты) не считаются
        постоянной поломкой модели и не сохраняются.

        Поиск прекращается после нахождения
        указанного количества рабочих моделей.

        Возвращает:
            список рабочих моделей.
        """

        active = []

        for entry in self.models:

            provider, model = entry

            console_status(
                f"AI: проверка пула | {len(active)}/{count}"
            )

            ok = self._test_model(entry)

            if ok:

                active.append(entry)

                self._save_model_state(
                    provider,
                    model,
                    status="WORKING",
                    parameters=""
                )

                console_status(
                    f"AI: проверка пула | {len(active)}/{count}"
                )

            else:

                status = self.last_error_type or "UNKNOWN"

                # Временную ошибку не заносим в bad_models.txt:
                # это лимит или сбой соединения, а не поломка.
                if self._can_blacklist(status):
                    self._save_model_state(
                        provider,
                        model,
                        status=status,
                        parameters=""
                    )

                console_status(
                    f"AI: проверка пула | {len(active)}/{count}"
                )

            if len(active) >= count:
                break

        console_status(
            f"AI-пул готов: {len(active)} рабочих моделей | "
            f"для одного пакета: {MIN_VOTES} судьи"
        )

        return active

    def _test_model(self, entry):
        """
        Проверяет, отвечает ли указанная модель
        на минимальный тестовый запрос.

        Параметры:
            entry — пара (provider, model).

        При HTTP 429 модель сразу считается
        временно недоступной.

        Повторные попытки при проверке пула
        не выполняются.
        """

        provider, model = entry

        prompt = """
    Верни строго JSON-массив из четырёх объектов:

    [
        {
            "слово": "chair",
            "группы": []
        },
        {
            "слово": "table",
            "группы": []
        },
        {
            "слово": "red",
            "группы": []
        },
        {
            "слово": "computer",
            "группы": []
        }
    ]

    Не добавляй пояснений.
    Верни только JSON.
    """.strip()

        payload = self._build_payload(
            entry,
            prompt
        )

        # При проверке пула НЕ повторяем HTTP 429.
        # Таймаут короткий (self.test_timeout): медленная
        # модель не нужна в пуле.
        content, error_type = self._send_request(
            payload,
            retry_on_429=False,
            timeout=self.test_timeout,
            provider=provider
        )

        if content is None:
            console_line("")
            console_line(
                f"AI: модель {model} | "
                f"тест HTTP: {error_type}"
            )

            return False

        try:

            if not self._validate_model_test_response(
                    content
            ):
                console_line("")
                console_line(
                    f"AI: модель {model} | "
                    f"тест FAILED: INVALID_AI_RESPONSE"
                )

                return False

            console_status(
                f"AI: модель {model} | "
                f"тест OK"
            )

            return True

        except Exception as e:
            console_line("")
            console_line(
                f"AI: модель {model} | "
                f"тест FAILED: "
                f"{type(e).__name__}: {e}"
            )

            return False

    def _validate_model_test_response(self, content):
            """
            Проверяет минимальный ответ модели
            при формировании пула.

            Не проверяет качество классификации.
            Проверяет только наличие корректного JSON-массива
            ожидаемой структуры.
            """

            if not content:
                return False

            content = content.strip()

            # ---------------------------------------------------
            # Убираем Markdown-обёртку ```json ... ```
            # ---------------------------------------------------

            if content.startswith("```"):

                lines = content.splitlines()

                if lines and lines[0].startswith("```"):
                    lines = lines[1:]

                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]

                content = "\n".join(lines).strip()

            try:

                result = json.loads(content)

            except json.JSONDecodeError:

                return False

            if not isinstance(result, list):
                return False

            if not result:
                return False

            for item in result:

                if not isinstance(item, dict):
                    return False

                if not isinstance(
                        item.get("слово"),
                        str
                ):
                    return False

                if not isinstance(
                        item.get("группы"),
                        list
                ):
                    return False

            return True

    # ===================================================
    # СОСТОЯНИЕ МОДЕЛЕЙ
    # ===================================================
    def _load_model_states(self):
        """
        Загружает сохраненное состояние моделей.

        Источник:
            output/bad_models.txt

        Для каждой модели сохраняется одна последняя запись:

            provider | model | status | parameters | datetime

        Загруженные состояния используются при формировании
        списка доступных моделей и позволяют временно
        исключать модели, ранее завершившиеся ошибкой.
        """

        self.bad_models = {}

        path = PROJECT_ROOT / "data" / "output" / "bad_models.txt"

        if not path.exists():
            return

        for line in path.read_text(
                encoding="utf-8").splitlines():

            line = line.strip()

            if not line or line.startswith("#"):
                continue

            parts = [x.strip() for x in line.split("|")]

            if len(parts) < 5:
                continue

            provider, model, status, parameters, checked_at = parts[:5]

            key = f"{provider}|{model}"

            self.bad_models[key] = {
                "provider": provider,
                "model": model,
                "status": status,
                "parameters": parameters,
                "checked_at": checked_at
            }

    def _save_model_state(self, provider, model, status=None,
                          parameters=""):
        """
        Сохраняет последнее состояние модели в памяти.

        Параметры:
            provider — имя провайдера модели;
            model    — идентификатор модели.

        Для каждой пары:
            provider + model
        хранится только одна актуальная запись.

        Если status не передан явно,
        используется тип последней ошибки.

        Запись на диск не выполняется: состояния
        накапливаются в памяти и сбрасываются
        на диск один раз методом flush_states()
        по завершении работы.

        Метод вызывается из потоков-судей, поэтому
        изменение словаря защищается _states_lock.
        """

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if status is None:
            status = self.last_error_type or "UNKNOWN"

        key = f"{provider}|{model}"

        with self._states_lock:
            self.bad_models[key] = {
                "provider": provider,
                "model": model,
                "status": status,
                "parameters": parameters,
                "checked_at": now
            }

    def flush_states(self):
        """
        Сбрасывает накопленные состояния моделей
        в файл output/bad_models.txt.

        Файл полностью пересобирается из текущего
        словаря bad_models. Вызывается один раз
        по завершении работы.

        Повторные вызовы безопасны (идемпотентны).
        """

        with self._states_lock:
            snapshot = dict(self.bad_models)

        path = PROJECT_ROOT / "data" / "output" / "bad_models.txt"

        # Гарантируем существование каталога.
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = []

        for item in sorted(
                snapshot.values(),
                key=lambda x: (x["provider"], x["model"])):
            lines.append(
                f'{item["provider"]} | '
                f'{item["model"]} | '
                f'{item["status"]} | '
                f'{item["parameters"]} | '
                f'{item["checked_at"]}'
            )

        path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8"
        )

    def close(self):
        """
        Освобождает ресурсы анализатора:
        сбрасывает состояния моделей на диск
        и закрывает HTTP-сессию.
        """

        self.flush_states()

        self.session.close()

    def __del__(self):
        """
        Страховка на случай, если close()/flush_states()
        не были вызваны явно: сбрасывает накопленные
        состояния моделей на диск при уничтожении объекта.

        Регистрируется также в atexit, чтобы состояние
        сохранялось и при аварийном завершении процесса.
        """

        try:
            self.flush_states()
        except Exception:
            pass

    def _can_blacklist(self, error_type=None):
        """
        Определяет, нужно ли заносить модель
        в список проблемных.

        Временные ошибки соединения и ограничения
        по частоте запросов не считаются основанием
        для исключения модели.

        Остальные ошибки считаются ошибками самой модели
        или ее конфигурации и могут быть сохранены
        в bad_models.txt.
        """

        if error_type is None:
            error_type = self.last_error_type

        if error_type is None:
            return False

        if error_type in TEMPORARY_AI_ERRORS:
            return False

        return True

    # ===================================================
    # ФОРМИРОВАНИЕ ЗАПРОСА
    # ===================================================
    def _build_payload(self, entry, prompt):
        """
        Формирует JSON-запрос для AI.

        Параметры:
            entry — пара (provider, model).

        В запрос передаются:

            • идентификатор модели;
            • текст prompt;
            • режим stream.

        Дополнительные параметры добавляются только
        если провайдер их поддерживает (конфигурация
        провайдера):

            • temperature — температура генерации;
            • reasoning  — отключение reasoning.

        Возвращает:
            dict — готовый JSON для HTTP-запроса.
        """

        provider, model = entry

        config = self._providers[provider]

        # ---------------------------------------------------
        # Основные параметры запроса
        # ---------------------------------------------------
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "stream": config["stream"]
        }
        # ---------------------------------------------------
        # Дополнительные параметры провайдера
        # ---------------------------------------------------
        if config.get("temperature"):
            payload["temperature"] = self.temperature

        if config["reasoning"]:
            payload["reasoning"] = {
                "enabled": False
            }

        return payload

    # ===================================================
    # HTTP
    # ===================================================
    def _send_request(self, payload, retry_on_429=True, timeout=None,
                      provider=None):
        """
        Выполняет HTTP-запрос к AI.

        HTTP 429 обрабатывается по типу:

            HTTP429_QUOTA      — квота, средства, дневной лимит:
                                 ситуация без разрешения, запрос
                                 сразу завершается ошибкой.
            HTTP429_RATELIMIT  — временный лимит частоты:
                                 запрос повторяется до
                                 self.max_attempts попыток,
                                 пауза берётся из заголовка
                                 Retry-After (если он есть),
                                 иначе 15 секунд.

        Параметры:
            timeout  — таймаут одного HTTP-запроса.
                       Если не задан — используется self.timeout.
            provider — имя провайдера, к которому идёт запрос.
                       Определяет URL и заголовки.

        Возвращает:

            (str, None)       — успешный ответ;
            (None, error)     — ошибка запроса.
        """

        attempt = 0

        while True:

            attempt += 1

            response = self._send_http_request(
                payload,
                timeout=timeout,
                provider=provider
            )

            if response is None:
                return None, "HTTP_REQUEST"

            try:

                result, error_type = self._handle_http_response(
                    response
                )

                if result == "retry":

                    if not retry_on_429:
                        return None, error_type

                    if attempt >= self.max_attempts:
                        return None, error_type

                    self._sleep_retry_after(response)
                    continue

                if result is not True:
                    return None, error_type

                data = self._parse_http_json(response)

                if data is None:
                    return None, "INVALID_JSON"

                content = self._extract_content(data)

                if content is None:
                    return None, "EMPTY_CONTENT"

                return content, None

            finally:
                response.close()

    @staticmethod
    def _sleep_retry_after(response, default=15):
        """
        Ждёт перед повторным запросом после HTTP 429.

        Пауза берётся из заголовка Retry-After:

            • число секунд ("30");
            • HTTP-дата ("Wed, 21 Oct 2015 07:28:00 GMT").

        Если заголовка нет или он неразбираемый —
        используется default секунд.
        """

        delay = default

        value = response.headers.get("Retry-After")

        if value:

            try:
                delay = float(value)
            except ValueError:

                try:
                    retry_at = datetime.strptime(
                        value,
                        "%a, %d %b %Y %H:%M:%S GMT"
                    )
                    delay = max(
                        0,
                        (retry_at - datetime.utcnow()).total_seconds()
                    )
                except ValueError:
                    delay = default

        if delay < 0:
            delay = 0

        time.sleep(delay)

    def _send_http_request(self, payload, timeout=None, provider=None):
        """
        Выполняет один HTTP POST-запрос к AI.

        URL и заголовки берутся из конфигурации провайдера:
        каждая модель обращается к своему провайдеру.

        Метод отвечает только за транспортный уровень:

            • очищает информацию о предыдущей ошибке;
            • отправляет запрос;
            • фиксирует HTTP-статус;
            • сохраняет исключение при сетевой ошибке.

        Возвращает:
            Response — успешный HTTP-ответ;
            None     — если запрос завершился исключением.
        """
        self.last_error = ""
        self.last_error_type = None

        config = self._providers[provider]

        url = config["url"]
        headers = config["headers"]

        try:
            # ---------------------------------------------------
            # Отправка HTTP-запроса
            # ---------------------------------------------------
            response = self.session.post(
                url,
                headers=headers,
                json=payload,
                timeout=(
                    timeout
                    if timeout is not None
                    else self.timeout
                )
            )
            if response.status_code == 429:
                console_line("")
                console_line(
                    f"AI HTTP429: {response.text}"
                )
        # Сетевая ошибка.
        # HTTP-ответ в этом случае отсутствует.
        except Exception as e:

            self.last_error = str(e)
            self.last_error_type = type(e).__name__

            # Сетевая ошибка — внутренняя информация.
            # Модель просто считается не ответившей и заменяется
            # следующей моделью из рабочего пула.
            return None

        return response

    # Маркеры 429, которые не имеют разрешения:
    # исчерпанная квота, отсутствие средств, дневной лимит.
    # Повторять такие запросы бесполезно.
    HTTP429_QUOTA_MARKERS = (
        "tokens per day",
        "per day limit",
        "daily limit",
        "limit_rpd",
        "quota",
        "insufficient balance",
        "insufficient credits",
        "no credits",
        "billing",
        "payment required",
        "payment method",
        "card",
        "over your",
        "exceeded your",
        "beyond your",
        "free tier",
        "premium",
        "deactivated",
        "suspended",
    )

    # Маркеры 429, которые разрешимы: превышена частота
    # запросов на короткое окно. Повторять можно.
    HTTP429_RATELIMIT_MARKERS = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "too frequent",
        "requests per minute",
        "tokens per minute",
        "limit_rpm",
        "limit_tpm",
        "try again later",
        "retry",
        "temporarily",
        "slow down",
    )

    def _handle_http_response(self, response):
        """
        Обрабатывает HTTP-статус ответа AI.

        HTTP 429 классифицируется:

            HTTP429_QUOTA      — неразрешимая ситуация
                                 (квота, средства, дневной лимит);
                                 повторять бесполезно.
            HTTP429_RATELIMIT  — временный лимит частоты запросов;
                                 запрос можно повторить,
                                 лучше с учётом Retry-After.

        Возвращает:

            (True, None)                  — успешный ответ;
            ("retry", "HTTP429_RATELIMIT") — повторить запрос;
            (False, "HTTP429_QUOTA")       — окончательная ошибка;
            (False, "HTTPxxx")             — окончательная ошибка.
        """

        if response.status_code == 429:

            error_type = self._classify_http_429(response)

            if error_type == "HTTP429_QUOTA":
                return False, error_type

            return "retry", error_type

        if response.status_code != 200:

            error_type = f"HTTP{response.status_code}"

            try:
                error = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                error = response.text

            return False, error_type

        return True, None

    @staticmethod
    def _classify_http_429(response):
        """
        Определяет тип HTTP 429 по заголовкам и телу ответа.

        Приоритет:

        1. Заголовок Retry-After — сервер явно сообщает,
           когда можно повторить: это временный лимит.
        2. Маркеры квоты в теле — ситуация без разрешения.
        3. Маркеры rate limit в теле — временный лимит.
        4. По умолчанию — временный лимит.

        Возвращает:
            "HTTP429_QUOTA"     — ситуация без разрешения;
            "HTTP429_RATELIMIT" — временный лимит.
        """

        retry_after = response.headers.get(
            "Retry-After"
        )

        if retry_after:
            return "HTTP429_RATELIMIT"

        try:
            text = response.text.lower()
        except Exception:
            text = ""

        for marker in FacetAnalyzer.HTTP429_QUOTA_MARKERS:
            if marker in text:
                return "HTTP429_QUOTA"

        for marker in FacetAnalyzer.HTTP429_RATELIMIT_MARKERS:
            if marker in text:
                return "HTTP429_RATELIMIT"

        return "HTTP429_RATELIMIT"

    # ===================================================
    # РАЗБОР ОТВЕТА
    # ===================================================
    def _parse_http_json(self, response):
        """
        Разбирает JSON из успешного HTTP-ответа AI.

        Проверяет:

            • корректность JSON;
            • отсутствие поля error.

        Возвращает:

            dict — разобранный JSON ответа AI;
            None — если JSON некорректен
                    или AI вернул ошибку.
        """

        try:
            data = response.json()

        except ValueError as e:

            self.last_error = str(e)
            self.last_error_type = "INVALID_JSON"

            self.report(
                f"{type(e).__name__}: {e}"
            )

            return None

        if "error" in data:
            self.last_error = str(
                data.get("error")
            )

            self.last_error_type = "AI_ERROR"

            return None

        return data

    def _extract_content(self, data):
        """
        Извлекает текст ответа модели
        из OpenAI-compatible JSON.

        Ожидаемая структура:

            choices[0].message.content

        Проверяет наличие:

            • choices;
            • message;
            • content.

        Возвращает:

            str  — текст ответа модели;
            None — если ожидаемая структура отсутствует.
        """

        try:
            choices = data.get("choices")

            if not choices:
                self.last_error = "В ответе отсутствует choices."
                self.last_error_type = "INVALID_RESPONSE"
                return None

            message = choices[0].get("message", {})

            content = message.get("content")

            if content is None:
                self.last_error = (
                    "В ответе отсутствует message.content."
                )
                self.last_error_type = "INVALID_RESPONSE"
                return None

            return content

        except Exception as e:

            self.last_error = str(e)
            self.last_error_type = type(e).__name__

            return None
