"""Jinja2 -> HTML -> WeasyPrint PDF."""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, pass_context, select_autoescape
from markupsafe import Markup

from .. import PACKAGE_DIR

SHARED_TEMPLATES = PACKAGE_DIR / "templates"

logging.getLogger("weasyprint").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="HarfBuzz-Subset", category=DeprecationWarning)


@pass_context
def _money(ctx, value, currency=None):
    return ctx["loc"].money(value, currency or ctx["currency"])


@pass_context
def _date(ctx, value, fmt=None):
    return ctx["loc"].date(value, fmt)


@pass_context
def _num(ctx, value, places=2):
    return ctx["loc"].number(value, places)


@pass_context
def _pct(ctx, value):
    return ctx["loc"].percent(value)


def _css_str(value) -> Markup:
    """Quote text for a CSS content: property (used in @page margin boxes)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ").replace("\n", " ")
    return Markup(f'"{escaped}"')


def logo_svg(party, theme, size: int = 46) -> Markup:
    """A monogram logo, so each synthetic issuer has a recognisable mark without image assets."""
    brand = party.brand or {}
    shape = theme.logo_shape if theme.logo_shape != "auto" else brand.get("shape", "circle")
    if shape == "none":
        return Markup("")
    color = brand.get("color", theme.colors["primary"])
    initials = brand.get("initials", party.name[:2].upper())
    r = {"circle": size / 2, "rounded": size / 5, "square": 0}.get(shape, size / 2)
    return Markup(
        f'<svg class="logo" width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg"><rect width="{size}" height="{size}" rx="{r}" fill="{color}"/>'
        f'<text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle" fill="#fff" '
        f'font-family="DejaVu Sans" font-weight="bold" font-size="{size * 0.4}">{initials}</text></svg>'
    )


class Renderer:
    def __init__(self, extra_dirs: list[Path] | None = None):
        dirs = [str(d) for d in (extra_dirs or [])] + [str(SHARED_TEMPLATES)]
        self.env = Environment(
            loader=FileSystemLoader(dirs),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters.update(money=_money, date=_date, num=_num, pct=_pct, css_str=_css_str)
        self.env.globals.update(logo_svg=logo_svg)

    def layouts(self) -> list[str]:
        return sorted(Path(n).name.removesuffix(".html.j2") for n in self.env.list_templates()
                      if n.startswith("layouts/") and n.endswith(".html.j2"))

    def render_html(self, candidates: list[str], context: dict) -> str:
        return self.env.select_template(candidates).render(**context)

    @staticmethod
    def html_to_pdf(html: str, base_url: Path | None = None, identifier: str = "") -> bytes:
        """Reproducible output: dates come from the <meta dcterms.*> tags in base.html.j2 and the
        PDF /ID from the doc id, so the same job always yields the same bytes."""
        from weasyprint import HTML  # imported lazily: slow import, not needed for --html-only

        return HTML(string=html, base_url=str(base_url or SHARED_TEMPLATES)).write_pdf(
            pdf_identifier=(identifier or "docgen").encode())
