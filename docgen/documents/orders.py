"""Documents built from a sales order: invoice, quote, credit note, packing slip, shipping order."""

from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal

from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, SummaryRow
from ..core.money import convert, q2
from ..core.registry import register
from ..sources.base import SrcOrder

ORDER_SOURCES = ["northwind", "adventureworks"]


class OrderDocument(BaseDocument):
    record_kind = "order"

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {
            "show_po_ref": rng.random() < 0.4,
            "show_salesperson": rng.random() < 0.5,
            "show_order_id": rng.random() < 0.7,
        }

    def apply_tax_policy(self, src: SrcOrder, ctx: GenContext):
        """Sources that state their tax (AdventureWorks) or sell tax-inclusive (Chinook) win over the draw."""
        if src.tax_policy in ("given", "none"):
            ctx.options["tax_mode"] = src.tax_policy
            ctx.options["show_tax_column"] = False

    def start(self, src: SrcOrder, ctx: GenContext, issue, prices: bool = True, lines=None) -> DocumentRecord:
        """Common skeleton: number, parties, (sampled) items."""
        self.apply_tax_policy(src, ctx)
        lines = self.sample_lines(src.lines, ctx) if lines is None else lines
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, issue, ctx),
                                issue_date=issue, currency=ctx.currency, items=self.make_items(lines, ctx, prices))
        record.parties["issuer"] = ctx.issuer
        record.parties["bill_to"] = src.customer
        if src.ship_to and ctx.options["show_ship_to"] and not src.b2c:
            record.parties["ship_to"] = src.ship_to
        record.extra["source_lines"] = len(src.lines)
        return record

    def common_refs(self, record: DocumentRecord, src: SrcOrder, ctx: GenContext):
        opts = ctx.options
        if opts["show_order_id"]:
            record.refs["order_id"] = src.ref
        if src.customer.code:
            record.refs["customer_id"] = src.customer.code
        if opts["show_po_ref"]:
            record.refs["po_number"] = src.po_number or f"PO-{ctx.rng.randint(1000, 99999)}"
        if opts["show_salesperson"] and src.salesperson:
            record.refs["salesperson"] = src.salesperson

    def pick_note(self, record: DocumentRecord, ctx: GenContext):
        notes = ctx.company.text("notes") if ctx.company else []
        record.notes = ctx.rng.choice(notes) if notes else ctx.locale.t("thank_you")


@register
class Invoice(OrderDocument):
    doc_type = "invoice"
    sources = ORDER_SOURCES + ["chinook"]
    number_prefix = "INV"
    number_formats = ["{prefix}-{yyyy}-{key}", "{prefix}{key:07d}", "F{yy}-{key}", "{yyyy}/{key}"]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {
            "payment_days": rng.choice([15, 30, 30, 45, 60]),
            "paid_stamp": rng.random() < 0.15,
        }

    def build_record(self, src: SrcOrder, ctx: GenContext) -> DocumentRecord:
        opts, loc = ctx.options, ctx.locale
        issue = src.ship_date or src.order_date
        record = self.start(src, ctx, issue)
        record.totals = self.make_totals(record.items, ctx, src.freight, src.tax_amount, src.lines)
        if src.b2c:
            record.extra["payment_terms"] = loc.t("paid_by_card")
            opts["paid_stamp"] = True
        else:
            record.dates["due_date"] = issue + timedelta(days=opts["payment_days"])
            record.extra["payment_terms"] = loc.t("net_days").format(days=opts["payment_days"])
            if ctx.company:
                record.extra["payment_instructions"] = ctx.company.text("payment_instructions")
        if opts["show_order_id"] and src.order_date != issue:
            record.dates["order_date"] = src.order_date
        self.common_refs(record, src, ctx)
        self.pick_note(record, ctx)
        return record


