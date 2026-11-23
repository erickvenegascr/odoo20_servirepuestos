import io
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.core.management.base import CommandError

from bridge.management.commands import sync_receipt_counts as command
from bridge.odoo_json2 import OdooJson2Error
from bridge.receipt_count_source import COLUMNS, ReceiptCountError, ReceiptCountSource, ReceiptPage, map_receipt


ZONE = ZoneInfo("America/Costa_Rica")


def header(receipt=101, **values):
    return {
        "Recibo": receipt, "Proveedor": "T01", "Bodega": 3, "Factura": "LOCAL-INVOICE",
        "FechaRecibo": datetime(2026, 11, 18, 8, 30), "RecibidoCompleto": b"\x01", **values,
    }


def line(receipt=101, **values):
    return {
        "Recibo": receipt, "Articulo": 7, "ArticuloNomb": "Artículo sintético",
        "PorMayor": b"\x00", "Unidad": "Caja", "UnidadDet": "Pieza", "CantRecibida": 11,
        "Recibido": b"\x01", "Actualizado": b"\x01", "NoRecibido": b"\x00", **values,
    }


class ReceiptMappingTests(TestCase):
    def test_only_applied_positive_lines_keep_distinct_native_presentations(self):
        payload, reason = map_receipt(header(), [
            line(), line(PorMayor=b"\x01", CantRecibida=2), line(Articulo=8, CantRecibida=0),
            line(Articulo=9, Actualizado=b"\x00"), line(Articulo=10, Recibido=b"\x00"),
            line(Articulo=11, NoRecibido=b"\x01", CantRecibida=None),
        ], "count-lab", ZONE)
        self.assertIsNone(reason)
        self.assertEqual(payload["source"], "count-lab")
        self.assertEqual(payload["receipt_key"], "101")
        self.assertEqual(payload["reference"], "Recibo 101 · LOCAL-INVOICE")
        second, _ = map_receipt(header(102), [line(102)], "count-lab", ZONE)
        self.assertEqual(second["reference"], "Recibo 102 · LOCAL-INVOICE")
        self.assertEqual(payload["received_at"], "2026-11-18 14:30:00")
        self.assertEqual(payload["location"], "Bodega 3")
        self.assertEqual(payload["sender_name"], "Proveedor T01")
        self.assertEqual([(r["key"], r["quantity"], r["unit"]) for r in payload["lines"]],
                         [("7:0", 11, "Detalle · Pieza"), ("7:1", 2, "Mayor · Caja")])
        self.assertTrue(all(r["description"] == "Artículo sintético" for r in payload["lines"]))

    def test_incomplete_empty_cross_receipt_and_duplicate_evidence(self):
        self.assertEqual(map_receipt(header(RecibidoCompleto=b"\x00"), [line()], "lab", ZONE), (None, "incomplete"))
        self.assertEqual(map_receipt(header(), [line(CantRecibida=-1)], "lab", ZONE), (None, "without_applied_lines"))
        with self.assertRaisesRegex(ReceiptCountError, "LINE_SCOPE_MISMATCH"):
            map_receipt(header(), [line(102)], "lab", ZONE)
        with self.assertRaisesRegex(ReceiptCountError, "DUPLICATE_LINE"):
            map_receipt(header(), [line(), line()], "lab", ZONE)

    def test_watermark_requires_timestamp_refresh_on_both_receipt_tables(self):
        source = ReceiptCountSource({})
        columns = [
            {"TABLE_NAME": table, "COLUMN_NAME": name, "DATA_TYPE": "timestamp",
             "IS_NULLABLE": "NO", "EXTRA": "on update CURRENT_TIMESTAMP", "ENGINE": "InnoDB"}
            for table, names in COLUMNS.items() for name in names
        ]
        source.rows = MagicMock(return_value=columns)
        source.check_schema()
        next(row for row in columns if row["TABLE_NAME"] == "recibosart" and row["COLUMN_NAME"] == "pgts")["EXTRA"] = ""
        with self.assertRaisesRegex(ReceiptCountError, "WATERMARK_UNVERIFIED"):
            source.check_schema()

    def test_paging_handles_updated_detail_timestamp_ties_and_scope(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.create_function("UNIX_TIMESTAMP", 1, int)
        db.create_function("FROM_UNIXTIME", 1, int)
        db.executescript("""
            CREATE TABLE recibos(Recibo integer PRIMARY KEY,Proveedor text,Bodega integer,Factura text,
                                 Pedido integer,FechaRecibo text,RecibidoCompleto integer,PGTS integer);
            CREATE TABLE recibosart(Recibo integer,PorMayor integer,Articulo integer,CantRecibida integer,
                                    Recibido integer,Actualizado integer,NoRecibido integer,PGTS integer);
            CREATE TABLE artsservs(Articulo integer,ArticuloNomb text,Unidad text,UnidadDet text);
            INSERT INTO artsservs VALUES (7,'Producto laboratorio','Caja','Pieza');
            INSERT INTO recibos VALUES
              (101,'T01',3,'A',NULL,'2026-11-18 08:30:00',1,201),
              (102,'T01',3,'A',NULL,'2026-11-18 08:30:00',1,210),
              (103,'T01',6,'A',NULL,'2026-11-18 08:30:00',1,210),
              (104,'T01',3,'A',99,'2026-11-18 08:30:00',1,210),
              (105,'T01',3,'A',NULL,'2026-10-18 08:30:00',1,210),
              (106,'T01',3,'A',NULL,'2026-11-18 08:30:00',0,211),
              (107,'T01',3,'A',NULL,'2026-11-18 08:30:00',1,230);
            INSERT INTO recibosart VALUES (101,1,7,3,1,1,0,210),(102,0,7,5,1,1,0,201),(106,0,7,3,1,1,0,211);
        """)
        reader = ReceiptCountSource({})

        def rows(query, parameters=()):
            self.assertTrue(query.startswith("SELECT "))
            result = [dict(row) for row in db.execute(query.replace("%s", "?"), parameters)]
            for row in result:
                if "FechaRecibo" in row:
                    row["FechaRecibo"] = datetime.fromisoformat(row["FechaRecibo"])
            return result

        reader.rows = rows
        scope = dict(since="2026-11-18", warehouses=(3,), excluded_orders=(99,), lower=205, upper=230, limit=1)
        first = reader.page(after=None, **scope)
        second = reader.page(after=first.after, **scope)
        last = reader.page(after=second.after, **scope)
        self.assertEqual([first.after, second.after, last.after], [(210, 101), (210, 102), (211, 106)])
        self.assertEqual((first.more, second.more, last.more), (True, True, False))
        self.assertEqual(first.rows[0][1][0]["CantRecibida"], 3)
        self.assertEqual(last.rows[0][1], [])


class ReceiptCommandTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.stdout = io.StringIO()
        self.cli = command.Command(stdout=self.stdout)
        self.config = {
            "SGA_RECEIPT_COUNT_SOURCE": {
                "host": "source.invalid", "instance": "count-lab", "warehouses": [3], "timezone": ZONE.key,
            },
            "ODOO_RECEIPT_COUNT_TARGET": {
                "source_instance": "count-lab", "url": "https://target.invalid", "database": "lab", "company_id": 2,
            },
        }
        self.reader_context = MagicMock()
        self.reader = self.reader_context.__enter__.return_value
        self.reader.epoch.return_value = 400
        self.reader.page.return_value = ReceiptPage(((header(), [line()]), (header(102), [line(102)])), (399, 102), False)
        self.client_type = MagicMock()
        for patcher in (
            patch.object(command, "configuration", side_effect=self.config.__getitem__),
            patch.object(command, "ReceiptCountSource", return_value=self.reader_context),
            patch.object(command, "OdooJson2Client", self.client_type),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, **values):
        self.cli.handle(**{
            "check": False, "apply": False, "checkpoint": self.path,
            "since": date(2026, 11, 18), "page_size": 2, **values,
        })

    def test_read_only_check_and_preview_make_no_odoo_call_or_checkpoint(self):
        self.run_command(check=True, since=None)
        self.reader.check_schema.assert_called_once()
        self.reader.page.assert_not_called()
        self.run_command()
        self.client_type.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertNotIn("LOCAL-INVOICE", self.stdout.getvalue())
        preview = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual((preview["mode"], preview["read"], preview["eligible"]), ("preview", 2, 2))

    def test_partial_failure_keeps_checkpoint_and_replay_uses_exact_api_contract(self):
        client = self.client_type.return_value
        client.call.side_effect = [
            {"id": 8081, "created": True},
            OdooJson2Error("ODOO_JSON2_HTTP_422"),
            {"id": 8081, "created": False},
            {"id": 8082, "created": True},
        ]
        with self.assertRaisesRegex(CommandError, "Fila 2 del lote: ODOO_JSON2_HTTP_422"):
            self.run_command(apply=True)
        self.assertFalse(self.path.exists())
        self.run_command(apply=True)
        first, second, replay, _ = client.call.call_args_list
        self.assertEqual(first, replay)
        self.assertEqual(first.args, ("receipt.count", "import_receipt"))
        self.assertNotIn("context", first.kwargs)
        self.assertEqual(second.kwargs["receipt_key"], "102")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        state = json.loads(self.path.read_text())
        self.assertEqual((state["upper"], state["after"]), (400, None))
        self.assertNotIn("LOCAL-INVOICE", self.stdout.getvalue())
        self.assertNotIn("8081", self.stdout.getvalue())
        self.assertEqual(json.loads(self.stdout.getvalue())["unchanged"], 1)

    def test_resume_keeps_window_and_next_scan_overlaps(self):
        self.client_type.return_value.call.return_value = {"id": 5, "created": False}
        self.reader.page.return_value = ReceiptPage(((header(), [line()]),), (390, 101), True)
        self.run_command(apply=True)
        self.reader.epoch.return_value = 450
        self.reader.page.return_value = ReceiptPage((), None, False)
        self.run_command(apply=True)
        second = self.reader.page.call_args.kwargs
        self.assertEqual((second["lower"], second["upper"], second["after"]), (1, 400, (390, 101)))
        self.run_command(apply=True)
        third = self.reader.page.call_args.kwargs
        self.assertEqual((third["lower"], third["upper"], third["after"]), (280, 450, None))
