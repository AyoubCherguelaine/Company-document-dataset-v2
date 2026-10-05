"""Run with: python -m unittest discover -s tests"""

import json
import random
import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from docgen import PACKAGE_DIR
from docgen.core import registry
from docgen.core.companies import SECTORS, create_companies
from docgen.core.issuers import IssuerPool
from docgen.core.llm import parse_json
from docgen.core.locale import load_locales
from docgen.core.models import LineItem, Totals
from docgen.core.pipeline import RunConfig, Workspace, make_job, plan, render_job
from docgen.core.renderer import Renderer
from docgen.core.selfcheck import check_pdf, locate
from docgen.core.textbank import generate_product_texts

LOCALES = load_locales([PACKAGE_DIR / "locales"])


class FakeLLM:
    """Stands in for OpenRouter: answers company and product prompts with canned JSON."""
    enabled = True

    def chat_json(self, system, user, max_tokens=2000):
        if "catalogue description" in user:
            ids = [line.split(" | ")[0] for line in user.splitlines() if " | " in line and line[0].isdigit()]
            return {i: f"Carefully packed specialty item number {i} from trusted growers." for i in ids}
        return {"tagline": "Fine foods since 1987", "about": "We supply restaurants with fine foods.",
                "payment_instructions": "Pay by bank transfer within the terms.", "footer": "See you soon.",
                "notes": ["Keep refrigerated after delivery, please.", "Late payments incur a fee of 1.5% monthly."]}


class MoneyAndTotals(unittest.TestCase):
    def test_totals_add_up(self):
        items = [LineItem("1", "A", 3, Decimal("9.99"), Decimal("0.1"), Decimal("0.20")).compute(),
                 LineItem("2", "B", 1, Decimal("0.05"), Decimal(0), Decimal("0.055")).compute()]
        t = Totals.from_items(items, "EUR", Decimal("4.5"))
        self.assertEqual(items[0].line_total, Decimal("26.97"))     # 29.97 * 0.9 = 26.973
        self.assertEqual(t.subtotal, Decimal("27.02"))
        self.assertEqual(t.tax_total, Decimal("5.39"))
        self.assertEqual(t.total, Decimal("36.91"))
        self.assertEqual(t.verify(items), [])
        t.total += Decimal("0.01")
        self.assertTrue(t.verify(items))


