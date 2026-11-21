import json
import sqlite3
from uuid import uuid4

from django.test import TestCase

from bridge.models import PayablesInbox
from bridge.payables_exchange import _digest, exchange_once, receive, record_readback


class SyntheticLedger:
    """Transactional native receipt and balance simulator for this test only."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE debt (native_key TEXT PRIMARY KEY, currency TEXT NOT NULL, cents INTEGER NOT NULL);
            CREATE TABLE receipt (operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL);
        """)

    def put(self, key, currency, cents):
        with self.db:
            self.db.execute("INSERT INTO debt VALUES (?, ?, ?)", (json.dumps(key), currency, cents))

    def balance(self, key):
        return self.db.execute("SELECT cents FROM debt WHERE native_key = ?", (json.dumps(key),)).fetchone()[0]

    def apply_and_readback(self, payload, payload_hash):
        operation_id = payload["operation_id"]
        cached = self.db.execute("SELECT payload_hash, result FROM receipt WHERE operation_id = ?", (operation_id,)).fetchone()
        if cached:
            if payload_hash != cached[0]:
                raise ValueError("NATIVE_OPERATION_CONFLICT")
            return json.loads(cached[1])

        with self.db:
            for row in payload["allocations"]:
                existing = self.db.execute("SELECT currency, cents FROM debt WHERE native_key = ?",
                                           (json.dumps(row["key"]),)).fetchone()
                if existing != (payload["currency"], int(row["before"].replace(".", ""))):
                    return {"state": "rejected", "payload_hash": payload_hash, "reason": "stale_balance"}
            if payload["operation"] == "reverse" and not self.db.execute(
                "SELECT 1 FROM receipt WHERE operation_id = ?", (payload["original_operation_id"],),
            ).fetchone():
                return {"state": "rejected", "payload_hash": payload_hash, "reason": "native_validation"}
            observed = []
            for row in payload["allocations"]:
                self.db.execute("UPDATE debt SET cents = ? WHERE native_key = ?",
                                (int(row["expected_after"].replace(".", "")), json.dumps(row["key"])))
                cents = self.balance(row["key"])
                observed.append({"key": row["key"], "after": f"{cents // 100}.{cents % 100:02}"})
            result = {"state": "confirmed", "payload_hash": payload_hash,
                      "native_receipt": "SYN-" + operation_id, "allocations": observed}
            self.db.execute("INSERT INTO receipt VALUES (?, ?, ?)", (operation_id, payload_hash, json.dumps(result)))
            return result


class SyntheticOdoo:
    def __init__(self, delivery):
        self.delivery = delivery
        self.fail_first_ack = False
        self.acknowledgements = []

    def call(self, method, **kwargs):
        if method == "payables_poll":
            return {"events": [self.delivery]}
        if method == "payables_ack":
            if self.fail_first_ack:
                self.fail_first_ack = False
                raise ConnectionError
            self.acknowledgements.append(kwargs["result"])
            return {"operation_id": kwargs["operation_id"], "state": kwargs["result"]["state"]}
        raise AssertionError(method)


class ExchangeTests(TestCase):
    def setUp(self):
        self.app = uuid4()
        self.instance = "cxp-test"
        self.key = ["T55", "FIC-01", "T55", "FIC-DET"]
        self.ledger = SyntheticLedger()
        self.addCleanup(self.ledger.db.close)

    def delivery(self, currency="USD", before="100.00", amount="25.00", after="75.00", original=None):
        operation_id = str(uuid4())
        payload = {
            "schema_version": 1, "operation_id": operation_id, "instance": self.instance,
            "operation": "reverse" if original else "apply", "currency": currency,
            "bank_reference": "SYN-BANK-01", "date": "2026-01-20",
            "original_operation_id": original, "allocations": [{
                "key": self.key, "amount": amount, "before": before,
                "expected_after": after, "source_fingerprint": "a" * 64,
            }],
        }
        return {"operation_id": operation_id, "lease": str(uuid4()), "payload_hash": _digest(payload), "payload": payload}

    def test_partial_payment_ack_retry_and_reverse(self):
        self.ledger.put(self.key, "USD", 10000)
        delivery = self.delivery()
        odoo = SyntheticOdoo(delivery)
        odoo.fail_first_ack = True
        with self.assertRaises(ConnectionError):
            exchange_once(odoo, self.app, self.instance, adapter=self.ledger)
        self.assertEqual(self.ledger.balance(self.key), 7500)
        native = self.ledger.apply_and_readback(delivery["payload"], delivery["payload_hash"])
        self.assertEqual(self.ledger.balance(self.key), 7500)
        odoo.delivery = {**delivery, "lease": str(uuid4())}
        self.assertEqual(exchange_once(odoo, self.app, self.instance, adapter=self.ledger)["acknowledged"], 1)
        self.assertEqual(odoo.acknowledgements[0], native)
        reverse = self.delivery(before="75.00", after="100.00", original=delivery["operation_id"])
        odoo.delivery = reverse
        exchange_once(odoo, self.app, self.instance, adapter=self.ledger)
        exchange_once(odoo, self.app, self.instance, adapter=self.ledger)
        self.assertEqual(self.ledger.balance(self.key), 10000)
        self.assertEqual(PayablesInbox.objects.count(), 2)

    def test_stale_balance_or_currency_keeps_native_balance(self):
        self.ledger.put(self.key, "CRC", 10000)
        odoo = SyntheticOdoo(self.delivery())
        exchange_once(odoo, self.app, self.instance, adapter=self.ledger)
        self.assertEqual(odoo.acknowledgements[0]["state"], "rejected")
        self.assertEqual(self.ledger.balance(self.key), 10000)

    def test_missing_adapter_only_stores_instruction(self):
        odoo = SyntheticOdoo(self.delivery())
        result = exchange_once(odoo, self.app, self.instance)
        self.assertEqual(result, {"received": 1, "awaiting_native": 1, "acknowledged": 0})
        self.assertEqual(PayablesInbox.objects.count(), 1)
        self.assertFalse(odoo.acknowledgements)

    def test_mismatched_readback_and_changed_replay_rejected(self):
        delivery = self.delivery()
        row = receive(self.app, self.instance, delivery)
        wrong = {"state": "confirmed", "payload_hash": row.payload_hash, "native_receipt": "SYN-2",
                 "allocations": [{"key": self.key, "after": "74.00"}]}
        with self.assertRaisesRegex(ValueError, "MISMATCH"):
            record_readback(self.app, row.operation_id, wrong)
        delivery["payload"]["bank_reference"] = "ALTERED"
        delivery["payload_hash"] = _digest(delivery["payload"])
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            receive(self.app, self.instance, delivery)
