# docgen

Sample business databases → normalized records → canonical document record → HTML (Jinja2) → PDF (WeasyPrint)
→ self-check → PDF + gold JSON + word boxes.

13 document types across 4 source databases. Every document is issued by a synthetic **company** with its own
fixed design, and texts can be written by a free LLM on OpenRouter.

```bash
pip install -r requirements.txt
python scripts/prepare_sources.py                 # once: lookup indexes (AdventureWorks ships with none)
cp .env.example .env                              # optional: OPENROUTER_API_KEY for LLM texts
python scripts/openrouter_models.py               # optional: pick a free model

python -m docgen companies init --llm             # 6 companies per sector (food, bikes, music, video)
python -m docgen texts generate                   # LLM company texts + Northwind product descriptions
python -m docgen list                             # types, sources, layouts, themes, companies

python -m docgen preview payslip --open           # one document, for designing templates
python -m docgen preview invoice.adventureworks --company iris-sports-corp --html-only
python -m docgen generate configs/example.yaml    # the whole catalog (290 documents, about 1 minute)
python -m unittest discover -s tests
```

## Catalog

| Document type | Sources | Built from |
|---|---|---|
| `invoice` | northwind, adventureworks, chinook | orders; AdventureWorks tax/freight as stated, Chinook B2C paid by card |
| `quote` | northwind, adventureworks | orders, dated before the order, with validity and acceptance |
| `credit_note` | northwind, adventureworks | 1–3 lines of an order, partial quantities, reason, original invoice number |
| `packing_slip` | northwind, adventureworks | order quantities, backorders, packages, weights (AdventureWorks) |
| `shipping_order` | northwind, adventureworks | consignee, carrier, tracking, weights, declared value, freight terms |
| `purchase_order` | northwind, adventureworks | AdventureWorks POs; Northwind: one per (order, supplier) |
| `goods_received_note` | adventureworks | completed POs: ordered / received / rejected / accepted |
| `account_statement` | northwind, adventureworks | a customer's invoices + seeded payments, running balance, aging, remittance slip |
| `inventory_report` | northwind, adventureworks | stock by category/supplier (Northwind) or warehouse location (AdventureWorks) |
| `receipt` | chinook, sakila | A5; music downloads, or a film rental with late fee; cash or card |
| `work_order` | adventureworks | production order and its routing operations, planned vs actual cost |
| `payslip` | adventureworks | employee, pay rate and frequency; locale-specific deductions (US / FR) |
| `employment_certificate` | adventureworks | letter with phrasing variants, signed by the HR manager |

Each source belongs to a sector, and only companies of that sector issue its documents:
northwind → food, adventureworks → bikes, chinook → music, sakila → video. Sakila's 2005–2006 dates are
shifted 19 years forward (`date_shift` in the config).

## Layers

| Layer | Where | Responsibility |
|---|---|---|
| Source | `sources/<name>.py` | One adapter per database. It turns rows into normalized records (`sources/base.py`): `SrcOrder`, `SrcPurchase`, `SrcAccount`, `SrcInventory`, `SrcReceipt`, `SrcEmployee`, `SrcWorkOrder`. |
| Document | `documents/*.py` | One class per type, written once against a record kind and registered per source as `<type>.<source>`. Builds the `DocumentRecord`: Decimal money, totals, summary rows, letter body. |
| Company | `companies/<slug>/company.yaml` (+ `templates/`) | Issuer identity, sector, layout and theme per document type, brand colours, numbering per type, pinned options, texts. |
| Layout | `templates/layouts/*.html.j2` | Where components go. Extends `base.html.j2` and composes `components.html.j2` macros. |
| Type template | `documents/templates/<type>/document.html.j2` | Extends the company's layout and overrides only the blocks that differ. |
| Style | `themes/*.yaml` | Fonts, colours, table style and spacing (CSS variables). A company's `colors` override them. |
| Locale | `locales/*.yaml` | Labels, number/money/date formats, tax rates, payroll deductions, certificate wording. |

## Config

```yaml
documents:
  - {type: invoice, count: 60}                     # every source that has invoices
  - {type: invoice, count: 10, sources: [chinook]} # restrict sources
  - {type: "*", count: 100, locales: [fr]}         # every type, French companies only
  - {type: payslip, count: 5, companies: [iris-sports-corp]}
sources: {northwind: data/northwind.db}            # override a database path
date_shift: {sakila: 19}
```

## Adding a document type

1. Pick the record kind it is built from (or add one, see below). Create `docgen/documents/<file>.py`:

