"""Issuer companies, each with a fixed identity, design and texts.

Every company lives in companies/<slug>/:

    company.yaml          identity, design per document type, pinned options, texts
    templates/            optional; overrides any template for this company only,
                          e.g. templates/invoice/document.html.j2 or templates/layouts/classic.html.j2

A real company sends every invoice with the same letterhead, so layout, theme, brand colour,
numbering and wording are fixed per company. Diversity comes from having many companies.
"""

from __future__ import annotations

import itertools
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import REPO_ROOT
from .issuers import COUNTRIES, make_issuer
from .models import Address, Party

DEFAULT_DIR = REPO_ROOT / "companies"
COUNTRY_CURRENCY = {"US": "USD", "UK": "GBP", "FR": "EUR", "BE": "EUR"}

NUMBER_FORMATS = {
    "invoice": ["INV-{yyyy}-{key}", "INV{key:07d}", "F{yy}-{key}", "{yyyy}/{key}", "{initials}-{yy}{key:06d}"],
    "quote": ["Q-{yyyy}-{key}", "QT{key:06d}", "{initials}-DEV-{key}"],
    "credit_note": ["CN-{yyyy}-{key}", "AV{yy}{key:06d}", "{initials}-CR-{key}"],
    "purchase_order": ["PO-{yyyy}-{key}", "PO{key:06d}", "{initials}-PO-{key}"],
    "packing_slip": ["PS-{key}", "DN{yy}{key:06d}", "{initials}-BL-{key}"],
    "shipping_order": ["SO-{yyyy}-{key}", "SHP{key:07d}", "{initials}-EXP-{key}"],
    "goods_received_note": ["GRN-{key}", "GR{yy}{key:06d}", "{initials}-REC-{key}"],
    "account_statement": ["ST-{yyyy}-{key}", "STM{key:06d}", "{initials}-REL-{key}"],
    "inventory_report": ["INV-RPT-{yyyy}-{key}", "STK{yy}{key:05d}"],
    "receipt": ["R-{key}", "RCPT{key:07d}", "{initials}{yy}{key:06d}"],
    "work_order": ["WO-{key}", "OF{yy}{key:06d}", "{initials}-WO-{key}"],
    "payslip": ["PAY-{yyyy}-{key}", "BP{yy}{key:05d}"],
    "employment_certificate": ["HR-{yyyy}-{key}", "ATT{yy}{key:05d}"],
}

PINNABLE = ["show_bank", "show_contact", "show_signature", "show_tax_column", "show_discount",
            "show_item_details"]

SECTORS = {
    "food": "wholesale food and beverage distributor selling to shops and restaurants",
    "bikes": "bicycle and cycling equipment manufacturer and distributor",
    "music": "online digital music store selling tracks and albums",
    "video": "video rental store chain renting films to the public",
}

SECTOR_TEXTS = {   # sector-specific fallbacks layered over FALLBACK_TEXTS
    "bikes": {"en": {"tagline": "Built for the road ahead.", "about": "Manufacturer and distributor of bicycles, components and cycling gear."},
              "fr": {"tagline": "Conçus pour la route.", "about": "Fabricant et distributeur de vélos, composants et équipements de cyclisme."}},
    "music": {"en": {"tagline": "Every track, instantly.", "about": "Online store for digital music downloads."},
              "fr": {"tagline": "Toute la musique, tout de suite.", "about": "Boutique en ligne de musique numérique."}},
    "video": {"en": {"tagline": "Great films, every night.", "about": "Neighbourhood film rental stores."},
              "fr": {"tagline": "De bons films, chaque soir.", "about": "Magasins de location de films de quartier."}},
}

