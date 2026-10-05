"""Receipt: proof of a paid retail transaction (Chinook downloads, Sakila rentals). A5 format."""

from __future__ import annotations

import math
from decimal import Decimal

from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, Totals
from ..core.money import q2
from ..core.registry import register
from ..sources.base import SrcReceipt


@register
class Receipt(BaseDocument):
    doc_type = "receipt"
    sources = ["chinook", "sakila"]
    record_kind = "receipt"
    number_prefix = "R"
    page_size = "A5"
    columns = [
        Column("description", "description"),
        Column("quantity", "quantity", "right", "int", "12%"),
        Column("unit_price", "unit_price", "right", "money", "20%", option="show_unit_price"),
        Column("line_total", "amount", "right", "money", "22%"),
    ]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"show_unit_price": rng.random() < 0.5,
                                                 "cash": rng.random() < 0.4, "max_items": 40}

    def build_record(self, src: SrcReceipt, ctx: GenContext) -> DocumentRecord:
        opts, rng, loc = ctx.options, ctx.rng, ctx.locale
        opts["tax_mode"], opts["show_tax_column"], opts["show_discount"] = "none", False, False
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, src.when.date(), ctx),
                                issue_date=src.when.date(), currency=ctx.currency,
                                items=self.make_items(src.lines, ctx))
        record.totals = Totals.from_items(record.items, ctx.currency, apply_tax=False)
        record.parties = {"issuer": ctx.issuer, "customer": src.customer}
        record.refs["time"] = src.when.strftime("%H:%M")
        record.refs["transaction"] = src.ref
        if src.extra.get("store"):
            record.refs["store"] = src.extra["store"]
        if src.served_by:
            record.refs["served_by"] = src.served_by
        for k in ("rental_date", "return_date"):
            if src.extra.get(k):
                record.dates[k] = src.extra[k].date()

        total = record.totals.total
        online = src.extra.get("channel") == "online"
        if opts["cash"] and not online:
            step = Decimal(rng.choice([1, 5, 10, 20]))
            tendered = q2(Decimal(math.ceil(total / step)) * step)
            record.extra["payment"] = [("payment_method", loc.t("cash")), ("amount_tendered", loc.money(tendered, ctx.currency)),
                                       ("change", loc.money(tendered - total, ctx.currency))]
        else:
            brand = rng.choice(["Visa", "Mastercard", "Amex"])
            record.extra["payment"] = [("payment_method", f"{loc.t('card')} {brand} •••• {rng.randint(1000, 9999)}"),
                                       ("amount_paid", loc.money(total, ctx.currency))]
        if online:
            record.refs["transaction"] = f"{loc.t('online_order')} {src.ref}"
        record.notes = loc.t("receipt_footer")
        opts["show_notes"] = True
        return record

    def meta_rows(self, record, ctx):
        loc = ctx.locale
        rows = [(loc.t("number"), record.number), (loc.t("date"), loc.date(record.issue_date))]
        rows += [(loc.t(k), v) for k, v in record.refs.items()]
        rows += [(loc.t(k), loc.date(v)) for k, v in record.dates.items()]
        return rows

    def required_strings(self, record, ctx):
        req = super().required_strings(record, ctx)
        for label, value in record.extra["payment"]:
            req[f"payment.{label}"] = value
        return req
