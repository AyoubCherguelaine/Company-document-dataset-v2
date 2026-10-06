#!/usr/bin/env python3
"""Generate every source record (~354k documents) and publish it to Hugging Face one document type at a
time, so the disk only ever holds one type (~9 GB at most) instead of the whole dataset (~70 GB).

    python3 scripts/run_in_parts.py                        # all types; refresh the card after every part
    python3 scripts/run_in_parts.py --types payslip quote  # only these; publish them immediately
    python3 scripts/run_in_parts.py --publish-only         # refresh the card from already-pushed parts
    python3 scripts/run_in_parts.py --status               # what is done, what is left
    python3 scripts/run_in_parts.py --augment-fraction 0.1

Per type: generate (count: all) -> augment -> export (parquet, data/<type>/ + scans/<type>/) -> push ->
keep the part's stats.json -> delete the local files -> refresh the card. Merge the completed parts'
stats and publish the card (README.md + stats.json + licenses) after each push, deleting the previous
folder export while keeping the parquet shards. Already-pushed parts are the main dataset immediately.

Splits are by company over all companies (docgen.core.export.company_splits), so every part splits the
same way. Export uses split seed 0, the seed of the published 2k dataset: same test/validation companies.

Standard library only (like run_pipeline.py, whose helpers it uses). Logs: logs/parts-<time>.log.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_pipeline as rp  # noqa: E402

REPO_ROOT = rp.REPO_ROOT
WORK = REPO_ROOT / "output/parts"          # generate + augment, one subdirectory per type
EXPORT = REPO_ROOT / "dataset/parts"       # parquet export, one subdirectory per type
STATE = REPO_ROOT / "dataset/full"         # the card dir; parts/<type>.json = stats of a pushed type
SPLIT_SEED = 0
# measured on the 2k run, per document: PDF + gold + words; parquet row; one scanned document (JPEG
# pages in output/ and again inside the scans parquet)
GEN_BYTES, PARQUET_BYTES, SCAN_DOC_BYTES = 40_000, 25_000, 2 * 185_000


def capacities() -> dict[str, int]:
    """Source records per document type (what `count: all` generates)."""
    code = ("import json; from docgen.core import registry; from docgen.core.pipeline import RunConfig, Workspace; "
            "ws = Workspace(RunConfig()); print(json.dumps({t: sum(len(ws.keys(registry.get(v))) "
            "for v in registry.variants(t)) for t in sorted(registry.doc_types())}))")
    out = subprocess.run([str(rp.PY), "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def done_types() -> set[str]:
    return {p.stem for p in (STATE / "parts").glob("*.json")}


def run_config(types: list[str], out_dir: str, args) -> dict:
    """Same settings as run_pipeline.make_config, with every record of the given types."""
    return {"run_name": Path(out_dir).name, "seed": args.seed, "out_dir": out_dir, "workers": args.workers,
            "fx": {"USD": "1", "EUR": "0.92", "GBP": "0.79"}, "self_check": True, "boxes": True, "resume": True,
            "documents": [{"type": t, "count": "all"} for t in types]}


def check_disk(t: str, n: int, args) -> None:
    need = n * (GEN_BYTES + PARQUET_BYTES + args.augment_fraction * SCAN_DOC_BYTES) + 2 * 1024 ** 3
    free = shutil.disk_usage(REPO_ROOT).free
    rp.say(f"{t}: {n:,} documents need ~{need / 1024 ** 3:.1f} GB, free {free / 1024 ** 3:.1f} GB")
    if need > free and not args.ignore_disk:
        raise RuntimeError(f"not enough disk space for {t}; free some, lower --augment-fraction, or --ignore-disk")


def push(*extra: str) -> None:
    code = rp.run(rp.PY, REPO_ROOT / "scripts/push_to_hf.py", *extra, check=False)
    if code:
        raise RuntimeError("push failed (the Hub allows 128 commits per hour); rerun to resume")


def do_type(t: str, n: int, args) -> None:
    check_disk(t, n, args)
    work, export = WORK / t, EXPORT / t
    cfg = REPO_ROOT / "logs" / f"parts-config-{t}.yaml"
    cfg.write_text(json.dumps(run_config([t], str(work.relative_to(REPO_ROOT)), args), indent=2))
    started = time.time()
    rp.docgen("generate", cfg)
    if args.augment_fraction > 0:
        rp.docgen("augment", work, "--fraction", args.augment_fraction, "--workers", args.workers,
                  "--seed", args.seed)
    rp.docgen("export", work, export, "--format", "parquet", "--seed", SPLIT_SEED)
    stats = json.loads((export / "stats.json").read_text())
    if args.no_push:                          # a trial: nothing is published, so the type isn't done
        rp.say(f"{t}: exported to {export.relative_to(REPO_ROOT)} (not pushed)")
        return
    # Upload shards first; the merged card is refreshed from completed checkpoints afterward.
    push("--folder", export, "--batch", "100", "--exclude", "README.md", "stats.json", "licenses/*")
    (STATE / "parts").mkdir(parents=True, exist_ok=True)
    (STATE / "parts" / f"{t}.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False))
    if not args.keep_local:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(export, ignore_errors=True)
    rp.say(f"{t}: done, {sum(stats['documents'].values()):,} documents, "
           f"{sum(stats['scans'].values()):,} scan pages, {(time.time() - started) / 60:.0f} min")


def finish(types: list[str], args) -> None:
    """Publish the completed parts as the main dataset and remove the old folder layout."""
    cfg = json.dumps(run_config(types, "output/parts/<type>", args) | {"run_name": "full"}, indent=2)
    code = ("import json, sys; from pathlib import Path; from docgen.core.export import merge_stats, write_card; "
            "parts = [json.loads(p.read_text()) for p in sorted(Path(sys.argv[1]).glob('parts/*.json'))]; "
            "write_card(Path(sys.argv[1]), merge_stats(parts), sys.argv[2])")
    rp.run(rp.PY, "-c", code, STATE, cfg)
    completed = sorted(done_types())
    remaining = sorted(set(types) - set(completed))
    if remaining:
        notice = ("This dataset is published incrementally. Only completed, uploaded document types are "
                  "included below; remaining types are added as generation finishes.\n\n"
                  f"Completed types: {', '.join(completed)}.\n\n"
                  f"Remaining types: {', '.join(remaining)}.\n\n")
        card = STATE / "README.md"
        card.write_text(card.read_text(encoding="utf-8").replace(
            "# Company Documents v2\n\n", "# Company Documents v2\n\n" + notice), encoding="utf-8")
    if not args.no_push:
        push("--folder", STATE, "--exclude", "parts/*", "--delete-stale", "--keep", "data/*.parquet", "scans/*.parquet")
    rp.say(f"card: {STATE.relative_to(REPO_ROOT)}/README.md")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--types", nargs="*", help="only these types (default: all)")
    ap.add_argument("--status", action="store_true", help="show done / left and exit")
    ap.add_argument("--publish-only", action="store_true", help="publish the card for already-pushed parts; generate nothing")
    ap.add_argument("--augment-fraction", type=float, default=0.3, help="share of documents to degrade")
    ap.add_argument("--workers", type=int, default=rp.os.cpu_count() or 4)
    ap.add_argument("--seed", type=int, default=42, help="generation seed (as run_pipeline.py)")
    ap.add_argument("--no-push", action="store_true", help="trial: export only, keep the files, mark nothing done")
    ap.add_argument("--keep-local", action="store_true", help="don't delete a type's files after the push")
    ap.add_argument("--redo", action="store_true", help="also rerun types already done")
    ap.add_argument("--ignore-disk", action="store_true")
    args = ap.parse_args()
    args.keep_local |= args.no_push

    if not rp.venv_ok():
        print("venv missing or broken: python3 scripts/run_pipeline.py --stages venv", file=sys.stderr)
        return 1
    caps = capacities()
    unknown = sorted(set(args.types or []) - set(caps))
    if unknown:
        ap.error(f"unknown types: {', '.join(unknown)} (choose from {', '.join(caps)})")
    done = done_types()
    if args.status:
        for t, n in sorted(caps.items(), key=lambda kv: kv[1]):
            print(f"  {'done' if t in done else 'left':4}  {t:24} {n:>8,} records")
        print(f"{len(done & set(caps))}/{len(caps)} types done")
        return 0

    # two runs would generate the same type into the same directory and corrupt its manifest
    lock = (REPO_ROOT / "logs" / ".run_in_parts.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another run_in_parts.py is already running (see logs/parts-*.log); "
              "check it with --status instead of starting a second one", file=sys.stderr)
        return 1
    rp.LOG_FILE = REPO_ROOT / "logs" / f"parts-{datetime.now():%Y%m%d-%H%M%S}.log"
    # Refresh on resume as well: a previous run may have uploaded a part but failed to publish its card.
    if done and not args.no_push:
        try:
            finish(sorted(caps), args)
        except Exception as exc:
            rp.say(f"card: FAILED: {exc}; rerun to retry")
            return 1
    if args.publish_only:
        if args.no_push:
            if done:
                finish(sorted(caps), args)
        elif not done:
            rp.say("no pushed parts yet; nothing to publish")
        return 0
    todo = [t for t in sorted(args.types or caps, key=lambda t: caps[t]) if args.redo or t not in done]
    rp.say(f"parts: {len(todo)} types to do ({sum(caps[t] for t in todo):,} documents), smallest first; "
           f"log: {rp.LOG_FILE.relative_to(REPO_ROOT)}")
    for i, t in enumerate(todo, 1):
        rp.say(f"=== {t} ({i}/{len(todo)}) ===")
        try:
            do_type(t, caps[t], args)
            if not args.no_push:
                finish(sorted(caps), args)
        except Exception as exc:
            rp.say(f"{t}: FAILED: {exc}")
            rp.say("fix it, then rerun the same command: finished types are skipped, generation resumes")
            return 1
    remaining = sorted(set(caps) - done_types())
    if remaining:
        rp.say(f"types remaining: {', '.join(remaining)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
