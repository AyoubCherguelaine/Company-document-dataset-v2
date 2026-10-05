"""Post-render verification and word boxes.

Re-extract the text from the PDF, check that every gold field is printed, and
align gold values to PyMuPDF word boxes (layout supervision without OCR).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

_WS = re.compile(r"\s+")
_HYPHEN_BREAK = re.compile(r"-\s+(?=\w)")


def norm(text: str) -> str:
    text = text.replace(" ", " ").replace(" ", " ").replace("‑", "-")
    text = _HYPHEN_BREAK.sub("-", text)      # "un-\ncompromised": a line wrapped after a hyphen
    return _WS.sub(" ", text).strip().casefold()


@dataclass
class CheckResult:
    ok: bool
    pages: int
    missing: dict[str, str] = field(default_factory=dict)
    arithmetic: list[str] = field(default_factory=list)
    boxes: dict[str, list[dict]] = field(default_factory=dict)
    words: list[dict] = field(default_factory=list)
    page_sizes: list[list[float]] = field(default_factory=list)   # [width, height] in points, per page


def extract_words(doc: pymupdf.Document) -> list[dict]:
    words = []
    for pno, page in enumerate(doc):
        for x0, y0, x1, y1, text, *_ in page.get_text("words", sort=False):  # stream order keeps wrapped cells contiguous
            words.append({"page": pno, "text": text,
                          "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]})
    return words


def _squash(text: str) -> str:
    """Normalized text with all whitespace removed (for word-boundary-aligned matching)."""
    return "".join(norm(text).replace("- ", "-").split()) if text else ""


def locate(value: str, words: list[dict]) -> list[dict]:
    """Every occurrence of value as a run of consecutive words on one page; returns merged boxes.

    Matching is done on the words glued together, aligned to word boundaries, so a value that
    the PDF splits differently ("top-of-the-" + "line", or "Bottle" "-" "30") is still found."""
    target = _squash(value)
    if not target:
        return []
    hits, i, n = [], 0, len(words)
    toks = [_squash(w["text"]) for w in words]
    while i < n:
        if toks[i] and target.startswith(toks[i]):
            acc, j = toks[i], i
            while len(acc) < len(target) and j + 1 < n and words[j + 1]["page"] == words[i]["page"]:
                j += 1
                acc += toks[j]
                if not target.startswith(acc):
                    break
            if acc == target:
                span = words[i:j + 1]
                hits.append({"page": span[0]["page"],
                             "bbox": [min(w["bbox"][0] for w in span), min(w["bbox"][1] for w in span),
                                      max(w["bbox"][2] for w in span), max(w["bbox"][3] for w in span)]})
        i += 1
    return hits


def check_pdf(pdf_bytes: bytes, required: dict[str, str], arithmetic: list[str],
              with_boxes: bool = False) -> CheckResult:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = norm(" ".join(page.get_text() for page in doc))
        pages = doc.page_count
        words = extract_words(doc) if with_boxes else []
        page_sizes = [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]
    missing = {k: v for k, v in required.items() if v and norm(v) not in text}
    boxes = {k: locate(v, words) for k, v in required.items()} if with_boxes else {}
    return CheckResult(ok=not missing and not arithmetic, pages=pages, missing=missing,
                       arithmetic=arithmetic, boxes=boxes, words=words, page_sizes=page_sizes)
