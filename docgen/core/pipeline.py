"""Run a YAML-described generation: plan jobs, render, self-check, write PDF + gold JSON."""

from __future__ import annotations

import collections
import hashlib
import json
import random
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import PACKAGE_DIR, REPO_ROOT
from ..sources import DEFAULT_DATE_SHIFT, DEFAULT_PATHS, open_source, source_classes
from . import registry
from .companies import DEFAULT_DIR as COMPANIES_DIR
from .companies import load_companies
from .document import GenContext
from .locale import load_locales
from .renderer import Renderer
from .selfcheck import check_pdf
from .textbank import DEFAULT_DIR as TEXTBANK_DIR
from .textbank import TextBank
from .theme import Theme, load_themes


@dataclass
class RunConfig:
    run_name: str = "run"
    seed: int = 0
    sources: dict[str, str] = field(default_factory=dict)      # name -> sqlite path (default: bundled)
    date_shift: dict[str, int] = field(default_factory=dict)   # name -> years (default: sakila +19)
    out_dir: str = "output/run"
    workers: int = 1
    companies_dir: str = str(COMPANIES_DIR)
    textbank_dir: str = str(TEXTBANK_DIR)
    fx: dict[str, Any] = field(default_factory=lambda: {"USD": "1", "EUR": "0.92", "GBP": "0.79"})
    self_check: bool = True
    boxes: bool = True
    keep_html: bool = False
    resume: bool = False          # skip documents whose PDF + gold already exist (interrupted runs)
    theme_dirs: list[str] = field(default_factory=list)
    locale_dirs: list[str] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "RunConfig":
        return cls(**(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}))

    def resolve(self, p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else REPO_ROOT / path


class Workspace:
    """Everything a worker needs, loaded once per process."""

    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.themes = load_themes([PACKAGE_DIR / "themes", *map(cfg.resolve, cfg.theme_dirs)])
        self.locales = load_locales([PACKAGE_DIR / "locales", *map(cfg.resolve, cfg.locale_dirs)])
        self.companies = load_companies(cfg.resolve(cfg.companies_dir))
        self.texts = TextBank(cfg.resolve(cfg.textbank_dir))
        self._sources: dict[str, Any] = {}
        self._renderers: dict[tuple, Renderer] = {}
        self._keys: dict[str, list] = {}

    def source(self, name: str):
        if name not in self._sources:
            path = self.cfg.sources.get(name)
            shift = self.cfg.date_shift.get(name, DEFAULT_DATE_SHIFT.get(name, 0))
            self._sources[name] = open_source(name, self.cfg.resolve(path) if path else DEFAULT_PATHS[name], shift)
        return self._sources[name]

    def keys(self, doc) -> list:
        if doc.name not in self._keys:
            self._keys[doc.name] = doc.source_keys(self.source(doc.source))
        return self._keys[doc.name]

    def renderer(self, doc, company=None) -> Renderer:
        """Template search order: company overrides, then document types, then shared layouts."""
        key = (doc.doc_type, company.slug if company else None)
        if key not in self._renderers:
            dirs = ([company.template_dir] if company and company.template_dir else []) + list(doc.template_dirs)
            self._renderers[key] = Renderer(dirs)
        return self._renderers[key]

    def company_theme(self, company, theme_name: str) -> Theme:
        base = self.themes[theme_name]
        if not company.colors:
            return base
        return Theme(**{**base.__dict__, "colors": {**base.colors, **company.colors}})

    def companies_for(self, doc, slugs: list[str] | None = None, locales: list[str] | None = None) -> list[str]:
        sector = source_classes()[doc.source].sector
        return [s for s in (slugs or sorted(self.companies))
                if s in self.companies and self.companies[s].sector == sector
                and (not locales or self.companies[s].locale in locales)]

    def build(self, job: dict):
        """job -> (document, record, ctx, html). Deterministic for a given job."""
        doc = registry.get(job["variant"])
        company = self.companies[job["company"]]
        rng = random.Random(job["seed"])
        locale = self.locales[job["locale"]]
        options = doc.variation(rng, locale) | company.options      # company choices are fixed
        ctx = GenContext(
            seed=job["seed"], rng=rng, layout=job["layout"], theme=self.company_theme(company, job["theme"]),
            locale=locale.with_date_format(options.get("date_format", locale.date_format)),
            issuer=company.to_party(), currency=company.currency, fx_rate=self.cfg.fx.get(company.currency, 1),
            options=options, company=company, texts=self.texts, repo=self.source(doc.source),
        )
        record = doc.build_record(doc.load(ctx.repo, job["key"]), ctx)
        record.extra.setdefault("doc_id", job["doc_id"])
        html = self.renderer(doc, company).render_html(doc.template_candidates(ctx.layout), doc.context(record, ctx))
        return doc, record, ctx, html


def doc_id_for(key, variant, company, seed) -> str:
    return hashlib.sha1(f"{key}|{variant}|{company}|{seed}".encode()).hexdigest()[:16]


def make_job(ws: Workspace, variant: str, key, company_slug: str, seed: int) -> dict:
    """A job is fully described by (variant, key, company, seed); design comes from the company."""
    doc = registry.get(variant)
    company = ws.companies[company_slug]
    design = company.design_for(doc.doc_type)
    layout, theme = design.get("layout", "classic"), design.get("theme", "plain")
    if layout not in ws.renderer(doc, company).layouts():
        raise ValueError(f"{company_slug}: unknown layout {layout!r}")
    if theme not in ws.themes:
        raise ValueError(f"{company_slug}: unknown theme {theme!r}")
    if company.locale not in ws.locales:
        raise ValueError(f"{company_slug}: unknown locale {company.locale!r}")
    return {"type": doc.doc_type, "variant": variant, "source": doc.source, "key": key, "company": company_slug,
            "layout": layout, "theme": theme, "locale": company.locale, "seed": seed,
            "doc_id": doc_id_for(key, variant, company_slug, seed)}


def allocate(count: int, capacity: dict[str, int]) -> dict[str, int]:
    """Split count evenly over variants, capped by how many records each has (water-filling).
    Only if every variant is exhausted does the remainder go round again (records reused)."""
    alloc = {v: 0 for v in capacity}
    left = count
    open_ = [v for v in capacity if capacity[v] > 0]
    while left > 0 and open_:
        share = max(1, left // len(open_))
        for v in list(open_):
            take = min(share, capacity[v] - alloc[v], left)
            alloc[v] += take
            left -= take
            if alloc[v] >= capacity[v]:
                open_.remove(v)
            if left == 0:
                break
    for i, v in enumerate(sorted(capacity, key=lambda v: -capacity[v])):   # more than all records
        alloc[v] += left // len(capacity) + (1 if i < left % len(capacity) else 0)
    return alloc


def plan(cfg: RunConfig, ws: Workspace) -> list[dict]:
    """Each spec: {type: invoice | "*", count: N | all, sources?, companies?, locales?}.

    A type expands to every source that supports it. count is split evenly over those variants
    (capped by their record counts) and records are drawn without repeats; "all" means every
    record of every variant exactly once. Records are reused only when count exceeds what a
    variant has, and then get a different company and seed."""
    if not ws.companies:
        raise SystemExit(f"no companies in {cfg.resolve(cfg.companies_dir)}; run: python -m docgen companies init")
    jobs = []
    for spec in cfg.documents:
        variants = registry.variants(spec["type"], spec.get("sources"))
        if not variants:
            raise ValueError(f"no document variants for {spec}")
        pools = {v: ws.companies_for(registry.get(v), spec.get("companies"), spec.get("locales")) for v in variants}
        missing = [v for v, p in pools.items() if not p]
        if missing:
            sectors = sorted({source_classes()[registry.get(v).source].sector for v in missing})
            raise SystemExit(f"no companies for {missing} (sector {', '.join(sectors)}); "
                             f"run: python -m docgen companies init --sectors {' '.join(sectors)}")
        rng = random.Random(f"{cfg.seed}:{spec['type']}:{','.join(variants)}")
        capacity = {v: len(ws.keys(registry.get(v))) for v in variants}
        count = spec.get("count", 10)
        alloc = dict(capacity) if count == "all" else allocate(int(count), capacity)
        for variant in variants:
            keys = list(ws.keys(registry.get(variant)))
            order: list = []
            for _ in range(alloc[variant]):
                if not order:                       # (re)shuffle: no repeats until exhausted
                    order = keys[:]
                    rng.shuffle(order)
                jobs.append(make_job(ws, variant, order.pop(), rng.choice(pools[variant]), rng.getrandbits(32)))
    return jobs


_WS: Workspace | None = None


def _init_worker(cfg: RunConfig):
    global _WS
    _WS = Workspace(cfg)


def render_job(job: dict, ws: Workspace | None = None) -> dict:
    ws = ws or _WS
    cfg = ws.cfg
    out = cfg.resolve(cfg.out_dir)
    gold_path = out / "gold" / job["type"] / f"{job['doc_id']}.json"
    words_path = out / "words" / job["type"] / f"{job['doc_id']}.json"
    if cfg.resume and gold_path.exists() and (out / "pdf" / job["type"] / f"{job['doc_id']}.pdf").exists() \
            and (words_path.exists() or not cfg.boxes):
        try:                                      # files are written pdf -> gold -> words; all must be whole
            pages = json.loads(gold_path.read_text()).get("pages")
            if cfg.boxes:
                json.loads(words_path.read_text())
            return {**job, "status": "ok", "pages": pages, "pdf": f"pdf/{job['type']}/{job['doc_id']}.pdf",
                    "resumed": True}
        except (OSError, json.JSONDecodeError):
            pass                                  # half-written file: render again
    try:
        doc, record, ctx, html = ws.build(job)
        pdf = Renderer.html_to_pdf(html, identifier=job["doc_id"])
        required = doc.required_strings(record, ctx)
        result = None
        if cfg.self_check or cfg.boxes:
            result = check_pdf(pdf, required, doc.validate(record), cfg.boxes)
            if cfg.self_check and not result.ok:
                return {**job, "status": "failed", "missing": result.missing, "arithmetic": result.arithmetic}
        stem = f"{job['type']}/{job['doc_id']}"
        for sub in ("pdf", "gold") + (("words",) if cfg.boxes else ()) + (("html",) if cfg.keep_html else ()):
            (out / sub / job["type"]).mkdir(parents=True, exist_ok=True)
        (out / "pdf" / f"{stem}.pdf").write_bytes(pdf)
        gold = {
            "doc_id": job["doc_id"], "doc_type": job["type"], "variant": job["variant"], "source": job["source"],
            "source_key": job["key"], "company": job["company"], "layout": job["layout"], "theme": job["theme"],
            "locale": job["locale"], "seed": job["seed"], "options": ctx.options,
            "pages": result.pages if result else None,
            "page_sizes": result.page_sizes if result else [], "fields": record.to_gold(), "rendered": required,
            "boxes": result.boxes if result and cfg.boxes else {},
        }
        compact = {"ensure_ascii": False, "separators": (",", ":"), "default": str}
        (out / "gold" / f"{stem}.json").write_text(json.dumps(gold, **compact))
        if cfg.boxes and result:
            (out / "words" / f"{stem}.json").write_text(json.dumps(result.words, **compact))
        if cfg.keep_html:
            (out / "html" / f"{stem}.html").write_text(html, encoding="utf-8")
        return {**job, "status": "ok", "pages": gold["pages"], "pdf": f"pdf/{stem}.pdf"}
    except Exception as exc:  # a bad document must not kill the run
        return {**job, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=-3)}


def run(cfg: RunConfig, log=print) -> dict:
    ws = Workspace(cfg)
    jobs = plan(cfg, ws)
    out = cfg.resolve(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.yaml").write_text(yaml.safe_dump(cfg.__dict__, sort_keys=False, allow_unicode=True))
    log(f"[plan] {len(jobs)} documents -> {out}")

    started = time.time()
    if cfg.workers > 1:
        with ProcessPoolExecutor(cfg.workers, initializer=_init_worker, initargs=(cfg,)) as pool:
            results = list(pool.map(render_job, jobs, chunksize=4))
    else:
        results = [render_job(job, ws) for job in jobs]

    counts: dict[str, int] = {}
    per_variant: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    tmp = out / "manifest.jsonl.tmp"            # written aside, then renamed: never left half-written
    with tmp.open("w", encoding="utf-8") as fh:
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            per_variant[r["variant"]][r["status"]] += 1
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            if r["status"] != "ok":
                log(f"[{r['status']}] {r['variant']} key={r['key']} "
                    f"{r.get('error') or r.get('missing') or r.get('arithmetic')}")
    tmp.replace(out / "manifest.jsonl")
    for variant, c in sorted(per_variant.items()):
        log(f"  {variant:<36} " + " ".join(f"{k}={v}" for k, v in sorted(c.items())))
    log(f"[done] {counts} in {time.time() - started:.1f}s")
    return counts
