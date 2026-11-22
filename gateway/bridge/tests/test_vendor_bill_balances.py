from decimal import Decimal
from unittest.mock import Mock

from django.test import SimpleTestCase

from bridge.vendor_bill_balances import Opening, collect_documents, load_drafts, make_plan


def document(reference, **changes):
    return {
        "key": ["T03", reference, "T03", reference], "creditor_code": "T03", "invoice_number": reference,
        "invoice_date": "2026-09-12", "due_date": "2026-10-03", "source_state": "open",
        "currency": "CRC", "residual": "62.3456", "retention_amount": "0.0000", "pending_amount": "0.0000",
        **changes,
    }


class VendorBillBalancesTests(SimpleTestCase):
    def setUp(self):
        self.client = Mock()
        self.opening = Opening("2026-11-21", 11, 22)
        self.records = {
            "res.company": [{"id": 1, "currency_id": 2}],
            "res.currency": [{"id": 2, "name": "CRC", "rounding": 0.01, "active": True}],
            "account.journal": [{"id": 11, "type": "purchase", "company_id": 1, "currency_id": False, "active": True}],
            "account.account": [{"id": 22, "account_type": "asset_current", "company_ids": [1],
                                 "active": True, "reconcile": True}],
            "sga.supplier.binding": [{"id": 1, "vendor_code": "T03", "partner_id": 35}],
            "res.partner": [{"id": 35, "commercial_partner_id": 34}],
            "fiscalbot.document": [{"id": 71, "document_number": "000000876543", "document_key": False}],
            "account.move": [{
                "id": 41, "company_id": 1, "commercial_partner_id": 34, "state": "posted",
                "move_type": "in_invoice", "currency_id": 2, "journal_id": 11, "ref": "PST001",
                "payment_reference": False, "invoice_date": "2026-09-12", "date": "2026-09-13",
                "invoice_date_due": "2026-10-03", "amount_total": 100.00, "amount_residual": 62.35,
                "invoice_line_ids": [51],
            }],
        }
        self.client.read.side_effect = lambda model, ids, fields: [
            row for row in self.records[model] if row["id"] in ids
        ]
        self.client.search_read.side_effect = self.search_read

    def search_read(self, model, domain, fields, **kwargs):
        origin = next((condition[2] for condition in domain if condition[0] == "invoice_origin"), None)
        return [row for row in self.records[model] if row["id"] > domain[-1][-1]
                and (origin is None or row["state"] == "draft" and row.get("invoice_origin") == origin)]

    def plan(self, *rows, opening=True):
        docs = {tuple(row["key"]): row for row in rows}
        return make_plan(self.client, 1, "test-sga", docs, [91], self.opening if opening else None)

    def test_plans_only_missing_historical_balances(self):
        _currency, counts, drafts = self.plan(
            document("PST001"), document("876543"), document("MISSING1"),
            document("USD001", currency="USD"), document("RECENT1", invoice_date="2026-12-01"),
            document("PEND001", pending_amount="5.0000"),
        )
        self.assertEqual(dict(counts), {
            "posted_balance_matches": 1, "fiscalbot_review": 1, "draft_candidate": 1,
            "foreign_currency_review": 1, "opening_missing_or_outside_cutoff": 1,
            "nonpositive_or_pending_sga_payment": 1,
        })
        self.assertEqual(drafts[0].amount, Decimal("62.35"))
        self.assertEqual(drafts[0].reference, "MISSING1")
        self.assertTrue(drafts[0].external_id.startswith("__import__.sga_vendor_balance_"))
        self.client.call.assert_not_called()

    def test_recurring_scan_does_not_reimport_a_previous_draft(self):
        draft = document("MISSING1")
        _currency, _counts, drafts = self.plan(draft)
        self.assertEqual(len(drafts), 1)
        self.records["account.move"].append({
            **self.records["account.move"][0], "id": 42, "state": "draft", "ref": "MISSING1", "amount_total": 62.35,
        })
        _currency, counts, drafts = self.plan(draft)
        self.assertEqual(counts["existing_bill_review"], 1)
        self.assertFalse(drafts)
        self.client.call.assert_not_called()

    def test_simulation_without_approved_opening_creates_no_candidates(self):
        _currency, counts, drafts = self.plan(document("MISSING1"), opening=False)
        self.assertEqual(counts["opening_missing_or_outside_cutoff"], 1)
        self.assertFalse(drafts)

    def test_reports_stale_drafts_without_resending_them(self):
        _currency, _counts, drafts = self.plan(document("MISSING1"))
        self.records["account.move"].append({
            **self.records["account.move"][0], "id": 42, "state": "draft", "ref": "MISSING1", "amount_total": 62.35,
            "invoice_origin": drafts[0].origin,
        })
        self.assertEqual(self.plan(document("MISSING1"))[1]["opening_draft_matches"], 1)
        self.assertEqual(self.plan(document("MISSING1", residual="53.0000"))[1]["opening_draft_review"], 1)
        self.assertEqual(self.plan()[1]["opening_draft_no_longer_open_review"], 1)
        self.client.call.assert_not_called()

    def test_import_uses_native_protected_ids_and_checks_accounting(self):
        _currency, _counts, drafts = self.plan(document("MISSING1"))
        self.client.call.return_value = {"ids": [42], "messages": [], "nextrow": 1}
        self.records["account.move"].append({
            **self.records["account.move"][0], "id": 42, "state": "draft", "ref": "MISSING1",
            "date": self.opening.date, "amount_total": 62.35, "invoice_line_ids": [52],
            "invoice_origin": drafts[0].origin,
        })
        self.records["account.move.line"] = [{"id": 52, "move_id": 42, "account_id": 22,
                                              "quantity": 1.0, "price_unit": 62.35, "tax_ids": []}]
        self.assertEqual(load_drafts(self.client, 1, 2, self.opening, drafts), 1)
        args, call = self.client.call.call_args
        self.assertEqual(args, ("account.move", "load"))
        self.assertEqual(call["context"]["mode"], "update")
        self.assertTrue(call["context"]["noupdate"])
        self.assertEqual(call["data"][0][-2:], ["62.35", ""])
        self.records["account.move.line"][0]["tax_ids"] = [3]
        with self.assertRaisesRegex(ValueError, "accounting readback"):
            load_drafts(self.client, 1, 2, self.opening, drafts)

    def test_source_protection_covers_header_and_detail(self):
        source = Mock()
        source.rows.return_value = [{"Pedido": 25, "Proveedor": "T03", "Factura": "BANK001"}]
        source.open_documents.side_effect = [
            [document("BANK001"), document("REF12345"), document("MISSING1")], [],
        ]
        rows, excluded = collect_documents(source, [25], ["12345"])
        self.assertEqual(excluded, 2)
        self.assertEqual([row["invoice_number"] for row in rows.values()], ["MISSING1"])
        source.rows.return_value = []
        with self.assertRaisesRegex(ValueError, "Protected order"):
            collect_documents(source, [25], [])
