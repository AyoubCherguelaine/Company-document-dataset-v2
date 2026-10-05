"""Export a run as a Hugging Face style dataset: one row per PDF, split by company.

Two formats share the same rows:

csv (default): tabular, one CSV per split. Each row is a PDF with its gold JSON, the issuing company
and the key document fields. The PDFs and one JSON file per PDF sit next to the CSVs, so
`file_name` / `json_file` resolve relative to the dataset root.

    dataset/
      data/train.csv, validation.csv, test.csv  one row per PDF
      pdf/<type>/<doc_id>.pdf                    the documents
      json/<type>/<doc_id>.json                  extracted_data of each PDF
      scans/train.csv ... + scans/<type>/*.jpg   degraded page images (if the run was augmented)
      README.md, stats.json, licenses/

parquet: the v1 CompanyDocuments schema (file_content, file_name, extracted_data, document_type,
chat_format) plus the same metadata, the PDF bytes, word boxes and gold-field boxes, in shards.

Splits are by company, per sector: test and validation documents come from issuers (and so from
letterheads, numbering schemes and wording) never seen in training.
"""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf
import yaml

from .. import REPO_ROOT

SYSTEM_PROMPT = ("You extract structured data from business documents. Return the document's fields as JSON "
                 "with the same keys as the example schema: doc_type, number, issue_date, parties, dates, refs, "
                 "items, totals, summary, body, notes.")
INTERNAL_EXTRA = {"doc_id", "source_lines", "status_key", "label_key", "product_id"}
FORMATS = ("csv", "parquet")
ROWS_PER_SHARD = 5000
SCAN_ROWS_PER_SHARD = 500           # images are large
DATASET_MARKER = "stats.json"       # an export directory has one; anything else is never deleted


def company_splits(companies: dict[str, str], seed: int = 0, val: float = 0.1, test: float = 0.1) -> dict[str, str]:
    """companies: slug -> sector. Per sector, a stable hash order puts ~test/val of issuers aside."""
    by_sector: dict[str, list[str]] = collections.defaultdict(list)
    for slug, sector in companies.items():
        by_sector[sector].append(slug)
    split = {}
    for sector, slugs in by_sector.items():
        slugs.sort(key=lambda s: hashlib.sha1(f"{seed}:{s}".encode()).hexdigest())
        n_test = max(1, round(len(slugs) * test)) if len(slugs) >= 3 else 0
        n_val = max(1, round(len(slugs) * val)) if len(slugs) >= 3 else 0
        for i, slug in enumerate(slugs):
            split[slug] = "test" if i < n_test else "validation" if i < n_test + n_val else "train"
    return split


def clean_fields(value: Any) -> Any:
    """Gold record without internal keys or empty values: what a model should extract."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in INTERNAL_EXTRA:
                continue
            v = clean_fields(v)
            if v not in (None, "", [], {}):
                out[k] = v
        return out
    if isinstance(value, list):
        return [clean_fields(v) for v in value if v not in (None, "", [], {})]
    return value


def company_info(company) -> dict[str, str]:
    """The issuer columns of a row, from a core.companies.Company."""
    party, address = company.party, company.party.get("address", {})
    return {"company_name": party.get("name", ""), "company_sector": company.sector,
            "company_country": address.get("country", ""), "company_city": address.get("city", ""),
            "company_tax_id": party.get("tax_id", ""), "company_locale": company.locale,
            "company_currency": company.currency}


def key_fields(fields: dict) -> dict[str, Any]:
    """A few gold values as plain columns: number, date, total, first counterparty."""
    total = (fields.get("totals") or {}).get("total")
    if total is None:                     # documents without commercial totals: their headline figure
        total = next((r["value"] for r in fields.get("summary", []) if r.get("strong") and r.get("fmt") == "money"),
                     None)
    role, party = next(((r, p) for r, p in fields.get("parties", {}).items() if r != "issuer"), ("", {}))
    return {"number": fields.get("number", ""), "issue_date": fields.get("issue_date", ""),
            "currency": fields.get("currency", ""), "total": total if total is not None else "",
            "items_count": len(fields.get("items", [])), "counterparty_role": role,
            "counterparty_name": party.get("name", "")}


def pdf_text(path: Path) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text("text", sort=True).strip() for page in doc)


def place_file(src: Path, dst: Path) -> None:
    """Hard link when possible (same disk, no extra space), copy otherwise."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def reset_dir(out_dir: Path) -> None:
    """Replace a previous export, but never delete a directory that isn't one."""
    if out_dir.exists():
        if any(out_dir.iterdir()) and not (out_dir / DATASET_MARKER).exists():
            raise SystemExit(f"{out_dir} is not empty and is not a previous export (no {DATASET_MARKER}); "
                             f"pick another directory")
        shutil.rmtree(out_dir)


