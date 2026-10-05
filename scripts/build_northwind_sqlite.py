#!/usr/bin/env python3
"""Convert Microsoft's T-SQL Northwind script (instnwnd.sql) into a SQLite database.

The T-SQL DDL is not portable, so the schema below is written by hand for SQLite
(same table and column names as the original). Only the INSERT statements are
parsed out of the source script, with T-SQL literals converted on the way:

    N'text'  -> 'text'          ('' escapes are honoured)
    0xABCD   -> BLOB
    'm/d/yyyy' in date columns -> 'yyyy-mm-dd'

Usage:
    python scripts/build_northwind_sqlite.py [--src PATH] [--out PATH]
"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = REPO_ROOT / "data/northwind/samples/databases/northwind-pubs/instnwnd.sql"
DEFAULT_OUT = REPO_ROOT / "data/northwind.db"

SCHEMA = """
PRAGMA foreign_keys = OFF;

CREATE TABLE "Categories" (
    "CategoryID"   INTEGER PRIMARY KEY,
    "CategoryName" TEXT NOT NULL,
    "Description"  TEXT,
    "Picture"      BLOB
);

CREATE TABLE "Customers" (
    "CustomerID"   TEXT PRIMARY KEY,
    "CompanyName"  TEXT NOT NULL,
    "ContactName"  TEXT,
    "ContactTitle" TEXT,
    "Address"      TEXT,
    "City"         TEXT,
    "Region"       TEXT,
    "PostalCode"   TEXT,
    "Country"      TEXT,
    "Phone"        TEXT,
    "Fax"          TEXT
);

CREATE TABLE "Employees" (
    "EmployeeID"      INTEGER PRIMARY KEY,
    "LastName"        TEXT NOT NULL,
    "FirstName"       TEXT NOT NULL,
    "Title"           TEXT,
    "TitleOfCourtesy" TEXT,
    "BirthDate"       DATE,
    "HireDate"        DATE,
    "Address"         TEXT,
    "City"            TEXT,
    "Region"          TEXT,
    "PostalCode"      TEXT,
    "Country"         TEXT,
    "HomePhone"       TEXT,
    "Extension"       TEXT,
    "Photo"           BLOB,
    "Notes"           TEXT,
    "ReportsTo"       INTEGER REFERENCES "Employees"("EmployeeID"),
    "PhotoPath"       TEXT
);

CREATE TABLE "Shippers" (
    "ShipperID"   INTEGER PRIMARY KEY,
    "CompanyName" TEXT NOT NULL,
    "Phone"       TEXT
);

CREATE TABLE "Suppliers" (
    "SupplierID"   INTEGER PRIMARY KEY,
    "CompanyName"  TEXT NOT NULL,
    "ContactName"  TEXT,
    "ContactTitle" TEXT,
    "Address"      TEXT,
    "City"         TEXT,
    "Region"       TEXT,
    "PostalCode"   TEXT,
    "Country"      TEXT,
    "Phone"        TEXT,
    "Fax"          TEXT,
    "HomePage"     TEXT
);

