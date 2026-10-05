"""Source adapters: each database is turned into a small set of normalized records.

Document types are written once against these records, and every source that can
produce a record kind gets that document type for free:

    orders     -> invoice, quote, credit note, packing slip, shipping order
    purchases  -> purchase order, goods received note
    accounts   -> account statement
    inventory  -> inventory report
    receipts   -> receipt
    employees  -> payslip, employment certificate
    work orders-> work order

Amounts are Decimal in the source currency (USD for every bundled database).
Dates are already shifted by the source's date_shift_years.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.models import Address, Party
from ..core.money import D


def clean(value) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


@dataclass
class SrcLine:
    sku: str
    description: str
    quantity: Decimal | int
    unit_price: Decimal = Decimal(0)
    discount_rate: Decimal = Decimal(0)
    unit: str = ""
    category: str = ""
    product_id: Any = None
    weight: Decimal | None = None          # per unit, kg
    details: dict[str, str] = field(default_factory=dict)   # locale -> longer description from the source
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SrcOrder:
    key: Any
    ref: str                               # the source's own order number
    order_date: date
    customer: Party
    lines: list[SrcLine]
    ship_to: Party | None = None
    required_date: date | None = None
    ship_date: date | None = None
    freight: Decimal = Decimal(0)
    tax_amount: Decimal | None = None      # given by the source (AdventureWorks); None = compute
    tax_policy: str = "any"                # any | given | none
    salesperson: str = ""
    carrier: Party | None = None
    po_number: str = ""
    tracking: str = ""
    b2c: bool = False


@dataclass
class SrcPurchase:
    key: Any
    ref: str
    order_date: date
    vendor: Party
    lines: list[SrcLine]                   # extra: received, rejected, stocked, due_date
    ship_date: date | None = None
    freight: Decimal = Decimal(0)
    tax_amount: Decimal | None = None
    buyer: str = ""
    carrier: Party | None = None
    status: str = ""


@dataclass
class SrcAccountEntry:
    date: date
    ref: str
    amount: Decimal                        # amount invoiced (incl. freight/tax)
    due_days: int = 30


@dataclass
class SrcAccount:
    key: Any
    customer: Party
    entries: list[SrcAccountEntry]


@dataclass
class SrcInventoryRow:
    sku: str
    description: str
    group: str
    on_hand: int
    unit_cost: Decimal
    on_order: int = 0
    reorder_level: int = 0
    location: str = ""
    discontinued: bool = False


@dataclass
class SrcInventory:
    key: Any
    title: str                             # e.g. category or warehouse location
    rows: list[SrcInventoryRow]
    as_of: date | None = None


@dataclass
class SrcReceipt:
    key: Any
    ref: str
    when: datetime
    customer: Party
    lines: list[SrcLine]
    total: Decimal
    served_by: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SrcEmployee:
    key: Any
    person: Party
    job_title: str
    department: str
    hire_date: date
    birth_date: date | None
    national_id: str
    pay_rate: Decimal                      # hourly
    pay_frequency: int                     # 1 = monthly, 2 = biweekly
    shift: str = ""
    salaried: bool = True
    gender: str = ""
    vacation_hours: int = 0
    sick_hours: int = 0


@dataclass
class SrcWorkOrder:
    key: Any
    ref: str
    product: SrcLine
    order_qty: int
    stocked_qty: int
    scrapped_qty: int
    start_date: date
    end_date: date | None
    due_date: date
    scrap_reason: str
    operations: list[dict]                 # seq, location, planned/actual start/end, hours, planned/actual cost


class SQLiteSource:
    """Read-only SQLite access with date shifting. Subclasses add record loaders."""

    name = ""
    sector = ""                            # company sector that issues documents from this source

    def __init__(self, path: str | Path, date_shift_years: int = 0):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"{self.name}: {path} not found")
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.shift = date_shift_years

    def all(self, sql: str, *params) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params)]

    def one(self, sql: str, *params) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def col(self, sql: str, *params) -> list:
        return [r[0] for r in self.conn.execute(sql, params)]

    def date(self, value) -> date | None:
        d = self.datetime(value)
        return d.date() if d else None

    def datetime(self, value) -> datetime | None:
        if not value:
            return None
        text = str(value).strip().replace("T", " ")[:19]
        d = datetime.fromisoformat(text) if len(text) > 10 else datetime.fromisoformat(text[:10])
        if self.shift:
            try:
                d = d.replace(year=d.year + self.shift)
            except ValueError:             # 29 Feb into a non-leap year
                d = d.replace(year=d.year + self.shift, day=28)
        return d

    @staticmethod
    def dec(value) -> Decimal:
        return D(value if value not in (None, "") else 0)

    @staticmethod
    def party(name, line1="", line2="", city="", region="", postal="", country="", **kw) -> Party:
        return Party(name=clean(name), address=Address(clean(line1), clean(line2), clean(city), clean(region),
                                                       clean(postal), clean(country)), **kw)

    # Record kinds a source can provide. Subclasses override the ones they support.
    def order_keys(self) -> list:
        raise NotImplementedError

    def purchase_keys(self) -> list:
        raise NotImplementedError

    def account_keys(self) -> list:
        raise NotImplementedError

    def inventory_keys(self) -> list:
        raise NotImplementedError

    def receipt_keys(self) -> list:
        raise NotImplementedError

    def employee_keys(self) -> list:
        raise NotImplementedError

    def work_order_keys(self) -> list:
        raise NotImplementedError

    def close(self):
        self.conn.close()
