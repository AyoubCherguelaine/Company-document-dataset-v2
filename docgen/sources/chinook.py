"""Chinook (online music store): small B2C invoices and receipts, customers worldwide."""

from __future__ import annotations

from decimal import Decimal

from .base import SQLiteSource, SrcLine, SrcOrder, SrcReceipt, clean


class ChinookSource(SQLiteSource):
    name = "chinook"
    sector = "music"

    def _invoice(self, key):
        inv = self.one('SELECT * FROM "Invoice" WHERE "InvoiceId" = ?', key)
        cust = self.one('SELECT * FROM "Customer" WHERE "CustomerId" = ?', inv["CustomerId"])
        rep = self.one('SELECT "FirstName", "LastName" FROM "Employee" WHERE "EmployeeId" = ?', cust["SupportRepId"])
        rows = self.all('''SELECT l.*, t."Name" AS track, t."Composer", a."Title" AS album, ar."Name" AS artist,
                                  m."Name" AS media, g."Name" AS genre
                           FROM "InvoiceLine" l JOIN "Track" t ON t."TrackId" = l."TrackId"
                           JOIN "Album" a ON a."AlbumId" = t."AlbumId" JOIN "Artist" ar ON ar."ArtistId" = a."ArtistId"
                           JOIN "MediaType" m ON m."MediaTypeId" = t."MediaTypeId"
                           LEFT JOIN "Genre" g ON g."GenreId" = t."GenreId"
                           WHERE l."InvoiceId" = ? ORDER BY l."InvoiceLineId"''', key)
        name = f"{cust['FirstName']} {cust['LastName']}"
        customer = self.party(name, inv["BillingAddress"], "", inv["BillingCity"], inv["BillingState"],
                              inv["BillingPostalCode"], inv["BillingCountry"], email=clean(cust["Email"]),
                              phone=clean(cust["Phone"]), code=f"C{cust['CustomerId']:05d}",
                              contact=clean(cust["Company"]))
        lines = [SrcLine(sku=f"T{r['TrackId']:05d}", description=clean(r["track"]), quantity=int(r["Quantity"]),
                         unit_price=self.dec(r["UnitPrice"]), unit=clean(r["media"]), category=clean(r["genre"]),
                         product_id=r["TrackId"],
                         details={"en": f"{clean(r['artist'])} · {clean(r['album'])}",
                                  "fr": f"{clean(r['artist'])} · {clean(r['album'])}"})
                 for r in rows]
        rep_name = f"{rep['FirstName']} {rep['LastName']}" if rep else ""
        return inv, customer, lines, rep_name

    def order_keys(self) -> list:
        return self.col('SELECT "InvoiceId" FROM "Invoice"')

    def load_order(self, key) -> SrcOrder:
        inv, customer, lines, rep = self._invoice(key)
        d = self.date(inv["InvoiceDate"])
        return SrcOrder(key=key, ref=str(inv["InvoiceId"]), order_date=d, customer=customer, lines=lines,
                        ship_date=d, tax_policy="none", salesperson=rep, b2c=True)

    def receipt_keys(self) -> list:
        return self.order_keys()

    def load_receipt(self, key) -> SrcReceipt:
        inv, customer, lines, rep = self._invoice(key)
        total = sum((l.unit_price * l.quantity for l in lines), Decimal(0))
        return SrcReceipt(key=key, ref=str(inv["InvoiceId"]), when=self.datetime(inv["InvoiceDate"]),
                          customer=customer, lines=lines, total=total.quantize(Decimal("0.01")), served_by=rep,
                          extra={"channel": "online"})
