"""CLI.

    python -m docgen llm ping                         # check .env / OpenRouter
    python -m docgen companies init --count 12 --llm  # create companies/<slug>/company.yaml
    python -m docgen companies list
    python -m docgen texts generate                   # LLM: company texts + product descriptions
    python -m docgen preview invoice.adventureworks --company <slug> --open
    python -m docgen preview payslip                  # a type picks one of its sources at random
    python -m docgen generate configs/example.yaml
    python -m docgen augment output/<run> --fraction 0.3   # degraded scan/photo page images
    python -m docgen export output/<run> dataset/          # CSV (one row per PDF) + card, split by company
    python -m docgen list
"""

from __future__ import annotations

import argparse
import random
import sys
import webbrowser
from pathlib import Path

from . import REPO_ROOT
from .core import registry
from .core.companies import SECTORS, create_companies, llm_texts_batch, rebalance_designs
from .core.llm import LLMClient
from .core.pipeline import RunConfig, Workspace, make_job, render_job, run
from .core.textbank import generate_product_texts


def _cfg(args, **kw) -> RunConfig:
    cfg = RunConfig(**kw)
    for item in getattr(args, "source", None) or []:
        name, _, path = item.partition("=")
        cfg.sources[name] = path
    if getattr(args, "companies_dir", None):
        cfg.companies_dir = args.companies_dir
    return cfg


def cmd_list(args):
    ws = Workspace(_cfg(args))
    print("document types (sources):")
    for doc_type, sources in registry.doc_types().items():
        print(f"  {doc_type:<24} {', '.join(sources)}")
    print("layouts:  ", ", ".join(ws.renderer(registry.get(registry.variants()[0])).layouts()))
    print("themes:   ", ", ".join(sorted(ws.themes)))
    print("locales:  ", ", ".join(sorted(ws.locales)))
    by_sector = {}
    for c in ws.companies.values():
        by_sector[c.sector] = by_sector.get(c.sector, 0) + 1
    print("companies:", ", ".join(f"{s}={n}" for s, n in sorted(by_sector.items())) or "none",
          "(python -m docgen companies list)")


def cmd_llm_ping(args):
    llm = LLMClient()
    if not llm.enabled:
        print("OPENROUTER_API_KEY is empty: copy .env.example to .env and set it", file=sys.stderr)
        return 1
    print("models:", ", ".join(llm.models))
    llm.temperature = 0.0
    # generous max_tokens: reasoning models spend tokens before answering
    print(llm.chat_json("You are a health check.", 'Reply with {"ok": true, "model": "<your model name>"}',
                        max_tokens=400))
    return 0


def cmd_companies_init(args):
    ws = Workspace(_cfg(args))
    root = ws.cfg.resolve(ws.cfg.companies_dir)
    layouts = args.layouts or ws.renderer(registry.get(registry.variants()[0])).layouts()
    themes = args.themes or sorted(ws.themes)
    llm = LLMClient() if args.llm else None
    if llm is not None and not llm.enabled:
        print("[llm] OPENROUTER_API_KEY empty: using built-in fallback texts", file=sys.stderr)
    taken = set(ws.companies)
    for sector in args.sectors:
        have = [c for c in ws.companies.values() if c.sector == sector]
        if have and not args.force:
            print(f"[skip] {sector}: {len(have)} companies already (use --force to add more)")
            continue
        for company in create_companies(args.count, args.seed, layouts, themes, args.locales, llm=llm,
                                        sector=sector, taken=taken):
            path = company.save(root)
            d = company.design["default"]
            print(f"  {sector:<6} {company.slug:<34} {company.locale}  {d['layout']:<8} {d['theme']:<10} {path}")
    return 0


def cmd_companies_rebalance(args):
    ws = Workspace(_cfg(args))
    layouts = ws.renderer(registry.get(registry.variants()[0])).layouts()
    companies = list(ws.companies.values())
    rebalance_designs(companies, layouts, sorted(ws.themes), args.seed)
    for c in companies:
        c.save(c.path.parent)
    combos = {(c.design["default"]["layout"], c.design["default"]["theme"]) for c in companies}
    print(f"{len(companies)} companies, {len(combos)}/{len(layouts) * len(ws.themes)} layout x theme combinations used")
    return 0