```python
@register
class DeliveryNote(OrderDocument):          # OrderDocument = BaseDocument + order helpers
    doc_type = "delivery_note"
    sources = ["northwind", "adventureworks"]
    number_prefix = "DN"
    columns = [Column("sku", "sku"), Column("description", "description"),
               Column("quantity", "quantity", "right", "int")]

    def build_record(self, src, ctx):       # src is a SrcOrder; ctx has company, locale, rng, repo
        record = self.start(src, ctx, src.ship_date or src.order_date, prices=False)
        record.summary = [SummaryRow("total_units", sum(i.quantity for i in record.items), "int", strong=True)]
        return record
```

   Optionally override `variation()` (call `super()` and merge), `meta_rows()`, `party_blocks()`,
   `required_strings()` (what the self-check must find in the PDF) and `validate()` (arithmetic rules).
   Helpers on `BaseDocument`: `make_number`, `sample_lines`, `make_items` (currency conversion, tax, LLM details),
   `make_totals`.

2. Add `documents/templates/<type>/document.html.j2`:

```jinja
{% extends "layouts/" ~ layout ~ ".html.j2" %}
{% import "components.html.j2" as c with context %}
{% block notes %}{{ c.signatures(['packed_by', 'received_by']) }}{% endblock %}
```

   Blocks: `header`, `meta`, `parties`, `body`, `before_items`, `items`, `summary` (`summary_left`), `notes`, `footer`,
   `extra_css`. Macros: `line_table`, `totals_table`, `summary_table`, `info_grid`, `signature_block`, `signatures`,
   `remittance`, `bank_block`, `stamp`, `notes_block`. Helpers: `t(key)`, `ctext(key)`, `fmtv(value, fmt)`, and the
   filters `money`, `date`, `num`, `pct`.

3. Add the title and new labels to every `locales/*.yaml`, and a numbering format to
   `core/companies.py:NUMBER_FORMATS`.

## Adding a source

1. Add the path to `sources/__init__.py:DEFAULT_PATHS`, and add any indexes to `scripts/prepare_sources.py`.
2. Create `sources/<name>.py` with a `SQLiteSource` subclass that sets `name` and `sector`, and implements
   `<kind>_keys()` and `load_<kind>(key)` for each record kind it can provide (dates through `self.date()`, so
   `date_shift` applies).
3. Register it in `sources/__init__.py:source_classes()` and add `name` to the `sources` list of the document types
   it supports. For a new sector, add it to `core/companies.py:SECTORS`, `core/issuers.py:SECTOR_NAME_B` and
   `SECTOR_TEXTS`, then run `companies init --sectors <sector>`.

## Companies

`companies init` creates seeded companies per sector, spread over every layout × theme. It never touches existing
ones; use `--force` to add more to a sector. Edit `company.yaml` freely:

```yaml
sector: bikes
design:
  default: {layout: modern, theme: corporate}
  payslip: {layout: classic, theme: plain}    # per document type
colors: {primary: "#14532d"}
options: {show_bank: true}                    # pinned; other options still vary per document
numbering: {invoice: "BW-{yy}{key:06d}"}      # {key} {yyyy} {yy}; missing types get a fixed default
texts: {tagline: ..., about: ..., payment_instructions: ..., footer: ..., notes: [...]}
```

**A company's own template**: files in `companies/<slug>/templates/` override shared templates for that company.
`templates/company/<type>.html.j2` can extend the standard one, e.g. `{% extends "invoice/document.html.j2" %}`
(see `companies/boreal-wholesale-ltd/`).

## LLM (OpenRouter, free models)

`.env` holds `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` (comma-separated fallbacks). Free accounts get roughly
50 requests per day, so the LLM fills a text bank once and is never called per document: 1 request per company and
1 per 20 Northwind products per locale. Responses are cached in `data/llm_cache.db`. AdventureWorks already has
English and French product descriptions, and Chinook shows artist and album, so neither needs the LLM.

## Output (`out_dir`)

```
pdf/<type>/<doc_id>.pdf
gold/<type>/<doc_id>.json    variant, source, company, fields (canonical record), rendered strings, boxes, options
words/<type>/<doc_id>.json   every PDF word with page + bbox (PyMuPDF points)
manifest.jsonl               one row per planned document: ok | failed (self-check) | error
config.yaml                  the exact run config
```

`doc_id = sha1(key|variant|company|seed)[:16]`. With the same config, companies and text bank, a rerun produces
byte-identical PDFs. A document is not written if its self-check fails: a gold value (number, party, line, total,
summary figure, letter paragraph, note) is missing from the PDF text, or its arithmetic doesn't hold (totals,
running balances, aging, payroll, received vs accepted).