FALLBACK_TEXTS = {
    "en": {"tagline": "Quality food products, delivered.",
           "about": "Wholesale distributor of specialty foods and beverages.",
           "payment_instructions": "Please include the invoice number with your payment.",
           "footer": "Thank you for your business.",
           "notes": ["Thank you for your business.", "Goods remain our property until paid in full.",
                     "Please report any damaged items within 7 days of delivery."]},
    "fr": {"tagline": "Des produits de qualité, livrés.",
           "about": "Grossiste en produits alimentaires et boissons fines.",
           "payment_instructions": "Merci de rappeler le numéro de facture lors du règlement.",
           "footer": "Merci de votre confiance.",
           "notes": ["Merci de votre confiance.", "Marchandises livrées sous réserve de propriété.",
                     "Toute réclamation doit être faite sous 7 jours après livraison."]},
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Company:
    slug: str
    locale: str
    currency: str
    party: dict[str, Any]
    sector: str = "food"                                               # food | bikes | music | video
    brand: dict[str, Any] = field(default_factory=dict)
    design: dict[str, dict[str, str]] = field(default_factory=dict)   # doc_type|default -> layout, theme
    colors: dict[str, str] = field(default_factory=dict)               # override theme colours
    options: dict[str, Any] = field(default_factory=dict)              # pinned variation flags
    numbering: dict[str, str] = field(default_factory=dict)            # doc_type -> number format
    texts: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Company":
        data = yaml.safe_load((path / "company.yaml").read_text(encoding="utf-8"))
        return cls(**data, path=path)

    def save(self, root: Path) -> Path:
        self.path = root / self.slug
        self.path.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in self.__dict__.items() if k != "path"}
        (self.path / "company.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
        return self.path

    @property
    def template_dir(self) -> Path | None:
        d = self.path / "templates" if self.path else None
        return d if d and d.is_dir() else None

    def design_for(self, doc_type: str) -> dict[str, str]:
        return {**self.design.get("default", {}), **self.design.get(doc_type, {})}

    def number_format(self, doc_type: str) -> str | None:
        return self.numbering.get(doc_type)

    def text(self, key: str, default=""):
        if self.texts.get(key):
            return self.texts[key]
        sector = SECTOR_TEXTS.get(self.sector, {}).get(self.locale, {})
        return sector.get(key) or FALLBACK_TEXTS.get(self.locale, FALLBACK_TEXTS["en"]).get(key, default)

    def to_party(self) -> Party:
        p = dict(self.party)
        p["address"] = Address(**p.get("address", {}))
        return Party(**p, brand={**self.brand, "tagline": self.text("tagline")})


def load_companies(root: Path = DEFAULT_DIR) -> dict[str, Company]:
    if not root.exists():
        return {}
    return {d.name: Company.load(d) for d in sorted(root.iterdir()) if (d / "company.yaml").exists()}


# ---------------------------------------------------------------- creation ----
COMPANY_PROMPT = """Write the business texts for a fictional company: a {sector}.
Company: {name}
City/country: {city}, {country}
Language for every text: {language}
Return a JSON object with exactly these keys:
  "tagline": short slogan, max 8 words
  "about": one sentence describing what the company sells and to whom, max 25 words
  "payment_instructions": one sentence on how to pay an invoice, max 25 words
  "footer": one short closing line for documents, max 12 words
  "notes": list of 6 different short notes this company prints at the bottom of its invoices and receipts
           (thanks, delivery, returns, late-payment terms, warranty or care advice fitting the business...),
           each max 20 words
Do not use placeholders, brackets, emojis, or real brand names. Plain text only."""

LANGUAGE = {"en": "English", "fr": "French"}


def llm_texts(llm, company: Company) -> dict[str, Any]:
    a = company.party["address"]
    data = llm.chat_json(
        "You write concise, realistic business copy.",
        COMPANY_PROMPT.format(name=company.party["name"], sector=SECTORS.get(company.sector, company.sector),
                              city=a.get("city"), country=a.get("country"),
                              language=LANGUAGE.get(company.locale, company.locale)),
    )
    return clean_texts(data)


BATCH_PROMPT = """Write the business texts for each fictional company below. Each company writes in its own language.
Companies (slug | name | business | city, country | language):
{rows}
For every company return an object with exactly these keys:
  "tagline": short slogan, max 8 words
  "about": one sentence describing what the company sells and to whom, max 25 words
  "payment_instructions": one sentence on how to pay an invoice, max 25 words
  "footer": one short closing line for documents, max 12 words
  "notes": list of 6 different short notes printed at the bottom of its invoices and receipts
           (thanks, delivery, returns, late-payment terms, warranty or care advice fitting the business...),
           each max 20 words
Make each company sound different. No placeholders, brackets, emojis or real brand names. Plain text only.
Return one JSON object mapping each slug to its object."""


def llm_texts_batch(llm, companies: list[Company]) -> dict[str, dict[str, Any]]:
    """Texts for several companies in one request (free tiers allow ~50 requests a day)."""
    rows = "\n".join(
        f'{c.slug} | {c.party["name"]} | {SECTORS.get(c.sector, c.sector)} | '
        f'{c.party["address"].get("city")}, {c.party["address"].get("country")} | {LANGUAGE.get(c.locale, c.locale)}'
        for c in companies)
    data = llm.chat_json("You write concise, realistic business copy.", BATCH_PROMPT.format(rows=rows),
                         max_tokens=1200 * len(companies))
    return {c.slug: clean_texts(data[c.slug]) for c in companies if isinstance(data.get(c.slug), dict)}


def clean_texts(data: dict) -> dict[str, Any]:
    def one(v, limit):
        v = " ".join(str(v or "").split()).strip(' "')
        return v[:limit].rsplit(" ", 1)[0] if len(v) > limit else v

    out = {k: one(data.get(k), n) for k, n in
           (("tagline", 70), ("about", 220), ("payment_instructions", 220), ("footer", 120))}
    notes = data.get("notes") or []
    out["notes"] = [one(n, 180) for n in notes if isinstance(n, str) and n.strip()][:8]
    return {k: v for k, v in out.items() if v}


def rebalance_designs(companies: list[Company], layouts: list[str], themes: list[str], seed: int = 0) -> None:
    """Spread every layout x theme combination evenly over each sector's companies.
    Only design["default"] changes; per-type overrides, identity and texts are kept."""
    combos = list(itertools.product(layouts, themes))
    by_sector: dict[str, list[Company]] = {}
    for c in companies:
        by_sector.setdefault(c.sector, []).append(c)
    for n, (sector, group) in enumerate(sorted(by_sector.items())):
        order = combos[:]
        random.Random(f"rebalance:{seed}:{sector}").shuffle(order)
        for i, company in enumerate(sorted(group, key=lambda c: c.slug)):
            layout, theme = order[(i + n * 5) % len(order)]
            company.design["default"] = {"layout": layout, "theme": theme}


def create_companies(count: int, seed: int, layouts: list[str], themes: list[str],
                     locales: list[str] | None = None, llm=None, log=print, sector: str = "food",
                     taken: set[str] | None = None) -> list[Company]:
    """Seeded companies of one sector, with designs spread evenly over every layout x theme combination."""
    rng = random.Random(f"companies:{seed}" if sector == "food" else f"companies:{seed}:{sector}")
    countries = [c for c, cfg in COUNTRIES.items() if not locales or set(cfg["locales"]) & set(locales)]
    combos = list(itertools.product(layouts, themes))
    rng.shuffle(combos)
    companies, seen = [], set()
    taken = taken or set()
    for i in range(count):
        country = countries[i % len(countries)]
        party = make_issuer(rng, country, sector)
        # distinct first words (up to the 24 available) so letterheads don't look alike
        while (party.name.split()[0] in seen and len(seen) < 24) or slugify(party.name) in taken:
            party = make_issuer(rng, country, sector)
        seen.add(party.name.split()[0])
        taken.add(slugify(party.name))
        layout, theme = combos[i % len(combos)]
        locale = COUNTRIES[country]["locales"][0]
        initials = party.brand["initials"]
        company = Company(
            slug=slugify(party.name), locale=locale, currency=COUNTRY_CURRENCY[country], sector=sector,
            party={"name": party.name, "address": party.address.__dict__, "phone": party.phone,
                   "email": party.email, "website": party.website, "tax_id": party.tax_id,
                   "code": party.code, "bank": party.bank},
            brand={"initials": initials, "color": party.brand["color"], "shape": party.brand["shape"]},
            design={"default": {"layout": layout, "theme": theme}},
            colors={"primary": party.brand["color"]} if rng.random() < 0.5 else {},
            options={k: rng.random() < 0.5 for k in rng.sample(PINNABLE, 3)},
            numbering={t: rng.choice(f).replace("{initials}", initials) for t, f in NUMBER_FORMATS.items()},
            texts={},
        )
        if llm is not None and llm.enabled:
            try:
                company.texts = llm_texts(llm, company)
                log(f"[llm] texts for {company.slug}")
            except Exception as exc:      # keep going: fallback texts are used instead
                log(f"[llm] {company.slug}: {exc}")
        companies.append(company)
    return companies
