"""Sakila (video rental chain): one receipt per payment (rental fee + late fee)."""

from __future__ import annotations

from decimal import Decimal

from .base import SQLiteSource, SrcLine, SrcReceipt, clean


class SakilaSource(SQLiteSource):
    name = "sakila"
    sector = "video"

    def _address(self, address_id) -> dict:
        return self.one('''SELECT a.*, c."city", co."country" FROM "address" a
                           JOIN "city" c ON c."city_id" = a."city_id"
                           JOIN "country" co ON co."country_id" = c."country_id"
                           WHERE a."address_id" = ?''', address_id) or {}

    def receipt_keys(self) -> list:
        return self.col('SELECT "payment_id" FROM "payment" WHERE "rental_id" IS NOT NULL')

    def load_receipt(self, key) -> SrcReceipt:
        p = self.one('SELECT * FROM "payment" WHERE "payment_id" = ?', key)
        r = self.one('''SELECT r.*, f."title", f."rental_rate", f."rental_duration", f."rating", f."film_id",
                               c."name" AS category, i."store_id"
                        FROM "rental" r JOIN "inventory" i ON i."inventory_id" = r."inventory_id"
                        JOIN "film" f ON f."film_id" = i."film_id"
                        LEFT JOIN "film_category" fc ON fc."film_id" = f."film_id"
                        LEFT JOIN "category" c ON c."category_id" = fc."category_id"
                        WHERE r."rental_id" = ?''', p["rental_id"])
        cust = self.one('SELECT * FROM "customer" WHERE "customer_id" = ?', p["customer_id"])
        staff = self.one('SELECT "first_name", "last_name" FROM "staff" WHERE "staff_id" = ?', p["staff_id"])
        a = self._address(cust["address_id"])
        customer = self.party(f"{cust['first_name'].title()} {cust['last_name'].title()}", a.get("address"),
                              a.get("address2"), a.get("city"), a.get("district"), a.get("postal_code"),
                              a.get("country"), phone=clean(a.get("phone")), email=clean(cust["email"]).lower(),
                              code=f"M{cust['customer_id']:05d}")
        amount = self.dec(p["amount"])
        rate = min(self.dec(r["rental_rate"]), amount)
        title = clean(r["title"]).title()
        lines = [SrcLine(sku=f"F{r['film_id']:04d}", description=title, quantity=1, unit_price=rate,
                         unit=f"{r['rental_duration']}d", category=clean(r["category"]), product_id=r["film_id"],
                         extra={"rating": clean(r["rating"])})]
        if amount > rate:
            lines.append(SrcLine(sku="LATE", description="late_fee", quantity=1, unit_price=amount - rate,
                                 extra={"label_key": True}))
        rental, ret = self.datetime(r["rental_date"]), self.datetime(r["return_date"])
        return SrcReceipt(
            key=key, ref=f"R{p['rental_id']:06d}", when=self.datetime(p["payment_date"]), customer=customer,
            lines=lines, total=amount.quantize(Decimal("0.01")),
            served_by=f"{staff['first_name']} {staff['last_name']}" if staff else "",
            extra={"rental_date": rental, "return_date": ret, "store": f"#{r['store_id']}", "channel": "store"},
        )
