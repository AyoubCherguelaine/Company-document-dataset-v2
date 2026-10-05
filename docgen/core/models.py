"""Canonical, template-agnostic document records.

Templates only ever see these objects (never database rows). The same objects
serialise to the gold JSON, so what is rendered and what is labelled cannot drift.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .money import ZERO, q2


@dataclass
class Address:
    line1: str = ""
    line2: str = ""
    city: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""

    def lines(self) -> list[str]:
        """Postal lines, skipping empty parts."""
        city_line = " ".join(p for p in (self.postal_code, self.city) if p)
        if self.region:
            city_line = f"{city_line}, {self.region}" if city_line else self.region
        return [line for line in (self.line1, self.line2, city_line, self.country) if line]


@dataclass
class Party:
    name: str
    address: Address = field(default_factory=Address)
    contact: str = ""
    phone: str = ""
    fax: str = ""
    email: str = ""
    website: str = ""
    tax_id: str = ""
    code: str = ""                     # customer / supplier id in the source system
    bank: dict[str, str] = field(default_factory=dict)
    brand: dict[str, Any] = field(default_factory=dict)   # logo hints; not part of gold


@dataclass
class LineItem:
    sku: str
    description: str
    quantity: int
    unit_price: Decimal = ZERO
    discount_rate: Decimal = Decimal(0)
    tax_rate: Decimal = Decimal(0)
    unit: str = ""
    line_total: Decimal = ZERO
    extra: dict[str, Any] = field(default_factory=dict)

    def compute(self) -> "LineItem":
        self.line_total = q2(self.unit_price * self.quantity * (1 - self.discount_rate))
        return self


@dataclass
class TaxLine:
    rate: Decimal
    base: Decimal
    amount: Decimal


@dataclass
class Totals:
    currency: str
    subtotal: Decimal = ZERO          # sum of line totals (after line discounts)
    gross: Decimal = ZERO             # sum of qty * unit price (before discounts)
    discount_total: Decimal = ZERO
    freight: Decimal = ZERO
    taxes: list[TaxLine] = field(default_factory=list)
    tax_total: Decimal = ZERO
    total: Decimal = ZERO

    @classmethod
    def from_items(cls, items: list[LineItem], currency: str, freight: Decimal = ZERO,
                   apply_tax: bool = True) -> "Totals":
        t = cls(currency=currency, freight=q2(freight))
        t.gross = q2(sum((i.unit_price * i.quantity for i in items), ZERO))
        t.subtotal = q2(sum((i.line_total for i in items), ZERO))
        t.discount_total = q2(t.gross - t.subtotal)
        if apply_tax:
            by_rate: dict[Decimal, Decimal] = {}
            for item in items:
                if item.tax_rate:
                    by_rate[item.tax_rate] = by_rate.get(item.tax_rate, ZERO) + item.line_total
            t.taxes = [TaxLine(rate, q2(base), q2(base * rate)) for rate, base in sorted(by_rate.items())]
        t.tax_total = q2(sum((x.amount for x in t.taxes), ZERO))
        t.total = q2(t.subtotal + t.freight + t.tax_total)
        return t

    def set_given_tax(self, amount: Decimal, rate: Decimal) -> "Totals":
        """Use a tax amount supplied by the source (e.g. AdventureWorks TaxAmt) instead of rates."""
        self.taxes = [TaxLine(rate, self.subtotal, q2(amount))] if amount else []
        self.tax_total = q2(sum((x.amount for x in self.taxes), ZERO))
        self.total = q2(self.subtotal + self.freight + self.tax_total)
        return self

    def verify(self, items: list[LineItem]) -> list[str]:
        """Arithmetic consistency check; returns a list of problems (empty == ok)."""
        problems = []
        for i in items:
            expected = q2(i.unit_price * i.quantity * (1 - i.discount_rate))
            if expected != i.line_total:
                problems.append(f"line {i.sku}: {i.line_total} != {expected}")
        if q2(sum((i.line_total for i in items), ZERO)) != self.subtotal:
            problems.append("subtotal != sum(line totals)")
        if q2(sum((x.amount for x in self.taxes), ZERO)) != self.tax_total:
            problems.append("tax_total != sum(tax lines)")
        if q2(self.subtotal + self.freight + self.tax_total) != self.total:
            problems.append("total != subtotal + freight + tax")
        return problems


@dataclass
class SummaryRow:
    """A labelled figure shown in the summary box of documents without commercial totals."""
    label: str                        # locale label key
    value: Any                        # Decimal (money), int, str or date
    fmt: str = "money"                # money | int | number | text | date | percent
    strong: bool = False


@dataclass
class DocumentRecord:
    doc_type: str
    number: str
    issue_date: date
    currency: str = "USD"
    parties: dict[str, Party] = field(default_factory=dict)     # role -> party (issuer, bill_to, ...)
    dates: dict[str, date] = field(default_factory=dict)        # label key -> date (due_date, ...)
    refs: dict[str, str] = field(default_factory=dict)          # label key -> value (order_id, ...)
    items: list[LineItem] = field(default_factory=list)
    totals: Totals | None = None
    summary: list[SummaryRow] = field(default_factory=list)
    body: list[str] = field(default_factory=list)                # letter paragraphs
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_gold(self) -> dict[str, Any]:
        data = asdict(self)
        for party in data["parties"].values():
            party.pop("brand", None)
        return _jsonable(data)


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