class ShardWriter:
    """Buffers rows per split and writes a parquet shard every `size` rows, so memory stays flat
    whatever the dataset size."""

    def __init__(self, out_dir: Path, schema: pa.Schema, size: int):
        self.out_dir, self.schema, self.size = out_dir, schema, size
        self.buffers: dict[str, list] = collections.defaultdict(list)
        self.files: dict[str, list[str]] = collections.defaultdict(list)
        self.counts: collections.Counter = collections.Counter()

    def add(self, split: str, row: dict) -> None:
        self.buffers[split].append(row)
        self.counts[split] += 1
        if len(self.buffers[split]) >= self.size:
            self.flush(split)

    def flush(self, split: str) -> None:
        rows = self.buffers.pop(split, [])
        if not rows:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{split}-{len(self.files[split]):05d}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=self.schema), self.out_dir / name, compression="zstd")
        self.files[split].append(name)

    def close(self) -> dict[str, list[str]]:
        for split in list(self.buffers):
            self.flush(split)
        return dict(self.files)


class CsvWriter:
    """One CSV per split, written row by row (memory stays flat). Same interface as ShardWriter."""

    def __init__(self, out_dir: Path, columns: list[str]):
        self.out_dir, self.columns = out_dir, columns
        self.handles: dict[str, Any] = {}
        self.writers: dict[str, csv.DictWriter] = {}
        self.counts: collections.Counter = collections.Counter()

    def add(self, split: str, row: dict) -> None:
        if split not in self.writers:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            fh = (self.out_dir / f"{split}.csv").open("w", encoding="utf-8", newline="")
            self.handles[split] = fh
            self.writers[split] = csv.DictWriter(fh, self.columns, extrasaction="ignore")
            self.writers[split].writeheader()
        self.writers[split].writerow(row)
        self.counts[split] += 1

    def close(self) -> dict[str, list[str]]:
        for fh in self.handles.values():
            fh.close()
        return {split: [f"{split}.csv"] for split in self.handles}


COMPANY_COLUMNS = ["company", "company_name", "company_sector", "company_country", "company_city",
                   "company_tax_id", "company_locale", "company_currency"]
KEY_COLUMNS = ["number", "issue_date", "currency", "total", "items_count", "counterparty_role", "counterparty_name"]
CSV_COLUMNS = (["doc_id", "split", "file_name", "json_file", "document_type", "source", "variant"] + COMPANY_COLUMNS
               + KEY_COLUMNS + ["layout", "theme", "locale", "pages", "extracted_data", "file_content"])
SCAN_CSV_COLUMNS = ["doc_id", "split", "document_type", "page", "profile", "image", "width", "height", "params",
                    "boxes"]

DOC_SCHEMA = pa.schema([
    ("file_content", pa.string()), ("file_name", pa.string()), ("extracted_data", pa.string()),
    ("document_type", pa.string()),
    ("chat_format", pa.list_(pa.struct([("content", pa.string()), ("role", pa.string())]))),
    ("doc_id", pa.string()), ("split", pa.string()), ("source", pa.string()), ("variant", pa.string()),
    *((c, pa.string()) for c in COMPANY_COLUMNS),
    *((c, pa.int32() if c == "items_count" else pa.string()) for c in KEY_COLUMNS),
    ("layout", pa.string()), ("theme", pa.string()),
    ("locale", pa.string()), ("pages", pa.int32()), ("options", pa.string()), ("rendered", pa.string()),
    ("boxes", pa.string()), ("words", pa.string()), ("pdf", pa.binary()),
])
SCAN_SCHEMA = pa.schema([
    ("doc_id", pa.string()), ("split", pa.string()), ("document_type", pa.string()), ("page", pa.int32()),
    ("profile", pa.string()), ("image", pa.binary()), ("width", pa.int32()), ("height", pa.int32()),
    ("params", pa.string()), ("words", pa.string()), ("boxes", pa.string()),
])


