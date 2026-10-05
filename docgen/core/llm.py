"""OpenRouter chat client: model fallback, retry on rate limits, and a SQLite response cache.

The cache key is (model list, messages, temperature), so a rerun with the same prompts costs
nothing and returns identical text, which keeps document generation reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import REPO_ROOT
from .env import env


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key: str | None = None, models: list[str] | None = None,
                 base_url: str | None = None, temperature: float | None = None,
                 cache_path: str | Path | None = None, timeout: int | None = None, log=print):
        self.api_key = api_key if api_key is not None else env("OPENROUTER_API_KEY")
        self.models = models or [m.strip() for m in env("OPENROUTER_MODEL", "openrouter/free").split(",") if m.strip()]
        self.base_url = (base_url or env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")
        self.temperature = temperature if temperature is not None else float(env("LLM_TEMPERATURE", "0.8"))
        self.timeout = timeout or int(env("LLM_TIMEOUT", "90"))
        self.log = log
        path = Path(cache_path or env("LLM_CACHE", "data/llm_cache.db"))
        path = path if path.is_absolute() else REPO_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = sqlite3.connect(path)
        self.cache.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, model TEXT, "
                           "content TEXT, created REAL)")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ api ----
    def _key(self, models, messages, json_mode) -> str:
        return hashlib.sha256(json.dumps([models, messages, self.temperature, json_mode],
                                         ensure_ascii=False).encode()).hexdigest()

    def chat(self, messages: list[dict], json_mode: bool = False, max_tokens: int = 2000,
             models: list[str] | None = None) -> str:
        models = models or self.models
        key = self._key(models, messages, json_mode)
        row = self.cache.execute("SELECT content FROM responses WHERE key = ?", (key,)).fetchone()
        if row:
            return row[0]
        if not self.enabled:
            raise LLMError("OPENROUTER_API_KEY is not set (see .env.example)")

        errors = []
        for model in models:
            for attempt in range(3):
                try:
                    content = self._request(model, messages, json_mode, max_tokens)
                    self.cache.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?)",
                                       (key, model, content, time.time()))
                    self.cache.commit()
                    return content
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode(errors="replace")[:300]
                    errors.append(f"{model}: HTTP {exc.code} {body}")
                    if exc.code in (401, 402, 403):
                        raise LLMError(errors[-1]) from None
                    if exc.code == 429 or exc.code >= 500:
                        wait = float(exc.headers.get("Retry-After") or 2 ** (attempt + 1))
                        self.log(f"[llm] {model} HTTP {exc.code}, retry in {wait:.0f}s")
                        time.sleep(min(wait, 30))
                        continue
                    break                       # 400/404: this model can't serve the request
                except (urllib.error.URLError, TimeoutError, LLMError) as exc:
                    errors.append(f"{model}: {exc}")
                    time.sleep(2 ** attempt)
            self.log(f"[llm] giving up on {model}, trying next model")
        raise LLMError("all models failed:\n  " + "\n  ".join(errors[-6:]))

    def chat_json(self, system: str, user: str, max_tokens: int = 2000):
        """Like chat(), but a reply that isn't JSON is dropped from the cache and the next model
        is tried (routers such as openrouter/free sometimes pick a reasoning or safety model)."""
        messages = [{"role": "system", "content": system + "\nAnswer with JSON only, no prose, no code fences."},
                    {"role": "user", "content": user}]
        last = None
        for i in range(len(self.models)):
            models = self.models[i:]
            text = self.chat(messages, json_mode=True, max_tokens=max_tokens, models=models)
            try:
                return parse_json(text)
            except LLMError as exc:
                last = exc
                self.cache.execute("DELETE FROM responses WHERE key = ?", (self._key(models, messages, True),))
                self.cache.commit()
                self.log(f"[llm] {models[0]}: not JSON, trying the next model")
        raise last

    def _request(self, model: str, messages: list[dict], json_mode: bool, max_tokens: int) -> str:
        payload = {"model": model, "messages": messages, "temperature": self.temperature,
                   "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/AyoubChLin/CompanyDocuments",
                     "X-Title": "CompanyDocuments docgen"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            raise LLMError(str(data["error"])[:300])
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if not content.strip():
            raise LLMError("empty completion")
        return content


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str):
    """Parse model output that should be JSON but may carry fences or chatter around it."""
    text = _FENCE.sub("", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not return JSON: {text[:200]!r}")
