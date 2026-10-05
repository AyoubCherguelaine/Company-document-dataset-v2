"""Shared LLM-written texts that are not tied to one company, e.g. product descriptions.

Stored as data/textbank/<kind>.<locale>.json so they can be reviewed, edited and versioned.
Generation is batched (one request per ~20 products) to stay inside free-tier quotas.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import REPO_ROOT

DEFAULT_DIR = REPO_ROOT / "data/textbank"

PRODUCT_PROMPT = """Write a short catalogue description for each food or drink product below, in {language}.
Each description: one line, 6 to 14 words, factual (origin, packaging, use), no prices, no product name repeated,
no quotes, no emojis.
Products (id | name | category | pack):
{rows}
Return a JSON object mapping each id (as a string) to its description."""


class TextBank:
    def __init__(self, root: Path = DEFAULT_DIR):
        self.root = root
        self._cache: dict[str, dict] = {}

    def get(self, kind: str, locale: str) -> dict[str, str]:
        name = f"{kind}.{locale}"
        if name not in self._cache:
            path = self.root / f"{name}.json"
            self._cache[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return self._cache[name]

    def put(self, kind: str, locale: str, data: dict[str, str]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{kind}.{locale}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self._cache[f"{kind}.{locale}"] = data
        return path

    def product_details(self, locale: str, product_id) -> str:
        return self.get("products", locale).get(str(product_id), "")


def generate_product_texts(llm, repo, bank: TextBank, locale: str, batch: int = 20, log=print) -> Path:
    from .companies import LANGUAGE

    existing = dict(bank.get("products", locale))
    todo = [p for p in repo.products() if str(p["ProductID"]) not in existing]
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        rows = "\n".join(f'{p["ProductID"]} | {p["ProductName"]} | {p["CategoryName"]} | {p["QuantityPerUnit"]}'
                         for p in chunk)
        try:
            data = llm.chat_json("You write concise product catalogue copy.",
                                 PRODUCT_PROMPT.format(language=LANGUAGE.get(locale, locale), rows=rows))
        except Exception as exc:
            log(f"[llm] products {locale} batch {i // batch + 1}: {exc}")
            continue
        for p in chunk:
            text = " ".join(str(data.get(str(p["ProductID"]), "")).split()).strip(' "')
            if text:
                existing[str(p["ProductID"])] = text[:160]
        bank.put("products", locale, existing)
        log(f"[llm] products {locale}: {len(existing)} described")
    return bank.put("products", locale, existing)
