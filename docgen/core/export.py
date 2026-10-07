"""Export a run as a Hugging Face style dataset: one row per PDF, split by company.

Two formats share the same rows:

csv (default; the name is historical): the layout the Hugging Face viewer renders directly (pdffolder /
imagefolder), for runs up to ~10k documents per type and split. Each type and split is a folder of PDFs
with a metadata.jsonl: one row per PDF with its gold JSON, the
issuing company and the key document fields. The card declares one subset per document type, plus
`all` and `scans`. `pdf_path` / `json_path` / `image_path` resolve relative to the dataset root.

    dataset/
      pdf/<type>/<split>/<doc_id>.pdf + metadata.jsonl     the documents, one row per PDF
      json/<type>/<doc_id>.json                          extracted_data of each PDF
      scans/<type>/<split>/<name>.jpg + metadata.jsonl     degraded page images (if the run was augmented)
      README.md, stats.json, licenses/

parquet: for large runs (the Hub allows 10k files per folder and recommends <100k per repo). Shards of
the same rows with the PDF embedded, plus word and gold-field boxes; the scans are a subset too.

    dataset/
      data/<type>/<split>-NNNNN.parquet      one row per PDF (`pdf` column: the file itself)
      scans/<type>/<split>-NNNNN.parquet     one row per degraded page image
      README.md, stats.json, licenses/

Columns carry Hugging Face feature metadata (Pdf, Image), so the viewer renders both.

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
from .env import env

INTERNAL_EXTRA = {"doc_id", "source_lines", "status_key", "label_key", "product_id"}
FORMATS = ("csv", "parquet")
ROWS_PER_SHARD = 5000               # ~150 MB of PDFs + text per shard
SCAN_ROWS_PER_SHARD = 500           # images are large
ROW_GROUP = 100                     # small row groups: the viewer reads one at a time
LANGUAGES = {"en": "English", "fr": "French", "ar": "Arabic"}
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
    """Buffers rows per <type>/<split> and writes a parquet shard every `size` rows, so memory stays
    flat whatever the dataset size."""

    def __init__(self, out_dir: Path, schema: pa.Schema, size: int):
        self.out_dir, self.schema, self.size = out_dir, schema, size
        self.buffers: dict[str, list] = collections.defaultdict(list)
        self.files: dict[str, list[str]] = collections.defaultdict(list)
        self.counts: collections.Counter = collections.Counter()

    def add(self, split: str, row: dict) -> None:
        folder = f'{row["document_type"]}/{split}'
        self.buffers[folder].append(row)
        self.counts[split] += 1
        if len(self.buffers[folder]) >= self.size:
            self.flush(folder)

    def flush(self, folder: str) -> None:
        rows = self.buffers.pop(folder, [])
        if not rows:
            return
        t, split = folder.split("/")
        (self.out_dir / t).mkdir(parents=True, exist_ok=True)
        name = f"{t}/{split}-{len(self.files[folder]):05d}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=self.schema), self.out_dir / name,
                       compression="zstd", row_group_size=ROW_GROUP)
        self.files[folder].append(name)

    def close(self) -> dict[str, list[str]]:
        for folder in list(self.buffers):
            self.flush(folder)
        files: dict[str, list[str]] = collections.defaultdict(list)
        for folder, names in sorted(self.files.items()):
            files[folder.split("/")[1]] += names
        return dict(files)


class MetadataWriter:
    """One metadata.jsonl per <type>/<split> folder, written row by row (memory stays flat). Same
    interface as ShardWriter; `file_name` in each row is relative to its folder, as the Hugging Face
    folder builders (pdffolder, imagefolder) expect. JSON lines, not CSV: CSV has no types, so a column
    that is empty in one folder would be read as a number there and the folders would not merge."""

    def __init__(self, out_dir: Path, columns: list[str]):
        self.out_dir, self.columns = out_dir, columns
        self.handles: dict[str, Any] = {}
        self.counts: collections.Counter = collections.Counter()

    def add(self, split: str, row: dict) -> None:
        folder = f'{row["document_type"]}/{split}'
        if folder not in self.handles:
            (self.out_dir / folder).mkdir(parents=True, exist_ok=True)
            self.handles[folder] = (self.out_dir / folder / "metadata.jsonl").open("w", encoding="utf-8")
        line = {c: row.get(c, "") for c in self.columns}
        self.handles[folder].write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self.counts[split] += 1

    def close(self) -> dict[str, list[str]]:
        for fh in self.handles.values():
            fh.close()
        files: dict[str, list[str]] = collections.defaultdict(list)
        for folder in sorted(self.handles):
            files[folder.split("/")[1]].append(f"{folder}/metadata.jsonl")
        return dict(files)


COMPANY_COLUMNS = ["company", "company_name", "company_sector", "company_country", "company_city",
                   "company_tax_id", "company_locale", "company_currency"]
KEY_COLUMNS = ["number", "issue_date", "currency", "total", "items_count", "counterparty_role", "counterparty_name"]
FOLDER_COLUMNS = (["file_name", "doc_id", "split", "document_type", "pdf_path", "json_path", "source", "variant"]
               + COMPANY_COLUMNS + KEY_COLUMNS + ["layout", "theme", "locale", "pages", "extracted_data",
                                                  "file_content"])
SCAN_FOLDER_COLUMNS = ["file_name", "doc_id", "split", "document_type", "page", "profile", "image_path", "pdf_path",
                    "json_path", "width", "height", "params", "boxes"]

FILE = pa.struct([("bytes", pa.binary()), ("path", pa.string())])     # datasets' Pdf / Image storage


def hf_schema(fields: list[tuple[str, pa.DataType]], files: dict[str, str]) -> pa.Schema:
    """Arrow schema with the Hugging Face features metadata, so `datasets` and the viewer decode the
    `files` columns as Pdf / Image instead of raw bytes."""
    dtypes = {pa.string(): "string", pa.int32(): "int32"}
    features = {name: {"_type": files[name]} if name in files else {"dtype": dtypes[t], "_type": "Value"}
                for name, t in fields}
    meta = {b"huggingface": json.dumps({"info": {"features": features}}).encode()}
    return pa.schema(fields, metadata=meta)


DOC_SCHEMA = hf_schema([
    ("pdf", FILE), ("file_name", pa.string()), ("doc_id", pa.string()), ("split", pa.string()),
    ("document_type", pa.string()), ("source", pa.string()), ("variant", pa.string()),
    *((c, pa.string()) for c in COMPANY_COLUMNS),
    *((c, pa.int32() if c == "items_count" else pa.string()) for c in KEY_COLUMNS),
    ("layout", pa.string()), ("theme", pa.string()), ("locale", pa.string()), ("pages", pa.int32()),
    ("extracted_data", pa.string()), ("file_content", pa.string()), ("options", pa.string()),
    ("rendered", pa.string()), ("boxes", pa.string()), ("words", pa.string()),
], {"pdf": "Pdf"})
SCAN_SCHEMA = hf_schema([
    ("image", FILE), ("doc_id", pa.string()), ("split", pa.string()), ("document_type", pa.string()),
    ("page", pa.int32()), ("profile", pa.string()), ("width", pa.int32()), ("height", pa.int32()),
    ("params", pa.string()), ("words", pa.string()), ("boxes", pa.string()),
], {"image": "Image"})


def export_run(run_dir: Path, out_dir: Path, companies: dict[str, Any], seed: int = 0,
               embed_pdf: bool = True, log=print, fmt: str = "csv", repo_id: str | None = None) -> dict:
    """companies: slug -> core.companies.Company. embed_pdf: parquet embeds the bytes, csv copies the files.
    repo_id: the Hugging Face repo named in the card's examples (default: HF_REPO_ID)."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; choose from {FORMATS}")
    rows = [json.loads(l) for l in (run_dir / "manifest.jsonl").open(encoding="utf-8")]
    rows = [r for r in rows if r["status"] == "ok"]
    unknown = sorted({r["company"] for r in rows} - set(companies))
    if unknown:
        raise SystemExit(f"companies of this run not found (pass --companies-dir?): {', '.join(unknown[:5])}")
    # over every company, not just this run's: a run done in parts (by type) splits them identically
    splits = company_splits({slug: c.sector for slug, c in companies.items()}, seed)
    reset_dir(out_dir)
    if fmt == "csv":
        docs, scans = MetadataWriter(out_dir / "pdf", FOLDER_COLUMNS), MetadataWriter(out_dir / "scans", SCAN_FOLDER_COLUMNS)
    else:
        docs = ShardWriter(out_dir / "data", DOC_SCHEMA, ROWS_PER_SHARD)
        scans = ShardWriter(out_dir / "scans", SCAN_SCHEMA, SCAN_ROWS_PER_SHARD)
    counters = {k: collections.Counter() for k in ("by_type", "by_source", "by_locale", "by_layout")}
    by_type_split: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_type_scan_split: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_type_source: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
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
            "doc_id": did, "split": split, "document_type": t, "source": r["source"], "variant": r["variant"],
            "company": r["company"], **company_info(companies[r["company"]]), **key_fields(gold["fields"]),
            "layout": r["layout"], "theme": r["theme"], "locale": r["locale"], "pages": gold.get("pages") or 0,
            "extracted_data": extracted, "file_content": text,
        }
        if fmt == "csv":
            row |= {"file_name": f"{did}.pdf", "pdf_path": f"pdf/{t}/{split}/{did}.pdf",
                    "json_path": f"json/{t}/{did}.json"}
            if embed_pdf:
                place_file(pdf_path, out_dir / row["pdf_path"])
            json_path = out_dir / row["json_path"]
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(fields, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            words_path = run_dir / "words" / t / f"{did}.json"
            row |= {
                "file_name": f"{t}/{did}.pdf",
                "total": str(row["total"]),
                "options": json.dumps(gold["options"], ensure_ascii=False),
                "rendered": json.dumps(gold["rendered"], ensure_ascii=False),
                "boxes": json.dumps(gold["boxes"], ensure_ascii=False),
                "words": words_path.read_text() if words_path.exists() else "[]",
                "pdf": {"bytes": pdf_path.read_bytes(), "path": f"{did}.pdf"} if embed_pdf else None,
            }
        docs.add(split, row)
        counters["by_type"][t] += 1
        counters["by_source"][r["source"]] += 1
        counters["by_locale"][r["locale"]] += 1
        counters["by_layout"][f'{r["layout"]}/{r["theme"]}'] += 1
        by_type_split[t][split] += 1
        by_type_source[t][r["source"]] += 1
        split_companies[split].add(r["company"])
        pages_total += gold.get("pages") or 0
        scan_meta = run_dir / "scans" / t / f"{did}.json"
        if scan_meta.exists():
            for page in json.loads(scan_meta.read_text())["pages"]:
                scan = {"doc_id": did, "split": split, "document_type": t, "page": page["page"],
                        "profile": page["profile"], "width": page["width"], "height": page["height"],
                        "params": json.dumps(page["params"]), "boxes": json.dumps(page["boxes"], ensure_ascii=False)}
                if fmt == "csv":
                    name = Path(page["image"]).name
                    scan |= {"file_name": name, "image_path": f"scans/{t}/{split}/{name}",
                             "pdf_path": row["pdf_path"], "json_path": row["json_path"]}
                    place_file(run_dir / page["image"], out_dir / scan["image_path"])
                else:
                    scan |= {"image": {"bytes": (run_dir / page["image"]).read_bytes(),
                                       "path": Path(page["image"]).name},
                             "words": json.dumps(page["words"], ensure_ascii=False)}
                scans.add(split, scan)
                by_type_scan_split[t][split] += 1
        if n % 5000 == 0:
            log(f"[export] {n:,}/{len(rows):,}")

    docs.close(), scans.close()

    stats = {
        "format": fmt,
        "documents": dict(docs.counts), "scans": dict(scans.counts),
        **{k: dict(v) for k, v in counters.items()},
        "by_type_split": {t: dict(c) for t, c in sorted(by_type_split.items())},
        "by_type_scan_split": {t: dict(c) for t, c in sorted(by_type_scan_split.items())},
        "by_type_source": {t: dict(c) for t, c in sorted(by_type_source.items())},
        "pages": pages_total,
        "companies": {s: sorted(v) for s, v in split_companies.items()},
    }
    run_cfg = (run_dir / "config.yaml").read_text() if (run_dir / "config.yaml").exists() else ""
    write_card(out_dir, stats, run_cfg, repo_id)
    log(f"[export] {stats['documents']} documents, {stats['scans']} scan pages ({fmt}) -> {out_dir}")
    return stats


def merge_stats(parts: list[dict]) -> dict:
    """stats.json of an export done in parts (one or more document types each) as one dataset."""
    out: dict[str, Any] = {"format": parts[0]["format"], "pages": sum(p["pages"] for p in parts)}
    for key in ("documents", "scans", "by_type", "by_source", "by_locale", "by_layout"):
        total: collections.Counter = collections.Counter()
        for p in parts:
            total.update(p.get(key, {}))
        out[key] = dict(total)
    for key in ("by_type_split", "by_type_source"):
        out[key] = {t: c for p in parts for t, c in p.get(key, {}).items()}
    out["by_type_scan_split"] = {}
    for p in parts:
        scan_counts = p.get("by_type_scan_split")
        # Older part checkpoints contain a single type and only aggregate scan counts.
        if scan_counts is None and len(p["by_type"]) == 1:
            scan_counts = {next(iter(p["by_type"])): p["scans"]}
        out["by_type_scan_split"].update(scan_counts or {})
    companies: dict[str, set] = collections.defaultdict(set)
    for p in parts:
        for split, slugs in p["companies"].items():
            companies[split].update(slugs)
    out["companies"] = {s: sorted(v) for s, v in companies.items()}
    return out


def write_card(out_dir: Path, stats: dict, run_cfg: str, repo_id: str | None = None) -> None:
    """stats.json, README.md (the card) and licenses/ of an export directory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / DATASET_MARKER).write_text(json.dumps(stats, indent=1, ensure_ascii=False))
    repo_id = repo_id or env("HF_REPO_ID", "<user>/<dataset>")
    (out_dir / "README.md").write_text(dataset_card(stats, run_cfg, repo_id), encoding="utf-8")
    lic = out_dir / "licenses"
    lic.mkdir(parents=True, exist_ok=True)
    for f in REPO_ROOT.glob("data/sample_dbs_*/licenses/*"):
        if "employees" not in f.name:                  # not used by this dataset
            shutil.copy(f, lic / f.name)


def size_category(n: int) -> str:
    for limit, name in ((1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"), (1_000_000, "100K<n<1M")):
        if n < limit:
            return name
    return "n>1M"


TYPE_INFO = {
    "invoice": "sales invoice: line items, discounts, tax, totals, payment terms",
    "quote": "price quotation with validity and lead time",
    "credit_note": "credit against an invoice, with a reason (damaged, wrong item, price...)",
    "packing_slip": "items packed for a shipment: ordered vs shipped quantities",
    "shipping_order": "instruction to ship an order: items, weights, consignee, carrier",
    "purchase_order": "order the issuer places with a vendor",
    "goods_received_note": "delivery check against a purchase order: ordered, received, accepted, rejected",
    "account_statement": "a customer's invoices and payments over a period, running balance and aging",
    "receipt": "proof of a paid retail transaction (A5)",
    "payslip": "employee pay for a period: earnings, deductions, net pay",
    "employment_certificate": "letter certifying an employee's job title, department and hire date",
    "work_order": "production order: routing operations, planned vs actual cost",
    "inventory_report": "stock on hand per product, with value and reorder status",
}

COMMON_COLUMNS = """| `doc_id`, `split`, `document_type` | identifiers |
| `source`, `variant` | source database and document variant (`<type>.<source>`) |
| `company`, `company_name`, `company_sector`, `company_country`, `company_city`, `company_tax_id`, `company_locale`, `company_currency` | the issuing company |
| `number`, `issue_date`, `currency`, `total`, `items_count`, `counterparty_role`, `counterparty_name` | key gold values as plain columns |
| `layout`, `theme`, `locale`, `pages` | letterhead layout, color theme, language, page count |
| `extracted_data` | all gold fields as JSON |
| `file_content` | text extracted from the PDF, in reading order |"""

COLUMNS_DOC = {
    "csv": f"""## Columns

One row per PDF (`all` and every document-type subset):

| column | content |
|---|---|
| `pdf` | the PDF itself (the viewer shows its first page; `datasets` opens it with pdfplumber) |
| `pdf_path`, `json_path` | the PDF and its gold JSON in this repo (`pdf/<type>/<split>/<doc_id>.pdf`, `json/<type>/<doc_id>.json`) |
{COMMON_COLUMNS}

Scans (`data_dir="scans"`), one row per degraded page image (profiles `scan` and `photo`: skew, blur, noise,
paper tint, lighting, JPEG):

| column | content |
|---|---|
| `image` | the page image |
| `doc_id`, `split`, `document_type`, `page`, `profile` | identifiers; join to the documents on `doc_id` |
| `image_path`, `pdf_path`, `json_path` | the image, its source PDF and gold JSON in this repo |
| `width`, `height` | image size in pixels |
| `params` | degradation parameters (JSON) |
| `boxes` | gold-field boxes in image pixels (JSON: field path -> list of `[x0, y0, x1, y1]`) |
""",
    "parquet": f"""## Columns

One row per PDF (`all` and every document-type subset):

| column | content |
|---|---|
| `pdf` | the PDF itself (the viewer shows its first page; `datasets` opens it with pdfplumber; raw bytes with `.cast_column("pdf", Pdf(decode=False))`) |
| `file_name` | `<type>/<doc_id>.pdf` |
{COMMON_COLUMNS}
| `options` | content variation drawn for the document (JSON) |
| `rendered` | each gold field as printed in the PDF (JSON) |
| `boxes` | where each gold field is printed (JSON: field path -> list of page + bbox in PDF points) |
| `words` | every word of the PDF with its page and bbox (JSON) |

`scans` subset, one row per degraded page image (profiles `scan` and `photo`: skew, blur, noise, paper tint,
lighting, JPEG):

| column | content |
|---|---|
| `image` | the page image |
| `doc_id`, `split`, `document_type`, `page`, `profile` | identifiers; join to the documents on `doc_id` |
| `width`, `height` | image size in pixels |
| `params` | degradation parameters (JSON) |
| `words`, `boxes` | words and gold-field boxes moved into image pixels (JSON) |
""",
}

FILES_DOC = {
    "csv": """```
pdf/<type>/<split>/<doc_id>.pdf      the documents
pdf/<type>/<split>/metadata.jsonl      one row per PDF (the columns below)
json/<type>/<doc_id>.json            gold fields of each PDF
scans/<type>/<split>/*.jpg           degraded page images
scans/<type>/<split>/metadata.jsonl    one row per image
stats.json                           counts per split, type, source, locale, layout; companies per split
licenses/                            source database licenses
```""",
    "parquet": """```
data/<type>/<split>-NNNNN.parquet    one row per PDF, the PDF embedded (the columns below)
scans/<type>/<split>-NNNNN.parquet   one row per degraded page image
stats.json                           counts per split, type, source, locale, layout; companies per split
licenses/                            source database licenses
```""",
}


def dataset_card(stats: dict, run_cfg: str = "", repo_id: str = "<user>/<dataset>") -> str:
    fmt = stats["format"]
    splits = [s for s in ("train", "validation", "test") if stats["documents"].get(s)]
    has_scans = bool(sum(stats["scans"].values()))

    def subset(name: str, pattern: str, counts: dict) -> dict:
        return {"config_name": name, "data_files": [
            {"split": s, "path": pattern.format(s=s)} for s in splits if counts.get(s)]}

    docs = "pdf/{t}/{s}/*" if fmt == "csv" else "data/{t}/{s}-*.parquet"
    # Name completed types explicitly: an interrupted upload of the next part must not enter `all`.
    configs = [{"config_name": "all", "default": True, "data_files": [
        {"split": s, "path": [docs.replace("{t}", t).format(s=s)
                               for t in sorted(stats["by_type"]) if stats["by_type_split"][t].get(s)]}
        for s in splits]}]
    configs += [subset(t, docs.replace("{t}", t), stats["by_type_split"][t]) for t in sorted(stats["by_type"])]
    # csv: no `scans` subset, the Hub picks one loader per repo (pdffolder there); the images load with
    # data_dir="scans" (imagefolder) instead. parquet carries its own features, so scans are a subset.
    if has_scans and fmt == "parquet":
        scan_counts = stats.get("by_type_scan_split")
        configs.append({"config_name": "scans", "data_files": [
            {"split": s, "path": [f"scans/{t}/{s}-*.parquet" for t in sorted(stats["by_type"])
                                   if scan_counts is None or scan_counts.get(t, {}).get(s)]}
            for s in splits if stats["scans"].get(s)]})
    total = sum(stats["documents"].values())
    header = {
        "language": sorted(stats["by_locale"]), "license": "apache-2.0",
        "size_categories": [size_category(total)],
        "task_categories": ["document-question-answering", "image-to-text", "token-classification",
                            "text-classification", "text-generation"],
        "pretty_name": "Company Documents v2",
        "tags": ["finance", "document-ai", "synthetic", "invoices", "pdf", "ocr", "key-information-extraction"],
        "configs": configs,
    }
    table = lambda d: "\n".join(f"| {k} | {v:,} |" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))  # noqa: E731
    counts = lambda d: " | ".join(f"{d.get(s, 0):,}" for s in splits)  # noqa: E731
    run_cfg = run_cfg.replace(f"{REPO_ROOT}/", "").replace(str(REPO_ROOT), ".")   # no local paths in the card
    n_companies = sum(len(v) for v in stats["companies"].values())
    n_scans = sum(stats["scans"].values())
    split_rows = "\n".join(f"| {s} | {stats['documents'].get(s, 0):,} | {len(stats['companies'].get(s, [])):,} "
                           f"| {stats['scans'].get(s, 0):,} |" for s in splits)
    type_rows = "\n".join(
        f"| `{t}` | {TYPE_INFO.get(t, '')} | {n:,} | {counts(stats['by_type_split'][t])} | "
        + ", ".join(sorted(stats["by_type_source"][t])) + " |"
        for t, n in sorted(stats["by_type"].items()))
    scans_row = (f"\n| `scans` | degraded page images of a sample of the documents | {n_scans:,} pages "
                 f"| {counts(stats['scans'])} | |") if has_scans and fmt == "parquet" else ""
    if fmt == "csv":
        paths = 'row["pdf_path"], row["json_path"]     # the same files in this repo\n'
        scans_load = f'load_dataset("{repo_id}", data_dir="scans", split="test")'
    else:
        paths = ""
        scans_load = f'load_dataset("{repo_id}", "scans", split="test")'
    scans_doc = f"""
## Scans

{n_scans:,} degraded page images (scan and photo profiles) of a sample of the documents, with the gold-field
boxes moved into image pixels:

```python
scans = {scans_load}
scans[0]["image"], json.loads(scans[0]["boxes"])
```
""" if has_scans else ""
    example_type = "invoice" if "invoice" in stats["by_type"] else sorted(stats["by_type"])[0]
    cfg = yaml.safe_load(run_cfg) if run_cfg.strip() else None
    full_run = isinstance(cfg, dict) and cfg.get("run_name") == "full"
    reproduction = "python3 scripts/run_pipeline.py"
    if full_run:
        reproduction = f"""# Generate all source records and their scans locally.
python3 scripts/run_pipeline.py --all-records --stages venv download index companies texts generate augment --seed {cfg.get('seed', 42)} --workers {cfg.get('workers', 8)} --augment-fraction 0.3
# Export with the published dataset's company split seed.
./venv/bin/python -m docgen export output/v2 dataset/full-local --format parquet --seed 0"""
    return f"""---
{yaml.safe_dump(header, sort_keys=False, allow_unicode=True).strip()}
---

# Company Documents v2

Synthetic, born-digital business documents rendered from four open sample databases, with exact gold
labels: {total:,} PDFs ({stats['pages']:,} pages) of {len(stats['by_type'])} document types in
{' and '.join(LANGUAGES.get(l, l) for l in sorted(stats['by_locale']))}, issued by {n_companies} synthetic companies,
each with its own letterhead, numbering and wording. Successor of
[CompanyDocuments](https://huggingface.co/datasets/AyoubChLin/CompanyDocuments) (2,677 PDFs, 4 types).

## Dataset overview

| property | value |
|---|---|
| PDF documents | {total:,} |
| PDF pages | {stats['pages']:,} |
| Document types | {len(stats['by_type'])} |
| Synthetic issuing companies | {n_companies} |
| Languages | {' / '.join(LANGUAGES.get(l, l) for l in sorted(stats['by_locale']))} |
| Degraded scan/photo page images | {n_scans:,} |
| Export format | {fmt} |
| Split unit | issuing company, grouped by sector |

## Intended uses

Document classification, OCR evaluation, key information extraction, layout-aware field extraction,
and document question answering using the gold fields to construct task-specific examples.
The dataset provides document text, structured fields and coordinates; it does not include a separate
set of question-answer pairs.

## Subsets

Pick a document type, or `all`. Every subset has the same train / validation / test split.

| subset | document | documents | {' | '.join(splits)} | sources |
|---|---|---|{'---|' * len(splits)}---|
| `all` (default) | every type below | {total:,} | {counts(stats['documents'])} | all four |
{type_rows}{scans_row}

## Quick start

```python
import json
from datasets import load_dataset

documents = load_dataset("{repo_id}", "{example_type}", split="train")
row = documents[0]
row["pdf"]                            # pdfplumber.PDF
json.loads(row["extracted_data"])     # the gold fields
{paths}```

Use `"all"` to load every document type. For a large subset, pass `streaming=True` to
`load_dataset` and read examples with `next(iter(documents))` to avoid downloading the entire subset.
{scans_doc}
## Files

{FILES_DOC[fmt]}

## Splits

Splits are **by issuing company** (per sector): validation and test documents come from companies whose
issuing company is absent from training. Source databases, products, counterparties, layout families
and themes can occur across splits; this is an issuer-held-out split, not a source-held-out split.

| split | documents | companies | scan pages |
|---|---|---|---|
{split_rows}

{COLUMNS_DOC[fmt]}
## Sources

| source | documents |
|---|---|
{table(stats['by_source'])}

Northwind (food wholesale), AdventureWorks (bicycle manufacturer: sales, purchasing, production, HR),
Chinook (online music store), Sakila (video rental, dates shifted +19 years). Issuers are synthetic;
counterparties, products, quantities and prices come from the databases.

| language | documents |
|---|---|
{table({LANGUAGES.get(k, k): v for k, v in stats['by_locale'].items()})}

## Guarantees

Every document passed a self-check: the checked gold values are present in the PDF text and the arithmetic
holds (line totals, tax, totals, running balances, aging, payroll, received vs accepted). Money is computed
with Decimal and half-up rounding; currencies are converted once from the source (USD).

## Limitations

- These are synthetic documents derived from sample databases. Their layouts, wording and simulated
  degradation do not cover the full variety of real business documents or camera captures.
- Document types are imbalanced; use the per-type counts when selecting training data and reporting
  evaluation results. Some types contain only a small number of examples.
- Scan rows represent pages of sampled PDFs, not additional independent business transactions.
  Keep scans with their parent document's split when constructing an evaluation dataset.
- Gold fields and word boxes come from the generation and PDF extraction pipeline. Arithmetic and
  text checks do not establish that every annotation or reading-order decision is error-free.
- The same underlying source records can support multiple document types. Holding out issuing
  companies does not guarantee that all business entities or transaction content are unseen.

## Licenses

Code and generated documents: Apache-2.0. Source data: Northwind (MIT), AdventureWorks (MIT),
Chinook (MIT), Sakila (BSD-2); see `licenses/`.

## Citation

Created by **Cherguelaine Ayoub**. If you use this dataset, please cite:

```bibtex
@misc{{cherguelaine2026companydocumentsv2,
  author = {{Cherguelaine, Ayoub}},
  title = {{Company Documents v2: Synthetic Business Documents}},
  year = {{2026}},
  url = {{https://github.com/AyoubCherguelaine/Company-document-dataset-v2}}
}}
```

## Reproduce

Generator source code: [Company-document-dataset-v2](https://github.com/AyoubCherguelaine/Company-document-dataset-v2).
To generate the content locally from a Git checkout:

```bash
git clone https://github.com/AyoubCherguelaine/Company-document-dataset-v2.git
cd Company-document-dataset-v2
{reproduction}
```

See the [repository README](https://github.com/AyoubCherguelaine/Company-document-dataset-v2#readme) for setup, configuration and full-dataset generation options.

Generated with the CompanyDocuments v2 generator (`docgen`) and this run config:

```yaml
{run_cfg.strip()}
```
"""