def cmd_companies_list(args):
    ws = Workspace(_cfg(args))
    for slug, c in ws.companies.items():
        d = c.design_for("invoice")
        custom = "custom templates" if c.template_dir else ""
        llm = "llm texts" if c.texts else "fallback texts"
        print(f"  {c.sector:<6} {slug:<34} {c.locale} {c.currency}  {d.get('layout'):<8} {d.get('theme'):<10} "
              f"{llm:<15} {custom}")


def cmd_texts_generate(args):
    llm = LLMClient()
    if not llm.enabled:
        print("OPENROUTER_API_KEY is empty: copy .env.example to .env and set it", file=sys.stderr)
        return 1
    ws = Workspace(_cfg(args))
    if not args.skip_companies:
        todo = [c for c in ws.companies.values() if args.force or not c.texts]
        for i in range(0, len(todo), args.batch):
            batch = todo[i:i + args.batch]
            try:
                texts = llm_texts_batch(llm, batch)
            except Exception as exc:
                print(f"[llm] batch {i // args.batch + 1}: {exc}", file=sys.stderr)
                continue
            for company in batch:
                if texts.get(company.slug):
                    company.texts = texts[company.slug]
                    company.save(company.path.parent)
                    print(f"[llm] texts for {company.slug}")
                else:
                    print(f"[llm] no texts returned for {company.slug} (rerun to retry)", file=sys.stderr)
    if not args.skip_products:
        locales = args.locales or sorted({c.locale for c in ws.companies.values()} or ws.locales)
        for loc in locales:
            print(generate_product_texts(llm, ws.source("northwind"), ws.texts, loc))
    return 0


def cmd_texts_status(args):
    """JSON report of what the LLM still has to write (used by scripts/run_pipeline.py --everything)."""
    import json

    ws = Workspace(_cfg(args))
    locales = sorted({c.locale for c in ws.companies.values()})
    products = [str(p["ProductID"]) for p in ws.source("northwind").products()]
    report = {
        "companies_missing": sorted(s for s, c in ws.companies.items() if not c.texts),
        "products_missing": {loc: [p for p in products if not ws.texts.product_details(loc, p)] for loc in locales},
    }
    report["complete"] = not report["companies_missing"] and not any(report["products_missing"].values())
    print(json.dumps(report))
    return 0


def cmd_preview(args):
    out_dir = Path(args.out).resolve()
    ws = Workspace(_cfg(args, out_dir=str(out_dir), keep_html=True, boxes=False, seed=args.seed))
    if not ws.companies:
        print("no companies yet; run: python -m docgen companies init", file=sys.stderr)
        return 1
    rng = random.Random(args.seed)
    variants = [args.type] if "." in args.type else registry.variants(args.type)
    if args.company:   # a company implies its sector, hence the source
        variants = [v for v in variants if args.company in ws.companies_for(registry.get(v), [args.company])] or variants
    variant = rng.choice(variants)
    doc = registry.get(variant)
    pool = ws.companies_for(doc, [args.company] if args.company else None)
    if not pool:
        print(f"no company for {variant} (sector {doc.source}); run: python -m docgen companies init --sectors ...",
              file=sys.stderr)
        return 1
    company = rng.choice(pool)
    key = args.key if args.key is not None else rng.choice(ws.keys(doc))
    if isinstance(rng.choice(ws.keys(doc)), int) and str(key).isdigit():
        key = int(key)
    job = make_job(ws, variant, key, company, args.seed)
    if args.html_only:
        *_, html = ws.build(job)
        path = out_dir / f"{job['doc_id']}.html"
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    else:
        result = render_job(job, ws)
        if result["status"] != "ok":
            print(result, file=sys.stderr)
            return 1
        path = out_dir / result["pdf"]
    print(f"{variant} key={key} company={company}: {path}")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_generate(args):
    counts = run(RunConfig.load(args.config))
    return 0 if counts.get("ok") else 1


def cmd_augment(args):
    from .core.augment import run_augment

    run_augment(Path(args.run).resolve(), args.fraction, args.profile, args.dpi, args.seed, args.workers)
    return 0


