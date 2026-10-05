"""BaseDocument: the class every document type inherits from.

A document type declares *what* it is (type, title, columns, record kind, sources) and how a
normalized source record becomes a DocumentRecord. Layouts, themes and companies decide how it
looks. One variant is registered per source ("invoice.northwind", "invoice.adventureworks"...):

    @register
    class Quote(OrderDocument):
        doc_type = "quote"
        sources = ["northwind", "adventureworks"]

        def build_record(self, src, ctx): ...
"""

from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from .. import PACKAGE_DIR
from .locale import Locale
from .models import DocumentRecord, LineItem, Party, SummaryRow, Totals
from .money import D, convert
from .theme import Theme

DOC_TEMPLATES = PACKAGE_DIR / "documents" / "templates"


@dataclass
class Column:
    key: str                      # LineItem attribute, or "extra.<name>"
    label: str                    # locale label key
    align: str = "left"           # left | right | center
    fmt: str = "text"             # text | int | money | percent | number | date
    width: str = ""               # optional CSS width
    option: str = ""              # only shown when ctx.options[option] is truthy


@dataclass
class GenContext:
    """Everything chosen for one document before it is built."""
    seed: int
    rng: random.Random
    layout: str
    theme: Theme
    locale: Locale
    issuer: Party
    currency: str
    fx_rate: Any = 1
    options: dict[str, Any] = field(default_factory=dict)
    company: Any = None               # core.companies.Company that issues the document
    texts: Any = None                 # core.textbank.TextBank (LLM-written shared texts)
    repo: Any = None                  # the source adapter (sources.base.SQLiteSource)


