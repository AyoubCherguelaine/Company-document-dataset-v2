"""Source databases. Each module defines one SQLiteSource subclass."""

from __future__ import annotations

from pathlib import Path

from .. import REPO_ROOT

_P1 = REPO_ROOT / "data/sample_dbs_part1_northwind_chinook_sakila/sqlite"
DEFAULT_PATHS = {
    "northwind": _P1 / "northwind.db",
    "adventureworks": REPO_ROOT / "data/sample_dbs_part2_adventureworks/sqlite/adventureworks.db",
    "chinook": _P1 / "chinook.db",
    "sakila": _P1 / "sakila.db",
}
# Sakila's activity is 2005-2006; move it next to the other sources.
DEFAULT_DATE_SHIFT = {"sakila": 19}


def source_classes() -> dict[str, type]:
    from .adventureworks import AdventureWorksSource
    from .chinook import ChinookSource
    from .northwind import NorthwindSource
    from .sakila import SakilaSource

    return {c.name: c for c in (NorthwindSource, AdventureWorksSource, ChinookSource, SakilaSource)}


def open_source(name: str, path: str | Path | None = None, date_shift_years: int | None = None):
    cls = source_classes()[name]
    shift = DEFAULT_DATE_SHIFT.get(name, 0) if date_shift_years is None else date_shift_years
    return cls(Path(path) if path else DEFAULT_PATHS[name], shift)
