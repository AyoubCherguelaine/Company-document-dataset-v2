"""HR documents from AdventureWorks employees: payslip and certificate of employment."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from ..core.document import BaseDocument, Column, GenContext
from ..core.models import DocumentRecord, LineItem, SummaryRow
from ..core.money import ZERO, D, convert, q2
from ..core.registry import register
from ..sources.base import SrcEmployee


def _mask(national_id: str) -> str:
    digits = "".join(ch for ch in national_id if ch.isdigit())
    return f"•••-••-{digits[-4:]}" if len(digits) >= 4 else national_id


class EmployeeDocument(BaseDocument):
    record_kind = "employee"
    sources = ["adventureworks"]

    def employee_rows(self, e: SrcEmployee, ctx: GenContext) -> list[tuple[str, str]]:
        loc = ctx.locale
        rows = [("employee_id", str(e.key)), ("job_title", e.job_title), ("department", e.department),
                ("hire_date", loc.date(e.hire_date))]
        if e.shift:
            rows.append(("shift", e.shift))
        return rows


@register
class Payslip(EmployeeDocument):
    doc_type = "payslip"
    number_prefix = "PAY"
    columns = [
        Column("description", "description"),
        Column("extra.hours", "hours", "right", "number", "14%"),
        Column("unit_price", "rate", "right", "money", "16%"),
        Column("line_total", "amount", "right", "money", "18%"),
    ]

    def variation(self, rng, locale):
        return super().variation(rng, locale) | {"overtime_hours": rng.choice([0, 0, 0, 4, 8, 12]),
                                                 "bonus": rng.random() < 0.15, "show_balances": rng.random() < 0.6,
                                                 "show_national_id": rng.random() < 0.5}

    def period(self, e: SrcEmployee, ctx: GenContext) -> tuple[date, date]:
        """A pay period in 2024-2025: a calendar month, or two weeks for biweekly pay."""
        first = max(date(2024, 1, 1), e.hire_date)
        day = first + timedelta(days=ctx.rng.randrange(max(1, (date(2025, 9, 30) - first).days)))
        if e.pay_frequency == 1:
            start = day.replace(day=1)
            return start, day.replace(day=calendar.monthrange(day.year, day.month)[1])
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=13)

    def build_record(self, e: SrcEmployee, ctx: GenContext) -> DocumentRecord:
        opts, loc, rng = ctx.options, ctx.locale, ctx.rng
        payroll = loc.payroll
        start, end = self.period(e, ctx)
        pay_date = end + timedelta(days=rng.randint(0, 4))
        rate = convert(e.pay_rate, ctx.fx_rate)
        hours = D(payroll["hours"][e.pay_frequency])
        earnings = [LineItem(sku="", description=loc.t("regular_pay"), quantity=1, unit_price=rate,
                             line_total=q2(rate * hours), extra={"hours": hours})]
        if opts["overtime_hours"] and not e.salaried:
            ot_rate = q2(rate * D(payroll["overtime_factor"]))
            ot = D(opts["overtime_hours"])
            earnings.append(LineItem(sku="", description=loc.t("overtime_pay"), quantity=1, unit_price=ot_rate,
                                     line_total=q2(ot_rate * ot), extra={"hours": ot}))
        if opts["bonus"]:
            earnings.append(LineItem(sku="", description=loc.t("bonus"), quantity=1, unit_price=None,
                                     line_total=q2(earnings[0].line_total * D(rng.choice(["0.05", "0.1", "0.15"]))),
                                     extra={"hours": None}))
        gross = sum((i.line_total for i in earnings), ZERO)
        deductions = []
        for d in payroll.get("deductions", []) + payroll.get("fixed", []):
            if "rate" in d:
                amount, shown_rate = q2(gross * D(d["rate"])), D(d["rate"])
            else:
                amount, shown_rate = convert(d["amount"], ctx.fx_rate), None
            deductions.append({"label": loc.t(d["label"]), "rate": shown_rate, "base": gross if shown_rate else None,
                               "amount": amount})
        total_ded = sum((d["amount"] for d in deductions), ZERO)

        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(e.key, end, ctx),
                                issue_date=pay_date, currency=ctx.currency, items=earnings)
        record.parties = {"issuer": ctx.issuer, "employee": e.person}
        record.refs["pay_period"] = f"{loc.date(start)} – {loc.date(end)}"
        record.extra["employee"] = self.employee_rows(e, ctx)
        if opts["show_national_id"]:
            record.extra["employee"].append(("national_id", _mask(e.national_id)))
        record.extra["deductions"] = deductions
        record.extra["balances"] = [("vacation_balance", e.vacation_hours), ("sick_balance", e.sick_hours)] \
            if opts["show_balances"] else []
        record.summary = [SummaryRow("gross_pay", gross), SummaryRow("total_deductions", total_ded),
                          SummaryRow("net_pay", gross - total_ded, strong=True)]
        record.extra["payment_note"] = loc.t("paid_by_transfer")
        return record

    def meta_rows(self, record, ctx):
        loc = ctx.locale
        return [(loc.t("number"), record.number), (loc.t("pay_period"), record.refs["pay_period"]),
                (loc.t("pay_date"), loc.date(record.issue_date))]

    def required_strings(self, record, ctx):
        req = super().required_strings(record, ctx)
        req.pop("issue_date", None)
        req["pay_date"] = ctx.locale.date(record.issue_date)
        for i, d in enumerate(record.extra["deductions"]):
            req[f"deductions.{i}.amount"] = ctx.locale.money(d["amount"], record.currency)
        for label, value in record.extra["employee"]:
            req[f"employee.{label}"] = value
        return req

    def validate(self, record):
        s = {r.label: r.value for r in record.summary}
        problems = []
        if sum((i.line_total for i in record.items), ZERO) != s["gross_pay"]:
            problems.append("gross != sum(earnings)")
        if sum((d["amount"] for d in record.extra["deductions"]), ZERO) != s["total_deductions"]:
            problems.append("total deductions mismatch")
        if s["gross_pay"] - s["total_deductions"] != s["net_pay"]:
            problems.append("net != gross - deductions")
        return problems


@register
class EmploymentCertificate(EmployeeDocument):
    doc_type = "employment_certificate"
    number_prefix = "HR"
    columns = []

    def build_record(self, e: SrcEmployee, ctx: GenContext) -> DocumentRecord:
        loc, rng = ctx.locale, ctx.rng
        cert = loc.certificate
        first = max(date(2024, 1, 1), e.hire_date + timedelta(days=60))
        issue = first + timedelta(days=rng.randrange(max(1, (date(2025, 9, 30) - first).days)))
        values = {"name": e.person.name, "company": ctx.issuer.name, "job_title": e.job_title,
                  "department": e.department, "hire_date": loc.date(e.hire_date),
                  "birth_date": loc.date(e.birth_date) if e.birth_date else "", "shift": e.shift.lower()}
        body = [rng.choice(cert["salutation"])] if cert.get("salutation") else []
        body.append(rng.choice(cert["body"]).format(**values))
        if rng.random() < 0.6 and cert.get("extra"):
            body.append(rng.choice(cert["extra"]).format(**values))
        body.append(rng.choice(cert["closing"]).format(**values))
        body.append(cert["issued"].format(city=ctx.issuer.address.city, date=loc.date(issue)))
        record = DocumentRecord(doc_type=self.doc_type, number=self.make_number(e.key, issue, ctx),
                                issue_date=issue, currency=ctx.currency, body=[p for p in body if p])
        record.parties = {"issuer": ctx.issuer}
        name, role = ctx.repo.hr_signatory()
        record.extra["signatory"] = {"name": name, "role": role}
        record.extra["employee"] = self.employee_rows(e, ctx)
        ctx.options["show_signature"] = True
        return record

    def required_strings(self, record, ctx):
        req = super().required_strings(record, ctx)
        if record.extra["signatory"]["name"]:
            req["signatory"] = record.extra["signatory"]["name"]
        return req
