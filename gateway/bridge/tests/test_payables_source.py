import json
import sqlite3
import tempfile
from contextlib import nullcontext
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from django.core.management.base import CommandError

from bridge.catalog_client import CatalogOdoo, CatalogRemoteError
from bridge.management.commands.sync_payables import Command
from bridge.payables_source import PayablesSource, PayablesSourceError


def _debt(**changes):
    return {
        "Proveedor": "OTR",
        "FacturaPrimaria": "1",
        "Acreedor": "007",
        "Factura": "000025",
        "Fecha": datetime(2026, 1, 15, 12),
        "TipoCredito": 30,
        "Monto": 100.25,
        "MontoCancelado": 20,
        "MontoNotas": -5,
        "Retencion": 1.25,
        "MontoPagosPend": 10,
        "Actualizada": b"\x01",
        "Moneda": None,
        "TipoCambio": 1,
        **changes,
    }


def _supplier(**changes):
    return {
        "Proveedor": "007",
        "ProveedorNomb": "Proveedor ficticio",
        "Cedula": "DEMO-007",
        "Activo": b"\x01",
        "EMail": "general@example.test",
        "EMailEnvio": "payments@example.test",
        "Banco": "0001",
        "IBAN": None,
        **changes,
    }


class PayablesSourceTest(TestCase):
    def setUp(self):
        self.source = PayablesSource({
            "currency_map": {"¢": "CRC", "$": "USD"},
            "national_currency_code": "¢",
        })
        self.source.rows = Mock()

    def test_partial_payment_notes_retention_and_pending(self):
        self.source.rows.return_value = [_debt()]
        document = self.source.open_documents()[0]
        self.assertEqual(document["key"], ["OTR", "1", "007", "000025"])
        self.assertEqual(document["invoice_number"], "000025")
        self.assertEqual(document["due_date"], "2026-02-14")
        self.assertEqual(document["currency"], "CRC")
        self.assertEqual(document["original_amount"], "100.2500")
        self.assertEqual(document["pending_amount"], "10.0000")
        self.assertEqual(document["residual"], "74.0000")
        self.assertEqual(document["source_state"], "open")
        self.assertEqual(document["fiscal_key"], "")
        self.assertEqual(self.source.open_documents()[0], document)

        self.source.rows.return_value = [_debt(MontoPagosPend=15)]
        changed = self.source.open_documents()[0]
        self.assertNotEqual(changed["fingerprint"], document["fingerprint"])
        self.assertEqual(changed["residual"], document["residual"])

    def test_multicurrency_preserves_document_denomination(self):
        self.source.rows.return_value = [_debt(Moneda="$", TipoCambio=520)]
        document = self.source.open_documents()[0]
        self.assertEqual(document["currency"], "USD")
        self.assertEqual(document["residual"], "74.0000")

        for changes in ({"Moneda": "$", "TipoCambio": 1},
                        {"Moneda": None, "TipoCambio": 520},
                        {"Moneda": "$", "TipoCambio": None},
                        {"Moneda": "?", "TipoCambio": 520}):
            with self.subTest(changes=changes):
                self.source.rows.return_value = [_debt(**changes)]
                self.assertIsNone(self.source.open_documents()[0]["currency"])

    def test_closure_and_unposted_by_exact_key(self):
        self.source.rows.return_value = [
            _debt(MontoCancelado=94),
            _debt(Factura="000026", Actualizada=b"\x00"),
            _debt(Factura="FACTURA"),
        ]
        keys = [["OTR", "1", "007", n] for n in ("000025", "000026", "factura")]
        documents = self.source.documents_by_keys(keys)
        self.assertEqual([doc["source_state"] for doc in documents], ["closed", "unposted"])
        self.assertEqual(documents[0]["residual"], "0.0000")
        sql, params = self.source.rows.call_args.args
        self.assertTrue(sql.startswith("SELECT "))
        self.assertIn(" IN (", sql)
        self.assertNotIn("000025", sql)
        self.assertEqual(params, tuple(part for key in keys for part in key))

    def test_missing_retention_is_indeterminate(self):
        self.source.rows.return_value = [_debt(Retencion=None)]
        document = self.source.open_documents()[0]
        self.assertIsNone(document["retention_amount"])
        self.assertIsNone(document["residual"])
        self.assertEqual(document["source_state"], "open")

    def test_open_keyset_preserves_leading_zeroes(self):
        self.source.rows.return_value = []
        after = ["OTR", "1", "007", "000025"]
        self.source.open_documents(after=after, limit=10)
        sql, params = self.source.rows.call_args.args
        self.assertIn("d.Actualizada = 1", sql)
        self.assertIn("d.Retencion IS NULL", sql)
        self.assertIn("ORDER BY d.Proveedor, d.FacturaPrimaria, d.Acreedor, d.Factura LIMIT %s", sql)
        self.assertEqual(params, (*after, 10))

    def test_valid_notifications_and_invalid_or_missing_payment_details(self):
        body = "000000000000000001"
        iban = f"CR{98 - int(body + '122700') % 97:02d}{body}"
        self.source.rows.return_value = [
            _supplier(IBAN=f"{iban[:4]} {iban[4:]}"),
            _supplier(EMailEnvio="payments@example.test, another@example.test", IBAN=iban[:-1] + "9"),
            _supplier(EMailEnvio="", IBAN=None, Banco=None),
        ]
        good, invalid, missing = self.source.contacts(after="006", limit=3)
        self.assertEqual(good["payment_email"], "payments@example.test")
        self.assertEqual(good["iban"], iban)
        self.assertEqual(good["bank_code"], "0001")
        self.assertNotIn("payment_email", invalid)
        self.assertNotIn("iban", invalid)
        self.assertIsNone(missing["payment_email"])
        self.assertIsNone(missing["bank_code"])
        self.assertIsNone(missing["iban"])
        sql, params = self.source.rows.call_args.args
        self.assertNotIn("EMail,", sql)
        self.assertEqual(params, ("006", 3))

    def test_invalid_input_does_not_embed_private_values_in_errors(self):
        self.source.rows.return_value = [_debt(Monto=float("nan"))]
        with self.assertRaisesRegex(PayablesSourceError, "^SGA_PAYABLE_AMOUNT_INVALID$"):
            self.source.open_documents()
        with self.assertRaisesRegex(PayablesSourceError, "^SGA_PAYABLE_KEY_INVALID$"):
            self.source.documents_by_keys([("OTR", "1", "007", "invalid\nkey")])

    def test_queries_reconcile_actual_rows_in_an_isolated_sql_fixture(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        database.executescript(
            """
            CREATE TABLE cuentaspagar (Proveedor TEXT, FacturaPrimaria TEXT);
            CREATE TABLE proveedoresimport (Proveedor TEXT, Moneda TEXT);
            CREATE TABLE cuentaspagardet (
                Proveedor TEXT, FacturaPrimaria TEXT, Acreedor TEXT, Factura TEXT,
                Fecha TEXT, TipoCredito INTEGER, Monto NUMERIC, MontoCancelado NUMERIC,
                MontoNotas NUMERIC, Retencion NUMERIC, MontoPagosPend NUMERIC,
                Actualizada INTEGER, TipoCambio NUMERIC
            );
            INSERT INTO cuentaspagar VALUES ('OTR', '1');
            INSERT INTO proveedoresimport VALUES ('007', '$');
            INSERT INTO cuentaspagardet VALUES
                ('OTR', '1', '007', '000025', '2026-01-15', 30, 100.25, 20, -5, 1.25, 10, 1, 520),
                ('OTR', '1', '007', '000026', '2026-01-15', 30, 100.25, 94, -5, 1.25,  0, 1, 520),
                ('OTR', '1', '007', '000027', '2026-01-15', 30, 100.25, 20, -5, 1.25,  0, 0, 520),
                ('OTR', '1', '007', '000028', '2026-01-15', 30, 100.25, 20, -5, NULL, 0, 1, 520);
            """
        )

        def select(sql, params):
            rows = [dict(row) for row in database.execute(sql.replace("%s", "?"), params)]
            for row in rows:
                row["Fecha"] = datetime.fromisoformat(row["Fecha"])
            return rows

        self.source.rows = select
        first, = self.source.open_documents(limit=1)
        second, = self.source.open_documents(after=first["key"], limit=1)
        self.assertEqual((first["invoice_number"], second["invoice_number"]), ("000025", "000028"))
        self.assertEqual((first["currency"], first["residual"]), ("USD", "74.0000"))
        self.assertIsNone(second["residual"])
        keys = [["OTR", "1", "007", n] for n in ("000026", "000027")]
        self.assertEqual([doc["source_state"] for doc in self.source.documents_by_keys(keys)],
                         ["closed", "unposted"])


class PayablesCommandTest(TestCase):
    routes = "bridge.management.commands.sync_payables"
    source_config = {"instance": "fixtures"}
    target_config = {"database": "lab", "company_id": 1, "application_id": "app"}
    open_key = ["001", "A", "001", "A"]
    closed_key = ["001", "B", "001", "B"]
    missing_key = ["001", "C", "001", "C"]

    def setUp(self):
        self.output = StringIO()
        self.source = Mock()
        self.source.contacts.side_effect = [[{"code": "001"}], []]
        self.source.open_documents.side_effect = [[{"key": self.open_key}], []]
        self.source.documents_by_keys.return_value = [{"key": self.closed_key}]
        self.client = Mock()
        self.client.call.side_effect = self._respond
        self.responses = {
            "catalog_info": {
                "contract": 1, **self.target_config, "source_instance": "fixtures",
                "authentication": "application_token",
            },
        }
        self._patch("configuration", side_effect=[self.source_config, self.target_config])
        self._patch("CatalogOdoo", return_value=self.client)
        self._patch("PayablesSource", return_value=nullcontext(self.source))
        self._patch("connection", Mock(vendor="sqlite"))

    def _patch(self, name, *args, **kwargs):
        patcher = patch(f"{self.routes}.{name}", *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _respond(self, method, **data):
        if method == "payables_keys":
            if data["after_id"] == 0:
                return {"keys": [self.open_key, self.closed_key], "next_id": 2}
            return {"keys": [], "next_id": None}
        if method == "apply_payables_snapshot":
            return {
                "documents": len(data["documents"]),
                "suppliers": len(data["suppliers"]),
                "dry_run": data["dry_run"],
                "quarantined": 0,
                "missing_keys": len(data.get("missing_keys", [])),
            }
        return self.responses[method]

    def test_reconciles_tracked_closures_and_never_infers_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            keys = Path(folder) / "keys.json"
            keys.write_text(json.dumps([self.missing_key, self.closed_key]))
            Command(stdout=self.output).handle(
                apply=False, batch_size=50, document_key_file=keys,
            )
        self.source.documents_by_keys.assert_called_once()
        self.assertCountEqual(
            self.source.documents_by_keys.call_args.args[0],
            [tuple(self.missing_key), tuple(self.closed_key)],
        )
        delivery = [call.kwargs for call in self.client.call.call_args_list
                    if call.args[0] == "apply_payables_snapshot"]
        self.assertEqual([batch["dry_run"] for batch in delivery], [True, True, True])
        self.assertEqual(delivery[-1]["missing_keys"], [self.missing_key])
        self.assertTrue(all("missing_keys" not in batch for batch in delivery[:-1]))
        self.assertIn("SIMULATION suppliers=1 documents=2 quarantined=0 missing_tracked=1", self.output.getvalue())
        self.assertNotIn(str(self.missing_key), self.output.getvalue())

    def test_fails_closed_when_destination_does_not_match(self):
        self.responses["catalog_info"]["company_id"] = 2
        with self.assertRaisesRegex(CommandError, "source, company or application mismatch"):
            Command(stdout=self.output).handle(apply=True, batch_size=50, document_key_file=None)
        self.source.contacts.assert_not_called()
        self.client.close.assert_called_once()

    def test_transport_error_does_not_print_payload_or_credentials(self):
        self.client.call.side_effect = OSError("payments@example.test")
        with self.assertRaisesRegex(CommandError, r"^Payables scan failed \(OSError\)$"):
            Command(stdout=self.output).handle(apply=True, batch_size=50, document_key_file=None)
        self.client.close.assert_called_once()


class PayablesTransportTest(TestCase):
    @patch("bridge.catalog_client.requests.Session")
    def test_routes_reuse_application_token_and_keep_response_body_private(self, session_factory):
        config = {
            "url": "https://odoo.example.test", "database": "lab",
            "token": "sgw1_" + ("x" * 40),
        }
        client = CatalogOdoo(config)
        session = session_factory.return_value
        session.post.return_value.status_code = 200
        routes = {
            "payables_keys": "payables/keys", "apply_payables_snapshot": "payables/snapshot",
            "payables_poll": "payables/poll", "payables_ack": "payables/ack",
        }
        for method, path in routes.items():
            with self.subTest(method=method):
                client.call(method, instance="fixture")
                session.post.assert_called_with(
                    f"https://odoo.example.test/sga_gateway/api/v1/{path}",
                    headers={"Authorization": "Bearer " + config["token"], "X-Odoo-Database": "lab"},
                    json={"instance": "fixture"}, timeout=(10, 120), allow_redirects=False,
                )
        session.post.return_value.status_code = 409
        session.post.return_value.json.reset_mock()
        with self.assertRaisesRegex(CatalogRemoteError, "^ODOO_CATALOG_HTTP_409$"):
            client.call("apply_payables_snapshot", documents=[])
        session.post.return_value.json.assert_not_called()