def export_run(run_dir: Path, out_dir: Path, companies: dict[str, Any], seed: int = 0,
               embed_pdf: bool = True, log=print, fmt: str = "csv") -> dict:
    """companies: slug -> core.companies.Company. embed_pdf: parquet embeds the bytes, csv copies the files."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; choose from {FORMATS}")
    rows = [json.loads(l) for l in (run_dir / "manifest.jsonl").open(encoding="utf-8")]
    rows = [r for r in rows if r["status"] == "ok"]
    unknown = sorted({r["company"] for r in rows} - set(companies))
    if unknown:
        raise SystemExit(f"companies of this run not found (pass --companies-dir?): {', '.join(unknown[:5])}")
    splits = company_splits({c: companies[c].sector for c in {r["company"] for r in rows}}, seed)
    reset_dir(out_dir)
    if fmt == "csv":
        docs, scans = CsvWriter(out_dir / "data", CSV_COLUMNS), CsvWriter(out_dir / "scans", SCAN_CSV_COLUMNS)
    else:
        docs = ShardWriter(out_dir / "data", DOC_SCHEMA, ROWS_PER_SHARD)
        scans = ShardWriter(out_dir / "scans", SCAN_SCHEMA, SCAN_ROWS_PER_SHARD)
    counters = {k: collections.Counter() for k in ("by_type", "by_source", "by_locale", "by_layout")}
    split_companies: dict[str, set] = collections.defaultdict(set)
    pages_total = 0
    for n, r in enumerate(rows, 1):
        t, did = r["type"], r["doc_id"]
        gold = json.loads((run_dir / "gold" / t / f"{did}.json").read_text())
        pdf_path = run_dir / "pdf" / t / f"{did}.pdf"
        text = pdf_text(pdf_path)
        fields = clean_fields(gold["fields"])
        extracted = json.dumps(fields, ensure_ascii=False)
        split = splits[r["company"]]
        row = {
            "doc_id": did, "split": split, "file_name": f"pdf/{t}/{did}.pdf" if fmt == "csv" else f"{t}/{did}.pdf",
            "json_file": f"json/{t}/{did}.json", "document_type": t, "source": r["source"], "variant": r["variant"],
            "company": r["company"], **company_info(companies[r["company"]]), **key_fields(gold["fields"]),
            "layout": r["layout"], "theme": r["theme"], "locale": r["locale"], "pages": gold.get("pages") or 0,
            "extracted_data": extracted, "file_content": text,
        }
        if fmt == "csv":
            if embed_pdf:
                place_file(pdf_path, out_dir / row["file_name"])
            json_path = out_dir / row["json_file"]
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(fields, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            words_path = run_dir / "words" / t / f"{did}.json"
            row |= {
                "chat_format": [{"content": SYSTEM_PROMPT, "role": "system"}, {"content": text, "role": "user"},
                                {"content": extracted, "role": "assistant"}],
                "total": str(row["total"]),
                "options": json.dumps(gold["options"], ensure_ascii=False),
                "rendered": json.dumps(gold["rendered"], ensure_ascii=False),
                "boxes": json.dumps(gold["boxes"], ensure_ascii=False),
                "words": words_path.read_text() if words_path.exists() else "[]",
                "pdf": pdf_path.read_bytes() if embed_pdf else None,
            }
        docs.add(split, row)
        counters["by_type"][t] += 1
        counters["by_source"][r["source"]] += 1
        counters["by_locale"][r["locale"]] += 1
        counters["by_layout"][f'{r["layout"]}/{r["theme"]}'] += 1
        split_companies[split].add(r["company"])
        pages_total += gold.get("pages") or 0
        scan_meta = run_dir / "scans" / t / f"{did}.json"
        if scan_meta.exists():
            for page in json.loads(scan_meta.read_text())["pages"]:
                scan = {"doc_id": did, "split": split, "document_type": t, "page": page["page"],
                        "profile": page["profile"], "width": page["width"], "height": page["height"],
                        "params": json.dumps(page["params"]), "boxes": json.dumps(page["boxes"], ensure_ascii=False)}
                if fmt == "csv":
                    place_file(run_dir / page["image"], out_dir / page["image"])   # scans/<type>/<name>.jpg
                    scan["image"] = page["image"]
                else:
                    scan |= {"image": (run_dir / page["image"]).read_bytes(),
                             "words": json.dumps(page["words"], ensure_ascii=False)}
                scans.add(split, scan)
        if n % 5000 == 0:
            log(f"[export] {n:,}/{len(rows):,}")

    files = {"data": docs.close(), "scans": scans.close()}
    lic = out_dir / "licenses"
    lic.mkdir(parents=True, exist_ok=True)
    for f in REPO_ROOT.glob("data/sample_dbs_*/licenses/*"):
        if "employees" not in f.name:                  # not used by this dataset
            shutil.copy(f, lic / f.name)

    stats = {
        "format": fmt,
        "documents": dict(docs.counts), "scans": dict(scans.counts),
        **{k: dict(v) for k, v in counters.items()},
        "pages": pages_total,
        "companies": {s: sorted(v) for s, v in split_companies.items()},
    }
    (out_dir / DATASET_MARKER).write_text(json.dumps(stats, indent=1, ensure_ascii=False))
    (out_dir / "README.md").write_text(dataset_card(stats, files, run_dir, fmt), encoding="utf-8")
    log(f"[export] {stats['documents']} documents, {stats['scans']} scan pages ({fmt}) -> {out_dir}")
    return stats


def size_category(n: int) -> str:
    for limit, name in ((1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"), (1_000_000, "100K<n<1M")):
        if n < limit:
            return name
    return "n>1M"


COLUMNS_DOC = {
    "csv": """## Columns (`default`, CSV)

