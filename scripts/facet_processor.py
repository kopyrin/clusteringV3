"""
scripts/facet_processor.py — фасетная классификация слов через ансамбль LLM.

Объединяет:
  • FacetAnalyzer — работа с AI-провайдерами (пул моделей, голосование,
    обработка 429, сохранение состояний в bad_models.txt)
  • HypothesisFinder — BFS по графу, двухэтапная классификация,
    promote, импорт в БД

Режимы:
  (без флагов) — полный цикл: BFS → AI этап 1 → AI этап 2 → promote → импорт

Запуск:
  python scripts/facet_processor.py
"""
import atexit
import json
import os
import pickle
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from colorama import Fore, Style
from dotenv import load_dotenv

from common import (
    ROOT, DB_FILE, OUTPUT_DIR, START_WORDS_FILE,
    BAD_MODELS_FILE, AUTO_WORDS_FILE, AUTO_PODGROUP_FILE,
    LAST_FACETS_FILE, JUDGES_RAW_FILE,
    ALLOWED_POS_FACET,
    console_status, console_line, clean_json,
)

load_dotenv()


# ============================================================
# AI-КОНСТАНТЫ ()
# ============================================================

# Текущий провайдер (используется, если provider не задан явно)
AI_PROVIDER = "alibaba"

# Температура текста
AI_TEMPERATURE = 0
# Время на ответ ИИ в нормальном запросе
AI_TIMEOUT = 120
# Время на тестовый запрос при формировании пула
AI_TEST_TIMEOUT = 30
AI_MAX_ATTEMPTS = 5
# Размер пула рабочих моделей
AI_MODEL_POOL_SIZE = 30
# Модели тестируем несмотря на срок
RETEST_BAD_MODELS = False
# Срок в днях для повторной проверки модели
BAD_MODEL_RETEST_DAYS = 3

# Ошибки, при которых модель не считается плохой (временные)
TEMPORARY_AI_ERRORS = {
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectionError",
    "HTTP429_RATELIMIT",
}