@register
class Quote(OrderDocument):
    doc_type = "quote"
    sources = ORDER_SOURCES
    number_prefix = "Q"

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"valid_days": rng.choice([15, 30, 30, 60]),
                                                 "lead_days": rng.randint(3, 21)}

    def build_record(self, src: SrcOrder, ctx: GenContext) -> DocumentRecord:
        opts = ctx.options
        issue = src.order_date - timedelta(days=opts["lead_days"])
        record = self.start(src, ctx, issue)
        record.totals = self.make_totals(record.items, ctx, src.freight, src.tax_amount, src.lines)
        record.dates["valid_until"] = issue + timedelta(days=opts["valid_days"])
        record.extra["validity"] = ctx.locale.t("quote_validity").format(days=opts["valid_days"])
        opts["show_order_id"] = False          # the order doesn't exist yet when quoting
        self.common_refs(record, src, ctx)
        self.pick_note(record, ctx)
        return record

    def required_strings(self, record, ctx):
        return super().required_strings(record, ctx) | {"validity": record.extra["validity"]}


@register
class CreditNote(OrderDocument):
    doc_type = "credit_note"
    sources = ORDER_SOURCES
    number_prefix = "CN"
    REASONS = ["credit_reason_damaged", "credit_reason_wrong_item", "credit_reason_expired",
               "credit_reason_price", "credit_reason_return"]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"reason": rng.choice(self.REASONS),
                                                 "days_after": rng.randint(5, 45),
                                                 "credit_lines": rng.choice([1, 1, 2, 3])}

    def build_record(self, src: SrcOrder, ctx: GenContext) -> DocumentRecord:
        opts, rng = ctx.options, ctx.rng
        invoice_date = src.ship_date or src.order_date
        issue = invoice_date + timedelta(days=opts["days_after"])
        picked = sorted(rng.sample(range(len(src.lines)), min(opts["credit_lines"], len(src.lines))))
        lines = []
        for i in picked:
            line = src.lines[i]
            qty = line.quantity if opts["reason"] == "credit_reason_price" else rng.randint(1, max(1, int(line.quantity)))
            lines.append(type(line)(**{**line.__dict__, "quantity": qty}))
        if opts["reason"] == "credit_reason_price":      # a price adjustment credits part of the price
            for line in lines:
                line.unit_price = q2(line.unit_price * Decimal(rng.choice(["0.05", "0.1", "0.15"])))
        record = self.start(src, ctx, issue, lines=lines)
        record.totals = self.make_totals(record.items, ctx, 0, src.tax_amount, src.lines, partial=True)
        record.refs["original_invoice"] = self.make_number(src.key, invoice_date, ctx, doc_type="invoice")
        if src.customer.code:
            record.refs["customer_id"] = src.customer.code
        record.extra["reason"] = ctx.locale.t(opts["reason"])
        record.notes = ctx.locale.t("credit_applied")
        opts["show_notes"] = True
        record.parties.pop("ship_to", None)
        return record

    def required_strings(self, record, ctx):
        return super().required_strings(record, ctx) | {"reason": record.extra["reason"],
                                                         "refs.original_invoice": record.refs["original_invoice"]}


def _packages(units: int, ctx: GenContext) -> int:
    return max(1, math.ceil(units / ctx.rng.choice([6, 12, 24, 48])))


def _weight(items) -> Decimal | None:
    weights = [i.extra["weight"] * i.extra.get("shipped", i.quantity) for i in items if i.extra.get("weight")]
    return sum(weights, Decimal(0)).quantize(Decimal("0.01")) if weights else None


