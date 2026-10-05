#!/usr/bin/env python3
"""Add lookup indexes to the source databases (idempotent; data is not modified).

AdventureWorks ships without any index, so every lookup by order id scans 121k rows.
"""

import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from docgen.sources import DEFAULT_PATHS  # noqa: E402

INDEXES = {
    "adventureworks": [
        ("Sales_SalesOrderHeader", ["SalesOrderID"]), ("Sales_SalesOrderHeader", ["CustomerID"]),
        ("Sales_SalesOrderDetail", ["SalesOrderID"]), ("Sales_Customer", ["CustomerID"]),
        ("Sales_Store", ["BusinessEntityID"]), ("Person_Person", ["BusinessEntityID"]),
        ("Person_Address", ["AddressID"]), ("Person_StateProvince", ["StateProvinceID"]),
        ("Person_BusinessEntityAddress", ["BusinessEntityID"]), ("Person_EmailAddress", ["BusinessEntityID"]),
        ("Person_PersonPhone", ["BusinessEntityID"]), ("Production_Product", ["ProductID"]),
        ("Production_ProductModelProductDescriptionCulture", ["ProductModelID"]),
        ("Production_ProductDescription", ["ProductDescriptionID"]),
        ("Purchasing_PurchaseOrderHeader", ["PurchaseOrderID"]), ("Purchasing_PurchaseOrderDetail", ["PurchaseOrderID"]),
        ("Purchasing_Vendor", ["BusinessEntityID"]), ("Production_WorkOrder", ["WorkOrderID"]),
        ("Production_WorkOrderRouting", ["WorkOrderID"]), ("Production_ProductInventory", ["LocationID"]),
        ("HumanResources_Employee", ["BusinessEntityID"]), ("HumanResources_EmployeePayHistory", ["BusinessEntityID"]),
        ("HumanResources_EmployeeDepartmentHistory", ["BusinessEntityID"]),
    ],
    "sakila": [("payment", ["customer_id"]), ("rental", ["rental_id"]), ("inventory", ["inventory_id"])],
    "chinook": [("InvoiceLine", ["InvoiceId"])],
}


def main():
    for source, indexes in INDEXES.items():
        path = DEFAULT_PATHS[source]
        if not path.exists():
            print(f"[skip] {source}: {path} not found")
            continue
        conn = sqlite3.connect(path)
        started = time.time()
        for table, cols in indexes:
            name = f"idx_{table}_{'_'.join(cols)}".lower()
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})')
        conn.commit()
        conn.execute("ANALYZE")
        conn.close()
        print(f"[ok] {source}: {len(indexes)} indexes in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