# ============================================================
# AI-ПРОВАЙДЕРЫ
# ============================================================

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
        "exclude_prefixes": ["auto/"],
        "exclude_contains": [
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
    },
    "alibaba": {
        "url": "https://llm-97sm6wobqxie76ta.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        "models_url": "https://dashscope-intl.aliyuncs.com/api/v1/deployments/models",
        "key": os.getenv("ALIBABA_API_KEY"),
        "models_key": "output.models",
        "model_field": "model_name",
        "total_key": "output.total",
        "pagination": {
            "param": "page_no",
            "size_param": "page_size",
            "page_size": 100,
            "params": {"version": "v1.0", "model_source": "base"},
        },
        "stream": False,
        "temperature": True,
        "reasoning": True,
        "exclude_prefixes": [],
        "exclude_contains": [
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": ["free"],
            "contains": [], "exclude": [],
        },
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
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
            "prompt-guard", "orpheus",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
    },
    "tokenrouter": {
        "url": "https://api.tokenrouter.com/v1/chat/completions",
        "models_url": None,
        "models": ["moonshotai/kimi-k3-free"],
        "key": os.getenv("TOKENROUTER_API_KEY"),
        "models_key": "data",
        "model_field": "id",
        "stream": False,
        "temperature": True,
        "reasoning": True,
        "exclude_prefixes": [],
        "exclude_contains": [
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "model_filter": {
            "values": ["moonshotai/kimi-k3-free"],
            "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
        "exclude_properties": ["require_workers_paid"],
        "exclude_prefixes": [],
        "exclude_contains": [
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "exclude_tags": [
            "moderation", "safety", "content-filtering", "guardrails",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
            "embedding", "embed", "rerank", "tts", "whisper",
            "audio", "vision", "image", "moderation", "safety",
        ],
        "model_filter": {
            "values": [], "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
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
            "embedding", "embed", "moderation", "ocr",
            "audio", "transcription", "tts",
        ],
        "model_filter": {
            "values": [
                "mistral-small-2603", "mistral-medium-3-5",
                "mistral-large-2512", "ministral-14b-2512", "glm-5-2",
            ],
            "prefix": [], "suffix": [],
            "contains": [], "exclude": [],
        },
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
            "embedding", "embed", "rerank", "tts", "whisper",
            "vision", "image", "moderation", "audio",
        ],
        "exclude_tags": [],
        "model_filter": {
            "values": [], "prefix": [], "suffix": ["free"],
            "contains": [], "exclude": [],
        },
    },
}


# ============================================================
# КОНСТАНТЫ ФАСЕТНОЙ КЛАССИФИКАЦИИ
# ============================================================

# Глубина поиска по соседям
MAX_DEPTH = 1
# Количество слов, передаваемых ИИ
TARGET_GRAPH_SIZE = 100
# Сколько слов отправлять одной модели за один запрос (этап 1)
AI_BATCH_SIZE = 30
# Сколько слов отправлять одной модели за один запрос (этап 2)
AI_SECOND_BATCH_SIZE = 30
# Начальный порог силы связи
START_LINK_WEIGHT = 100
# Если True — стартовые слова сразу передаются AI без поиска
CLASSIFY_START_WORDS_ONLY = False
# Количество судей для голосования
MIN_VOTES = 3

# Типы групп
GROUP_TYPE_NORMAL = 1
GROUP_TYPE_STUDY = 2
GROUP_TYPE_FACET = 3

# Разрешённые части речи для фасетной классификации
# (используем ALLOWED_POS_FACET из common.py)
ALLOWED_POS = ALLOWED_POS_FACET

# Группы частей речи, исключаемые из тематических групп
NO_FILTER_GROUPS = {
    2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
}
POS_GROUP_IDS = tuple(sorted(NO_FILTER_GROUPS))

CONFIRM_FILES = [
    "output/last_facets.txt",
    "output/level1.txt",
]


# ============================================================
# FacetAnalyzer — работа с AI-провайдерами
# ============================================================

class FacetAnalyzer:
    """
    Анализатор фасетов через ансамбль LLM-моделей.
    Поддерживает множество провайдеров, пул рабочих моделей,
    голосование MIN_VOTES судей, обработку 429, сохранение
    состояний моделей в bad_models.txt.
    """

    def __init__(self, report, provider=None, model=None):
        self.report = report
        if provider == "all":
            self.provider = "all"
            provider_names = list(AI_PROVIDERS.keys())
        else:
            self.provider = provider or AI_PROVIDER
            provider_names = [self.provider]

        self._providers = {}
        for name in provider_names:
            cfg = dict(AI_PROVIDERS[name])
            url = cfg["url"]
            models_url = cfg["models_url"]
            account_id = cfg.get("account_id")
            if account_id:
                url = url.format(account_id=account_id)
                if models_url:
                    models_url = models_url.format(account_id=account_id)
            headers = {"Content-Type": "application/json"}
            key = cfg.get("key")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            cfg["url"] = url
            cfg["models_url"] = models_url
            cfg["headers"] = headers
            self._providers[name] = cfg

        self.model = model
        self.no_models = False
        self.temperature = AI_TEMPERATURE
        self.timeout = AI_TIMEOUT
        self.test_timeout = AI_TEST_TIMEOUT
        self.max_attempts = AI_MAX_ATTEMPTS

        self.session = requests.Session()
        self.session.trust_env = False

        self.models = []
        self.active_models = []
        self._pool_lock = threading.Lock()
        self._states_lock = threading.Lock()
        self._local = threading.local()

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
        недостающие ответы добираются следующими моделями.
        """
        if not prompt or judges < 1:
            return None

        if not self.active_models:
            self._ensure_model()
        if not self.active_models:
            return None

        answers = []
        used = set()
        while len(answers) < judges:
            with self._pool_lock:
                candidates = [m for m in self.active_models if m not in used]
            if not candidates:
                break
            models = candidates[:judges]
            if not models:
                break
            used.update(models)

            with ThreadPoolExecutor(max_workers=len(models)) as executor:
                futures = {}
                for entry in models:
                    payload = self._build_payload(entry, prompt)
                    _, model = entry
                    console_status(f"AI: запускаю судью | {model}")
                    future = executor.submit(self._judge_request, entry, payload)
                    futures[future] = entry

                for future in futures:
                    try:
                        entry, content, error_type = future.result()
                    except Exception:
                        continue
                    _, model = entry
                    if content is None:
                        console_status(f"AI: {model} | ответ не получен: {error_type}")
                        if error_type and error_type not in TEMPORARY_AI_ERRORS:
                            self._deactivate_model(entry, error_type)
                        continue
                    answers.append({"model": model, "answer": content})
                    console_status(f"AI: {model} | ответ принят ({len(answers)}/{judges})")
                    if len(answers) >= judges:
                        break

        if len(answers) < judges:
            return None
        return answers[:judges]

    def _judge_request(self, entry, payload):
        provider, model = entry
        t0 = time.perf_counter()
        content, error_type = self._send_request(
            payload, retry_on_429=True, timeout=self.timeout, provider=provider,
        )
        elapsed = time.perf_counter() - t0
        console_status(f"AI: {model} | HTTP: {elapsed:.2f} сек")
        return entry, content, error_type

    def _deactivate_model(self, entry, error_type):
        provider, model = entry
        with self._pool_lock:
            if entry in self.active_models:
                self.active_models.remove(entry)
        self._save_model_state(provider, model, status=error_type, parameters="")
        console_status(f"AI: {model} | исключена из пула: {error_type}")

    # ===================================================
    # ПОДГОТОВКА МОДЕЛЕЙ
    # ===================================================

    def _ensure_model(self):
        if self.model and self.provider != "all":
            with self._pool_lock:
                if not self.active_models:
                    self.active_models = [(self.provider, self.model)]
            return True
        with self._pool_lock:
            has_active = bool(self.active_models)
        if has_active:
            return True
        self._load_models()
        if not self.models:
            self.no_models = True
            self.report("Не найдено ни одной доступной модели.")
            return False
        self.active_models = self._find_working_models(AI_MODEL_POOL_SIZE)
        if not self.active_models:
            self.no_models = True
            self.report("Не найдено ни одной рабочей модели.")
            return False
        return True

    def _load_models(self):
        self.models.clear()
        for provider, config in self._providers.items():
            self._load_provider_models(provider, config)
        self.models = list(dict.fromkeys(self.models))
        console_status(f"Получено моделей: {len(self.models)}")
        return self.models

    def _load_provider_models(self, provider, config):
        try:
            if not config["models_url"]:
                configured_models = config.get("models", [])
                if configured_models:
                    self.models.extend((provider, m) for m in configured_models)
                    console_status(f"Модели из конфигурации ({provider}): {len(configured_models)}")
                    return
                console_status(f"У провайдера {provider} нет URL списка моделей.")
                return

            pagination = config.get("pagination")
            page_param = None
            size_param = None
            page_size = None
            fixed_params = {}
            if pagination:
                page_param = pagination.get("param", "page")
                size_param = pagination.get("size_param", "page_size")
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
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                models_data = self._get_path(data, config["models_key"], [])
                for item in models_data:
                    entry = self._extract_model(item, config, provider)
                    if entry is not None:
                        self.models.append(entry)

                if not page_param:
                    break
                if not models_data:
                    break
                if len(models_data) < page_size:
                    break
                total = self._get_path(data, config.get("total_key"), None)
                if total and len(self.models) >= total:
                    break
                page_no += 1
        except Exception as e:
            self.last_error = str(e)
            self.report(f"Не удалось получить список моделей ({provider}).")
            self.report(f"{type(e).__name__}: {e}")

    @staticmethod
    def _get_path(data, key, default=None):
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
        if not isinstance(item, dict):
            return None
        model = item.get(config["model_field"], "")
        if not model:
            return None
        if config.get("lowercase_model_ids", True):
            model = model.lower()

        task_filter = config.get("task_filter")
        if task_filter:
            task = item.get("task", {})
            task_name = task.get("name", "")
            if task_name != task_filter:
                return None
            exclude_tags = config.get("exclude_tags", [])
            if exclude_tags:
                tags = item.get("tags", [])
                if any(tag in exclude_tags for tag in tags):
                    return None

        exclude_properties = config.get("exclude_properties", [])
        if exclude_properties:
            properties = item.get("properties", [])
            property_ids = {prop.get("property_id") for prop in properties}
            if any(prop in property_ids for prop in exclude_properties):
                return None

        if any(model.startswith(prefix) for prefix in config["exclude_prefixes"]):
            return None
        if any(text in model for text in config["exclude_contains"]):
            return None

        model_filter = config.get("model_filter", {})
        values = model_filter.get("values", [])
        if values and model not in values:
            return None
        prefixes = model_filter.get("prefix", [])
        if prefixes and not any(model.startswith(p) for p in prefixes):
            return None
        suffixes = model_filter.get("suffix", [])
        if suffixes and not any(model.endswith(s) for s in suffixes):
            return None
        contains = model_filter.get("contains", [])
        if contains and not any(text in model for text in contains):
            return None
        exclude = model_filter.get("exclude", [])
        if any(text in model for text in exclude):
            return None

        key = f"{provider}|{model}"
        state = self.bad_models.get(key)
        if state and state["status"] != "WORKING" and not RETEST_BAD_MODELS:
            try:
                checked_at = datetime.strptime(state["checked_at"], "%Y-%m-%d %H:%M:%S")
                age_days = (datetime.now() - checked_at).total_seconds() / 86400
            except Exception:
                age_days = 999
            if age_days < BAD_MODEL_RETEST_DAYS:
                return None

        return (provider, model)

    def _find_working_models(self, count):
        active = []
        for entry in self.models:
            provider, model = entry
            console_status(f"AI: проверка пула | {len(active)}/{count}")
            ok = self._test_model(entry)
            if ok:
                active.append(entry)
                self._save_model_state(provider, model, status="WORKING", parameters="")
            else:
                status = self.last_error_type or "UNKNOWN"
                if self._can_blacklist(status):
                    self._save_model_state(provider, model, status=status, parameters="")
            console_status(f"AI: проверка пула | {len(active)}/{count}")
            if len(active) >= count:
                break
        console_status(
            f"AI-пул готов: {len(active)} рабочих моделей | "
            f"для одного пакета: {MIN_VOTES} судьи"
        )
        return active

    def _test_model(self, entry):
        provider, model = entry
        prompt = """
Верни строго JSON-массив из четырёх объектов:
[
    {"слово": "chair", "группы": []},
    {"слово": "table", "группы": []},
    {"слово": "red", "группы": []},
    {"слово": "computer", "группы": []}
]
Не добавляй пояснений. Верни только JSON.
""".strip()
        payload = self._build_payload(entry, prompt)
        content, error_type = self._send_request(
            payload, retry_on_429=False, timeout=self.test_timeout, provider=provider,
        )
        if content is None:
            console_line("")
            console_line(f"AI: модель {model} | тест HTTP: {error_type}")
            return False
        try:
            if not self._validate_model_test_response(content):
                console_line("")
                console_line(f"AI: модель {model} | тест FAILED: INVALID_AI_RESPONSE")
                return False
            console_status(f"AI: модель {model} | тест OK")
            return True
        except Exception as e:
            console_line("")
            console_line(f"AI: модель {model} | тест FAILED: {type(e).__name__}: {e}")
            return False

    def _validate_model_test_response(self, content):
        if not content:
            return False
        content = content.strip()
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
        if not isinstance(result, list) or not result:
            return False
        for item in result:
            if not isinstance(item, dict):
                return False
            if not isinstance(item.get("слово"), str):
                return False
            if not isinstance(item.get("группы"), list):
                return False
        return True

    # ===================================================
    # СОСТОЯНИЕ МОДЕЛЕЙ
    # ===================================================

    def _load_model_states(self):
        self.bad_models = {}
        if not BAD_MODELS_FILE.exists():
            return
        for line in BAD_MODELS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 5:
                continue
            provider, model, status, parameters, checked_at = parts[:5]
            key = f"{provider}|{model}"
            self.bad_models[key] = {
                "provider": provider, "model": model,
                "status": status, "parameters": parameters,
                "checked_at": checked_at,
            }

    def _save_model_state(self, provider, model, status=None, parameters=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if status is None:
            status = self.last_error_type or "UNKNOWN"
        key = f"{provider}|{model}"
        with self._states_lock:
            self.bad_models[key] = {
                "provider": provider, "model": model,
                "status": status, "parameters": parameters,
                "checked_at": now,
            }

    def flush_states(self):
        with self._states_lock:
            snapshot = dict(self.bad_models)
        BAD_MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for item in sorted(snapshot.values(), key=lambda x: (x["provider"], x["model"])):
            lines.append(
                f'{item["provider"]} | {item["model"]} | '
                f'{item["status"]} | {item["parameters"]} | {item["checked_at"]}'
            )
        BAD_MODELS_FILE.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8",
        )

    def close(self):
        self.flush_states()
        self.session.close()

    def __del__(self):
        try:
            self.flush_states()
        except Exception:
            pass

    def _can_blacklist(self, error_type=None):
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
        provider, model = entry
        config = self._providers[provider]
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": config["stream"],
        }
        if config.get("temperature"):
            payload["temperature"] = self.temperature
        if config["reasoning"]:
            payload["reasoning"] = {"enabled": False}
        return payload

    # ===================================================
    # HTTP
    # ===================================================

    HTTP429_QUOTA_MARKERS = (
        "tokens per day", "per day limit", "daily limit", "limit_rpd",
        "quota", "insufficient balance", "insufficient credits", "no credits",
        "billing", "payment required", "payment method", "card",
        "over your", "exceeded your", "beyond your", "free tier",
        "premium", "deactivated", "suspended",
    )
    HTTP429_RATELIMIT_MARKERS = (
        "rate limit", "rate_limit", "too many requests", "too frequent",
        "requests per minute", "tokens per minute", "limit_rpm", "limit_tpm",
        "try again later", "retry", "temporarily", "slow down",
    )

    def _send_request(self, payload, retry_on_429=True, timeout=None, provider=None):
        attempt = 0
        while True:
            attempt += 1
            response = self._send_http_request(payload, timeout=timeout, provider=provider)
            if response is None:
                return None, "HTTP_REQUEST"
            try:
                result, error_type = self._handle_http_response(response)
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
        delay = default
        value = response.headers.get("Retry-After")
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    retry_at = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT")
                    delay = max(0, (retry_at - datetime.utcnow()).total_seconds())
                except ValueError:
                    delay = default
        if delay < 0:
            delay = 0
        time.sleep(delay)

    def _send_http_request(self, payload, timeout=None, provider=None):
        self.last_error = ""
        self.last_error_type = None
        config = self._providers[provider]
        url = config["url"]
        headers = config["headers"]
        try:
            response = self.session.post(
                url, headers=headers, json=payload,
                timeout=timeout if timeout is not None else self.timeout,
            )
            if response.status_code == 429:
                console_line("")
                console_line(f"AI HTTP429: {response.text}")
        except Exception as e:
            self.last_error = str(e)
            self.last_error_type = type(e).__name__
            return None
        return response

    def _handle_http_response(self, response):
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
        retry_after = response.headers.get("Retry-After")
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

    def _parse_http_json(self, response):
        try:
            data = response.json()
        except ValueError as e:
            self.last_error = str(e)
            self.last_error_type = "INVALID_JSON"
            self.report(f"{type(e).__name__}: {e}")
            return None
        if "error" in data:
            self.last_error = str(data.get("error"))
            self.last_error_type = "AI_ERROR"
            return None
        return data

    def _extract_content(self, data):
        try:
            choices = data.get("choices")
            if not choices:
                self.last_error = "В ответе отсутствует choices."
                self.last_error_type = "INVALID_RESPONSE"
                return None
            message = choices[0].get("message", {})
            content = message.get("content")
            if content is None:
                self.last_error = "В ответе отсутствует message.content."
                self.last_error_type = "INVALID_RESPONSE"
                return None
            return content
        except Exception as e:
            self.last_error = str(e)
            self.last_error_type = type(e).__name__
            return None


# ============================================================
# HypothesisFinder — фасетная классификация
# ============================================================

class HypothesisFinder:

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.word_info_cache = {}
        self.auto_words = {}
        self.auto_podgroup = {}
        self.min_link_weight = START_LINK_WEIGHT

        self.connect()
        self.facet_analyzer = FacetAnalyzer(self.report, provider="all")
        self.last_error_type = None
        self.last_error = None

        self.batch_size = AI_BATCH_SIZE
        self.second_batch_size = AI_SECOND_BATCH_SIZE
        self.start_words = []
        self.start_words_ids = []
        self.studies = []
        self.first_stage_result = []
        self.judges_raw = []
        self._judges_raw_lock = threading.Lock()

    def connect(self):
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self.load_auto_groups()
        self.load_auto_podgroups()
        self.cursor.executescript("""
            DROP TABLE IF EXISTS wave;
            DROP TABLE IF EXISTS wave_next;
            DROP TABLE IF EXISTS visited;
            CREATE TABLE wave (
                word_id INTEGER PRIMARY KEY, parent_id INTEGER,
                depth INTEGER, weakest INTEGER
            );
            CREATE TABLE wave_next (
                word_id INTEGER PRIMARY KEY, parent_id INTEGER,
                depth INTEGER, weakest INTEGER
            );
            CREATE TABLE visited (
                word_id INTEGER PRIMARY KEY, parent_id INTEGER,
                depth INTEGER, weakest INTEGER
            );
        """)
        self.conn.commit()

    # ===================================================
    # ПОДГОТОВКА
    # ===================================================

    def load_auto_groups(self):
        if AUTO_WORDS_FILE.exists():
            try:
                with AUTO_WORDS_FILE.open("rb") as f:
                    self.auto_words = pickle.load(f)
                if not isinstance(self.auto_words, dict):
                    raise ValueError("Кэш auto_words имеет неправильный формат.")
                console_status(f"Взято из файла кэша: {len(self.auto_words)} записей")
                return
            except (EOFError, pickle.UnpicklingError, ValueError, OSError) as e:
                console_line(f"Кэш auto_words повреждён: {type(e).__name__}: {e}")
                console_line("Кэш будет пересоздан из базы данных.")
                try:
                    AUTO_WORDS_FILE.unlink()
                except OSError:
                    pass

        self.auto_words = {}
        placeholders = ",".join("?" for _ in POS_GROUP_IDS)
        self.cursor.execute(
            f"""
            SELECT w.word, g.name
            FROM group_words gw
            JOIN words w ON w.id = gw.word_id
            JOIN groups g ON g.id = gw.group_id
            WHERE g.type = ? AND g.id NOT IN ({placeholders})
            """,
            (GROUP_TYPE_NORMAL,) + POS_GROUP_IDS,
        )
        rows = self.cursor.fetchall()
        for word, group_name in rows:
            self.auto_words[word] = group_name
        console_line(f"записей в кэше = {len(rows)}")

        temp_file = AUTO_WORDS_FILE.with_suffix(".pkl.tmp")
        try:
            with temp_file.open("wb") as f:
                pickle.dump(self.auto_words, f, protocol=pickle.HIGHEST_PROTOCOL)
                f.flush()
            temp_file.replace(AUTO_WORDS_FILE)
            console_line("кэш для фильтра создан")
        except Exception:
            try:
                temp_file.unlink()
            except OSError:
                pass
            raise

    def load_auto_podgroups(self):
        if AUTO_PODGROUP_FILE.exists():
            with AUTO_PODGROUP_FILE.open("rb") as f:
                self.auto_podgroup = pickle.load(f)
            total = sum(len(groups) for groups in self.auto_podgroup.values())
            console_status(f"Взято из файла кэша подгрупп: {total} записей")
            return

        self.auto_podgroup = {}
        excluded = tuple(NO_FILTER_GROUPS)
        placeholders = ",".join("?" for _ in excluded)
        self.cursor.execute(
            f"""
            SELECT parent.id, parent.name, child.id, child.name
            FROM groups AS parent
            JOIN groups AS child ON child.parent_id = parent.id
            WHERE parent.id NOT IN ({placeholders})
              AND child.id NOT IN ({placeholders})
            ORDER BY parent.id, child.id
            """,
            excluded + excluded,
        )
        rows = self.cursor.fetchall()
        for parent_id, parent_name, child_id, child_name in rows:
            self.auto_podgroup.setdefault(parent_name, []).append(child_name)

        with AUTO_PODGROUP_FILE.open("wb") as f:
            pickle.dump(self.auto_podgroup, f, protocol=pickle.HIGHEST_PROTOCOL)
        total = sum(len(groups) for groups in self.auto_podgroup.values())
        console_line(f"Кэш подгрупп создан: {total} записей")

    def load_start_words(self):
        # Используем START_WORDS_FILE из common.py (исправлен баг с "start_words.txt")
        if not START_WORDS_FILE.exists():
            self.report(f"Файл стартовых слов не найден: {START_WORDS_FILE}")
            return
        self.studies = []
        all_words = []
        all_ids = []
        seen_ids = set()
        for line in START_WORDS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            words = line.split()
            for word in words:
                word_id = self.find_word_id(word)
                if word_id is None:
                    self.report(f"Не найдено: {word}")
                    continue
                if word_id in seen_ids:
                    continue
                seen_ids.add(word_id)
                all_words.append(word)
                all_ids.append(word_id)
        if all_words:
            self.studies.append((all_words, all_ids))

    # ===================================================
    # ЗАПУСК ИССЛЕДОВАНИЯ
    # ===================================================

    def run_study(self):
        self.load_start_words()
        for self.start_words, self.start_words_ids in self.studies:
            self.report()
            if CLASSIFY_START_WORDS_ONLY:
                self.find_related_words(start_words_only=True)
            else:
                self.find_related_words()
            if self.facet_analyzer.no_models:
                console_line("Рабочих AI-моделей нет. Исследование остановлено.")
                return

    # ===================================================
    # ПОИСК СВЯЗАННЫХ СЛОВ
    # ===================================================

    def find_related_words(self, start_words_only=False):
        if start_words_only:
            filtered = [{"слово": word} for word in self.start_words]
            console_line(f"Режим без поиска: {len(filtered)} готовых слов передано судьям")
            ai_data = {
                "Стартовые слова": self.start_words,
                "Связанные слова": filtered,
            }
        else:
            self.min_link_weight = START_LINK_WEIGHT
            lower_weight = None
            upper_weight = None
            graph_attempt = 0

            def evaluate_weight(weight):
                nonlocal graph_attempt
                graph_attempt += 1
                self.min_link_weight = weight
                self._build_graph()
                ai_data = self._build_ai_data()
                filtered = self._filter_for_ai(ai_data["Связанные слова"])
                return ai_data, filtered, len(filtered)

            weight = START_LINK_WEIGHT
            ai_data, filtered, filtered_count = evaluate_weight(weight)
            best_weight = weight
            best_distance = abs(filtered_count - TARGET_GRAPH_SIZE)
            best_filtered = filtered

            if filtered_count > TARGET_GRAPH_SIZE:
                lower_weight = weight
                while filtered_count > TARGET_GRAPH_SIZE:
                    weight *= 2
                    ai_data, filtered, filtered_count = evaluate_weight(weight)
                    distance = abs(filtered_count - TARGET_GRAPH_SIZE)
                    if distance < best_distance:
                        best_weight = weight
                        best_distance = distance
                        best_filtered = filtered
                    upper_weight = weight
            elif filtered_count < TARGET_GRAPH_SIZE:
                upper_weight = weight
                while filtered_count < TARGET_GRAPH_SIZE:
                    weight = max(1, weight // 2)
                    if weight == upper_weight:
                        break
                    ai_data, filtered, filtered_count = evaluate_weight(weight)
                    distance = abs(filtered_count - TARGET_GRAPH_SIZE)
                    if distance < best_distance:
                        best_weight = weight
                        best_distance = distance
                        best_filtered = filtered
                    lower_weight = weight
                    if weight == 1:
                        break
            else:
                best_weight = weight
                best_filtered = filtered

            if lower_weight is not None and upper_weight is not None:
                while upper_weight - lower_weight > 1:
                    weight = (lower_weight + upper_weight) // 2
                    ai_data, filtered, filtered_count = evaluate_weight(weight)
                    distance = abs(filtered_count - TARGET_GRAPH_SIZE)
                    if distance < best_distance:
                        best_weight = weight
                        best_distance = distance
                        best_filtered = filtered
                    if filtered_count > TARGET_GRAPH_SIZE:
                        lower_weight = weight
                    elif filtered_count < TARGET_GRAPH_SIZE:
                        upper_weight = weight
                    else:
                        best_weight = weight
                        best_filtered = filtered
                        break

            self.min_link_weight = best_weight
            console_status(
                f"Граф: {graph_attempt} перестроений | порог {best_weight} | "
                f"перед AI {len(best_filtered)}"
            )
            filtered = best_filtered

        ai_data["Связанные слова"] = filtered

        batches = []
        for i in range(0, len(ai_data["Связанные слова"]), self.batch_size):
            batches.append(ai_data["Связанные слова"][i:i + self.batch_size])

        self.first_stage_result = []
        for batch_no, batch in enumerate(batches, start=1):
            console_status(f"ЭТАП 1 | пакет {batch_no}/{len(batches)} | подготовка")
            words = [item["слово"] for item in batch]
            prompt = self._build_first_prompt(words)
            answers = self.facet_analyzer.ask(prompt, judges=MIN_VOTES)
            if answers is None:
                console_line("Первый этап остановлен: не удалось получить 3 ответа AI.")
                return

            for judge_no, judge in enumerate(answers, start=1):
                with self._judges_raw_lock:
                    self.judges_raw.append({
                        "stage": "first", "batch": batch_no, "judge": judge_no,
                        "model": judge["model"], "words": words,
                        "prompt": prompt, "answer": judge["answer"],
                    })

            judge_results = []
            for judge in answers:
                result = self._parse_first_result(judge["answer"])
                if result is None:
                    continue
                judge_results.append(result)

            if len(judge_results) < MIN_VOTES:
                console_line("Первый этап остановлен: недостаточно корректных ответов AI.")
                return

            voted_result = self._vote_first_stage(judge_results)
            self.first_stage_result.extend(voted_result)

        self._save_first_stage_files(self.first_stage_result)
        self._run_second_stage()
        console_line("Второй этап завершен → last_facets.txt")
        self._append_second_stage_to_last_facets()
        self._save_judges_raw()
        console_line("Готово → last_facets.txt")

    # ===================================================
    # ПОСТРОЕНИЕ ГРАФА
    # ===================================================

    def _build_graph(self):
        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("DELETE FROM wave_next")
        self.cursor.execute("DELETE FROM visited")
        for word_id in self.start_words_ids:
            self.cursor.execute(
                "INSERT INTO wave (word_id, parent_id, depth, weakest) VALUES (?, ?, ?, ?)",
                (word_id, word_id, 0, 2147483647),
            )
        self.cursor.execute("INSERT OR IGNORE INTO visited SELECT * FROM wave")
        for depth in range(1, MAX_DEPTH + 1):
            rows = self._find_next_wave()
            if rows == 0:
                break
        rows = self.cursor.execute("SELECT COUNT(*) FROM visited").fetchone()[0]
        return rows

    def _find_next_wave(self):
        self.cursor.execute(
            """
            INSERT OR IGNORE INTO wave_next (word_id, parent_id, depth, weakest)
            SELECT l.word2_id, w.word_id, w.depth + 1,
                   CASE WHEN w.weakest < l.count THEN w.weakest ELSE l.count END
            FROM wave w
            CROSS JOIN links l ON l.word1_id = w.word_id
            LEFT JOIN visited v ON v.word_id = l.word2_id
            WHERE l.count >= ? AND v.word_id IS NULL
            """,
            (self.min_link_weight,),
        )
        self.cursor.execute("SELECT COUNT(*) FROM wave_next")
        rows = self.cursor.fetchone()[0]
        self.cursor.execute("INSERT OR REPLACE INTO visited SELECT * FROM wave_next")
        self.cursor.execute("DELETE FROM wave")
        self.cursor.execute("INSERT INTO wave SELECT * FROM wave_next")
        self.cursor.execute("DELETE FROM wave_next")
        return rows

    def _find_word(self, word=None, word_id=None):
        if word_id is not None:
            self.cursor.execute("SELECT word FROM words WHERE id = ?", (word_id,))
        else:
            self.cursor.execute("SELECT id FROM words WHERE word = ?", (word,))
        return self.cursor.fetchone()

    def find_word_id(self, word):
        row = self._find_word(word=word)
        return row[0] if row else None

    def get_neighbors(self, word_id):
        self.cursor.execute(
            """
            SELECT CASE WHEN word1_id = ? THEN word2_id ELSE word1_id END AS neighbor_id, count
            FROM links WHERE word1_id = ? OR word2_id = ?
            ORDER BY count DESC
            """,
            (word_id, word_id, word_id),
        )
        return self.cursor.fetchall()

    def _build_ai_data(self):
        ai_data = {"Стартовые слова": self.start_words, "Связанные слова": []}
        self.cursor.execute("SELECT word_id FROM visited ORDER BY weakest DESC")
        rows = self.cursor.fetchall()
        for (word_id,) in rows:
            word = self.get_word(word_id)
            if word:
                ai_data["Связанные слова"].append({"слово": word})
        return ai_data

    def _filter_for_ai(self, words):
        result = []
        for item in words:
            word = item["слово"]
            self.cursor.execute("SELECT pos FROM words WHERE word = ?", (word,))
            row = self.cursor.fetchone()
            if not row:
                continue
            if row[0] not in ALLOWED_POS:
                continue
            if any(c.isalpha() for c in word) and any(c.isdigit() for c in word):
                continue
            if word in self.auto_words:
                continue
            result.append(item)
        return result

    # ===================================================
    # ПЕРВЫЙ ЭТАП — ОПРЕДЕЛЕНИЕ ГРУПП
    # ===================================================

    def _build_first_prompt(self, words):
        top_groups = list(self.auto_podgroup.keys())
        prompt = [
            "Ответь строго одним корректным JSON-массивом, без пояснений и другого текста.",
            "Для каждого слова определи наиболее подходящие верхние группы.",
            "Используй только названия групп из списка, ничего нового не добавляй.",
            "Для каждого слова выбери не более двух групп.",
            "Если слово уверенно относится только к одной группе, укажи только её.",
            "Если слово нельзя уверенно отнести ни к одной группе, верни пустой список.",
            "",
            "Верхние группы:",
        ]
        for group_name in top_groups:
            prompt.append(f"- {group_name}")
        prompt.append("")
        prompt.append("Слова для классификации:")
        for word in words:
            prompt.append(word)
        prompt.append("")
        prompt.append("Ответ:")
        prompt.append("""Формат ответа — JSON-массив:
[
  {"слово": "слово из списка", "группы": ["группа из разрешённых"]}
]""".strip())
        return "\n".join(prompt)

    def _parse_first_result(self, content, expected_words=None, report_error=True):
        if not content or not content.strip():
            self._save_bad_answer(content)
            return None
        content = clean_json(content)
        try:
            result = json.loads(content)
            if isinstance(result, dict):
                result = [result]
            if not isinstance(result, list):
                raise Exception("Ответ первого этапа должен быть JSON-массивом.")
            allowed_groups = set(self.auto_podgroup.keys())
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
                groups = [g for g in groups if g in allowed_groups]
                groups = groups[:2]
                clean_result.append({"слово": word, "группы": groups})

            if expected_words is not None:
                expected = [str(w).strip().lower() for w in expected_words if str(w).strip()]
                actual = [item["слово"].strip().lower() for item in clean_result if item.get("слово")]
                expected_set = set(expected)
                actual_set = set(actual)
                if not clean_result:
                    if report_error:
                        self.report("AI не вернул ни одного результата для пакета.")
                    self._save_bad_answer(content)
                    return None
                if len(clean_result) != len(expected):
                    if report_error:
                        self.report(f"AI вернул {len(clean_result)} результатов из {len(expected)} ожидаемых.")
                    self._save_bad_answer(content)
                    return None
                if len(actual) != len(actual_set):
                    if report_error:
                        self.report("AI вернул повторяющиеся слова.")
                    self._save_bad_answer(content)
                    return None
                if actual_set != expected_set:
                    if report_error:
                        self.report("AI вернул не тот набор слов, который был передан в запросе.")
                    self._save_bad_answer(content)
                    return None
            return clean_result
        except json.JSONDecodeError:
            if report_error:
                self.report("Неверный JSON первого этапа")
            self._save_bad_answer(content)
            return None

    def _vote_first_stage(self, judge_results):
        votes = Counter()
        for result in judge_results:
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
            voted.append({"слово": word, "группы": groups[:2]})
        return voted

    def _save_first_stage_files(self, result):
        output_dir = OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        for group_name in self.auto_podgroup:
            file_path = output_dir / f"ai_1_{group_name}.txt"
            file_path.write_text("", encoding="utf-8")

        files = {}
        try:
            for item in result:
                word = item["слово"]
                for group_name in item["группы"]:
                    if group_name not in self.auto_podgroup:
                        continue
                    if group_name not in files:
                        file_path = output_dir / f"ai_1_{group_name}.txt"
                        files[group_name] = file_path.open("a", encoding="utf-8")
                    files[group_name].write(word + "\n")
        finally:
            for file in files.values():
                file.close()

        for group_name in self.auto_podgroup:
            file_path = output_dir / f"ai_1_{group_name}.txt"
            if not file_path.exists():
                continue
            count = sum(1 for line in file_path.open("r", encoding="utf-8") if line.strip())
            if count:
                console_line(f"{group_name}: {count} слов")

    # ===================================================
    # ВТОРОЙ ЭТАП — ОПРЕДЕЛЕНИЕ ПОДГРУПП
    # ===================================================

    def _run_second_stage(self):
        output_dir = OUTPUT_DIR
        for group_name in self.auto_podgroup:
            input_file = output_dir / f"ai_1_{group_name}.txt"
            if not input_file.exists():
                continue
            words = [line.strip() for line in input_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not words:
                continue
            batches = []
            for i in range(0, len(words), self.second_batch_size):
                batches.append(words[i:i + self.second_batch_size])
            console_status(f"Второй этап: {group_name} | {len(words)} слов | {len(batches)} пакетов")

            group_results = []

            def classify_one(batch_no_batch):
                batch_no, batch = batch_no_batch
                return self._classify_second_stage_group(group_name, batch, batch_no, len(batches))

            with ThreadPoolExecutor(max_workers=len(batches)) as executor:
                for result in executor.map(classify_one, enumerate(batches, start=1)):
                    group_results.extend(result or [])

            output_file = output_dir / f"ai_2_{group_name}.txt"
            with output_file.open("a", encoding="utf-8") as f:
                for item in group_results:
                    word = item.get("слово")
                    podgroup = item.get("подгруппа")
                    if not word or not podgroup:
                        continue
                    f.write(f"{group_name}\t{podgroup}\t{word}\n")

    def _classify_second_stage_group(self, group_name, words, batch_no=None, total_batches=None):
        if not words:
            return []
        prompt = self._build_second_prompt(words, group_name)
        answers = self.facet_analyzer.ask(prompt, judges=MIN_VOTES)
        if answers is None:
            console_line(f"Не удалось получить ответы всех AI-судей для группы: {group_name}")
            return []

        for judge_no, judge in enumerate(answers, start=1):
            with self._judges_raw_lock:
                self.judges_raw.append({
                    "stage": "second", "batch": batch_no, "group": group_name,
                    "judge": judge_no, "model": judge["model"],
                    "words": words, "prompt": prompt, "answer": judge["answer"],
                })

        judge_results = []
        for judge in answers:
            result = self._parse_second_result(judge["answer"])
            if result is not None:
                judge_results.append(result)

        if len(judge_results) < MIN_VOTES:
            console_line(f"Недостаточно корректных ответов AI-судей для группы: {group_name}")
            return []

        result = []
        for judge_result in judge_results:
            for item in judge_result:
                word = item.get("слово")
                podgroup = item.get("подгруппа")
                if not word or not podgroup:
                    continue
                result.append({"слово": word, "подгруппа": podgroup})
        return result

    def _build_second_prompt(self, words, group_name):
        podgroups = self.get_podgroups(group_name)
        prompt = [
            "Ответь строго одним корректным JSON-массивом, без пояснений и другого текста.",
            f"Все слова относятся к верхней группе: {group_name}",
            "Для каждого слова выбери наиболее подходящую подгруппу из предоставленного списка.",
            "Используй только названия подгрупп из списка, ничего нового не добавляй.",
            "Классифицируй слово по его смысловому значению, а не по внешнему сходству с названием подгруппы.",
            "Если слово может относиться к подгруппе только при дополнительном предположении, не выбирай её.",
            "Если ни одна подгруппа не подходит достаточно уверенно, укажи пустую строку.",
            "Разные написания одного и того же названия (например: BAXI, baxi, бакси) считай одним объектом.",
            "",
            "Разрешённые подгруппы:",
        ]
        for podgroup in podgroups:
            prompt.append(f"- {podgroup}")
        prompt.append("")
        prompt.append("Слова:")
        prompt.extend(words)
        prompt.append("")
        prompt.append("Ответ:")
        prompt.append("""Формат ответа — JSON-массив:
[
  {"слово": "слово из списка", "подгруппа": "подгруппа из разрешённых"}
]""".strip())
        return "\n".join(prompt)

    def _parse_second_result(self, content):
        if not content or not content.strip():
            self._save_bad_answer(content)
            return None
        content = clean_json(content)
        try:
            result = json.loads(content)
            if isinstance(result, dict):
                result = [result]
            if not isinstance(result, list):
                self.report("Ответ второго этапа должен быть JSON-массивом.")
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
                clean_result.append({"слово": word, "подгруппа": podgroup})
            return clean_result
        except json.JSONDecodeError:
            self.report("Неверный JSON второго этапа")
            self._save_bad_answer(content)
            return None

    # ===================================================
    # СОХРАНЕНИЕ РЕЗУЛЬТАТОВ
    # ===================================================

    def _append_second_stage_to_last_facets(self):
        output_dir = OUTPUT_DIR
        last_file = LAST_FACETS_FILE
        added = 0
        with last_file.open("a", encoding="utf-8") as out:
            for file_path in sorted(output_dir.glob("ai_2_*.txt")):
                lines = file_path.read_text(encoding="utf-8").splitlines()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    out.write(line + "\n")
                    added += 1
        console_line(f"В last_facets.txt добавлено: {added}")

    def _save_judges_raw(self):
        JUDGES_RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
        JUDGES_RAW_FILE.write_text(
            json.dumps(self.judges_raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def print_judge_stats(self):
        if not self.judges_raw:
            console_line("Нет данных об ответах судей.")
            return
        console_line()
        console_line("=== Статистика ответов AI ===")
        by_stage = {}
        for entry in self.judges_raw:
            by_stage.setdefault(entry.get("stage", "?"), []).append(entry)
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
                        completeness.append(min(1.0, n_items / n_words))
                else:
                    invalid += 1
            valid_pct = valid * 100 / total if total else 0
            avg_completeness = sum(completeness) / len(completeness) if completeness else 0
            console_line(
                f"Этап {stage}: ответов {total} | "
                f"JSON валиден {valid} ({valid_pct:.0f}%) | "
                f"невалиден {invalid} | полнота {avg_completeness:.0%}"
            )

        by_model = {}
        for entry in self.judges_raw:
            model = entry.get("model", "?")
            by_model.setdefault(model, {"total": 0, "valid": 0})
            by_model[model]["total"] += 1
            if self._is_valid_json(entry.get("answer", "")):
                by_model[model]["valid"] += 1

        console_line()
        console_line("Распределение по моделям:")
        for model, stat in sorted(by_model.items(), key=lambda kv: -kv[1]["total"]):
            total = stat["total"]
            valid = stat["valid"]
            pct = valid * 100 / total if total else 0
            console_line(f"  {model}: {total} ответов | JSON валиден {valid} ({pct:.0f}%)")

    def _is_valid_json(self, content):
        if not content or not content.strip():
            return False
        try:
            json.loads(clean_json(content))
            return True
        except (json.JSONDecodeError, ValueError):
            return False

    def _json_item_count(self, content):
        try:
            result = json.loads(clean_json(content))
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(result, list):
            return len(result)
        if isinstance(result, dict):
            return 1
        return None

    # ===================================================
    # РАБОТА С ПОДТВЕРЖДЁННЫМИ РЕЗУЛЬТАТАМИ
    # ===================================================

    def promote(self, input_file, output_file, threshold=3):
        input_path = OUTPUT_DIR / input_file if not Path(input_file).is_absolute() else Path(input_file)
        output_path = OUTPUT_DIR / output_file if not Path(output_file).is_absolute() else Path(output_file)
        if not input_path.exists():
            return
        lines = input_path.read_text(encoding="utf-8").splitlines()
        console_line(f"{input_path.name} строк: {len(lines)}")
        lines = [line.strip() for line in lines if "\t" in line and line.strip()]
        if not lines:
            input_path.write_text("", encoding="utf-8")
            return
        counts = Counter(lines)
        console_line(f"Уникальных строк: {len(counts)}")
        repeated = [(line, count) for line, count in counts.items() if count > 1]
        console_line(f"Повторяющихся строк: {len(repeated)}")

        remain = []
        added = 0
        with output_path.open("a", encoding="utf-8") as out:
            for line, count in counts.items():
                promoted = count // threshold
                remainder = count % threshold
                if promoted:
                    for _ in range(promoted):
                        out.write(line + "\n")
                    added += promoted
                if remainder:
                    remain.extend([line] * remainder)

        if remain:
            input_path.write_text("\n".join(remain) + "\n", encoding="utf-8")
        else:
            input_path.write_text("", encoding="utf-8")
        console_line(f"remain = {len(remain)}")
        console_line(f"{input_path.name} -> {output_path.name}: добавлено {added}")

    def import_level(self, filename):
        path = OUTPUT_DIR / filename if not Path(filename).is_absolute() else Path(filename)
        if not path.exists():
            return 0
        imported = 0
        skipped = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                group_name, podgroup_name, word = line.split("\t", 2)
            except ValueError:
                skipped += 1
                continue
            self.cursor.execute(
                "SELECT id FROM groups WHERE name = ? AND parent_id IS NULL",
                (group_name,),
            )
            row = self.cursor.fetchone()
            if not row:
                skipped += 1
                continue
            group_id = row[0]
            self.cursor.execute(
                "SELECT id FROM groups WHERE name = ? AND parent_id = ?",
                (podgroup_name, group_id),
            )
            row = self.cursor.fetchone()
            if not row:
                skipped += 1
                continue
            podgroup_id = row[0]
            self.cursor.execute("SELECT id FROM words WHERE word = ?", (word,))
            row = self.cursor.fetchone()
            if not row:
                skipped += 1
                continue
            word_id = row[0]
            self.cursor.execute(
                "INSERT OR IGNORE INTO group_words (group_id, word_id) VALUES (?, ?)",
                (podgroup_id, word_id),
            )
            imported += 1
        self.conn.commit()
        path.write_text("", encoding="utf-8")
        console_line(f"импортировано: {imported} | отброшено кривых: {skipped}")
        return imported

    def rebuild_auto_words_cache(self):
        auto_words = {}
        placeholders = ",".join("?" for _ in POS_GROUP_IDS)
        self.cursor.execute(
            f"""
            SELECT w.word, parent.name
            FROM group_words gw
            JOIN words w ON w.id = gw.word_id
            JOIN groups subgroup ON subgroup.id = gw.group_id
            JOIN groups parent ON parent.id = subgroup.parent_id
            WHERE parent.type = ? AND subgroup.id NOT IN ({placeholders})
            """,
            (GROUP_TYPE_NORMAL,) + POS_GROUP_IDS,
        )
        rows = self.cursor.fetchall()
        for word, group_name in rows:
            auto_words[word] = group_name
        with AUTO_WORDS_FILE.open("wb") as f:
            pickle.dump(auto_words, f, protocol=pickle.HIGHEST_PROTOCOL)
        self.auto_words = auto_words
        console_line(f"auto_words.pkl пересоздан: {len(auto_words)} записей")

    # ===================================================
    # СЛУЖЕБНЫЕ ФУНКЦИИ
    # ===================================================

    def get_podgroups(self, group_name):
        return self.auto_podgroup.get(group_name, [])

    def get_word(self, word_id):
        row = self._find_word(word_id=word_id)
        return row[0] if row else None

    def get_word_info(self, word_id):
        if word_id in self.word_info_cache:
            return self.word_info_cache[word_id]
        cursor = self.conn.cursor()
        cursor.execute("SELECT word, count FROM words WHERE id = ?", (word_id,))
        row = cursor.fetchone()
        if row:
            info = {"word": row[0], "count": row[1]}
        else:
            info = {"word": str(word_id), "count": 0}
        self.word_info_cache[word_id] = info
        return info

    def color(self, text, color=Fore.RED):
        return f"{color}{text}{Style.RESET_ALL}"

    def report(self, text="", color=None):
        if color:
            print(self.color(text, color))
        else:
            print(text)

    def _save_bad_answer(self, content):
        path = OUTPUT_DIR / "bad_json.txt"
        if path.exists():
            text = path.read_text(encoding="utf8")
            count = text.count("===== ")
        else:
            count = 0
        n = count + 1
        key = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            key = chr(ord("A") + rem) + key
        with path.open("a", encoding="utf8") as f:
            f.write(f"\n===== {key} =====\n")
            f.write(content)
            f.write("\n")


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main():
    db_path = DB_FILE
    finder = HypothesisFinder(db_path)
    try:
        finder.run_study()
        console_line(f"promote {CONFIRM_FILES[0]} -> {CONFIRM_FILES[1]}")
        finder.promote(CONFIRM_FILES[0], CONFIRM_FILES[1])
        imported = finder.import_level(CONFIRM_FILES[1])
        console_line(f"В группы добавлено: {imported}")
        finder.rebuild_auto_words_cache()
    finally:
        if finder.conn is not None:
            finder.conn.close()
            console_line()
            console_line("Программа закончила работу штатно")
        finder.print_judge_stats()


if __name__ == "__main__":
    main()