class BaseDocument(ABC):
    doc_type: ClassVar[str] = ""
    sources: ClassVar[list[str]] = []
    record_kind: ClassVar[str] = "order"      # repo.<kind>_keys() / repo.load_<kind>(key)
    title_key: ClassVar[str] = ""             # defaults to doc_type
    template_dirs: ClassVar[list[Path]] = [DOC_TEMPLATES]
    page_size: ClassVar[str] = ""             # overrides the theme, e.g. "A5"
    number_prefix: ClassVar[str] = "DOC"
    number_formats: ClassVar[list[str]] = ["{prefix}-{yyyy}-{key}", "{prefix}{key:06d}", "{prefix}/{yy}/{key}"]

    columns: ClassVar[list[Column]] = [
        Column("sku", "sku", width="13%"),
        Column("description", "description"),
        Column("quantity", "quantity", "right", "int", "9%"),
        Column("unit_price", "unit_price", "right", "money", "14%"),
        Column("discount_rate", "discount", "right", "percent", "9%", option="show_discount"),
        Column("tax_rate", "tax_rate", "right", "percent", "9%", option="show_tax_column"),
        Column("line_total", "amount", "right", "money", "15%"),
    ]

    def __init__(self, source: str):
        self.source = source
        self.name = f"{self.doc_type}.{source}"

    # ---------------------------------------------------------------- data ----
    def source_keys(self, repo) -> list:
        return getattr(repo, f"{self.record_kind}_keys")()

    def load(self, repo, key):
        return getattr(repo, f"load_{self.record_kind}")(key)

    @abstractmethod
    def build_record(self, src, ctx: GenContext) -> DocumentRecord:
        """Normalized source record -> canonical DocumentRecord. Templates never touch the DB."""

    def variation(self, rng: random.Random, locale: Locale) -> dict[str, Any]:
        """Content toggles drawn per document. Extend with super().variation(...) | {...}."""
        return {
            "show_discount": rng.random() < 0.6,
            "show_tax_column": rng.random() < 0.3,
            "show_notes": rng.random() < 0.5,
            "show_contact": rng.random() < 0.7,
            "show_bank": rng.random() < 0.7,
            "show_ship_to": rng.random() < 0.8,
            "show_signature": rng.random() < 0.3,
            "show_item_details": rng.random() < 0.4,
            "tax_mode": rng.choice(["none", "single", "single", "multi"]),
            "date_format": rng.choice(locale.date_formats),
            "max_items": rng.choice([3, 5, 8, 12, 12, 20, 40]),
        }

    # ------------------------------------------------------------- helpers ----
    def make_number(self, key, issue: date, ctx: GenContext, doc_type: str | None = None) -> str:
        """The company's numbering for doc_type; otherwise a class default fixed per company."""
        doc_type = doc_type or self.doc_type
        fmt = ctx.company.number_format(doc_type) if ctx.company else None
        if not fmt:
            seed = f"{ctx.company.slug if ctx.company else ''}:{doc_type}"
            fmt = random.Random(seed).choice(self.number_formats)
        return fmt.format(prefix=self.number_prefix, key=numeric_key(key), yyyy=issue.year, yy=f"{issue.year % 100:02d}")

    def sample_lines(self, lines: list, ctx: GenContext) -> list:
        """Keep at most options['max_items'] lines, in their original order."""
        limit = ctx.options.get("max_items") or len(lines)
        if len(lines) <= limit:
            return list(lines)
        keep = sorted(ctx.rng.sample(range(len(lines)), limit))
        return [lines[i] for i in keep]

    def tax_rate(self, category: str, ctx: GenContext) -> Decimal:
        mode, cfg = ctx.options.get("tax_mode", "none"), ctx.locale.tax
        if mode in ("none", "given") or not cfg:
            return Decimal(0)
        if mode == "multi" and category in cfg.get("reduced_categories", []):
            return D(cfg["reduced"])
        return D(cfg["standard"])

    def details_for(self, line, ctx: GenContext) -> str:
        loc = ctx.locale.code
        text = (line.details or {}).get(loc) or (line.details or {}).get("en", "")
        if not text and ctx.texts and self.source == "northwind":
            text = ctx.texts.product_details(loc, line.product_id)
        return text

    def make_items(self, lines: list, ctx: GenContext, prices: bool = True) -> list[LineItem]:
        items = []
        for line in lines:
            item = LineItem(sku=line.sku, description=line.description, quantity=int(line.quantity),
                            unit=line.unit, extra={"category": line.category, "product_id": line.product_id,
                                                   **(line.extra or {})})
            if line.weight is not None:
                item.extra["weight"] = line.weight
            if prices:
                item.unit_price = convert(line.unit_price, ctx.fx_rate)
                item.discount_rate = D(line.discount_rate)
                item.tax_rate = self.tax_rate(line.category, ctx)
                item.compute()
            if ctx.options.get("show_item_details"):
                details = self.details_for(line, ctx)
                if details:
                    item.extra["details"] = details
            items.append(item)
        return items

    def make_totals(self, items: list[LineItem], ctx: GenContext, freight=0, given_tax=None,
                    source_lines: list | None = None, partial: bool | None = None) -> Totals:
        """Totals in the document currency. A source tax amount is converted once and kept."""
        totals = Totals.from_items(items, ctx.currency, convert(freight or 0, ctx.fx_rate),
                                   apply_tax=ctx.options.get("tax_mode") not in ("none", "given"))
        if ctx.options.get("tax_mode") == "given" and given_tax:
            lines = source_lines or []
            base = sum((D(l.unit_price) * l.quantity * (1 - D(l.discount_rate)) for l in lines), Decimal(0))
            rate = (D(given_tax) / base).quantize(Decimal("0.0001")) if base else Decimal(0)
            sampled = partial if partial is not None else (source_lines is not None and len(source_lines) != len(items))
            # all lines shown: the source's own tax amount; lines sampled: the source rate on what is shown
            amount = totals.subtotal * rate if sampled else convert(given_tax, ctx.fx_rate)
            totals.set_given_tax(amount, rate)
        return totals

    # ------------------------------------------------------------ rendering ----
    def title(self, ctx: GenContext) -> str:
        return ctx.locale.t(self.title_key or self.doc_type)

    def template_candidates(self, layout: str) -> list[str]:
        """First existing template wins. company/<type>.html.j2 only exists in a company's own
        templates/ dir, so it can extend "<type>/document.html.j2" instead of replacing it."""
        return [f"company/{self.doc_type}.html.j2", f"{self.doc_type}/{layout}.html.j2",
                f"{self.doc_type}/document.html.j2", f"layouts/{layout}.html.j2"]

    def line_columns(self, record: DocumentRecord, ctx: GenContext) -> list[Column]:
        return [c for c in self.columns if not c.option or ctx.options.get(c.option)]

    def meta_rows(self, record: DocumentRecord, ctx: GenContext) -> list[tuple[str, str]]:
        """(label, value) pairs shown in the header meta box."""
        loc = ctx.locale
        rows = [(loc.t("number"), record.number), (loc.t("date"), loc.date(record.issue_date))]
        rows += [(loc.t(k), loc.date(v)) for k, v in record.dates.items() if v]
        rows += [(loc.t(k), v) for k, v in record.refs.items() if v]
        return rows

    def party_blocks(self, record: DocumentRecord, ctx: GenContext) -> list[tuple[str, Party]]:
        """(label, party) pairs shown as address blocks; issuer is rendered by the header."""
        return [(ctx.locale.t(role), p) for role, p in record.parties.items() if role != "issuer"]

    def context(self, record: DocumentRecord, ctx: GenContext) -> dict[str, Any]:
        loc = ctx.locale
        columns = self.line_columns(record, ctx)
        return {
            "doc": record,
            "issuer": record.parties.get("issuer", ctx.issuer),
            "title": self.title(ctx),
            "layout": ctx.layout,
            "theme": ctx.theme,
            "page_size": self.page_size or ctx.theme.page_size,
            "loc": loc,
            "t": loc.t,
            "opts": ctx.options,
            "columns": columns,
            "cell": lambda item, col: format_cell(item, col, loc, record.currency),
            "fmtv": lambda value, fmt="money": format_value(value, fmt, loc, record.currency),
            "meta_rows": self.meta_rows(record, ctx),
            "party_blocks": self.party_blocks(record, ctx),
            "currency": record.currency,
            "company": ctx.company,
            "ctext": (lambda key, default="": ctx.company.text(key, default)) if ctx.company else
                     (lambda key, default="": default),
        }

    # ----------------------------------------------------------- self-check ----
    def required_strings(self, record: DocumentRecord, ctx: GenContext) -> dict[str, str]:
        """Gold field -> exact text that must appear in the rendered PDF."""
        loc = ctx.locale
        req = {"number": record.number, "issue_date": loc.date(record.issue_date)}
        for role, party in record.parties.items():
            req[f"parties.{role}.name"] = party.name
        columns = self.line_columns(record, ctx)
        for i, item in enumerate(record.items):
            req[f"items.{i}.description"] = format_cell(item, Column("description", ""), loc, record.currency)
            for col in columns:
                if col.fmt == "money" and col.key == "line_total":
                    req[f"items.{i}.line_total"] = format_cell(item, col, loc, record.currency)
            if item.extra.get("details"):
                req[f"items.{i}.details"] = item.extra["details"]
        if record.totals:
            req["totals.total"] = loc.money(record.totals.total, record.currency)
        for row in record.summary:
            req[f"summary.{row.label}"] = format_value(row.value, row.fmt, loc, record.currency)
        for i, para in enumerate(record.body):
            req[f"body.{i}"] = para
        if ctx.options.get("show_notes") and record.notes:
            req["notes"] = record.notes
        return req

    def validate(self, record: DocumentRecord) -> list[str]:
        return record.totals.verify(record.items) if record.totals else []


def numeric_key(key) -> int:
    """Source keys as an int for number formats ("16072:16" -> 1607216; text keys hashed stably)."""
    digits = "".join(ch for ch in str(key) if ch.isdigit())
    if digits and len(digits) == len(str(key).replace(":", "").replace("-", "")):
        return int(digits[-9:])
    return int(hashlib.sha1(str(key).encode()).hexdigest()[:8], 16) % 10 ** 6


def format_value(value, fmt: str, loc: Locale, currency: str) -> str:
    if value is None or value == "":
        return ""
    if fmt == "money":
        return loc.money(value, currency)
    if fmt == "percent":
        return loc.percent(value) if value else "–"
    if fmt == "int":
        return loc.number(value, 0)
    if fmt == "number":
        return loc.number(value)
    if fmt == "date":
        return loc.date(value)
    return str(value)


def format_cell(item: LineItem, col: Column, loc: Locale, currency: str) -> str:
    if col.key.startswith("extra."):
        value = item.extra.get(col.key[6:], "")
    else:
        value = getattr(item, col.key)
    if col.key == "description" and item.extra.get("label_key"):
        value = loc.t(value)                   # descriptions like "late_fee" are label keys
    return format_value(value, col.fmt, loc, currency)
