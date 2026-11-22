"""Plan historical SGA balances as native Odoo vendor bill drafts."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .payables_source import MAX_BATCH, document_key


_AMOUNT_STEP = Decimal("0.01")
_IMPORT_FIELDS = [
    "id", "company_id/.id", "partner_id/.id", "ref", "payment_reference",
    "invoice_date", "date", "invoice_date_due", "invoice_payment_term_id/id", "invoice_origin",
    "journal_id/.id", "currency_id/.id", "invoice_line_ids/id",
    "invoice_line_ids/name", "invoice_line_ids/account_id/.id",
    "invoice_line_ids/quantity", "invoice_line_ids/price_unit", "invoice_line_ids/tax_ids/id",
]
_BILL_FIELDS = [
    "id", "ref", "payment_reference", "state", "move_type", "company_id",
    "commercial_partner_id", "currency_id", "invoice_date", "date", "invoice_date_due",
    "amount_total", "amount_residual", "journal_id", "invoice_line_ids", "invoice_origin",
]


def _digest(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def reference_keys(value):
    if not value:
        return set()
    keys = {("text", value.strip().casefold())}
    digits = re.sub(r"[^0-9]", "", value)
    for candidate in (digits, digits[-10:]):
        candidate = candidate.lstrip("0")
        if len(candidate) >= 5:
            keys.add(("digits", candidate))
    return keys


def _rounded(value):
    return Decimal(str(value)).quantize(_AMOUNT_STEP, rounding=ROUND_HALF_UP)


def _id(value):
    if isinstance(value, list):
        value = value[0] if value else None
    return value if type(value) is int and value > 0 else None


def _rows(client, model, domain, fields):
    after = 0
    while batch := client.search_read(model, [*domain, ["id", ">", after]], fields, limit=200, order="id asc"):
        for row in batch:
            if not (record_id := _id(row["id"])) or record_id <= after:
                raise ValueError("Invalid Odoo pagination")
            after = record_id
            yield row


@dataclass(frozen=True)
class Opening:
    date: str
    journal_id: int
    bridge_account_id: int

    @classmethod
    def configured(cls, target):
        values = target.get("opening")
        if values is None:
            return None
        if set(values) != {"date", "journal_id", "bridge_account_id"}:
            raise ValueError("Incomplete opening configuration")
        day = date.fromisoformat(values["date"]).isoformat()
        journal_id = _id(values["journal_id"])
        account_id = _id(values["bridge_account_id"])
        if not journal_id or not account_id:
            raise ValueError("Invalid opening journal or account")
        return cls(day, journal_id, account_id)


def collect_documents(source, order_ids, excluded_references):
    excluded = set().union(*(reference_keys(ref) for ref in excluded_references))
    protected_pairs = set()
    if order_ids:
        markers = ", ".join(["%s"] * len(order_ids))
        receipts = source.rows(
            "SELECT DISTINCT Pedido, Proveedor, Factura FROM recibos "
            f"WHERE Pedido IN ({markers})", tuple(order_ids),
        )
        if {int(row["Pedido"]) for row in receipts} != set(order_ids):
            raise ValueError("Protected order receipts not found")
        protected_pairs = {(row["Proveedor"].strip().casefold(), ref) for row in receipts
                           for ref in reference_keys(row["Factura"])}
    cursor = None
    documents = {}
    skipped = 0
    while batch := source.open_documents(after=cursor, limit=MAX_BATCH):
        next_cursor = document_key(batch[-1]["key"])
        if next_cursor == cursor:
            raise ValueError("SGA cursor did not advance")
        for row in batch:
            key = document_key(row["key"])
            if (any(reference_keys(ref) & excluded for ref in (key[1], key[3]))
                    or any((vendor.strip().casefold(), ref) in protected_pairs
                           for vendor, invoice in (key[:2], key[2:]) for ref in reference_keys(invoice))):
                skipped += 1
                continue
            if key in documents or row["source_state"] != "open":
                raise ValueError("Unexpected SGA document")
            documents[key] = row
        cursor = next_cursor
    return documents, skipped


def _single(client, model, record_id, fields):
    rows = client.read(model, [record_id], fields)
    if len(rows) != 1 or rows[0]["id"] != record_id:
        raise ValueError("Missing Odoo configuration")
    return rows[0]


def _company_currency(client, company_id, opening):
    company = _single(client, "res.company", company_id, ["id", "currency_id"])
    currency_id = _id(company["currency_id"])
    currency = _single(client, "res.currency", currency_id, ["id", "name", "rounding", "active"])
    if not currency["active"] or Decimal(str(currency["rounding"])) != _AMOUNT_STEP:
        raise ValueError("Unsupported company currency rounding")
    if opening:
        journal = _single(client, "account.journal", opening.journal_id,
                          ["id", "company_id", "type", "currency_id", "active"])
        bridge = _single(client, "account.account", opening.bridge_account_id,
                         ["id", "company_ids", "account_type", "active", "reconcile"])
        if (not journal["active"] or journal["type"] != "purchase" or _id(journal["company_id"]) != company_id
                or _id(journal["currency_id"]) not in (None, currency_id)
                or company_id not in bridge["company_ids"] or not bridge["active"]
                or bridge["account_type"] not in ("asset_current", "liability_current") or not bridge["reconcile"]):
            raise ValueError("Invalid migration journal or clearing account")
    return currency


def _indexes(client, company, instance, documents, excluded_fiscalbot_ids):
    codes = sorted({row["creditor_code"] for row in documents.values()})
    bindings = defaultdict(set)
    for row in _rows(client, "sga.supplier.binding", [
        ["company_id", "=", company], ["source_instance", "=", instance], ["vendor_code", "in", codes],
    ], ["id", "vendor_code", "partner_id"]):
        if (partner := _id(row["partner_id"])):
            bindings[row["vendor_code"]].add(partner)
    partner_ids = sorted(set().union(*bindings.values()))
    partners = {row["id"]: _id(row["commercial_partner_id"]) for row in _rows(client, "res.partner", [
        ["id", "in", partner_ids], ["company_id", "in", [False, company]], ["active", "=", True],
    ], ["id", "commercial_partner_id"])}
    commercial_ids = sorted({partner for partner in partners.values() if partner})
    bills = defaultdict(dict)
    if commercial_ids:
        for row in _rows(client, "account.move", [
            ["company_id", "=", company], ["commercial_partner_id", "in", commercial_ids],
            ["move_type", "in", ["in_invoice", "in_refund", "in_receipt"]],
        ], _BILL_FIELDS):
            for ref in reference_keys(row["ref"]) | reference_keys(row["payment_reference"]):
                bills[(_id(row["commercial_partner_id"]), ref)][row["id"]] = row

    fiscal = set()
    # Records awaiting assignment can lack a supplier. Look across the company
    # and block a reference collision even if a supplier is not yet assigned.
    for row in _rows(client, "fiscalbot.document", [
        ["company_id", "=", company], ["id", "not in", excluded_fiscalbot_ids],
        ["document_type", "in", ["invoice", "debit_note", "ticket", "credit_note"]],
    ], ["id", "document_number", "document_key"]):
        fiscal.update(reference_keys(row["document_number"]))
        if row["document_key"]:
            fiscal.add(("fiscal", row["document_key"]))
    return bindings, partners, bills, fiscal


def _classify(row, partner, matches, fiscal, duplicate, currency, opening, origin):
    if not partner or duplicate or len(matches) > 1:
        return "ambiguous_or_unmapped", None
    if row["currency"] is None or row["residual"] is None or row["retention_amount"] is None:
        return "invalid_source_balance", None
    if row["currency"] != currency["name"]:
        return "foreign_currency_review", None
    amount = _rounded(row["residual"])
    if amount <= 0 or Decimal(row["pending_amount"]) != 0:
        return "nonpositive_or_pending_sga_payment", None
    if matches:
        bill = next(iter(matches.values()))
        if (bill["state"] == "posted" and bill["move_type"] == "in_invoice"
                and bill["ref"] == row["invoice_number"] and bill["invoice_date"] == row["invoice_date"]
                and _id(bill["currency_id"]) == currency["id"]
                and _rounded(bill["amount_residual"]) == amount):
            return "posted_balance_matches", None
        if bill.get("invoice_origin") == origin and bill["state"] == "draft":
            if (bill["move_type"] == "in_invoice" and bill["ref"] == row["invoice_number"]
                    and _id(bill["currency_id"]) == currency["id"] and _rounded(bill["amount_total"]) == amount
                    and bill["invoice_date_due"] == row["due_date"] and not fiscal):
                return "opening_draft_matches", None
            return "opening_draft_review", None
        return "existing_bill_review", None
    if fiscal:
        return "fiscalbot_review", None
    if not opening or row["invoice_date"] > opening.date:
        return "opening_missing_or_outside_cutoff", None
    return "draft_candidate", amount


@dataclass(frozen=True)
class BillDraft:
    external_id: str
    partner_id: int
    reference: str
    invoice_date: str
    maturity: str
    amount: Decimal
    origin: str

    def row(self, company, currency_id, opening):
        return [
            self.external_id, str(company), str(self.partner_id), self.reference, self.reference,
            self.invoice_date, opening.date, self.maturity, "", self.origin,
            str(opening.journal_id), str(currency_id), self.external_id + "_line",
            "Saldo pendiente de apertura SGA", str(opening.bridge_account_id), "1", format(self.amount, ".2f"), "",
        ]


def make_plan(client, company, instance, documents, excluded_fiscalbot_ids, opening):
    currency = _company_currency(client, company, opening)
    bindings, partners, bills, fiscal = _indexes(client, company, instance, documents, excluded_fiscalbot_ids)
    resolved = {
        code: partners.get(next(iter(ids))) for code, ids in bindings.items() if len(ids) == 1
    }
    ref_counts = Counter((resolved.get(row["creditor_code"]), ref) for row in documents.values()
                         for ref in reference_keys(row["invoice_number"]))
    counts = Counter()
    drafts = []
    origin = "Saldo de apertura SGA " + _digest(instance)[:16]
    for key, row in sorted(documents.items()):
        partner = resolved.get(row["creditor_code"])
        refs = reference_keys(row["invoice_number"])
        duplicate = not refs or any(ref_counts[(partner, ref)] > 1 for ref in refs)
        matches = {}
        for ref in refs:
            matches.update(bills[(partner, ref)])
        if row.get("fiscal_key"):
            refs.add(("fiscal", row["fiscal_key"]))
        status, amount = _classify(row, partner, matches, bool(refs & fiscal), duplicate, currency, opening, origin)
        counts[status] += 1
        if amount is not None:
            xml_id = "__import__.sga_vendor_balance_" + _digest([instance, company, key])
            drafts.append(BillDraft(xml_id, partner, row["invoice_number"], row["invoice_date"],
                                    row["due_date"], amount, origin))
    open_refs = {(resolved.get(row["creditor_code"]), row["invoice_number"]) for row in documents.values()}
    for bill in _rows(client, "account.move", [
        ["company_id", "=", company], ["move_type", "=", "in_invoice"], ["state", "=", "draft"],
        ["invoice_origin", "=", origin],
    ], ["id", "commercial_partner_id", "ref"]):
        if (_id(bill["commercial_partner_id"]), bill["ref"]) not in open_refs:
            counts["opening_draft_no_longer_open_review"] += 1
    return currency, counts, drafts


def load_drafts(client, company, currency_id, opening, drafts):
    rows = [draft.row(company, currency_id, opening) for draft in drafts]
    result = client.call("account.move", "load", fields=_IMPORT_FIELDS, data=rows, context={
        "default_move_type": "in_invoice", "lang": "en_US", "import_file": True,
        "mode": "update", "noupdate": True,
    })
    ids = result.get("ids") if isinstance(result, dict) else None
    if (not isinstance(ids, list) or len(ids) != len(drafts) or not all(_id(i) for i in ids)
            or len(set(ids)) != len(ids) or any(message.get("type") == "error" for message in result["messages"])):
        raise ValueError("Odoo did not acknowledge the complete draft batch")

    observed = {row["id"]: row for row in client.read("account.move", ids, _BILL_FIELDS)}
    line_ids = [line_id for row in observed.values() for line_id in row["invoice_line_ids"]]
    lines = {line["id"]: line for line in client.read(
        "account.move.line", line_ids, ["id", "move_id", "account_id", "tax_ids", "quantity", "price_unit"],
    )}
    for move_id, draft in zip(ids, drafts):
        row = observed[move_id]
        if (row["state"] != "draft" or row["move_type"] != "in_invoice" or row["ref"] != draft.reference
                or _id(row["company_id"]) != company or _id(row["commercial_partner_id"]) != draft.partner_id
                or _id(row["currency_id"]) != currency_id or _id(row["journal_id"]) != opening.journal_id
                or row["invoice_date"] != draft.invoice_date or row["date"] != opening.date
                or row["invoice_date_due"] != draft.maturity or _rounded(row["amount_total"]) != draft.amount
                or row["invoice_origin"] != draft.origin
                or len(row["invoice_line_ids"]) != 1):
            raise ValueError("Native draft readback mismatch")
        line = lines[row["invoice_line_ids"][0]]
        if (_id(line["move_id"]) != move_id or _id(line["account_id"]) != opening.bridge_account_id
                or line["tax_ids"] or line["quantity"] != 1 or _rounded(line["price_unit"]) != draft.amount):
            raise ValueError("Native draft accounting readback mismatch")
    return len(ids)
