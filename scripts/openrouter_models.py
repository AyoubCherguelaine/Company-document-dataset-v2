#!/usr/bin/env python3
"""List the free models on OpenRouter and pick one for .env.

    python scripts/openrouter_models.py              # list, then choose interactively
    python scripts/openrouter_models.py --json-only  # only models that support JSON output (recommended)
    python scripts/openrouter_models.py --pick 3     # choose #3 without prompting
    python scripts/openrouter_models.py --list       # just print the table

The chosen model goes first in OPENROUTER_MODEL in .env. The previous models are kept after it as
fallbacks. The model list is public, so no API key is needed.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
MODELS_URL = "https://openrouter.ai/api/v1/models"


def fetch_free_models():
    with urllib.request.urlopen(MODELS_URL, timeout=30) as resp:
        models = json.loads(resp.read())["data"]
    free = []
    for m in models:
        pricing = m.get("pricing") or {}
        is_free = m["id"].endswith(":free") or m["id"] == "openrouter/free" or (
            str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0")
        # text-only output: drops music/image models that are "free" because they bill elsewhere
        text_out = ((m.get("architecture") or {}).get("output_modalities") or ["text"]) == ["text"]
        if is_free and text_out:
            params = m.get("supported_parameters") or []
            free.append({"id": m["id"], "context": m.get("context_length") or 0,
                         "json": "response_format" in params or "structured_outputs" in params,
                         "name": m.get("name", "")})
    return sorted(free, key=lambda m: (not m["json"], -m["context"], m["id"]))


def read_current_models():
    if not ENV_PATH.exists():
        return []
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("OPENROUTER_MODEL="):
            return [m.strip() for m in line.split("=", 1)[1].split(",") if m.strip()]
    return []


def write_model(model_id):
    """Put model_id first in OPENROUTER_MODEL; keep the other entries as fallbacks."""
    models = [model_id] + [m for m in read_current_models() if m != model_id]
    new_line = "OPENROUTER_MODEL=" + ",".join(models)
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith("OPENROUTER_MODEL="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return new_line


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-only", action="store_true", help="only models that support JSON output")
    ap.add_argument("--pick", type=int, help="choose this number without prompting")
    ap.add_argument("--list", action="store_true", help="print the table and exit")
    args = ap.parse_args()

    models = fetch_free_models()
    if args.json_only:
        models = [m for m in models if m["json"]]
    if not models:
        print("no free models found", file=sys.stderr)
        return 1

    current = read_current_models()
    print(f"{'#':>3}  {'model':<52} {'context':>9}  json  ")
    for i, m in enumerate(models, 1):
        mark = "  <- current" if current and m["id"] == current[0] else ""
        print(f"{i:>3}  {m['id']:<52} {m['context']:>9,}  {'yes' if m['json'] else ' - '}{mark}")
    print("\njson = supports JSON output (docgen asks for JSON, so prefer these)")
    if args.list:
        return 0

    choice = args.pick
    if choice is None:
        try:
            raw = input(f"\nChoose a model [1-{len(models)}] (Enter to cancel): ").strip()
        except EOFError:
            raw = ""
        if not raw:
            print("cancelled, .env unchanged")
            return 0
        choice = int(raw) if raw.isdigit() else -1
    if not 1 <= choice <= len(models):
        print(f"invalid choice: {choice}", file=sys.stderr)
        return 1

    print(f"{write_model(models[choice - 1]['id'])}  (written to {ENV_PATH})")
    print("test it with: python -m docgen llm ping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