class Formatting(unittest.TestCase):
    def test_locales(self):
        en, fr = LOCALES["en"], LOCALES["fr"]
        self.assertEqual(en.money(Decimal("1234.5")), "$1,234.50")
        self.assertEqual(fr.money(Decimal("1234.5"), "EUR"), "1 234,50 €")
        self.assertEqual(fr.date(date(2020, 8, 3)), "3 août 2020")
        self.assertEqual(fr.percent(Decimal("0.055")), "5,5 %")

    def test_parse_json_tolerates_fences(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('Sure! {"a": [1]} hope it helps'), {"a": [1]})


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        llm = FakeLLM()
        for sector in SECTORS:
            for c in create_companies(2, seed=3, layouts=["classic", "modern"], themes=["plain", "warm"],
                                      llm=llm, log=lambda *_: None, sector=sector):
                c.options["show_item_details"] = True
                c.options["show_notes"] = True
                c.save(cls.tmp / "companies")
        cls.cfg = RunConfig(seed=7, out_dir=str(cls.tmp / "out"), companies_dir=str(cls.tmp / "companies"),
                            textbank_dir=str(cls.tmp / "textbank"),
                            documents=[{"type": "invoice", "count": 4}])
        cls.ws = Workspace(cls.cfg)
        for loc in ("en", "fr"):
            generate_product_texts(llm, cls.ws.source("northwind"), cls.ws.texts, loc, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_plan_is_deterministic(self):
        self.assertEqual(plan(self.cfg, self.ws), plan(self.cfg, self.ws))

    def test_company_design_is_fixed(self):
        for job in plan(self.cfg, self.ws):
            design = self.ws.companies[job["company"]].design_for("invoice")
            self.assertEqual((job["layout"], job["theme"]), (design["layout"], design["theme"]))

    def test_render_with_llm_texts(self):
        for job in plan(self.cfg, self.ws):
            result = render_job(job, self.ws)
            self.assertEqual(result["status"], "ok", result)
            gold = json.loads((self.tmp / "out" / "gold" / "invoice" / f"{job['doc_id']}.json").read_text())
            self.assertIn("notes", gold["rendered"])             # LLM company note printed + checked
            if job["source"] == "northwind":
                self.assertIn("items.0.details", gold["rendered"])  # LLM product text printed + checked

    def test_every_variant_renders_and_passes_selfcheck(self):
        for n, variant in enumerate(registry.variants()):
            doc = registry.get(variant)
            company = self.ws.companies_for(doc)[n % 2]
            keys = self.ws.keys(doc)
            job = make_job(self.ws, variant, keys[(n * 7919) % len(keys)], company, seed=n)
            result = render_job(job, self.ws)
            self.assertEqual(result["status"], "ok", f"{variant}: {result}")

    def test_companies_match_source_sector(self):
        for job in plan(RunConfig(**{**self.cfg.__dict__, "documents": [{"type": "*", "count": 30}]}), self.ws):
            sector = self.ws.companies[job["company"]].sector
            self.assertEqual(sector, self.ws.source(job["source"]).sector, job)

    def test_selfcheck_rejects_missing_field(self):
        job = plan(self.cfg, self.ws)[0]
        doc, record, ctx, html = self.ws.build(job)
        total = ctx.locale.money(record.totals.total, record.currency)
        pdf = Renderer.html_to_pdf(html.replace(f">{total}<", "><"))
        result = check_pdf(pdf, doc.required_strings(record, ctx), [])
        self.assertFalse(result.ok)
        self.assertIn("totals.total", result.missing)


    def test_csv_export_one_row_per_pdf(self):
        import csv

        from docgen.core.export import export_run
        from docgen.core.pipeline import run

        run(self.cfg, log=lambda *_: None)
        out = self.tmp / "dataset"
        stats = export_run(self.tmp / "out", out, self.ws.companies, log=lambda *_: None)
        rows = [r for f in sorted((out / "data").glob("*.csv")) for r in csv.DictReader(f.open(encoding="utf-8"))]
        self.assertEqual(len(rows), sum(stats["documents"].values()))
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue((out / row["file_name"]).exists())
            fields = json.loads(row["extracted_data"])
            self.assertEqual(json.loads((out / row["json_file"]).read_text(encoding="utf-8")), fields)
            self.assertEqual(row["number"], fields["number"])
            self.assertEqual(row["company_name"], self.ws.companies[row["company"]].party["name"])
        (out / "keep.txt").write_text("x")
        export_run(self.tmp / "out", out, self.ws.companies, log=lambda *_: None)   # replaces a previous export
        with self.assertRaises(SystemExit):                                          # never deletes anything else
            export_run(self.tmp / "out", self.tmp / "companies", self.ws.companies, log=lambda *_: None)


class BoxLocation(unittest.TestCase):
    @staticmethod
    def words(*texts):
        return [{"page": 0, "text": t, "bbox": [i * 10, 0, i * 10 + 8, 5]} for i, t in enumerate(texts)]

    def test_split_and_spaced_hyphens(self):
        self.assertEqual(len(locate("top-of-the-line bike", self.words("a", "top-of-the-", "line", "bike"))), 1)
        self.assertEqual(len(locate("Water Bottle - 30 oz.", self.words("Water", "Bottle", "-", "30", "oz."))), 1)
        self.assertEqual(locate("Bottle 30", self.words("Bottle", "300")), [])


class Issuers(unittest.TestCase):
    def test_issuers_match_locale(self):
        pool = IssuerPool(seed=1)
        self.assertIn(pool.pick(random.Random(0), "fr").code, ("FR", "BE"))


if __name__ == "__main__":
    unittest.main()
