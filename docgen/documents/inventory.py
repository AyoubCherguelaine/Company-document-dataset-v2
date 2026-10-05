"""Inventory report: stock on hand per product, grouped, with value and reorder status."""

from __future__ import annotations

from datetime import date, timedelta


from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, LineItem, SummaryRow
from ..core.money import ZERO, convert, q2
from ..core.registry import register
from ..sources.base import SrcInventory

AS_OF_RANGE = {"northwind": (date(2016, 1, 1), date(2023, 10, 1)), "adventureworks": (date(2023, 1, 1), date(2025, 9, 1))}


@register
class InventoryReport(BaseDocument):
    doc_type = "inventory_report"
    sources = ["northwind", "adventureworks"]
    record_kind = "inventory"
    number_prefix = "STK"
    columns = [
        Column("sku", "sku", width="11%"),
        Column("description", "description"),
        Column("extra.location", "location", width="14%", option="show_location"),
        Column("quantity", "on_hand", "right", "int", "8%"),
        Column("extra.on_order", "on_order", "right", "int", "8%", option="has_on_order"),
        Column("extra.reorder_level", "reorder_level", "right", "int", "8%"),
        Column("unit_price", "unit_cost", "right", "money", "11%"),
        Column("line_total", "stock_value", "right", "money", "12%"),
        Column("extra.status", "status", width="11%"),
    ]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"show_location": rng.random() < 0.6,
                                                 "max_items": rng.choice([15, 25, 40, 60]),
                                                 "group_rows": rng.random() < 0.7}

    def build_record(self, src: SrcInventory, ctx: GenContext) -> DocumentRecord:
        opts, loc = ctx.options, ctx.locale
        lo, hi = AS_OF_RANGE.get(self.source, (date(2024, 1, 1), date(2025, 1, 1)))
        as_of = lo + timedelta(days=ctx.rng.randrange((hi - lo).days))
        rows = sorted(self.sample_lines(src.rows, ctx), key=lambda r: (r.group, r.description))
        items = []
        for r in rows:
            status = ("status_discontinued" if r.discontinued else "status_out" if r.on_hand == 0
                      else "status_reorder" if r.on_hand <= r.reorder_level else "status_ok")
            item = LineItem(sku=r.sku, description=r.description, quantity=r.on_hand,
                            unit_price=convert(r.unit_cost, ctx.fx_rate),
                            extra={"group": r.group, "on_order": r.on_order, "reorder_level": r.reorder_level,
                                   "location": r.location, "status": loc.t(status), "status_key": status})
            item.line_total = q2(item.unit_price * item.quantity)
            items.append(item)
        opts["has_on_order"] = any(i.extra["on_order"] for i in items)
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, as_of, ctx),
                                issue_date=as_of, currency=ctx.currency, items=items)
        record.parties = {"issuer": ctx.issuer}
        record.refs["scope"] = src.title or loc.t("all_products")
        record.summary = [
            SummaryRow("total_items", len(items), "int"),
            SummaryRow("total_units", sum(i.quantity for i in items), "int"),
            SummaryRow("items_to_reorder", sum(i.extra["status_key"] in ("status_reorder", "status_out") for i in items), "int"),
            SummaryRow("total_value", sum((i.line_total for i in items), ZERO), strong=True),
        ]
        return record

    def meta_rows(self, record, ctx):
        loc = ctx.locale
        return [(loc.t("number"), record.number), (loc.t("as_of"), loc.date(record.issue_date)),
                (loc.t("scope"), record.refs["scope"])]

    def validate(self, record):
        total = {r.label: r.value for r in record.summary}["total_value"]
        return [] if sum((i.line_total for i in record.items), ZERO) == total else ["total value mismatch"]


