"""Northwind (B2B food wholesale): orders, purchases from suppliers, customer accounts, inventory."""

from __future__ import annotations

from decimal import Decimal

from ..core.models import Party
from .base import (SQLiteSource, SrcAccount, SrcAccountEntry, SrcInventory, SrcInventoryRow, SrcLine,
                   SrcOrder, SrcPurchase, clean)

# The extended Northwind stores world regions in Region columns, not states.
MACRO_REGIONS = {"British Isles", "Central America", "Eastern Europe", "North America", "Northern Europe",
                 "Scandinavia", "South America", "Southern Europe", "Western Europe"}


class NorthwindSource(SQLiteSource):
    name = "northwind"
    sector = "food"

    # ---------------------------------------------------------------- parties ----
    def _party(self, row: dict, prefix: str = "", name_key: str = "CompanyName", **kw) -> Party:
        region = clean(row.get(f"{prefix}Region"))
        return self.party(row[name_key], row.get(f"{prefix}Address"), "", row.get(f"{prefix}City"),
                          "" if region in MACRO_REGIONS else region, row.get(f"{prefix}PostalCode"),
                          row.get(f"{prefix}Country"), **kw)

    def customer(self, customer_id: str) -> Party:
        row = self.one('SELECT * FROM "Customers" WHERE "CustomerID" = ?', customer_id)
        return self._party(row, contact=clean(row["ContactName"]), phone=clean(row["Phone"]),
                           fax=clean(row["Fax"]), code=row["CustomerID"])

    def supplier(self, supplier_id) -> Party:
        row = self.one('SELECT * FROM "Suppliers" WHERE "SupplierID" = ?', supplier_id)
        return self._party(row, contact=clean(row["ContactName"]), phone=clean(row["Phone"]),
                           fax=clean(row["Fax"]), code=str(row["SupplierID"]))

    def shipper(self, shipper_id) -> Party | None:
        row = self.one('SELECT * FROM "Shippers" WHERE "ShipperID" = ?', shipper_id)
        return Party(name=clean(row["CompanyName"]), phone=clean(row["Phone"]), code=str(shipper_id)) if row else None

    def employee_name(self, employee_id) -> str:
        row = self.one('SELECT "FirstName", "LastName" FROM "Employees" WHERE "EmployeeID" = ?', employee_id)
        return f"{row['FirstName']} {row['LastName']}" if row else ""

    def _lines(self, rows: list[dict], price_key: str = "UnitPrice") -> list[SrcLine]:
        return [SrcLine(
            sku=f"{r['ProductID']:03d}", description=clean(r["ProductName"]), quantity=int(r["Quantity"]),
            unit_price=self.dec(r[price_key]), discount_rate=self.dec(r.get("Discount") or 0).quantize(Decimal("0.01")),
            unit=clean(r["QuantityPerUnit"]), category=clean(r["CategoryName"]), product_id=r["ProductID"],
        ) for r in rows]

    # ----------------------------------------------------------------- orders ----
    def order_keys(self) -> list:
        return self.col('SELECT "OrderID" FROM "Orders" ORDER BY 1')

    def load_order(self, key) -> SrcOrder:
        o = self.one('SELECT * FROM "Orders" WHERE "OrderID" = ?', key)
        rows = self.all('''SELECT d.*, p."ProductName", p."QuantityPerUnit", c."CategoryName"
                           FROM "Order Details" d JOIN "Products" p ON p."ProductID" = d."ProductID"
                           JOIN "Categories" c ON c."CategoryID" = p."CategoryID"
                           WHERE d."OrderID" = ? ORDER BY d."ProductID"''', key)
        return SrcOrder(
            key=key, ref=str(o["OrderID"]), order_date=self.date(o["OrderDate"]), customer=self.customer(o["CustomerID"]),
            ship_to=self._party(o, prefix="Ship", name_key="ShipName"), lines=self._lines(rows),
            required_date=self.date(o["RequiredDate"]), ship_date=self.date(o["ShippedDate"]),
            freight=self.dec(o["Freight"]), salesperson=self.employee_name(o["EmployeeID"]),
            carrier=self.shipper(o["ShipVia"]),
        )

    # -------------------------------------------------------------- purchases ----
    # Northwind has no purchase orders, so one is derived per (order, supplier): the
    # wholesaler restocks from each supplier what a customer order consumed.
    def purchase_keys(self) -> list:
        return self.col('''SELECT DISTINCT d."OrderID" || ':' || p."SupplierID" FROM "Order Details" d
                           JOIN "Products" p ON p."ProductID" = d."ProductID" WHERE d."OrderID" % 7 = 0''')

    def load_purchase(self, key) -> SrcPurchase:
        order_id, supplier_id = (int(x) for x in str(key).split(":"))
        o = self.one('SELECT * FROM "Orders" WHERE "OrderID" = ?', order_id)
        rows = self.all('''SELECT d."Quantity", p.*, c."CategoryName" FROM "Order Details" d
                           JOIN "Products" p ON p."ProductID" = d."ProductID"
                           JOIN "Categories" c ON c."CategoryID" = p."CategoryID"
                           WHERE d."OrderID" = ? AND p."SupplierID" = ? ORDER BY p."ProductID"''', order_id, supplier_id)
        return SrcPurchase(
            key=key, ref=f"{order_id}-{supplier_id}", order_date=self.date(o["OrderDate"]),
            vendor=self.supplier(supplier_id), lines=self._lines(rows), ship_date=self.date(o["RequiredDate"]),
            buyer=self.employee_name(o["EmployeeID"]), carrier=self.shipper(o["ShipVia"]),
        )

    # --------------------------------------------------------------- accounts ----
    def account_keys(self) -> list:
        return self.col('SELECT "CustomerID" FROM "Customers" WHERE "CustomerID" IN (SELECT "CustomerID" FROM "Orders")')

    def load_account(self, key) -> SrcAccount:
        rows = self.all('''SELECT o."OrderID", o."OrderDate", o."Freight",
                                  SUM(d."UnitPrice" * d."Quantity" * (1 - d."Discount")) AS net
                           FROM "Orders" o JOIN "Order Details" d ON d."OrderID" = o."OrderID"
                           WHERE o."CustomerID" = ? GROUP BY o."OrderID" ORDER BY o."OrderDate"''', key)
        entries = [SrcAccountEntry(self.date(r["OrderDate"]), str(r["OrderID"]),
                                   (self.dec(r["net"]) + self.dec(r["Freight"])).quantize(Decimal("0.01")))
                   for r in rows]
        return SrcAccount(key=key, customer=self.customer(key), entries=entries)

    def products(self) -> list[dict]:
        """Catalogue rows, used to generate LLM product descriptions (core/textbank.py)."""
        return self.all('''SELECT p.*, c."CategoryName" FROM "Products" p
                           JOIN "Categories" c ON c."CategoryID" = p."CategoryID" ORDER BY p."ProductID"''')

    # -------------------------------------------------------------- inventory ----
    def inventory_keys(self) -> list:
        return ["all"] + [f"category:{c}" for c in self.col('SELECT "CategoryID" FROM "Categories"')] + \
               [f"supplier:{s}" for s in self.col('SELECT "SupplierID" FROM "Suppliers"')]

    def load_inventory(self, key) -> SrcInventory:
        sql = '''SELECT p.*, c."CategoryName", s."CompanyName" AS supplier FROM "Products" p
                 JOIN "Categories" c ON c."CategoryID" = p."CategoryID"
                 JOIN "Suppliers" s ON s."SupplierID" = p."SupplierID"'''
        kind, _, value = str(key).partition(":")
        if kind == "category":
            rows, title = self.all(sql + ' WHERE p."CategoryID" = ?', int(value)), None
            title = rows[0]["CategoryName"] if rows else ""
        elif kind == "supplier":
            rows = self.all(sql + ' WHERE p."SupplierID" = ?', int(value))
            title = rows[0]["supplier"] if rows else ""
        else:
            rows, title = self.all(sql), ""
        return SrcInventory(key=key, title=clean(title), rows=[SrcInventoryRow(
            sku=f"{r['ProductID']:03d}", description=clean(r["ProductName"]),
            group=clean(r["CategoryName"] if kind != "category" else r["supplier"]),
            on_hand=int(r["UnitsInStock"] or 0), on_order=int(r["UnitsOnOrder"] or 0),
            reorder_level=int(r["ReorderLevel"] or 0), unit_cost=self.dec(r["UnitPrice"]),
            location=clean(r["QuantityPerUnit"]), discontinued=str(r["Discontinued"]) in ("1", "True", "true"),
        ) for r in rows])
