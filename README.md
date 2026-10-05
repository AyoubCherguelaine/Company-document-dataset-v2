# CompanyDocuments v2: synthetic business document generator

Generates realistic business PDFs (invoices, purchase orders, payslips, statements and more) from four open
sample databases. Each document comes with exact gold labels and word boxes. The output is the training data for
**CompanyDocuments v2**, the successor of the
[CompanyDocuments dataset](https://huggingface.co/datasets/AyoubChLin/CompanyDocuments) (2,677 PDFs, 4 types).

```
SQLite databases ──► normalized records ──► document record ──► HTML (Jinja2) ──► PDF (WeasyPrint)
                                                                                     │
         dataset (CSV) ◄──── export ◄── scans (noise stage) ◄── self-check + gold JSON + word boxes
```

- **13 document types** from Northwind, AdventureWorks, Chinook and Sakila (353k source records available).
- **60 synthetic issuer companies** in 4 sectors, each with a fixed letterhead (4 layouts × 5 themes), its own
  numbering, and texts written by a free LLM.
- **English and French**, with locale-specific number, date and money formats, taxes and payroll deductions.
- **Exact labels:** money is computed with `Decimal`. Every document is re-read after rendering and dropped if a
  gold value is missing from the PDF text or the arithmetic doesn't hold.
- **Reproducible:** the same config, companies and seed give byte-identical PDFs.

Design notes: [docs/CompanyDocuments_v2_Generator_Design.md](docs/CompanyDocuments_v2_Generator_Design.md).
Developer guide (architecture, adding document types, sources and layouts): [docgen/README.md](docgen/README.md).

## Status

| Part | State |
|---|---|
| Source adapters, 13 document types, templates, 4 layouts, 5 themes, en/fr | done, all variants render and pass the self-check |
| Companies (60, 15 per sector), per-company templates | done |
| LLM texts (OpenRouter, free models) | 44 of 60 companies, 20 of 77 product descriptions; the pipeline's `texts` stage fills the rest |
| Planner: balanced counts, no repeats, `count: all` | done |
| Noise stage (`augment`) and parquet export (`export`) | done, tested end to end (boxes verified on rotated/scaled scans; export streams shards) |
| One-command pipeline (`scripts/run_pipeline.py`) | done; download step tested against all four upstream sources |
| Full dataset run | **pending** |
| Arabic (RTL) | deferred: PDF text extraction reorders mixed Arabic/Latin text, so the self-check isn't reliable yet |

## One command

```bash
python3 scripts/run_pipeline.py                   # download → index → companies → texts → generate → augment → export
```

[scripts/run_pipeline.py](scripts/run_pipeline.py) needs only the standard library. It creates or repairs `./venv`
and then runs every stage. Each stage is safe to rerun, and generation resumes where an interrupted run stopped.

| Stage | Does |
|---|---|
| `venv` | create or repair `./venv` and install `requirements.txt` (also after an OS Python upgrade) |
| `download` | fetch and build the 4 SQLite databases and their licenses ([scripts/download_sources.py](scripts/download_sources.py)); skips those present |
| `index` | add lookup indexes |
| `companies` | 15 issuers per sector if missing, then spread every layout × theme over each sector |
| `texts` | LLM company texts and product descriptions, if `OPENROUTER_API_KEY` is set (optional; only fills gaps) |
| `generate` | render all document types, self-check, gold JSON, word boxes |
| `augment` | degraded scan/photo page images for 30% of the documents |
| `export` | CSV dataset (one row per PDF, PDFs + JSON alongside) + card in `dataset/v2`, split by company; `--format parquet` for the v1 parquet schema |
| `test` | unit tests (with `--with-tests`) |

```bash
python3 scripts/run_pipeline.py --per-type 20         # quick end-to-end check (~260 documents)
python3 scripts/run_pipeline.py                       # balanced dataset: 2,000 per type (~26k documents, ~1 h)
python3 scripts/run_pipeline.py --all-records         # every source record once (~354k documents, ~10 h)
python3 scripts/run_pipeline.py --everything          # all records + LLM texts must be complete + tests
python3 scripts/run_pipeline.py --from generate       # resume from a stage
python3 scripts/run_pipeline.py --stages export --out output/v2 --dataset dataset/v2
```

**Disk space.** Before generating, the pipeline estimates the space it needs and refuses to start if it won't fit
(`--ignore-disk` overrides this):

| Run | Documents | Generate | Scans (30%) | Export | Total |
|---|---|---|---|---|---|
| default (`--per-type 2000`) | 26,000 | 0.8 GB | 3.7 GB | 0.6 GB | ~7 GB |
| `--everything` | 353,590 | 11.2 GB | 49.8 GB | 7.9 GB | ~71 GB |

For `--everything` on a small disk, put `--out`/`--dataset` on a bigger drive, or shrink it:

| `--everything` plus | Total |
|---|---|
| `--augment-fraction 0.05` | ~29 GB |
| `--augment-fraction 0` | ~21 GB |
| `--augment-fraction 0 --no-pdf` (PDFs only in `--out`, not in the parquet) | ~15 GB |

Scans can be added later on their own: `python3 scripts/run_pipeline.py --stages augment export --augment-fraction 0.1`.

`--everything` reruns the LLM texts until every company and product has text (up to `--text-passes`, waiting
between passes). If the free daily quota runs out it stops; resume the next day with `--from texts`, or accept
the built-in fallback texts with `--allow-missing-texts`.

Options: `--out`, `--dataset`, `--workers` (default: all cores), `--seed`, `--augment-fraction`, `--no-pdf`,
`--config <file>` (your own run config), `--skip <stages>`, `--force-download`, `--rebuild-venv`.
Every run is logged to `logs/pipeline-<time>.log`.

## Setup

Requires Python 3.12+ and the Pango/HarfBuzz libraries WeasyPrint uses (installed by default on Ubuntu).

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

> After an OS upgrade that changes the system Python (e.g. 3.12 → 3.14), the venv breaks
> (`No module named 'yaml'`). Rebuild it with `python3 -m venv --clear venv` and reinstall.

### Data

| Path | Source |
|---|---|
| `data/sample_dbs_part1_northwind_chinook_sakila/sqlite/` | Northwind (extended, 16k orders), Chinook, Sakila |
| `data/sample_dbs_part2_adventureworks/sqlite/adventureworks.db` | AdventureWorks OLTP (68 tables) |
| `data/northwind/` | Microsoft's T-SQL Northwind (`scripts/clone_sources.py`), optional |
| `data/company-documents/` | the v1 dataset (reference only, not used as input) |

```bash
python3 scripts/download_sources.py        # the four databases (done by the pipeline's download stage)
python scripts/prepare_sources.py          # once: lookup indexes (AdventureWorks ships with none)
python scripts/build_northwind_sqlite.py   # optional: classic Northwind (830 orders) -> data/northwind.db
```

### LLM (optional)

```bash
cp .env.example .env                       # set OPENROUTER_API_KEY (free key, no credits needed)
python scripts/openrouter_models.py        # list the current free models and pick one
python -m docgen llm ping
```

Without a key, companies use built-in fallback texts. Free accounts get roughly 50 requests a day, so the LLM only
fills a text bank (4 companies per request, 20 products per request). It is never called per document, and every
response is cached in `data/llm_cache.db`.

## Usage

```bash
# 1. issuers: 15 companies per sector, then spread every layout x theme over each sector
python -m docgen companies init --count 15 --llm
python -m docgen companies rebalance
python -m docgen companies list

# 2. texts: company copy and Northwind product descriptions (cached, safe to rerun)
python -m docgen texts generate

# 3. design: render one document and inspect it
python -m docgen list
python -m docgen preview invoice --open
python -m docgen preview payslip --company iris-sports-corp --html-only   # HTML for browser devtools

# 4. generate, add degraded scans, export
python -m docgen generate configs/example.yaml
python -m docgen augment output/all-documents-demo --fraction 0.3
python -m docgen export output/all-documents-demo dataset/

python -m unittest discover -s tests
```

## Document catalog

| Type | Sources | Records |
|---|---|---|
| invoice | Northwind, AdventureWorks, Chinook | 48,159 |
| quote · credit note · packing slip · shipping order | Northwind, AdventureWorks | 47,747 each |
| purchase order | Northwind, AdventureWorks | 50,313 |
| work order | AdventureWorks | 42,625 |
| receipt (A5) | Chinook, Sakila | 16,456 |
| goods received note | AdventureWorks | 3,689 |
| account statement | Northwind, AdventureWorks | 728 |
| payslip · employment certificate | AdventureWorks | 290 each |
| inventory report | Northwind, AdventureWorks | 52 |

Each source has a sector, and only companies of that sector issue its documents: Northwind → food,
AdventureWorks → bikes, Chinook → music, Sakila → video.

## Run config

```yaml
seed: 42
out_dir: output/v2
workers: 8
documents:
  - {type: invoice, count: 2000}                    # split evenly over its sources, no repeated records
  - {type: payslip, count: 2000}                    # only 290 employees: reused with other companies/dates
  - {type: receipt, count: all}                     # every record once
  - {type: "*", count: 500, locales: [fr]}          # every type, French companies only
  - {type: invoice, count: 100, sources: [chinook], companies: [quarry-music-llc]}
fx: {USD: "1", EUR: "0.92", GBP: "0.79"}
date_shift: {sakila: 19}                            # Sakila's 2005-2006 activity moved to 2024-2025
self_check: true
boxes: true
```

A full run with every record once is about 354k documents, 16 GB and ~10 h on 8 workers. A balanced run with 2,000
per type is about 26k documents, ~1.2 GB and ~1 h.

## Output

```
output/<run>/
  pdf/<type>/<doc_id>.pdf          clean born-digital document
  gold/<type>/<doc_id>.json        canonical fields, rendered strings, field boxes, options, company, layout
  words/<type>/<doc_id>.json       every PDF word with page + bbox
  scans/<type>/<doc_id>_p<n>_<profile>.jpg (+ .json)   degraded pages with transformed boxes (augment)
  manifest.jsonl                   one row per planned document: ok | failed | error
  config.yaml                      the exact config used

dataset/                           (export)
  data/{train,validation,test}-*.parquet     v1 columns + v2 metadata, PDF bytes, boxes
  scans/{train,validation,test}-*.parquet    degraded page images
  README.md, stats.json, licenses/
```

Splits are by issuing company, per sector: validation and test documents come from companies never seen in
training.

## Project layout

```
docgen/
  __main__.py          CLI
  core/                document base class, records, money, locale, theme, renderer, self-check,
                       pipeline, companies, LLM client, text bank, augment, export
  sources/             one adapter per database (northwind, adventureworks, chinook, sakila)
  documents/           document types + templates/<type>/document.html.j2
  templates/           base.html.j2, components.html.j2 (macros), layouts/, css/
  themes/  locales/    YAML
companies/<slug>/      company.yaml (+ optional templates/ overriding the shared ones)
configs/               run configs
scripts/               data preparation, OpenRouter model picker
tests/                 unittest suite (every variant rendered and self-checked)
```

## License

Code and generated documents: Apache-2.0. Source data: Northwind, AdventureWorks and Chinook (MIT), Sakila (BSD-2).
The license files are in `data/sample_dbs_*/licenses/` and are copied into every export.
