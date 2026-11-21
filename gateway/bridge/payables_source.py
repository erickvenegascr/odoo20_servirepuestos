"""Read the SGA accounts-payable ledger in a single SELECT-only snapshot."""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .catalog_source import bit, text
from .supplier_source import SupplierSource

MAX_BATCH = 200
SUPPORTED_CURRENCIES = {"CRC", "USD"}
AMOUNT_SCALE = Decimal("0.0001")
MAX_AMOUNT = Decimal("999999999999999.9999")

# SGAPago q50, q89, q113 and q118. Read the debt detail by its primary key.
# Currency belongs to the creditor. The primary supplier can just be a group
# such as OTR for services. The header does not establish each detail's balance.
DEBT_SELECT = (
    "SELECT d.Proveedor, d.FacturaPrimaria, d.Acreedor, d.Factura, d.Fecha, "
    "d.TipoCredito, d.Monto, d.MontoCancelado, d.MontoNotas, d.Retencion, "
    "d.MontoPagosPend, d.Actualizada, d.TipoCambio, i.Moneda "
    "FROM cuentaspagardet d "
    "JOIN cuentaspagar h ON h.Proveedor=d.Proveedor AND h.FacturaPrimaria=d.FacturaPrimaria "
    "LEFT JOIN proveedoresimport i ON i.Proveedor=d.Acreedor"
)
DEBT_KEY = ("Proveedor", "FacturaPrimaria", "Acreedor", "Factura")
DEBT_COLUMNS = ", ".join(f"d.{field}" for field in DEBT_KEY)
DEBT_ORDER = f" ORDER BY {DEBT_COLUMNS}"
EMAIL = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)


class PayablesSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _native_identifier(value, max_length):
    if (not isinstance(value, str) or not value.strip() or len(value) > max_length
            or not value.isprintable()):
        raise PayablesSourceError("SGA_PAYABLE_KEY_INVALID")
    return value


def document_key(value):
    if not isinstance(value, (list, tuple)) or len(value) != len(DEBT_KEY):
        raise PayablesSourceError("SGA_PAYABLE_KEY_INVALID")
    return tuple(_native_identifier(part, length) for part, length in zip(value, (3, 20, 3, 20)))


def _check_limit(limit):
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise PayablesSourceError("SGA_PAYABLE_BATCH_SIZE_INVALID")