One row per PDF.

- `doc_id`, `split`, `document_type`, `source`, `variant`
- `file_name`: the PDF (`pdf/<type>/<doc_id>.pdf`); `json_file`: its gold JSON (`json/<type>/<doc_id>.json`)
- `company` (slug), `company_name`, `company_sector`, `company_country`, `company_city`, `company_tax_id`,
  `company_locale`, `company_currency`: the issuing company
- `number`, `issue_date`, `currency`, `total`, `items_count`, `counterparty_role`, `counterparty_name`:
  key gold values as plain columns
- `layout`, `theme`, `locale`, `pages`
- `extracted_data`: the gold fields (JSON), same content as `json_file`
- `file_content`: text extracted from the PDF (reading order)

## Columns (`scans`, CSV)

Degraded page images (profiles `scan` and `photo`: skew, blur, noise, paper tint, lighting, JPEG):
`image` is the path of the JPEG, `boxes` the gold-field boxes in image pixels. Join to `default` on `doc_id`.
""",
    "parquet": """## Columns (`default`)

- `file_content`: text extracted from the PDF (reading order)
- `file_name`, `document_type`, `extracted_data` (JSON gold fields), `chat_format` (system/user/assistant)
  — same as v1
- `doc_id`, `split`, `source`, `variant`, `layout`, `theme`, `locale`, `pages`
- `company` and `company_*`: the issuing company; `number`, `issue_date`, `currency`, `total`, `items_count`,
  `counterparty_role`, `counterparty_name`: key gold values
- `options`: content variation drawn for the document (JSON)
- `rendered`: each gold field as printed in the PDF (JSON); `boxes`: their boxes (page, bbox in PDF points)
- `words`: every PDF word with page and bbox (JSON); `pdf`: the PDF file

## Columns (`scans`)

Degraded page images (profiles `scan` and `photo`: skew, blur, noise, paper tint, lighting, JPEG) with
`words` and `boxes` transformed into image pixels. Join to `default` on `doc_id`.
""",
}


def dataset_card(stats: dict, files: dict, run_dir: Path, fmt: str = "csv") -> str:
    pattern = "{dir}/{split}.csv" if fmt == "csv" else "{dir}/{split}-*"
    configs = [{"config_name": "default", "default": True,
                "data_files": [{"split": s, "path": pattern.format(dir="data", split=s)} for s in sorted(files["data"])]}]
    if files["scans"]:
        configs.append({"config_name": "scans",
                        "data_files": [{"split": s, "path": pattern.format(dir="scans", split=s)}
                                       for s in sorted(files["scans"])]})
    total = sum(stats["documents"].values())
    header = {
        "language": sorted({l for l in stats["by_locale"]}), "license": "apache-2.0",
        "size_categories": [size_category(total)],
        "task_categories": ["text-classification", "token-classification", "image-to-text", "feature-extraction",
                            "text-generation"],
        "pretty_name": "Company Documents v2", "tags": ["finance", "document-ai", "synthetic", "invoices", "tabular"],
        "configs": configs,
    }
    table = lambda d: "\n".join(f"| {k} | {v:,} |" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))  # noqa: E731
    run_cfg = (run_dir / "config.yaml").read_text() if (run_dir / "config.yaml").exists() else ""
    return f"""---
{yaml.safe_dump(header, sort_keys=False, allow_unicode=True).strip()}
---

# Company Documents v2

Synthetic, born-digital business documents rendered from four open sample databases, with exact gold
labels. {total:,} documents ({stats['pages']:,} pages) of {len(stats['by_type'])} types,
issued by {sum(len(v) for v in stats['companies'].values())} synthetic companies with their own letterheads.

## Splits

Splits are **by issuing company** (per sector): validation and test documents come from companies
whose letterhead, numbering and wording never appear in training.

| split | documents |
|---|---|
{table(stats['documents'])}

## Document types

| type | documents |
|---|---|
{table(stats['by_type'])}

## Sources

| source | documents |
|---|---|
{table(stats['by_source'])}

Northwind (food wholesale), AdventureWorks (bicycle manufacturer: sales, purchasing, production, HR),
Chinook (online music store), Sakila (video rental, dates shifted +19 years). Issuers are synthetic;
counterparties, products, quantities and prices come from the databases.

{COLUMNS_DOC[fmt]}
## Guarantees

Every document passed a self-check: the checked gold values are present in the PDF text and the arithmetic
holds (line totals, tax, totals, running balances, aging, payroll, received vs accepted). Money is computed
with Decimal and half-up rounding; currencies are converted once from the source (USD).

## Licenses

Code and generated documents: Apache-2.0. Source data: Northwind (MIT), AdventureWorks (MIT),
Chinook (MIT), Sakila (BSD-2); see `licenses/`.

## Reproduce

```yaml
{run_cfg.strip()}
```
"""
