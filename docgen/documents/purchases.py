"""Documents built from a purchase: purchase order (our company buys) and goods received note."""

from __future__ import annotations

from datetime import timedelta

from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, Party, SummaryRow
from ..core.registry import register
from ..sources.base import SrcPurchase


def _deliver_to(ctx: GenContext) -> Party:
    issuer = ctx.issuer
    return Party(name=issuer.name, address=issuer.address, phone=issuer.phone)


@register
class PurchaseOrder(BaseDocument):
    doc_type = "purchase_order"
    sources = ["northwind", "adventureworks"]
    record_kind = "purchase"
    number_prefix = "PO"

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"show_approver": rng.random() < 0.5,
                                                 "payment_days": rng.choice([30, 45, 60])}

    def build_record(self, src: SrcPurchase, ctx: GenContext) -> DocumentRecord:
        opts = ctx.options
        if src.tax_amount is not None:
            opts["tax_mode"], opts["show_tax_column"] = "given", False
        lines = self.sample_lines(src.lines, ctx)
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, src.order_date, ctx),
                                issue_date=src.order_date, currency=ctx.currency, items=self.make_items(lines, ctx))
        record.totals = self.make_totals(record.items, ctx, src.freight, src.tax_amount, src.lines)
        record.parties = {"issuer": ctx.issuer, "vendor": src.vendor}
        if opts["show_ship_to"]:
            record.parties["deliver_to"] = _deliver_to(ctx)
        if src.ship_date:
            record.dates["delivery_date"] = src.ship_date
        if src.vendor.code:
            record.refs["account_number"] = src.vendor.code
        if src.buyer:
            record.refs["requested_by"] = src.buyer
        if src.carrier:
            record.refs["carrier"] = src.carrier.name
        record.extra["payment_terms"] = ctx.locale.t("net_days").format(days=opts["payment_days"])
        record.extra["instructions"] = ctx.locale.t("po_instructions")
        return record

    def required_strings(self, record, ctx):
        return super().required_strings(record, ctx) | {"instructions": record.extra["instructions"]}


@register
class GoodsReceivedNote(BaseDocument):
    doc_type = "goods_received_note"
    sources = ["adventureworks"]
    record_kind = "purchase"
    number_prefix = "GRN"
    columns = [
        Column("sku", "sku", width="14%"),
        Column("description", "description"),
        Column("quantity", "ordered", "right", "int", "10%"),
        Column("extra.received", "received", "right", "int", "10%"),
        Column("extra.rejected", "rejected", "right", "int", "10%"),
        Column("extra.accepted", "accepted", "right", "int", "10%"),
    ]

    def source_keys(self, repo):
        return repo.purchase_keys(complete_only=True)

    def build_record(self, src: SrcPurchase, ctx: GenContext) -> DocumentRecord:
        received_on = (src.ship_date or src.order_date) + timedelta(days=ctx.rng.randint(0, 3))
        lines = self.sample_lines(src.lines, ctx)
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, received_on, ctx),
                                issue_date=received_on, currency=ctx.currency,
                                items=self.make_items(lines, ctx, prices=False))
        for item in record.items:
            item.extra["accepted"] = item.extra["received"] - item.extra["rejected"]
        record.parties = {"issuer": ctx.issuer, "supplier": src.vendor}
        record.refs["po_reference"] = self.make_number(src.key, src.order_date, ctx, doc_type="purchase_order")
        record.dates["order_date"] = src.order_date
        if src.carrier:
            record.refs["carrier"] = src.carrier.name
        total = lambda k: sum(i.extra[k] if k != "quantity" else i.quantity for i in record.items)  # noqa: E731
        record.summary = [SummaryRow("total_ordered", total("quantity"), "int"),
                          SummaryRow("total_received", total("received"), "int"),
                          SummaryRow("total_rejected", total("rejected"), "int"),
                          SummaryRow("total_accepted", total("accepted"), "int", strong=True)]
        record.extra["inspector"] = src.buyer
        return record

    def validate(self, record):
        return [f"{i.sku}: accepted != received - rejected" for i in record.items
                if i.extra["accepted"] != i.extra["received"] - i.extra["rejected"]]
