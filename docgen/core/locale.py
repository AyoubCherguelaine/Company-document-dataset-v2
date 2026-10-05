"""Locale: translated labels plus number, money and date formatting (no Babel dependency)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .money import D, q2

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "CHF": "CHF"}


@dataclass
class Locale:
    code: str
    labels: dict[str, str]
    months: list[str]
    decimal_sep: str = "."
    group_sep: str = ","
    currency_format: str = "{symbol}{amount}"
    date_formats: list[str] = field(default_factory=lambda: ["{month} {d}, {yyyy}"])
    direction: str = "ltr"
    currency: str = "USD"
    tax: dict = field(default_factory=dict)
    payroll: dict = field(default_factory=dict)          # deductions, hours per pay frequency
    certificate: dict = field(default_factory=dict)      # letter phrasing variants
    date_format: str = ""              # picked per document from date_formats

    @classmethod
    def load(cls, path: Path) -> "Locale":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        loc = cls(**data)
        loc.date_format = loc.date_formats[0]
        return loc

    def with_date_format(self, fmt: str) -> "Locale":
        clone = Locale(**{k: getattr(self, k) for k in self.__dataclass_fields__})
        clone.date_format = fmt
        return clone

    # --- labels -----------------------------------------------------------
    def t(self, key: str) -> str:
        return self.labels.get(key, key.replace("_", " ").title())

    # --- formatting -------------------------------------------------------
    def number(self, value, places: int = 2) -> str:
        value = D(value)
        if places == 2:
            value = q2(value)
        sign = "-" if value < 0 else ""
        text = f"{abs(value):,.{places}f}"
        whole, _, frac = text.partition(".")
        whole = whole.replace(",", self.group_sep)
        return sign + (f"{whole}{self.decimal_sep}{frac}" if places else whole)

    def money(self, value, currency: str | None = None) -> str:
        currency = currency or self.currency
        symbol = CURRENCY_SYMBOLS.get(currency, currency)
        return self.currency_format.format(symbol=symbol, amount=self.number(value), code=currency)

    def percent(self, rate) -> str:
        pct = D(rate) * 100
        places = 0 if pct == pct.to_integral_value() else 1
        return f"{self.number(pct, places)}%" if self.code == "en" else f"{self.number(pct, places)} %"

    def date(self, value: date | None, fmt: str | None = None) -> str:
        if value is None:
            return ""
        return (fmt or self.date_format).format(
            d=value.day, dd=f"{value.day:02d}", mm=f"{value.month:02d}", m=value.month,
            yyyy=value.year, yy=f"{value.year % 100:02d}", month=self.months[value.month - 1],
            mon=self.months[value.month - 1][:3],
        )


def load_locales(directories: list[Path]) -> dict[str, Locale]:
    locales = {}
    for directory in directories:
        for path in sorted(directory.glob("*.yaml")):
            locales[path.stem] = Locale.load(path)
    return locales


