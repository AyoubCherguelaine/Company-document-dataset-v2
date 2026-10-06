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
| Noise stage (`augment`) and export (`export`: folder layout or Parquet shards) | done, tested end to end (boxes verified on rotated/scaled scans; export streams shards) |
| One-command pipeline (`scripts/run_pipeline.py`) | done; download step tested against all four upstream sources |
| Full dataset run (every record, `scripts/run_in_parts.py`) | **running** |
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
| `export` | dataset + card in `dataset/v2`, split by company, laid out for the Hugging Face viewer (one subset per document type); `--format parquet` for Parquet shards (large runs) |
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

**The full dataset on a small disk.** [scripts/run_in_parts.py](scripts/run_in_parts.py) generates every record
and publishes it one document type at a time: generate, augment, export to Parquet, push, delete the local files.
The disk only holds one type at a time (~9 GB at most, for `purchase_order`). After each type is uploaded, it
merges the completed parts' stats, updates the card and viewer, and removes the previous folder export from the
Hub while preserving the Parquet shards. Completed parts are immediately available in the default `all` subset;
the card lists the remaining types until the run finishes. Rerun the same command
after an interruption: pushed types are skipped and generation resumes.

```bash
python3 scripts/run_in_parts.py                       # every record of every type -> HF_REPO_ID
python3 scripts/run_in_parts.py --status              # types done / left
python3 scripts/run_in_parts.py --publish-only        # update the card/viewer from already-pushed parts
python3 scripts/run_in_parts.py --types payslip --no-push   # trial: export one type, publish nothing
```

The full dataset is Parquet because the Hub allows at most 10k files per folder and recommends under 100k per
repo, which one-file-per-PDF can't meet at 354k documents. Splits are computed over all companies with seed 0,
so every part splits the same way, and the same as the published 2k dataset.

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

The source databases are **not in git** (about 140 MB, and `adventureworks.db` is over GitHub's 100 MB limit).
One script downloads and builds them, so after a fresh clone:

```bash
python3 scripts/run_pipeline.py --stages venv download index   # venv + the four databases + indexes
```

or just the databases: `python3 scripts/download_sources.py` (skips those present; `--force` to re-download,
`--only sakila chinook` for some). It needs only the standard library, plus `git` for AdventureWorks. License files
are fetched too. `data/textbank/` (LLM texts) **is** versioned: it costs API quota to rebuild.

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

dataset/                           (export, default csv format: what the Hugging Face viewer reads)
  pdf/<type>/<split>/<doc_id>.pdf + metadata.jsonl     one row per PDF: paths, company, source, key fields,
                                                     gold JSON, PDF text
  json/<type>/<doc_id>.json                          gold fields of each PDF
  scans/<type>/<split>/*.jpg + metadata.jsonl          degraded page images + gold boxes in pixels
  README.md (card: subsets `all` + one per type), stats.json, licenses/

dataset/                           (export --format parquet: large runs, the Hub's limits)
  data/<type>/<split>-NNNNN.parquet          one row per PDF: the PDF (Pdf feature), the columns above,
                                             options, printed values, gold-field and word boxes
  scans/<type>/<split>-NNNNN.parquet         degraded page images (Image feature) + boxes in pixels
  README.md (card: `all`, one subset per type, `scans`), stats.json, licenses/
```

On the Hub each document type is a subset (`load_dataset(repo, "invoice")`) with train/validation/test, and the
viewer shows the PDFs. In the folder layout scans are not a subset (the Hub picks one loader per repo, there the
PDF one) and load with `load_dataset(repo, data_dir="scans")`; Parquet carries its own types, so there `scans`
is a subset like the others. The folder layout suits runs up to ~10k documents per type and split.

Splits are by issuing company, per sector: validation and test documents come from companies never seen in
training.

## Publish to Hugging Face

[scripts/push_to_hf.py](scripts/push_to_hf.py) uploads an exported folder to a dataset repo. It is separate from the
pipeline, so run it after `export`, on any machine that has the folder.

```bash
# .env: HF_TOKEN=<write token from https://huggingface.co/settings/tokens>, HF_REPO_ID=<user>/<name>
# (or run `hf auth login` once and pass --repo)
./venv/bin/python scripts/push_to_hf.py --dry-run                 # what would be sent
./venv/bin/python scripts/push_to_hf.py                           # upload dataset/v2 (creates a private repo)
./venv/bin/python scripts/push_to_hf.py --repo <user>/<name> --public
./venv/bin/python scripts/push_to_hf.py --exclude "scans/*"       # skip the degraded scans
```

It commits 1,000 files at a time (`--batch`), so a 60k-file export stays within the Hub's per-commit limits.
Interrupted? Rerun it: files already on the Hub with the same size are skipped. The card, `stats.json` and the
`metadata.jsonl` files are compared by content and sent last, so the dataset viewer never points at missing files. The Hub allows 128 commits per repo per hour; if the script hits that, it stops and a later rerun resumes. `--folder` picks another
export, `--revision` a branch, and `--delete-stale` removes remote files that are gone locally.

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
scripts/               pipeline, data download/preparation, Hugging Face upload, OpenRouter model picker
tests/                 unittest suite (every variant rendered and self-checked)
```

## License

Code and generated documents: Apache-2.0. Source data: Northwind, AdventureWorks and Chinook (MIT), Sakila (BSD-2).
The license files are in `data/sample_dbs_*/licenses/` and are copied into every export.
