"""Work order: a production order with its routing operations, planned vs actual cost."""

from __future__ import annotations

from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, LineItem, SummaryRow
from ..core.money import ZERO, convert
from ..core.registry import register
from ..sources.base import SrcWorkOrder


@register
class WorkOrder(BaseDocument):
    doc_type = "work_order"
    sources = ["adventureworks"]
    record_kind = "work_order"
    number_prefix = "WO"
    columns = [
        Column("sku", "operation", width="6%"),
        Column("description", "location"),
        Column("extra.planned_start", "planned_start", fmt="date", width="12%"),
        Column("extra.planned_end", "planned_end", fmt="date", width="12%"),
        Column("extra.actual_start", "actual_start", fmt="date", width="12%", option="show_actual_dates"),
        Column("extra.actual_end", "actual_end", fmt="date", width="12%", option="show_actual_dates"),
        Column("extra.hours", "hours", "right", "number", "8%"),
        Column("extra.planned_cost", "planned_cost", "right", "money", "12%"),
        Column("extra.actual_cost", "actual_cost", "right", "money", "12%"),
    ]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"show_actual_dates": rng.random() < 0.6}

    def build_record(self, src: SrcWorkOrder, ctx: GenContext) -> DocumentRecord:
        items = [LineItem(sku=str(op["seq"]), description=op["location"], quantity=1, extra={
            "planned_start": op["planned_start"], "planned_end": op["planned_end"],
            "actual_start": op["actual_start"], "actual_end": op["actual_end"], "hours": op["hours"],
            "planned_cost": convert(op["planned_cost"], ctx.fx_rate), "actual_cost": convert(op["actual_cost"], ctx.fx_rate),
        }) for op in src.operations]
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, src.start_date, ctx),
                                issue_date=src.start_date, currency=ctx.currency, items=items)
        record.parties = {"issuer": ctx.issuer}
        record.dates["due_date"] = src.due_date
        if src.end_date:
            record.dates["end_date"] = src.end_date
        record.extra["product"] = [("product", src.product.description), ("product_number", src.product.sku),
                                   ("order_qty", src.order_qty), ("stocked_qty", src.stocked_qty),
                                   ("scrapped_qty", src.scrapped_qty)]
        if src.scrap_reason:
            record.extra["product"].append(("scrap_reason", src.scrap_reason))
        planned = sum((i.extra["planned_cost"] for i in items), ZERO)
        actual = sum((i.extra["actual_cost"] for i in items), ZERO)
        record.summary = [SummaryRow("total_hours", sum((i.extra["hours"] for i in items), ZERO), "number"),
                          SummaryRow("total_planned_cost", planned), SummaryRow("total_actual_cost", actual),
                          SummaryRow("variance", actual - planned, strong=True)]
        return record

    def required_strings(self, record, ctx):
        req = super().required_strings(record, ctx)
        for label, value in record.extra["product"]:
            req[f"product.{label}"] = str(value)
        return req

    def validate(self, record):
        s = {r.label: r.value for r in record.summary}
        return [] if s["total_actual_cost"] - s["total_planned_cost"] == s["variance"] else ["variance mismatch"]
