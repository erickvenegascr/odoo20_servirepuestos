"""Read-only, warehouse-scoped SGA receipts for the independent count board."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .catalog_source import CatalogSource, text


MAX_PAGE = 100
MAX_LINES = 500
COLUMNS = {
    "recibos": {"recibo", "proveedor", "bodega", "factura", "pedido",
                "fecharecibo", "recibidocompleto", "pgts"},
    "recibosart": {"recibo", "pormayor", "articulo", "cantrecibida",
                   "recibido", "actualizado", "norecibido", "pgts"},
    "artsservs": {"articulo", "articulonomb", "unidad", "unidaddet"},
}


class ReceiptCountError(ValueError):
    """A bounded diagnostic without SGA business identifiers."""


def flag(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if value in (b"\x00", b"\x01"):
        return value == b"\x01"
    raise ReceiptCountError("SGA_RECEIPT_INVALID_FLAG")


def label(value, limit=240):
    visible = "".join(char if char.isprintable() else " " for char in text(value))
    return " ".join(visible.split())[:limit]


def map_receipt(header, details, instance, source_timezone):
    if not flag(header["RecibidoCompleto"]):
        return None, "incomplete"
    receipt_id = header["Recibo"]
    warehouse = header["Bodega"]
    received = header["FechaRecibo"]
    if (type(receipt_id) is not int or receipt_id <= 0 or type(warehouse) is not int or warehouse <= 0
            or not isinstance(received, datetime) or received.tzinfo is not None):
        raise ReceiptCountError("SGA_RECEIPT_INVALID_HEADER")
    if len(details) > MAX_LINES:
        raise ReceiptCountError("SGA_RECEIPT_TOO_MANY_LINES")

    lines = []
    keys = set()
    for row in details:
        if not flag(row["Recibido"]) or not flag(row["Actualizado"]) or flag(row["NoRecibido"]):
            continue
        quantity = row["CantRecibida"]
        if type(quantity) is not int:
            raise ReceiptCountError("SGA_RECEIPT_INVALID_QUANTITY")
        if quantity <= 0:
            continue
        article = row["Articulo"]
        if row["Recibo"] != receipt_id or type(article) is not int or article <= 0:
            raise ReceiptCountError("SGA_RECEIPT_LINE_SCOPE_MISMATCH")
        wholesale = flag(row["PorMayor"])
        key = f"{article}:{int(wholesale)}"
        if key in keys:
            raise ReceiptCountError("SGA_RECEIPT_DUPLICATE_LINE")
        keys.add(key)
        presentation = "Mayor" if wholesale else "Detalle"
        native_unit = label(row["Unidad"] if wholesale else row["UnidadDet"], 50)
        lines.append({
            "key": key,
            "code": str(article),
            "description": label(row["ArticuloNomb"]) or f"Artículo {article}",
            "quantity": quantity,
            "unit": f"{presentation} · {native_unit}" if native_unit else presentation,
        })
    if not lines:
        return None, "without_applied_lines"
    vendor = label(header["Proveedor"], 3)
    invoice = label(header["Factura"], 20)
    reference = f"Recibo {receipt_id}"
    if invoice:
        reference += f" · {invoice}"
    return {
        "source": instance,
        "receipt_key": str(receipt_id),
        "reference": reference,
        "received_at": received.replace(tzinfo=source_timezone).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "location": f"Bodega {warehouse}",
        "sender_name": f"Proveedor {vendor}" if vendor else None,
        "lines": sorted(lines, key=lambda line: line["key"]),
    }, None


@dataclass(frozen=True)
class ReceiptPage:
    rows: tuple
    after: tuple[int, int] | None
    more: bool


def _placeholders(values):
    return ",".join(["%s"] * len(values))


class ReceiptCountSource(CatalogSource):
    """A bounded reader. Writes and the purchase exchange are absent."""

    def check_schema(self):
        found = defaultdict(set)
        stamps = set()
        for row in self.rows(
            "SELECT c.TABLE_NAME,c.COLUMN_NAME,c.DATA_TYPE,c.IS_NULLABLE,c.EXTRA,t.ENGINE "
            "FROM information_schema.COLUMNS c JOIN information_schema.TABLES t "
            "ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME "
            "WHERE c.TABLE_SCHEMA=DATABASE() AND c.TABLE_NAME IN (%s,%s,%s)",
            tuple(COLUMNS),
        ):
            table = row["TABLE_NAME"].lower()
            column = row["COLUMN_NAME"].lower()
            if row["ENGINE"] != "InnoDB":
                raise ReceiptCountError("SGA_RECEIPT_TABLE_NOT_TRANSACTIONAL")
            found[table].add(column)
            if (column == "pgts" and row["DATA_TYPE"].lower() == "timestamp"
                    and row["IS_NULLABLE"] == "NO" and "on update current_timestamp" in row["EXTRA"].lower()):
                stamps.add(table)
        if any(not columns <= found[table] for table, columns in COLUMNS.items()):
            raise ReceiptCountError("SGA_RECEIPT_SCHEMA_UNSUPPORTED")
        if not {"recibos", "recibosart"} <= stamps:
            raise ReceiptCountError("SGA_RECEIPT_WATERMARK_UNVERIFIED")

    def epoch(self):
        value = self.rows("SELECT UNIX_TIMESTAMP() AS epoch")[0]["epoch"]
        if type(value) is not int or value <= 0:
            raise ReceiptCountError("SGA_RECEIPT_CLOCK_UNAVAILABLE")
        return value

    def page(self, *, since, warehouses, excluded_orders, lower, upper, after, limit):
        if not warehouses or not 1 <= limit <= MAX_PAGE:
            raise ReceiptCountError("SGA_RECEIPT_PAGE_SCOPE_INVALID")
        scope = f"r.FechaRecibo >= %s AND r.Bodega IN ({_placeholders(warehouses)})"
        scope_args = (since, *warehouses)
        if excluded_orders:
            scope += f" AND (r.Pedido IS NULL OR r.Pedido NOT IN ({_placeholders(excluded_orders)}))"
            scope_args += tuple(excluded_orders)
        previous_epoch, previous_id = after or (0, 0)
        keys = self.rows(
            "SELECT changes.Recibo,MAX(changes.epoch) AS epoch FROM ("
            f"SELECT r.Recibo,CAST(UNIX_TIMESTAMP(r.PGTS) AS UNSIGNED) AS epoch FROM recibos r WHERE {scope} "
            "AND r.PGTS >= FROM_UNIXTIME(%s) AND r.PGTS < FROM_UNIXTIME(%s) "
            "UNION ALL "
            f"SELECT r.Recibo,CAST(UNIX_TIMESTAMP(a.PGTS) AS UNSIGNED) AS epoch FROM recibosart a "
            f"JOIN recibos r ON r.Recibo=a.Recibo WHERE {scope} "
            "AND a.PGTS >= FROM_UNIXTIME(%s) AND a.PGTS < FROM_UNIXTIME(%s)"
            ") changes GROUP BY changes.Recibo "
            "HAVING MAX(changes.epoch) > %s OR (MAX(changes.epoch) = %s AND changes.Recibo > %s) "
            "ORDER BY MAX(changes.epoch),changes.Recibo LIMIT %s",
            (*scope_args, lower, upper, *scope_args, lower, upper, previous_epoch, previous_epoch, previous_id, limit + 1),
        )
        if not keys:
            return ReceiptPage((), None, False)
        selected = keys[:limit]
        ids = tuple(row["Recibo"] for row in selected)
        headers = {row["Recibo"]: row for row in self.rows(
            "SELECT Recibo,Proveedor,Bodega,Factura,FechaRecibo,RecibidoCompleto "
            f"FROM recibos WHERE Recibo IN ({_placeholders(ids)})", ids,
        )}
        if set(headers) != set(ids) or len(ids) != len(set(ids)):
            raise ReceiptCountError("SGA_RECEIPT_SNAPSHOT_DIVERGED")
        details = defaultdict(list)
        completed = tuple(key for key in ids if flag(headers[key]["RecibidoCompleto"]))
        if completed:
            rows = self.rows(
                "SELECT a.Recibo,a.PorMayor,a.Articulo,a.CantRecibida,a.Recibido,a.Actualizado,a.NoRecibido,"
                "p.ArticuloNomb,p.Unidad,p.UnidadDet "
                "FROM recibosart a LEFT JOIN artsservs p ON p.Articulo=a.Articulo "
                f"WHERE a.Recibo IN ({_placeholders(completed)}) "
                "ORDER BY a.Recibo,a.PorMayor,a.Articulo LIMIT %s", (*completed, MAX_LINES * len(completed) + 1),
            )
            for row in rows:
                details[row["Recibo"]].append(row)
            if any(len(items) > MAX_LINES for items in details.values()):
                raise ReceiptCountError("SGA_RECEIPT_TOO_MANY_LINES")
        result = tuple((headers[key], details[key]) for key in ids)
        return ReceiptPage(result, (selected[-1]["epoch"], ids[-1]), len(keys) > limit)


def source_zone(value):
    if not isinstance(value, str) or not value:
        raise ReceiptCountError("SGA_RECEIPT_TIME_ZONE_REQUIRED")
    return ZoneInfo(value)