CREATE TABLE "Products" (
    "ProductID"       INTEGER PRIMARY KEY,
    "ProductName"     TEXT NOT NULL,
    "SupplierID"      INTEGER REFERENCES "Suppliers"("SupplierID"),
    "CategoryID"      INTEGER REFERENCES "Categories"("CategoryID"),
    "QuantityPerUnit" TEXT,
    "UnitPrice"       NUMERIC DEFAULT 0 CHECK ("UnitPrice" >= 0),
    "UnitsInStock"    INTEGER DEFAULT 0 CHECK ("UnitsInStock" >= 0),
    "UnitsOnOrder"    INTEGER DEFAULT 0 CHECK ("UnitsOnOrder" >= 0),
    "ReorderLevel"    INTEGER DEFAULT 0 CHECK ("ReorderLevel" >= 0),
    "Discontinued"    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE "Orders" (
    "OrderID"        INTEGER PRIMARY KEY,
    "CustomerID"     TEXT REFERENCES "Customers"("CustomerID"),
    "EmployeeID"     INTEGER REFERENCES "Employees"("EmployeeID"),
    "OrderDate"      DATE,
    "RequiredDate"   DATE,
    "ShippedDate"    DATE,
    "ShipVia"        INTEGER REFERENCES "Shippers"("ShipperID"),
    "Freight"        NUMERIC DEFAULT 0,
    "ShipName"       TEXT,
    "ShipAddress"    TEXT,
    "ShipCity"       TEXT,
    "ShipRegion"     TEXT,
    "ShipPostalCode" TEXT,
    "ShipCountry"    TEXT
);

CREATE TABLE "Order Details" (
    "OrderID"   INTEGER NOT NULL REFERENCES "Orders"("OrderID"),
    "ProductID" INTEGER NOT NULL REFERENCES "Products"("ProductID"),
    "UnitPrice" NUMERIC NOT NULL DEFAULT 0 CHECK ("UnitPrice" >= 0),
    "Quantity"  INTEGER NOT NULL DEFAULT 1 CHECK ("Quantity" > 0),
    "Discount"  REAL NOT NULL DEFAULT 0 CHECK ("Discount" BETWEEN 0 AND 1),
    PRIMARY KEY ("OrderID", "ProductID")
);

CREATE TABLE "CustomerDemographics" (
    "CustomerTypeID" TEXT PRIMARY KEY,
    "CustomerDesc"   TEXT
);

CREATE TABLE "CustomerCustomerDemo" (
    "CustomerID"     TEXT NOT NULL REFERENCES "Customers"("CustomerID"),
    "CustomerTypeID" TEXT NOT NULL REFERENCES "CustomerDemographics"("CustomerTypeID"),
    PRIMARY KEY ("CustomerID", "CustomerTypeID")
);

CREATE TABLE "Region" (
    "RegionID"          INTEGER PRIMARY KEY,
    "RegionDescription" TEXT NOT NULL
);

CREATE TABLE "Territories" (
    "TerritoryID"          TEXT PRIMARY KEY,
    "TerritoryDescription" TEXT NOT NULL,
    "RegionID"             INTEGER NOT NULL REFERENCES "Region"("RegionID")
);

CREATE TABLE "EmployeeTerritories" (
    "EmployeeID"  INTEGER NOT NULL REFERENCES "Employees"("EmployeeID"),
    "TerritoryID" TEXT NOT NULL REFERENCES "Territories"("TerritoryID"),
    PRIMARY KEY ("EmployeeID", "TerritoryID")
);

CREATE INDEX "idx_orders_customer"   ON "Orders"("CustomerID");
CREATE INDEX "idx_orders_employee"   ON "Orders"("EmployeeID");
CREATE INDEX "idx_orders_shipvia"    ON "Orders"("ShipVia");
CREATE INDEX "idx_details_product"   ON "Order Details"("ProductID");
CREATE INDEX "idx_products_supplier" ON "Products"("SupplierID");
CREATE INDEX "idx_products_category" ON "Products"("CategoryID");
"""

# Northwind's handy views, rewritten in SQLite syntax.
VIEWS = """
CREATE VIEW "Order Subtotals" AS
SELECT "OrderID",
       ROUND(SUM("UnitPrice" * "Quantity" * (1 - "Discount")), 2) AS "Subtotal"
FROM "Order Details"
GROUP BY "OrderID";

CREATE VIEW "Invoices" AS
SELECT o."ShipName", o."ShipAddress", o."ShipCity", o."ShipRegion", o."ShipPostalCode",
       o."ShipCountry", o."CustomerID", c."CompanyName" AS "CustomerName",
       c."Address", c."City", c."Region", c."PostalCode", c."Country",
       e."FirstName" || ' ' || e."LastName" AS "Salesperson",
       o."OrderID", o."OrderDate", o."RequiredDate", o."ShippedDate",
       s."CompanyName" AS "ShipperName",
       d."ProductID", p."ProductName", d."UnitPrice", d."Quantity", d."Discount",
       ROUND(d."UnitPrice" * d."Quantity" * (1 - d."Discount"), 2) AS "ExtendedPrice",
       o."Freight"
FROM "Orders" o
JOIN "Customers" c       ON c."CustomerID" = o."CustomerID"
JOIN "Employees" e       ON e."EmployeeID" = o."EmployeeID"
JOIN "Shippers" s        ON s."ShipperID"  = o."ShipVia"
JOIN "Order Details" d   ON d."OrderID"    = o."OrderID"
JOIN "Products" p        ON p."ProductID"  = d."ProductID";
"""

DATE_COLUMNS = {
    "Employees": {"BirthDate", "HireDate"},
    "Orders": {"OrderDate", "RequiredDate", "ShippedDate"},
}

# Matches the head of every INSERT form used in the script:
#   INSERT "T"(...) VALUES(...)   INSERT INTO "T" (...) VALUES (...)   Insert Into T Values (...)
INSERT_HEAD = re.compile(
    r'\bINSERT\s+(?:INTO\s+)?(?:\[?dbo\]?\.)?("([^"]+)"|\[([^\]]+)\]|(\w+))\s*(\([^)]*\))?\s*VALUES\s*\(',
    re.IGNORECASE,
)
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
HEX = re.compile(r"0x([0-9A-Fa-f]*)")


class ParseError(Exception):
    pass


def parse_values(text, pos):
    """Parse a T-SQL VALUES tuple starting just after '('. Returns (values, end_pos)."""
    values = []
    n = len(text)
    while True:
        while pos < n and text[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            raise ParseError("unterminated VALUES tuple")
        ch = text[pos]

        if ch in "Nn" and pos + 1 < n and text[pos + 1] == "'":
            pos += 1
            ch = "'"
        if ch == "'":
            pos += 1
            buf = []
            while True:
                end = text.find("'", pos)
                if end < 0:
                    raise ParseError("unterminated string literal")
                buf.append(text[pos:end])
                if end + 1 < n and text[end + 1] == "'":
                    buf.append("'")
                    pos = end + 2
                    continue
                pos = end + 1
                break
            values.append("".join(buf))
        elif text.startswith(("0x", "0X"), pos):
            m = HEX.match(text, pos)
            values.append(bytes.fromhex(m.group(1)))
            pos = m.end()
        elif text[pos:pos + 4].upper() == "NULL":
            values.append(None)
            pos += 4
        else:
            m = NUMBER.match(text, pos)
            if not m:
                raise ParseError(f"unexpected token near: {text[pos:pos + 40]!r}")
            raw = m.group(0)
            values.append(float(raw) if any(c in raw for c in ".eE") else int(raw))
            pos = m.end()

        while pos < n and text[pos] in " \t\r\n":
            pos += 1
        if text[pos] == ",":
            pos += 1
        elif text[pos] == ")":
            return values, pos + 1
        else:
            raise ParseError(f"expected ',' or ')' near: {text[pos:pos + 40]!r}")


def to_iso_date(value):
    if value is None or not isinstance(value, str):
        return value
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise ParseError(f"unrecognised date: {value!r}")


def iter_inserts(sql_text):
    pos = 0
    while True:
        m = INSERT_HEAD.search(sql_text, pos)
        if not m:
            return
        table = m.group(2) or m.group(3) or m.group(4)
        cols = None
        if m.group(5):
            cols = [c.strip().strip('"[]') for c in m.group(5)[1:-1].split(",")]
        values, pos = parse_values(sql_text, m.end())
        yield table, cols, values


def table_columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def build(src, out):
    sql_text = src.read_text(encoding="utf-8", errors="strict")
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(out)
    conn.executescript(SCHEMA)

    col_cache = {}
    counts = {}
    for table, cols, values in iter_inserts(sql_text):
        if table not in col_cache:
            col_cache[table] = table_columns(conn, table)
            if not col_cache[table]:
                raise ParseError(f"INSERT into unknown table {table!r}")
        cols = cols or col_cache[table][: len(values)]
        if len(cols) != len(values):
            raise ParseError(f"{table}: {len(cols)} columns but {len(values)} values")
        date_cols = DATE_COLUMNS.get(table, set())
        values = [to_iso_date(v) if c in date_cols else v for c, v in zip(cols, values)]
        if table in ("Territories", "Region", "CustomerDemographics"):
            # nchar columns are space-padded in the source
            values = [v.rstrip() if isinstance(v, str) else v for v in values]
        col_sql = ", ".join(f'"{c}"' for c in cols)
        marks = ", ".join("?" * len(values))
        conn.execute(f'INSERT INTO "{table}" ({col_sql}) VALUES ({marks})', values)
        counts[table] = counts.get(table, 0) + 1

    conn.executescript(VIEWS)
    conn.commit()

    conn.execute("PRAGMA foreign_keys = ON")
    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise RuntimeError(f"foreign key violations: {fk_errors[:5]} ...")
    conn.execute("VACUUM")
    conn.close()
    return counts


EXPECTED = {
    "Categories": 8, "Customers": 91, "Employees": 9, "Shippers": 3, "Suppliers": 29,
    "Products": 77, "Orders": 830, "Order Details": 2155,
    "Region": 4, "Territories": 53, "EmployeeTerritories": 49,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    counts = build(args.src, args.out)
    ok = True
    for table, expected in EXPECTED.items():
        got = counts.get(table, 0)
        flag = "ok" if got == expected else "MISMATCH"
        ok &= got == expected
        print(f"  {table:<22} {got:>5}  {flag}")
    print(f"[done] {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