@register
class PackingSlip(OrderDocument):
    doc_type = "packing_slip"
    sources = ORDER_SOURCES
    number_prefix = "PS"
    columns = [
        Column("sku", "sku", width="14%"),
        Column("description", "description"),
        Column("unit", "unit", width="16%", option="show_unit"),
        Column("quantity", "ordered", "right", "int", "10%"),
        Column("extra.shipped", "shipped", "right", "int", "10%"),
        Column("extra.backordered", "backordered", "right", "int", "11%", option="has_backorder"),
    ]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"show_unit": rng.random() < 0.6,
                                                 "backorder": rng.random() < 0.2}

    def build_record(self, src: SrcOrder, ctx: GenContext) -> DocumentRecord:
        opts = ctx.options
        record = self.start(src, ctx, src.ship_date or src.order_date, prices=False)
        short = ctx.rng.randrange(len(record.items)) if opts["backorder"] and record.items else -1
        for i, item in enumerate(record.items):
            shipped = item.quantity if i != short or item.quantity < 2 else ctx.rng.randint(1, item.quantity - 1)
            item.extra["shipped"], item.extra["backordered"] = shipped, item.quantity - shipped
        opts["has_backorder"] = any(i.extra["backordered"] for i in record.items)
        units = sum(i.extra["shipped"] for i in record.items)
        record.summary = [SummaryRow("packages", _packages(units, ctx), "int"),
                          SummaryRow("total_units", units, "int", strong=True)]
        weight = _weight(record.items)
        if weight:
            record.summary.insert(1, SummaryRow("total_weight", f"{ctx.locale.number(weight)} kg", "text"))
        self.common_refs(record, src, ctx)
        if src.carrier:
            record.refs["carrier"] = src.carrier.name
        if src.tracking:
            record.refs["tracking_number"] = src.tracking
        self.pick_note(record, ctx)
        return record


@register
class ShippingOrder(OrderDocument):
    doc_type = "shipping_order"
    sources = ORDER_SOURCES
    number_prefix = "SO"
    columns = [
        Column("sku", "sku", width="14%"),
        Column("description", "description"),
        Column("quantity", "quantity", "right", "int", "9%"),
        Column("extra.line_weight", "weight", "right", "number", "13%", option="has_weight"),
        Column("line_total", "declared_value", "right", "money", "16%", option="show_values"),
    ]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"show_values": rng.random() < 0.6,
                                                 "freight_terms": rng.choice(["freight_prepaid", "freight_collect"])}

    def build_record(self, src: SrcOrder, ctx: GenContext) -> DocumentRecord:
        opts = ctx.options
        opts["tax_mode"] = "none"
        record = self.start(src, ctx, src.ship_date or src.order_date)
        consignee = src.ship_to or src.customer
        record.parties = {"issuer": ctx.issuer, "consignee": consignee}
        if src.carrier:
            record.parties["carrier"] = src.carrier
        for item in record.items:
            if item.extra.get("weight"):
                item.extra["line_weight"] = (item.extra["weight"] * item.quantity).quantize(Decimal("0.01"))
        opts["has_weight"] = any("line_weight" in i.extra for i in record.items)
        units = sum(i.quantity for i in record.items)
        value = sum((i.line_total for i in record.items), Decimal(0))
        record.summary = [SummaryRow("packages", _packages(units, ctx), "int")]
        weight = _weight(record.items)
        if weight:
            record.summary.append(SummaryRow("total_weight", f"{ctx.locale.number(weight)} kg", "text"))
        if opts["show_values"]:
            record.summary.append(SummaryRow("declared_value", value, "money"))
        record.summary += [SummaryRow("freight_terms", ctx.locale.t(opts["freight_terms"]), "text"),
                           SummaryRow("freight", convert(src.freight, ctx.fx_rate), "money", strong=True)]
        record.dates["pickup_date"] = src.ship_date or src.order_date
        if src.required_date:
            record.dates["deliver_by"] = src.required_date
        record.refs["order_id"] = src.ref
        if src.tracking:
            record.refs["tracking_number"] = src.tracking
        record.notes = ctx.locale.t("handling_text")
        opts["show_notes"] = True
        return record

    def party_blocks(self, record, ctx):
        return [(ctx.locale.t(role), p) for role, p in record.parties.items() if role != "issuer"]
