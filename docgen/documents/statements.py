"""Statement of account: a customer's invoices and payments over a period, with running balance."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, LineItem, SummaryRow
from ..core.money import ZERO, convert
from ..core.registry import register
from ..sources.base import SrcAccount


@register
class AccountStatement(BaseDocument):
    doc_type = "account_statement"
    sources = ["northwind", "adventureworks"]
    record_kind = "account"
    number_prefix = "ST"
    columns = [
        Column("extra.date", "date", fmt="date", width="15%"),
        Column("description", "description"),
        Column("extra.debit", "debit", "right", "money", "15%"),
        Column("extra.credit", "credit", "right", "money", "15%"),
        Column("extra.balance", "balance", "right", "money", "16%"),
    ]

    # AdventureWorks stores order every few months, Northwind customers every few weeks
    WINDOWS = {"adventureworks": [180, 365, 365], "northwind": [30, 60, 90, 90, 180]}

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"window_days": rng.choice(self.WINDOWS.get(self.source, [90])),
                                                 "pay_ratio": rng.choice([0.5, 0.75, 0.9]),
                                                 "show_aging": rng.random() < 0.7,
                                                 "show_remittance": rng.random() < 0.6}

    def build_record(self, src: SrcAccount, ctx: GenContext) -> DocumentRecord:
        opts, rng, loc = ctx.options, ctx.rng, ctx.locale
        entries = src.entries
        anchor = entries[rng.randrange(len(entries) // 3, len(entries))] if len(entries) > 2 else entries[-1]
        window = timedelta(days=opts["window_days"])
        end = anchor.date + timedelta(days=rng.randint(1, max(1, opts["window_days"] // 2)))
        start = end - window + timedelta(days=1)

        def inv_number(entry):
            digits = "".join(ch for ch in entry.ref if ch.isdigit()) or entry.ref
            return self.make_number(digits, entry.date, ctx, doc_type="invoice")

        def payment_date(entry):
            return entry.date + timedelta(days=entry.due_days + rng.randint(-10, 12))

        events = []            # (date, order, description, debit, credit)
        open_invoices = {}     # number -> (date, amount) still unpaid at the statement date
        opening = ZERO
        for e in entries:
            amount = convert(e.amount, ctx.fx_rate)
            if e.date < start:
                paid_on = payment_date(e)
                if paid_on >= start:                         # still open at period start
                    opening += amount
                    if paid_on <= end:
                        events.append((paid_on, 1, loc.t("payment_entry") + f" · {inv_number(e)}", ZERO, amount))
                    else:
                        open_invoices[inv_number(e)] = (e.date, amount)
            elif e.date <= end:
                events.append((e.date, 0, loc.t("invoice_entry").format(ref=inv_number(e)), amount, ZERO))
                paid_on = payment_date(e)
                if paid_on <= end and rng.random() < opts["pay_ratio"]:
                    events.append((paid_on, 1, loc.t("payment_entry") + f" · {inv_number(e)}", ZERO, amount))
                else:
                    open_invoices[inv_number(e)] = (e.date, amount)
        events.sort(key=lambda ev: (ev[0], ev[1]))

        balance, items = opening, []
        for when, _, text, debit, credit in events:
            balance += debit - credit
            items.append(LineItem(sku="", description=text, quantity=1, extra={
                "date": when, "debit": debit or None, "credit": credit or None, "balance": balance}))
        debits = sum((i.extra["debit"] or ZERO for i in items), ZERO)
        credits = sum((i.extra["credit"] or ZERO for i in items), ZERO)

        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(src.key, end, ctx),
                                issue_date=end, currency=ctx.currency, items=items)
        record.parties = {"issuer": ctx.issuer, "customer": src.customer}
        record.refs["account_number"] = src.customer.code or str(src.key)
        record.refs["period"] = f"{loc.date(start)} – {loc.date(end)}"
        record.summary = [SummaryRow("opening_balance", opening), SummaryRow("invoiced", debits),
                          SummaryRow("payments", credits), SummaryRow("amount_due", balance, strong=True)]
        record.extra["aging"] = self.aging(open_invoices, end) if opts["show_aging"] else []
        return record

    @staticmethod
    def aging(open_invoices: dict, end) -> list[tuple[str, Decimal]]:
        """Unpaid invoices bucketed by days past due (30-day terms); the buckets sum to the amount due."""
        buckets = {"aging_current": ZERO, "aging_30": ZERO, "aging_60": ZERO, "aging_90": ZERO}
        for when, amount in open_invoices.values():
            overdue = (end - when).days - 30
            key = ("aging_current" if overdue <= 0 else "aging_30" if overdue <= 30
                   else "aging_60" if overdue <= 60 else "aging_90")
            buckets[key] += amount
        return list(buckets.items())

    def meta_rows(self, record, ctx):
        loc = ctx.locale
        return [(loc.t("number"), record.number), (loc.t("statement_date"), loc.date(record.issue_date))] + \
               [(loc.t(k), v) for k, v in record.refs.items()]

    def required_strings(self, record, ctx):
        req = super().required_strings(record, ctx)
        req.pop("issue_date", None)
        req["statement_date"] = ctx.locale.date(record.issue_date)
        for i, item in enumerate(record.items):
            req[f"items.{i}.balance"] = ctx.locale.money(item.extra["balance"], record.currency)
        return req

    def validate(self, record):
        s = {r.label: r.value for r in record.summary}
        problems = []
        if s["opening_balance"] + s["invoiced"] - s["payments"] != s["amount_due"]:
            problems.append("opening + invoiced - payments != amount due")
        if record.items and record.items[-1].extra["balance"] != s["amount_due"]:
            problems.append("last running balance != amount due")
        if record.extra["aging"] and sum(v for _, v in record.extra["aging"]) != s["amount_due"]:
            problems.append("aging buckets != amount due")
        return problems