def cmd_export(args):
    from .core.export import export_run

    ws = Workspace(_cfg(args))
    stats = export_run(Path(args.run).resolve(), Path(args.out).resolve(), ws.companies, args.seed,
                       not args.no_pdf, fmt=args.format)
    return 0 if stats["documents"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="docgen", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", metavar="NAME=PATH",
                    help="override a source database, e.g. northwind=data/northwind.db")
    ap.add_argument("--companies-dir", help="default: companies/")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show document types, layouts, themes, locales").set_defaults(fn=cmd_list)

    llm = sub.add_parser("llm", help="LLM utilities").add_subparsers(dest="llm_cmd", required=True)
    llm.add_parser("ping", help="check the OpenRouter key and model").set_defaults(fn=cmd_llm_ping)

    comp = sub.add_parser("companies", help="issuer companies").add_subparsers(dest="comp_cmd", required=True)
    ci = comp.add_parser("init", help="create seeded companies with a fixed design each")
    ci.add_argument("--count", type=int, default=6, help="companies per sector")
    ci.add_argument("--sectors", nargs="*", default=sorted(SECTORS), help=f"default: {' '.join(sorted(SECTORS))}")
    ci.add_argument("--seed", type=int, default=0)
    ci.add_argument("--layouts", nargs="*", help="default: all layouts")
    ci.add_argument("--themes", nargs="*", help="default: all themes")
    ci.add_argument("--locales", nargs="*", help="default: all locales")
    ci.add_argument("--llm", action="store_true", help="write each company's texts with the LLM")
    ci.add_argument("--force", action="store_true", help="add companies to sectors that already have some")
    ci.set_defaults(fn=cmd_companies_init)
    comp.add_parser("list").set_defaults(fn=cmd_companies_list)
    cr = comp.add_parser("rebalance", help="spread all layout x theme combinations over each sector")
    cr.add_argument("--seed", type=int, default=0)
    cr.set_defaults(fn=cmd_companies_rebalance)

    texts = sub.add_parser("texts", help="LLM-written texts").add_subparsers(dest="texts_cmd", required=True)
    tg = texts.add_parser("generate", help="fill company texts and product descriptions (cached)")
    tg.add_argument("--locales", nargs="*")
    tg.add_argument("--skip-companies", action="store_true")
    tg.add_argument("--skip-products", action="store_true")
    tg.add_argument("--force", action="store_true", help="rewrite company texts that already exist")
    tg.add_argument("--batch", type=int, default=4, help="companies per LLM request")
    tg.set_defaults(fn=cmd_texts_generate)
    texts.add_parser("status", help="JSON: companies and products still without LLM texts").set_defaults(
        fn=cmd_texts_status)

    p = sub.add_parser("preview", help="render one document, for designing templates")
    p.add_argument("type", help="document type (invoice) or variant (invoice.adventureworks)")
    p.add_argument("--company", help="company slug (random if omitted)")
    p.add_argument("--key", help="source key, e.g. an OrderID (random if omitted)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", default=str(REPO_ROOT / "output/preview"))
    p.add_argument("--html-only", action="store_true", help="skip PDF; write the HTML for browser devtools")
    p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_preview)

    g = sub.add_parser("generate", help="run a YAML config")
    g.add_argument("config")
    g.set_defaults(fn=cmd_generate)

    a = sub.add_parser("augment", help="degraded scan/photo images of a run's pages (noise stage)")
    a.add_argument("run", help="a generate out_dir")
    a.add_argument("--fraction", type=float, default=1.0, help="share of documents to augment")
    a.add_argument("--profile", choices=["mixed", "scan", "photo"], default="mixed")
    a.add_argument("--dpi", type=int, default=150)
    a.add_argument("--seed", type=int, default=0)
    a.add_argument("--workers", type=int, default=4)
    a.set_defaults(fn=cmd_augment)

    e = sub.add_parser("export", help="dataset (CSV or parquet) + card, split by company")
    e.add_argument("run", help="a generate out_dir")
    e.add_argument("out", help="dataset directory (replaced if it is a previous export)")
    e.add_argument("--format", choices=["csv", "parquet"], default="csv",
                   help="csv: one row per PDF, PDFs and JSON files alongside (default); parquet: v1 schema + boxes")
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--no-pdf", action="store_true", help="don't copy (csv) or embed (parquet) the PDFs")
    e.set_defaults(fn=cmd_export)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
