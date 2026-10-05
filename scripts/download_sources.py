#!/usr/bin/env python3
"""Download the four source databases and build them as SQLite files (standard library only).

    northwind       jpwhite3/northwind-SQLite3 (extended, 16k orders)         ready-made SQLite
    chinook         lerocha/chinook-database release v1.4.5                   ready-made SQLite
    sakila          jOOQ/sakila sqlite-sakila-db/*.sql                         built from SQL scripts
    adventureworks  microsoft/sql-server-samples oltp-install-script (CSV)     built from CSV + DDL

Files land where docgen expects them (docgen/sources/__init__.py:DEFAULT_PATHS). Existing files
are kept unless --force. Licenses are downloaded next to the databases.

    python scripts/download_sources.py [--force] [--only northwind chinook ...]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PART1 = REPO_ROOT / "data/sample_dbs_part1_northwind_chinook_sakila"
PART2 = REPO_ROOT / "data/sample_dbs_part2_adventureworks"

TARGETS = {
    "northwind": PART1 / "sqlite/northwind.db",
    "chinook": PART1 / "sqlite/chinook.db",
    "sakila": PART1 / "sqlite/sakila.db",
    "adventureworks": PART2 / "sqlite/adventureworks.db",
}
NORTHWIND_URL = "https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/main/dist/northwind.db"
CHINOOK_URL = "https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite"
SAKILA_URL = "https://raw.githubusercontent.com/jOOQ/sakila/main/sqlite-sakila-db/{}"
AW_REPO = "https://github.com/microsoft/sql-server-samples"
AW_PATH = "samples/databases/adventure-works/oltp-install-script"
LICENSES = {
    PART1 / "licenses/northwind-sqlite3_MIT.txt": "https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/main/LICENSE",
    PART1 / "licenses/chinook_MIT.md": "https://raw.githubusercontent.com/lerocha/chinook-database/master/LICENSE.md",
    PART1 / "licenses/sakila_BSD-2.txt": "https://raw.githubusercontent.com/jOOQ/sakila/main/LICENSE",
    PART2 / "licenses/adventureworks_MIT.txt": "https://raw.githubusercontent.com/microsoft/sql-server-samples/master/license.txt",
}


def log(msg: str) -> None:
    print(f"[download] {msg}", flush=True)


def fetch(url: str, dest: Path, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "companydocuments-docgen"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh, 1 << 20)
            tmp.replace(dest)
            return
        except Exception as exc:  # network hiccup: retry, then give up loudly
            if attempt == retries - 1:
                raise RuntimeError(f"download failed: {url}: {exc}") from exc
            time.sleep(2 ** attempt)


def check_sqlite(path: Path, table: str, minimum: int) -> int:
    conn = sqlite3.connect(path)
    try:
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()
    if n < minimum:
        raise RuntimeError(f"{path.name}: {table} has {n} rows, expected >= {minimum}")
    return n


# ------------------------------------------------------------------ sources ----
def northwind(dest: Path) -> None:
    fetch(NORTHWIND_URL, dest)
    log(f"northwind: {check_sqlite(dest, 'Orders', 16000)} orders")


def chinook(dest: Path) -> None:
    fetch(CHINOOK_URL, dest)
    log(f"chinook: {check_sqlite(dest, 'Invoice', 400)} invoices")


def sakila(dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "sakila.db"
        conn = sqlite3.connect(tmp_db)
        # ~47k single INSERT statements: without this every one is synced to disk (minutes instead of seconds)
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA journal_mode = MEMORY")
        for name in ("sqlite-sakila-schema.sql", "sqlite-sakila-insert-data.sql"):
            fetch(SAKILA_URL.format(name), Path(tmp) / name)
            conn.executescript((Path(tmp) / name).read_text(encoding="utf-8", errors="replace"))
        conn.commit()
        conn.close()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp_db, dest)
    log(f"sakila: {check_sqlite(dest, 'payment', 16000)} payments")


def adventureworks(dest: Path) -> None:
    """Sparse-clone the OLTP install script (CSV + T-SQL DDL) and load every table as text columns,
    like the original build (docgen.sources.adventureworks casts what it reads)."""
    if not shutil.which("git"):
        raise RuntimeError("git is required to download AdventureWorks")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "sql-server-samples"
        run = lambda *cmd, cwd=None: subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)  # noqa: E731
        run("git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", AW_REPO, str(repo))
        run("git", "sparse-checkout", "set", AW_PATH, cwd=repo)
        src = repo / AW_PATH
        script = (src / "instawdb.sql").read_text(encoding="utf-8-sig", errors="replace")

        tables = {}
        for m in re.finditer(r"CREATE TABLE \[(\w+)\]\.\[(\w+)\]\s*\((.*?)\)\s*ON \[PRIMARY\]", script, re.S):
            schema, name, body = m.groups()
            cols = []
            for line in body.split("\n"):
                cm = re.match(r"\s*\[(\w+)\]\s+(.*)", line)
                if cm and not re.match(r"AS\s*\(", cm.group(2).strip(), re.I):   # computed columns aren't in CSVs
                    cols.append(cm.group(1))
            tables[name] = (schema, cols)

        tmp_db = Path(tmp) / "adventureworks.db"
        conn = sqlite3.connect(tmp_db)
        loaded, skipped = 0, []
        bulk = r"BULK INSERT \[(\w+)\]\.\[(\w+)\] FROM '\$\(SqlSamplesSourceDataPath\)(\w+)\.csv'\s*WITH\s*\((.*?)\)"
        for m in re.finditer(bulk, script, re.S):
            schema, name, fname, opts = m.groups()
            if name not in tables:
                continue
            cols = tables[name][1]
            fterm = "+|" if "'+|'" in opts else "\t"
            rterm = "&|\n" if "&|" in opts else "\n"
            raw = (src / f"{fname}.csv").read_bytes()
            text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig", errors="replace")
            rows = [[c.strip("\r") for c in r.split(fterm)] for r in text.split(rterm) if r.strip()]
            good = [r[:len(cols)] for r in rows if len(r) >= len(cols)]
            table = f"{schema}_{name}"
            conn.execute(f'CREATE TABLE "{table}" (' + ", ".join(f'"{c}"' for c in cols) + ")")
            conn.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})',
                             [[None if v == "" else v for v in r] for r in good])
            loaded += 1
            if len(rows) != len(good):
                skipped.append((table, len(rows) - len(good)))
        conn.commit()
        conn.close()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp_db, dest)
    log(f"adventureworks: {loaded} tables, {check_sqlite(dest, 'Sales_SalesOrderHeader', 30000)} sales orders"
        + (f", rows skipped: {skipped}" if skipped else ""))


BUILDERS = {"northwind": northwind, "chinook": chinook, "sakila": sakila, "adventureworks": adventureworks}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download sources that already exist")
    ap.add_argument("--only", nargs="*", choices=sorted(BUILDERS), help="default: all four")
    args = ap.parse_args()

    failed = []
    for name in args.only or list(BUILDERS):
        dest = TARGETS[name]
        if dest.exists() and dest.stat().st_size > 0 and not args.force:
            log(f"{name}: present ({dest.stat().st_size // 1024:,} KB), skipped")
            continue
        log(f"{name}: downloading...")
        try:
            BUILDERS[name](dest)
        except Exception as exc:
            failed.append(name)
            log(f"{name}: FAILED: {exc}")
    for dest, url in LICENSES.items():
        if not dest.exists():
            try:
                fetch(url, dest)
            except Exception as exc:
                log(f"license {dest.name}: {exc} (add it by hand)")
    if failed:
        log(f"failed: {', '.join(failed)}")
        return 1
    log("all sources ready")
    return 0


if __name__ == "__main__":
    os.chdir(REPO_ROOT)
    sys.exit(main())