def _amount(value):
    if type(value) not in (int, float, Decimal):
        raise PayablesSourceError("SGA_PAYABLE_AMOUNT_INVALID")
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and abs(amount) <= MAX_AMOUNT:
            return amount.quantize(AMOUNT_SCALE, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        pass
    raise PayablesSourceError("SGA_PAYABLE_AMOUNT_INVALID")


def _amount_string(value):
    return format(abs(value) if value.is_zero() else value, "f")


def _fingerprint(document):
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _iban(value):
    value = re.sub(r"[ -]", "", value.upper())
    if not re.fullmatch(r"CR[0-9]{20}", value):
        return None
    # Costa Rican IBAN has a numeric BBAN. C=12 and R=27 in the IBAN check.
    return value if int(value[4:] + "1227" + value[2:4]) % 97 == 1 else None


def _contact(row):
    supplier = {
        "code": _native_identifier(row["Proveedor"], 3),
        "name": text(row["ProveedorNomb"]),
        "tax_id": text(row["Cedula"]),
        "active": bit(row["Activo"]),
    }
    # The dedicated EMailEnvio value is the only candidate. EMail is a general
    # contact address and is not selected. Empty or invalid values cannot authorize a transfer.
    raw_email = text(row["EMailEnvio"])
    if not raw_email or (len(raw_email) <= 254 and EMAIL.fullmatch(raw_email)
                         and raw_email.index("@") <= 64):
        supplier["payment_email"] = raw_email or None
    raw_bank = text(row["Banco"])
    if not raw_bank or len(raw_bank) <= 4 and raw_bank.isprintable():
        supplier["bank_code"] = raw_bank or None
    raw_iban = text(row["IBAN"])
    iban = _iban(raw_iban)
    if not raw_iban or iban:
        supplier["iban"] = iban
    return supplier


class PayablesSource(SupplierSource):
    """Use the existing gateway's grant verification and InnoDB snapshot."""

    def __init__(self, config):
        super().__init__(config)
        self.currency_map = config.get("currency_map", {})
        self.national_currency = config.get("national_currency_code")
        if (not isinstance(self.currency_map, dict)
                or any(not isinstance(native, str) or not native.strip()
                       or not isinstance(iso, str) or not re.fullmatch(r"[A-Z]{3}", iso)
                       for native, iso in self.currency_map.items())
                or self.national_currency is not None and (
                    not isinstance(self.national_currency, str)
                    or self.national_currency not in self.currency_map
                )):
            raise PayablesSourceError("SGA_PAYABLE_CURRENCY_CONFIG_INVALID")

    def _currency(self, row):
        # SGAPago q113 compares ProveedoresImport.Moneda with PGMonNac().
        # q35 multiplies the document amount by TipoCambio for tax reporting.
        native = text(row["Moneda"]) or self.national_currency
        currency = self.currency_map.get(native)
        if currency not in SUPPORTED_CURRENCIES or self.national_currency is None:
            return None
        if row["TipoCambio"] is None:
            return None
        try:
            rate = Decimal(str(row["TipoCambio"]))
        except InvalidOperation:
            return None
        if not rate.is_finite() or rate <= 0 or (native == self.national_currency) != (rate == 1):
            return None
        return currency

    def _document(self, row):
        key = document_key([row[field] for field in DEBT_KEY])
        invoice_date = row["Fecha"]
        if isinstance(invoice_date, datetime):
            invoice_date = invoice_date.date()
        if type(invoice_date) is not date or type(row["TipoCredito"]) is not int:
            raise PayablesSourceError("SGA_PAYABLE_DATE_INVALID")
        try:
            due_date = invoice_date + timedelta(days=row["TipoCredito"])
        except OverflowError:
            raise PayablesSourceError("SGA_PAYABLE_DATE_INVALID") from None
        original, paid, notes, pending = (
            _amount(row[field]) for field in ("Monto", "MontoCancelado", "MontoNotas", "MontoPagosPend")
        )
        retention = _amount(row["Retencion"]) if row["Retencion"] is not None else None
        # Access propagates NULL in this expression. Missing retention cannot
        # establish a numeric balance. Pending payments never reduce the ledger.
        residual = original - paid + notes - retention if retention is not None else None
        state = "open"
        if not bit(row["Actualizada"]):
            state = "unposted"
        elif residual is not None and residual <= 0:
            state = "closed"
        document = {
            "key": list(key),
            "supplier_code": key[0],
            "creditor_code": key[2],
            "invoice_number": key[3],
            "invoice_date": invoice_date.isoformat(),
            "due_date": due_date.isoformat(),
            "currency": self._currency(row),
            "original_amount": _amount_string(original),
            "paid_amount": _amount_string(paid),
            "notes_amount": _amount_string(notes),
            "retention_amount": _amount_string(retention) if retention is not None else None,
            "pending_amount": _amount_string(pending),
            "residual": _amount_string(residual) if residual is not None else None,
            "source_state": state,
            "fiscal_key": "",
        }
        document["fingerprint"] = _fingerprint(document)
        return document

    def contacts(self, *, after=None, limit=MAX_BATCH):
        _check_limit(limit)
        sql = "SELECT Proveedor, ProveedorNomb, Cedula, Activo, EMailEnvio, Banco, IBAN FROM proveedores"
        params = ()
        if after is not None:
            sql += " WHERE Proveedor > %s"
            params = (_native_identifier(after, 3),)
        return [_contact(row) for row in self.rows(sql + " ORDER BY Proveedor LIMIT %s", (*params, limit))]

    def open_documents(self, *, after=None, limit=MAX_BATCH):
        _check_limit(limit)
        # Send unknown retention for review so an unresolved liability cannot
        # disappear during refresh. Locate closures explicitly by tracked key.
        sql = DEBT_SELECT + (
            " WHERE d.Actualizada = 1 AND (d.Retencion IS NULL OR "
            "d.Monto - d.MontoCancelado + d.MontoNotas - d.Retencion > 0)"
        )
        params = ()
        if after is not None:
            key = document_key(after)
            sql += f" AND ({DEBT_COLUMNS}) > (%s, %s, %s, %s)"
            params = key
        rows = self.rows(sql + DEBT_ORDER + " LIMIT %s", (*params, limit))
        return [self._document(row) for row in rows]

    def documents_by_keys(self, keys):
        _check_limit(len(keys))
        keys = [document_key(key) for key in keys]
        if len(keys) != len(set(keys)):
            raise PayablesSourceError("SGA_PAYABLE_DUPLICATE_KEY")
        placeholders = ", ".join(["(%s, %s, %s, %s)"] * len(keys))
        sql = DEBT_SELECT + f" WHERE ({DEBT_COLUMNS}) IN ({placeholders})" + DEBT_ORDER
        rows = self.rows(sql, tuple(part for key in keys for part in key))
        documents = [self._document(row) for row in rows]
        # A collation match (case/accent/padding) can find a different stored
        # key. Only exact native identity may close a previously tracked debt.
        requested = set(keys)
        return [document for document in documents if tuple(document["key"]) in requested]
