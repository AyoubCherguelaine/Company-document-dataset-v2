#!/usr/bin/env python3
"""Run the whole CompanyDocuments v2 pipeline, end to end. Standard library only: it builds
the venv itself.

    python3 scripts/run_pipeline.py                         # everything, balanced dataset (2,000 per type)
    python3 scripts/run_pipeline.py --per-type 20           # quick end-to-end check
    python3 scripts/run_pipeline.py --all-records           # every source record once (~354k documents)
    python3 scripts/run_pipeline.py --everything            # all records + complete LLM texts + tests
    python3 scripts/run_pipeline.py --from generate         # rerun from a stage
    python3 scripts/run_pipeline.py --stages generate export --config configs/example.yaml

Stages, in order (every stage is safe to rerun):

    venv       create/repair ./venv and install requirements (also fixes a venv broken by an OS
               Python upgrade)
    download   fetch and build the four SQLite databases (skips those present)
    index      add lookup indexes (scripts/prepare_sources.py)
    companies  create 15 issuers per sector if missing, spread layout x theme over each sector
    texts      LLM company texts + product descriptions (only if OPENROUTER_API_KEY is set; optional)
    generate   render every document (resumes an interrupted run)
    augment    degraded scan/photo page images for --augment-fraction of the documents
    export     CSV dataset (one row per PDF) + card, split by company (--export-format parquet)
    test       unit tests

Output: --out (documents), --dataset (CSV or parquet), logs/pipeline-<time>.log
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV = REPO_ROOT / "venv"
PY = VENV / "bin" / "python"
STAGES = ["venv", "download", "index", "companies", "texts", "generate", "augment", "export", "test"]
OPTIONAL = {"texts"}                 # a failure here warns and continues (not with --everything)
LOG_FILE: Path | None = None
ALL_RECORDS = 353_590                # one document per source record, all 22 variants
N_TYPES = 13
# measured per document (compact JSON): generate = PDF + gold + words; export = parquet row
GEN_BYTES, EXPORT_BYTES, EXPORT_NO_PDF_BYTES = 34_000, 24_000, 6_000
SCAN_BYTES = 2 * 1.4 * 180_000      # ~1.4 pages per document, JPEG once in scans/ and once in parquet


def say(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run(*cmd, check: bool = True) -> int:
    """Run a command, streaming its output to the console and the log."""
    say("$ " + " ".join(str(c) for c in cmd))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen([str(c) for c in cmd], cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, env=env) as proc:
        for line in proc.stdout:
            print("  " + line, end="", flush=True)
            if LOG_FILE:
                with LOG_FILE.open("a", encoding="utf-8") as fh:
                    fh.write("  " + line)
        code = proc.wait()
    if check and code:
        raise RuntimeError(f"command failed ({code}): {' '.join(str(c) for c in cmd)}")
    return code


def docgen(*args, check: bool = True) -> int:
    return run(PY, "-m", "docgen", *args, check=check)


def venv_ok() -> bool:
    if not PY.exists():
        return False
    probe = "import yaml, jinja2, weasyprint, pymupdf, numpy, pyarrow, PIL"
    return subprocess.run([str(PY), "-c", probe], capture_output=True).returncode == 0


# --------------------------------------------------------------------- stages ----
def stage_venv(args) -> None:
    if venv_ok() and not args.rebuild_venv:
        say("venv: ok")
        return
    say("venv: missing or broken (e.g. the system Python changed), rebuilding")
    # never rebuild a venv with its own interpreter: use the system Python behind it
    base = getattr(sys, "_base_executable", "") if sys.prefix != sys.base_prefix else sys.executable
    run(base or shutil.which("python3"), "-m", "venv", "--clear", VENV)
    run(PY, "-m", "pip", "install", "-q", "--upgrade", "pip")
    run(PY, "-m", "pip", "install", "-q", "-r", REPO_ROOT / "requirements.txt")
    if not venv_ok():
        raise RuntimeError("venv still broken after install; are Pango/HarfBuzz installed? (apt install libpango-1.0-0)")


def stage_download(args) -> None:
    run(sys.executable, REPO_ROOT / "scripts/download_sources.py", *(["--force"] if args.force_download else []))


def stage_index(args) -> None:
    run(PY, REPO_ROOT / "scripts/prepare_sources.py")


def stage_companies(args) -> None:
    docgen("companies", "init", "--count", args.companies_per_sector)      # skips sectors that have companies
    docgen("companies", "rebalance")


def stage_texts(args) -> None:
    env_file = REPO_ROOT / ".env"
    has_key = os.environ.get("OPENROUTER_API_KEY") or (
        env_file.exists() and any(l.startswith("OPENROUTER_API_KEY=") and l.split("=", 1)[1].strip()
                                  for l in env_file.read_text().splitlines()))
    if not has_key:
        say("texts: no OPENROUTER_API_KEY (.env), companies keep their fallback texts; skipped")
        return
    if not args.everything:
        docgen("texts", "generate")
        return
    # --everything: keep going until every company and product has LLM text (free tiers rate-limit)
    for attempt in range(1, args.text_passes + 1):
        docgen("texts", "generate", check=False)
        status = json.loads(subprocess.run([str(PY), "-m", "docgen", "texts", "status"], cwd=REPO_ROOT,
                                           capture_output=True, text=True, check=True).stdout)
        missing = len(status["companies_missing"]) + sum(len(v) for v in status["products_missing"].values())
        if status["complete"]:
            say("texts: complete")
            return
        say(f"texts: {missing} still missing after pass {attempt}/{args.text_passes}")
        if attempt < args.text_passes:
            time.sleep(60)
    raise RuntimeError(f"LLM texts incomplete ({status['companies_missing'][:5]}...); free quota may be used up "
                       "for today: rerun later with --from texts, or pass --allow-missing-texts")


def estimate_docs(args) -> int | None:
    if args.config:
        return None
    return ALL_RECORDS if args.all_records else args.per_type * N_TYPES


def check_disk(args, stages: list[str]) -> None:
    """Refuse to start a run that cannot fit, instead of failing hours in."""
    docs = estimate_docs(args)
    if docs is None or not {"generate", "augment", "export"} & set(stages):
        return
    need = {}
    if "generate" in stages:
        need["generate"] = docs * GEN_BYTES
    if "augment" in stages and args.augment_fraction > 0:
        need["augment + scans export"] = docs * args.augment_fraction * SCAN_BYTES
    if "export" in stages:
        need["export"] = docs * (EXPORT_NO_PDF_BYTES if args.no_pdf else EXPORT_BYTES)
    total = sum(need.values()) + 2 * 1024 ** 3                     # 2 GB headroom
    target = (REPO_ROOT / args.out).resolve()
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    gb = lambda b: f"{b / 1024 ** 3:.1f} GB"                       # noqa: E731
    say(f"disk: {docs:,} documents need ~{gb(total)} ({', '.join(f'{k} {gb(v)}' for k, v in need.items())}); "
        f"free {gb(free)}")
    if total > free and not args.ignore_disk:
        raise RuntimeError(
            f"not enough disk space: need ~{gb(total)}, have {gb(free)}. Options: free space; "
            "--out/--dataset on a bigger disk; --no-pdf (PDFs stay in --out only); a lower --augment-fraction; "
            "--per-type N; or --ignore-disk to try anyway")


def make_config(args) -> Path:
    """The run config: --config as is, or every document type with --per-type / all records."""
    if args.config:
        return Path(args.config)
    types = json.loads(subprocess.run(
        [str(PY), "-c", "import json; from docgen.core import registry; print(json.dumps(sorted(registry.doc_types())))"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout)
    count = "all" if args.all_records else args.per_type
    cfg = {
        "run_name": Path(args.out).name, "seed": args.seed, "out_dir": args.out, "workers": args.workers,
        "fx": {"USD": "1", "EUR": "0.92", "GBP": "0.79"}, "self_check": True, "boxes": True, "resume": True,
        "documents": [{"type": t, "count": count} for t in types],
    }
    path = REPO_ROOT / "logs" / f"pipeline-config-{Path(args.out).name}.yaml"
    # JSON is valid YAML, and keeps this script free of a yaml dependency
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    say(f"config: {len(types)} types x {count} -> {path.relative_to(REPO_ROOT)}")
    return path


def stage_generate(args) -> None:
    docgen("generate", make_config(args))


def run_dir(args) -> str:
    if args.config:
        cfg = json.loads(subprocess.run(
            [str(PY), "-c", f"import json, yaml; print(json.dumps(yaml.safe_load(open({str(args.config)!r}))))"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout)
        return cfg.get("out_dir", "output/run")
    return args.out


def stage_augment(args) -> None:
    if args.augment_fraction <= 0:
        say("augment: fraction 0, skipped")
        return
    docgen("augment", run_dir(args), "--fraction", args.augment_fraction, "--workers", args.workers,
           "--seed", args.seed)


def stage_export(args) -> None:
    docgen("export", run_dir(args), args.dataset, "--seed", args.seed, "--format", args.export_format,
           *(["--no-pdf"] if args.no_pdf else []))


def stage_test(args) -> None:
    run(PY, "-m", "unittest", "discover", "-s", "tests")


def main() -> int:
    global LOG_FILE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stages", nargs="*", choices=STAGES, help="run only these stages")
    ap.add_argument("--from", dest="start", choices=STAGES, help="run from this stage on")
    ap.add_argument("--skip", nargs="*", choices=STAGES, default=[], help="skip these stages")
    ap.add_argument("--per-type", type=int, default=2000, help="documents per type (default 2000)")
    ap.add_argument("--all-records", action="store_true", help="every source record once (~354k documents)")
    ap.add_argument("--everything", action="store_true",
                    help="all records, LLM texts must be complete (retried), tests at the end")
    ap.add_argument("--text-passes", type=int, default=4, help="--everything: LLM passes before giving up")
    ap.add_argument("--allow-missing-texts", action="store_true", help="--everything: fallback texts are fine")
    ap.add_argument("--ignore-disk", action="store_true", help="skip the free-space check")
    ap.add_argument("--config", help="use this run config instead of building one")
    ap.add_argument("--out", default="output/v2", help="documents directory (default output/v2)")
    ap.add_argument("--dataset", default="dataset/v2", help="exported dataset directory (default dataset/v2)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--companies-per-sector", type=int, default=15)
    ap.add_argument("--augment-fraction", type=float, default=0.3, help="share of documents to degrade (0 = skip)")
    ap.add_argument("--export-format", choices=["csv", "parquet"], default="csv", help="dataset format (default csv)")
    ap.add_argument("--no-pdf", action="store_true", help="don't copy (csv) or embed (parquet) the PDFs")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--rebuild-venv", action="store_true")
    ap.add_argument("--with-tests", action="store_true", help="also run the unit tests at the end")
    args = ap.parse_args()
    if args.everything:
        args.all_records = True
        args.with_tests = True
        if args.allow_missing_texts:
            OPTIONAL.add("texts")
        else:
            OPTIONAL.discard("texts")

    stages = args.stages or STAGES[STAGES.index(args.start):] if args.start else (args.stages or STAGES)
    stages = [s for s in stages if s not in args.skip and (s != "test" or args.with_tests or args.stages)]

    (REPO_ROOT / "logs").mkdir(exist_ok=True)
    LOG_FILE = REPO_ROOT / "logs" / f"pipeline-{datetime.now():%Y%m%d-%H%M%S}.log"
    say(f"pipeline: {' -> '.join(stages)}  (log: {LOG_FILE.relative_to(REPO_ROOT)})")
    try:
        check_disk(args, stages)
    except RuntimeError as exc:
        say(f"preflight: {exc}")
        return 1
    timings = []
    for stage in stages:
        if stage != "venv" and not venv_ok():
            say("venv is not usable; running the venv stage first")
            stage_venv(args)
        say(f"=== {stage} ===")
        started = time.time()
        try:
            globals()[f"stage_{stage}"](args)
        except Exception as exc:
            if stage in OPTIONAL:
                say(f"{stage}: {exc} (optional stage, continuing)")
            else:
                say(f"{stage}: FAILED: {exc}")
                say(f"fix it, then resume with: python3 scripts/run_pipeline.py --from {stage}")
                return 1
        timings.append((stage, time.time() - started))
    say("done: " + ", ".join(f"{s} {t:.0f}s" for s, t in timings))
    if "export" in stages:
        say(f"dataset: {args.dataset}/  (README.md, stats.json, data/*.parquet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
