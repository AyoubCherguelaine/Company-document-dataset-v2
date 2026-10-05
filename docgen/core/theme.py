"""Themes: fonts, colours, table style and spacing, exposed to templates as CSS variables."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from markupsafe import Markup

DEFAULT_COLORS = {
    "primary": "#1f2937",
    "accent": "#2563eb",
    "text": "#111827",
    "muted": "#6b7280",
    "border": "#d1d5db",
    "header_bg": "#1f2937",
    "header_fg": "#ffffff",
    "stripe": "#f3f4f6",
    "surface": "#f9fafb",
}


@dataclass
class Theme:
    name: str
    font_family: str = "'DejaVu Sans', sans-serif"
    heading_font: str = ""
    base_size: str = "9pt"
    title_size: str = "22pt"
    title_transform: str = "uppercase"
    table_style: str = "striped"       # striped | grid | lines | plain
    logo_shape: str = "circle"         # circle | square | rounded | none
    spacing: float = 1.0
    page_size: str = "A4"
    colors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.colors = {**DEFAULT_COLORS, **(self.colors or {})}
        self.heading_font = self.heading_font or self.font_family

    @classmethod
    def load(cls, path: Path) -> "Theme":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("name", path.stem)
        return cls(**data)

    def css_vars(self) -> Markup:
        lines = [f"--c-{k.replace('_', '-')}: {v};" for k, v in self.colors.items()]
        lines += [
            f"--font: {self.font_family};",
            f"--heading-font: {self.heading_font};",
            f"--base-size: {self.base_size};",
            f"--title-size: {self.title_size};",
            f"--title-transform: {self.title_transform};",
            f"--gap: {round(6 * self.spacing, 2)}mm;",
        ]
        # Theme files are trusted config; values go into <style>, so they must not be HTML-escaped.
        return Markup(":root {\n  " + "\n  ".join(lines) + "\n}")


def load_themes(directories: list[Path]) -> dict[str, Theme]:
    themes = {}
    for directory in directories:
        for path in sorted(directory.glob("*.yaml")):
            themes[path.stem] = Theme.load(path)
    return themes
