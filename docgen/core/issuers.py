"""Synthetic issuer companies.

Northwind has a single seller, so every header would be identical. Issuers are
generated deterministically from a seed instead; Northwind customers and
suppliers act as counterparties. Each issuer belongs to a country whose
conventions (tax id, bank format, phone) match the document locale.
"""

from __future__ import annotations

import random

from .models import Address, Party

NAME_A = ["Atlas", "Boreal", "Cobalt", "Delta", "Ember", "Fjord", "Granite", "Harbor", "Iris",
          "Juniper", "Keystone", "Lumen", "Meridian", "Northgate", "Orchard", "Pioneer",
          "Quarry", "Riverside", "Summit", "Tidewater", "Union", "Vantage", "Willow", "Zenith"]
NAME_B = ["Trading", "Foods", "Distribution", "Provisions", "Supply", "Imports", "Logistics",
          "Wholesale", "Gourmet", "Commerce", "Partners", "Merchants"]
# Second name word per sector, so a bike maker doesn't sound like a grocer.
SECTOR_NAME_B = {
    "food": NAME_B,
    "bikes": ["Cycles", "Bikes", "Cycling", "Sports", "Outdoor", "Components", "Wheelworks", "Velo"],
    "music": ["Music", "Records", "Sound", "Audio", "Media", "Tunes", "Recordings", "Beats"],
    "video": ["Video", "Movies", "Cinema", "Rentals", "Screen", "Entertainment", "Films", "Reels"],
}

COUNTRIES = {
    "US": {
        "locales": ["en"], "forms": ["Inc.", "LLC", "Corp.", "Co."],
        "streets": ["Market St", "Elm Avenue", "Harbor Blvd", "Oak Street", "Industrial Pkwy", "5th Avenue"],
        "cities": [("Portland", "OR", "97205"), ("Austin", "TX", "78701"), ("Boston", "MA", "02110"),
                   ("Denver", "CO", "80202"), ("Chicago", "IL", "60606")],
        "country": "USA", "phone": "({a}) 555-{b:04d}", "tax": "EIN {n2}-{n7}",
        "domain": "com",
    },
    "UK": {
        "locales": ["en"], "forms": ["Ltd", "PLC", "& Co. Ltd"],
        "streets": ["High Street", "King's Road", "Victoria Street", "Mill Lane", "Station Road"],
        "cities": [("London", "", "EC1A 1BB"), ("Manchester", "", "M1 2AB"), ("Bristol", "", "BS1 4DJ"),
                   ("Leeds", "", "LS1 5AA")],
        "country": "United Kingdom", "phone": "+44 20 {a}{b:04d}", "tax": "VAT GB{n9}",
        "domain": "co.uk",
    },
    "FR": {
        "locales": ["fr"], "forms": ["SARL", "SAS", "SA"],
        "streets": ["rue de la République", "avenue Jean Jaurès", "boulevard Voltaire",
                    "rue du Commerce", "quai des Chartrons"],
        "cities": [("Lyon", "", "69002"), ("Bordeaux", "", "33000"), ("Lille", "", "59000"),
                   ("Nantes", "", "44000"), ("Paris", "", "75011")],
        "country": "France", "phone": "+33 4 {a2} {b2} {c2} {d2}", "tax": "TVA FR{n2}{n9}",
        "domain": "fr",
    },
    "BE": {
        "locales": ["fr"], "forms": ["SPRL", "SA", "SRL"],
        "streets": ["rue Neuve", "avenue Louise", "chaussée de Wavre", "rue des Bouchers"],
        "cities": [("Bruxelles", "", "1000"), ("Liège", "", "4000"), ("Namur", "", "5000")],
        "country": "Belgique", "phone": "+32 2 {a3} {b2} {c2}", "tax": "TVA BE0{n9}",
        "domain": "be",
    },
}

BRAND_COLORS = ["#1f4e79", "#0f766e", "#7c2d12", "#4c1d95", "#14532d", "#9d174d", "#1e3a8a", "#78350f"]


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def make_issuer(rng: random.Random, country_code: str, sector: str = "food") -> Party:
    c = COUNTRIES[country_code]
    a, b = rng.choice(NAME_A), rng.choice(SECTOR_NAME_B.get(sector, NAME_B))
    name = f"{a} {b} {rng.choice(c['forms'])}"
    city, region, postal = rng.choice(c["cities"])
    number = rng.randint(2, 480)
    line1 = f"{number} {rng.choice(c['streets'])}" if country_code in ("US", "UK") else \
        f"{number}, {rng.choice(c['streets'])}"
    line2 = rng.choice(["", "", f"Suite {rng.randint(100, 900)}", f"Building {rng.choice('ABCD')}"])
    slug = f"{a}{b}".lower()
    fill = {"a": rng.randint(200, 989), "b": rng.randint(0, 9999), "a2": _digits(rng, 2),
            "b2": _digits(rng, 2), "c2": _digits(rng, 2), "d2": _digits(rng, 2), "a3": _digits(rng, 3),
            "n2": _digits(rng, 2), "n7": _digits(rng, 7), "n9": _digits(rng, 9)}
    bank = {"bank_name": rng.choice(["First Commerce Bank", "Banque Populaire du Centre",
                                     "Crédit Régional", "Northern Trust Bank", "City Savings Bank"])}
    if country_code == "US":
        bank.update(account=_digits(rng, 10), routing=_digits(rng, 9))
    else:
        cc = "GB" if country_code == "UK" else country_code
        bank.update(iban=f"{cc}{_digits(rng, 2)} {' '.join(_digits(rng, 4) for _ in range(5))}",
                    bic=f"{a[:4].upper()}{cc}{_digits(rng, 2)}")
    return Party(
        name=name,
        address=Address(line1=line1, line2=line2, city=city, region=region, postal_code=postal,
                        country=c["country"]),
        phone=c["phone"].format(**fill),
        email=f"{rng.choice(['billing', 'accounts', 'sales', 'contact'])}@{slug}.{c['domain']}",
        website=f"www.{slug}.{c['domain']}",
        tax_id=c["tax"].format(**fill),
        code=country_code,
        bank=bank,
        brand={"initials": (a[0] + b[0]).upper(), "color": rng.choice(BRAND_COLORS),
               "shape": rng.choice(["circle", "square", "rounded"])},
    )


class IssuerPool:
    """A fixed, seeded set of issuers, grouped by the locales they can issue in."""

    def __init__(self, seed: int, per_country: int = 3):
        rng = random.Random(f"issuers:{seed}")
        self.by_locale: dict[str, list[Party]] = {}
        for code, cfg in COUNTRIES.items():
            for _ in range(per_country):
                issuer = make_issuer(rng, code)
                for loc in cfg["locales"]:
                    self.by_locale.setdefault(loc, []).append(issuer)

    def pick(self, rng: random.Random, locale: str) -> Party:
        pool = self.by_locale.get(locale) or [p for ps in self.by_locale.values() for p in ps]
        return rng.choice(pool)
