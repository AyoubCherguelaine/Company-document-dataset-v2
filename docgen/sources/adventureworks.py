"""AdventureWorks (bicycle manufacturer): sales, purchasing, inventory, production, HR.

Every column is stored as text and nothing is typed, so ids are always passed as strings
and numbers go through Decimal. Run scripts/prepare_sources.py once for indexes.
"""

from __future__ import annotations

from decimal import Decimal

from ..core.models import Party
from .base import (SQLiteSource, SrcAccount, SrcAccountEntry, SrcEmployee, SrcInventory, SrcInventoryRow,
                   SrcLine, SrcOrder, SrcPurchase, SrcWorkOrder, clean)

LB_TO_KG = Decimal("0.45359237")


def _id(value) -> str:
    return str(int(value)) if str(value).strip().lstrip("-").isdigit() else str(value)


class AdventureWorksSource(SQLiteSource):
    name = "adventureworks"
    sector = "bikes"

    # ---------------------------------------------------------------- helpers ----
    def address(self, address_id) -> dict:
        return self.one('''SELECT a."AddressLine1", a."AddressLine2", a."City", a."PostalCode",
                                  sp."StateProvinceCode", sp."Name" AS state, cr."Name" AS country
                           FROM "Person_Address" a
                           JOIN "Person_StateProvince" sp ON sp."StateProvinceID" = a."StateProvinceID"
                           JOIN "Person_CountryRegion" cr ON cr."CountryRegionCode" = sp."CountryRegionCode"
                           WHERE a."AddressID" = ?''', _id(address_id)) or {}

    def entity_address_id(self, entity_id):
        row = self.one('SELECT "AddressID" FROM "Person_BusinessEntityAddress" WHERE "BusinessEntityID" = ? '
                       'ORDER BY "AddressTypeID" LIMIT 1', _id(entity_id))
        return row["AddressID"] if row else None

    def _party(self, name, address_id, **kw) -> Party:
        a = self.address(address_id) if address_id else {}
        region = clean(a.get("StateProvinceCode")) if a.get("country") in ("United States", "Canada", "Australia") \
            else ""
        return self.party(name, a.get("AddressLine1"), a.get("AddressLine2"), a.get("City"), region,
                          a.get("PostalCode"), a.get("country"), **kw)

    def person_name(self, entity_id) -> str:
        p = self.one('SELECT "Title", "FirstName", "MiddleName", "LastName" FROM "Person_Person" '
                     'WHERE "BusinessEntityID" = ?', _id(entity_id)) if entity_id else None
        return " ".join(clean(p[k]) for k in ("FirstName", "LastName") if p[k]) if p else ""

    def _contact(self, entity_id) -> dict:
        email = self.one('SELECT "EmailAddress" FROM "Person_EmailAddress" WHERE "BusinessEntityID" = ?', _id(entity_id))
        phone = self.one('SELECT "PhoneNumber" FROM "Person_PersonPhone" WHERE "BusinessEntityID" = ?', _id(entity_id))
        return {"email": clean(email and email["EmailAddress"]), "phone": clean(phone and phone["PhoneNumber"])}

    def customer(self, customer_id, address_id=None) -> tuple[Party, bool]:
        c = self.one('SELECT * FROM "Sales_Customer" WHERE "CustomerID" = ?', _id(customer_id))
        if c.get("StoreID"):
            store = self.one('SELECT "Name" FROM "Sales_Store" WHERE "BusinessEntityID" = ?', _id(c["StoreID"]))
            contact = self.person_name(c["PersonID"]) if c.get("PersonID") else ""
            addr = address_id or self.entity_address_id(c["StoreID"])
            return self._party(store["Name"], addr, contact=contact, code=clean(c["AccountNumber"])), False
        addr = address_id or self.entity_address_id(c["PersonID"])
        return self._party(self.person_name(c["PersonID"]), addr, code=clean(c["AccountNumber"]),
                           **self._contact(c["PersonID"])), True

    def ship_method(self, ship_method_id) -> Party | None:
        row = self.one('SELECT "Name" FROM "Purchasing_ShipMethod" WHERE "ShipMethodID" = ?', _id(ship_method_id))
        return Party(name=clean(row["Name"]).title(), code=_id(ship_method_id)) if row else None

    def _weight_kg(self, row) -> Decimal | None:
        w, unit = row.get("Weight"), clean(row.get("WeightUnitMeasureCode"))
        if not w:
            return None
        w = self.dec(w)
        return (w * LB_TO_KG if unit == "LB" else w / 1000 if unit == "G" else w).quantize(Decimal("0.001"))

    def descriptions(self, model_id) -> dict[str, str]:
        if not model_id:
            return {}
        rows = self.all('''SELECT TRIM(c."CultureID") AS culture, d."Description" FROM
                           "Production_ProductModelProductDescriptionCulture" c
                           JOIN "Production_ProductDescription" d ON d."ProductDescriptionID" = c."ProductDescriptionID"
                           WHERE c."ProductModelID" = ?''', _id(model_id))
        return {r["culture"]: clean(r["Description"]) for r in rows if r["culture"] in ("en", "fr")}

    def _line(self, p: dict, qty, price, discount=0, **extra) -> SrcLine:
        return SrcLine(
            sku=clean(p["ProductNumber"]), description=clean(p["Name"]), quantity=int(self.dec(qty)),
            unit_price=self.dec(price).quantize(Decimal("0.01")), discount_rate=self.dec(discount).quantize(Decimal("0.01")),
            unit="EA", category=clean(p.get("subcategory")), product_id=_id(p["ProductID"]),
            weight=self._weight_kg(p), details=self.descriptions(p.get("ProductModelID")), extra=extra,
        )

    PRODUCT_SQL = '''SELECT p.*, s."Name" AS subcategory FROM "Production_Product" p
                     LEFT JOIN "Production_ProductSubcategory" s ON s."ProductSubcategoryID" = p."ProductSubcategoryID"
                     WHERE p."ProductID" = ?'''

    def product(self, product_id) -> dict:
        return self.one(self.PRODUCT_SQL, _id(product_id))

    # ----------------------------------------------------------------- orders ----
    def order_keys(self) -> list:
        return self.col('SELECT "SalesOrderID" FROM "Sales_SalesOrderHeader"')

    def load_order(self, key) -> SrcOrder:
        h = self.one('SELECT * FROM "Sales_SalesOrderHeader" WHERE "SalesOrderID" = ?', _id(key))
        customer, b2c = self.customer(h["CustomerID"], h["BillToAddressID"])
        details = self.all('SELECT * FROM "Sales_SalesOrderDetail" WHERE "SalesOrderID" = ? '
                           'ORDER BY CAST("SalesOrderDetailID" AS INTEGER)', _id(key))
        lines = [self._line(self.product(d["ProductID"]), d["OrderQty"], d["UnitPrice"], d["UnitPriceDiscount"])
                 for d in details]
        ship_to = self._party(customer.name, h["ShipToAddressID"])
        return SrcOrder(
            key=key, ref=clean(h["SalesOrderNumber"]), order_date=self.date(h["OrderDate"]), customer=customer,
            ship_to=ship_to, lines=lines, required_date=self.date(h["DueDate"]), ship_date=self.date(h["ShipDate"]),
            freight=self.dec(h["Freight"]).quantize(Decimal("0.01")),
            tax_amount=self.dec(h["TaxAmt"]).quantize(Decimal("0.01")), tax_policy="given",
            salesperson=self.person_name(h["SalesPersonID"]) if h.get("SalesPersonID") else "",
            carrier=self.ship_method(h["ShipMethodID"]), po_number=clean(h["PurchaseOrderNumber"]),
            tracking=clean(details[0]["CarrierTrackingNumber"]) if details else "", b2c=b2c,
        )

    # -------------------------------------------------------------- purchases ----
    def purchase_keys(self, complete_only: bool = False) -> list:
        sql = 'SELECT "PurchaseOrderID" FROM "Purchasing_PurchaseOrderHeader"'
        return self.col(sql + (' WHERE "Status" = \'4\'' if complete_only else ""))

    def vendor(self, vendor_id) -> Party:
        v = self.one('SELECT * FROM "Purchasing_Vendor" WHERE "BusinessEntityID" = ?', _id(vendor_id))
        return self._party(v["Name"], self.entity_address_id(vendor_id), code=clean(v["AccountNumber"]))

    def load_purchase(self, key) -> SrcPurchase:
        h = self.one('SELECT * FROM "Purchasing_PurchaseOrderHeader" WHERE "PurchaseOrderID" = ?', _id(key))
        rows = self.all('SELECT * FROM "Purchasing_PurchaseOrderDetail" WHERE "PurchaseOrderID" = ? '
                        'ORDER BY CAST("PurchaseOrderDetailID" AS INTEGER)', _id(key))
        lines = [self._line(self.product(r["ProductID"]), r["OrderQty"], r["UnitPrice"],
                            received=int(self.dec(r["ReceivedQty"])), rejected=int(self.dec(r["RejectedQty"])),
                            stocked=int(self.dec(r["StockedQty"])), due_date=self.date(r["DueDate"]))
                 for r in rows]
        status = {"1": "pending", "2": "approved", "3": "rejected", "4": "complete"}.get(_id(h["Status"]), "")
        return SrcPurchase(
            key=key, ref=f"PO{int(key):06d}", order_date=self.date(h["OrderDate"]), vendor=self.vendor(h["VendorID"]),
            lines=lines, ship_date=self.date(h["ShipDate"]), freight=self.dec(h["Freight"]).quantize(Decimal("0.01")),
            tax_amount=self.dec(h["TaxAmt"]).quantize(Decimal("0.01")), buyer=self.person_name(h["EmployeeID"]),
            carrier=self.ship_method(h["ShipMethodID"]), status=status,
        )

    # --------------------------------------------------------------- accounts ----
    def account_keys(self) -> list:
        return self.col('''SELECT c."CustomerID" FROM "Sales_Customer" c WHERE c."StoreID" IS NOT NULL
                           AND c."StoreID" != '' AND EXISTS (SELECT 1 FROM "Sales_SalesOrderHeader" h
                           WHERE h."CustomerID" = c."CustomerID")''')

    def load_account(self, key) -> SrcAccount:
        customer, _ = self.customer(key)
        rows = self.all('SELECT "OrderDate", "SalesOrderNumber", "TotalDue" FROM "Sales_SalesOrderHeader" '
                        'WHERE "CustomerID" = ? ORDER BY "OrderDate"', _id(key))
        return SrcAccount(key=key, customer=customer, entries=[
            SrcAccountEntry(self.date(r["OrderDate"]), clean(r["SalesOrderNumber"]),
                            self.dec(r["TotalDue"]).quantize(Decimal("0.01"))) for r in rows])

    # -------------------------------------------------------------- inventory ----
    def inventory_keys(self) -> list:
        return self.col('SELECT DISTINCT "LocationID" FROM "Production_ProductInventory"')

    def load_inventory(self, key) -> SrcInventory:
        loc = self.one('SELECT "Name" FROM "Production_Location" WHERE "LocationID" = ?', _id(key))
        rows = self.all('''SELECT i."Shelf", i."Bin", i."Quantity", p.*, s."Name" AS subcategory
                           FROM "Production_ProductInventory" i
                           JOIN "Production_Product" p ON p."ProductID" = i."ProductID"
                           LEFT JOIN "Production_ProductSubcategory" s ON s."ProductSubcategoryID" = p."ProductSubcategoryID"
                           WHERE i."LocationID" = ? ORDER BY s."Name", p."Name"''', _id(key))
        return SrcInventory(key=key, title=clean(loc["Name"]), rows=[SrcInventoryRow(
            sku=clean(r["ProductNumber"]), description=clean(r["Name"]), group=clean(r["subcategory"]) or "Components",
            on_hand=int(self.dec(r["Quantity"])), unit_cost=self.dec(r["StandardCost"]).quantize(Decimal("0.01")),
            reorder_level=int(self.dec(r["ReorderPoint"])),
            location="" if clean(r["Shelf"]) in ("", "N/A") else f"{clean(r['Shelf'])}-{_id(r['Bin'])}",
            discontinued=bool(r.get("DiscontinuedDate")),
        ) for r in rows])

    # -------------------------------------------------------------- employees ----
    def employee_keys(self) -> list:
        return self.col('SELECT "BusinessEntityID" FROM "HumanResources_Employee" WHERE "CurrentFlag" = \'1\'')

    def load_employee(self, key) -> SrcEmployee:
        e = self.one('SELECT * FROM "HumanResources_Employee" WHERE "BusinessEntityID" = ?', _id(key))
        pay = self.one('SELECT * FROM "HumanResources_EmployeePayHistory" WHERE "BusinessEntityID" = ? '
                       'ORDER BY "RateChangeDate" DESC LIMIT 1', _id(key))
        dept = self.one('''SELECT d."Name", s."Name" AS shift FROM "HumanResources_EmployeeDepartmentHistory" h
                           JOIN "HumanResources_Department" d ON d."DepartmentID" = h."DepartmentID"
                           JOIN "HumanResources_Shift" s ON s."ShiftID" = h."ShiftID"
                           WHERE h."BusinessEntityID" = ? ORDER BY (h."EndDate" IS NULL) DESC, h."StartDate" DESC
                           LIMIT 1''', _id(key)) or {}
        person = self._party(self.person_name(key), self.entity_address_id(key), code=_id(key), **self._contact(key))
        return SrcEmployee(
            key=key, person=person, job_title=clean(e["JobTitle"]), department=clean(dept.get("Name")),
            hire_date=self.date(e["HireDate"]), birth_date=self.date(e["BirthDate"]),
            national_id=clean(e["NationalIDNumber"]), pay_rate=self.dec(pay["Rate"]).quantize(Decimal("0.01")),
            pay_frequency=int(self.dec(pay["PayFrequency"])), shift=clean(dept.get("shift")),
            salaried=_id(e["SalariedFlag"]) == "1", gender=clean(e["Gender"]),
            vacation_hours=int(self.dec(e["VacationHours"])), sick_hours=int(self.dec(e["SickLeaveHours"])),
        )

    def hr_signatory(self) -> tuple[str, str]:
        row = self.one('''SELECT e."BusinessEntityID", e."JobTitle" FROM "HumanResources_Employee" e
                          WHERE e."JobTitle" = 'Human Resources Manager' LIMIT 1''')
        return (self.person_name(row["BusinessEntityID"]), clean(row["JobTitle"])) if row else ("", "")

    # ------------------------------------------------------------ work orders ----
    def work_order_keys(self) -> list:
        return self.col('SELECT DISTINCT "WorkOrderID" FROM "Production_WorkOrderRouting"')

    def load_work_order(self, key) -> SrcWorkOrder:
        w = self.one('SELECT * FROM "Production_WorkOrder" WHERE "WorkOrderID" = ?', _id(key))
        product = self.product(w["ProductID"])
        scrap = self.one('SELECT "Name" FROM "Production_ScrapReason" WHERE "ScrapReasonID" = ?',
                         _id(w["ScrapReasonID"])) if w.get("ScrapReasonID") else None
        ops = self.all('''SELECT r.*, l."Name" AS location FROM "Production_WorkOrderRouting" r
                          JOIN "Production_Location" l ON l."LocationID" = r."LocationID"
                          WHERE r."WorkOrderID" = ? ORDER BY CAST(r."OperationSequence" AS INTEGER)''', _id(key))
        return SrcWorkOrder(
            key=key, ref=f"WO-{int(key):06d}", product=self._line(product, w["OrderQty"], product["StandardCost"]),
            order_qty=int(self.dec(w["OrderQty"])), stocked_qty=int(self.dec(w["StockedQty"])),
            scrapped_qty=int(self.dec(w["ScrappedQty"])), start_date=self.date(w["StartDate"]),
            end_date=self.date(w["EndDate"]), due_date=self.date(w["DueDate"]),
            scrap_reason=clean(scrap and scrap["Name"]),
            operations=[{
                "seq": int(self.dec(o["OperationSequence"])), "location": clean(o["location"]),
                "planned_start": self.date(o["ScheduledStartDate"]), "planned_end": self.date(o["ScheduledEndDate"]),
                "actual_start": self.date(o["ActualStartDate"]), "actual_end": self.date(o["ActualEndDate"]),
                "hours": self.dec(o["ActualResourceHrs"]).quantize(Decimal("0.01")),
                "planned_cost": self.dec(o["PlannedCost"]).quantize(Decimal("0.01")),
                "actual_cost": self.dec(o["ActualCost"]).quantize(Decimal("0.01")),
            } for o in ops],
        )
