"""Preview or import one bounded page of applied SGA receipts."""

import fcntl
import hashlib
import json
import os
import re
import stat
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.management.base import BaseCommand, CommandError

from ...catalog_source import configuration
from ...odoo_json2 import OdooJson2Client, OdooJson2Error
from ...receipt_count_source import MAX_PAGE, ReceiptCountError, ReceiptCountSource, map_receipt, source_zone


def _scope(source, target, since):
    instance = source["instance"]
    warehouses = source["warehouses"]
    orders = source.get("exclude_order_ids", [])
    if (not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", instance)
            or instance != target["source_instance"]
            or type(target["company_id"]) is not int or target["company_id"] <= 0
            or not isinstance(warehouses, list) or not 1 <= len(warehouses) <= 32
            or any(type(item) is not int or not 1 <= item <= 32767 for item in warehouses)
            or not isinstance(orders, list) or len(orders) > 100
            or any(type(item) is not int or item <= 0 for item in orders)):
        raise ReceiptCountError("RECEIPT_COUNT_SCOPE_INVALID")
    zone = source_zone(source["timezone"])
    overlap = source.get("overlap_seconds", 120)
    if type(overlap) is not int or not 2 <= overlap <= 3600:
        raise ReceiptCountError("RECEIPT_COUNT_OVERLAP_INVALID")
    warehouses = tuple(sorted(set(warehouses)))
    orders = tuple(sorted(set(orders)))
    identity = [instance, source["host"], int(source.get("port", 3306)), source.get("database", "sgadata"),
                warehouses, orders, zone.key, overlap,
                target["url"], target["database"], target["company_id"], since]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return warehouses, orders, zone, overlap, digest


@contextmanager
def _locked_checkpoint(path):
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ReceiptCountError("RECEIPT_COUNT_CHECKPOINT_UNPROTECTED")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReceiptCountError("RECEIPT_COUNT_CHECKPOINT_IN_USE") from None
        yield
    finally:
        os.close(descriptor)


def _load_checkpoint(path, digest):
    if not path:
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 2048:
            raise ReceiptCountError("RECEIPT_COUNT_CHECKPOINT_UNPROTECTED")
        state = json.load(stream)
    if (not isinstance(state, dict) or set(state) != {"scope", "lower", "upper", "after"}
            or state["scope"] != digest
            or type(state["lower"]) is not int or type(state["upper"]) is not int
            or not 1 <= state["lower"] <= state["upper"]):
        raise ReceiptCountError("RECEIPT_COUNT_CHECKPOINT_SCOPE_INVALID")
    after = state["after"]
    if after is not None and (
        not isinstance(after, list) or len(after) != 2 or any(type(number) is not int for number in after)
        or not state["lower"] <= after[0] < state["upper"] or after[1] <= 0
    ):
        raise ReceiptCountError("RECEIPT_COUNT_CHECKPOINT_CURSOR_INVALID")
    return state


def _save_checkpoint(path, state):
    staging = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".receipt-count-",
                                delete=False) as stream:
            staging = Path(stream.name)
            json.dump(state, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        if staging:
            staging.unlink(missing_ok=True)


def _scan(state, now, overlap):
    if state is None:
        return 1, now, None
    if now < state["upper"]:
        raise ReceiptCountError("RECEIPT_COUNT_SOURCE_CLOCK_REGRESSED")
    if state["after"] is not None:
        return state["lower"], state["upper"], tuple(state["after"])
    return max(1, state["upper"] - overlap), now, None


def _prepare(source_config, state, since, warehouses, orders, zone, overlap, limit):
    with ReceiptCountSource(source_config) as source:
        upper_hint = source.epoch()
        source.check_schema()
        lower, upper, after = _scan(state, upper_hint, overlap)
        page = source.page(since=since, warehouses=warehouses, excluded_orders=orders,
                           lower=lower, upper=upper, after=after, limit=limit)
    prepared = []
    skipped = Counter()
    for ordinal, (header, details) in enumerate(page.rows, 1):
        try:
            payload, reason = map_receipt(header, details, source_config["instance"], zone)
        except ReceiptCountError as exc:
            raise CommandError(f"Fila {ordinal} del lote: {exc}. El cursor permanece en el lote anterior.") from None
        if payload is not None:
            prepared.append((ordinal, payload))
        else:
            skipped[reason] += 1
    return page, lower, upper, prepared, skipped


def _send_page(target, prepared):
    client = OdooJson2Client(target)
    result = Counter()
    try:
        for ordinal, payload in prepared:
            try:
                response = client.call("receipt.count", "import_receipt", **payload)
                if (not isinstance(response, dict) or set(response) != {"id", "created"}
                        or type(response["id"]) is not int or response["id"] <= 0
                        or type(response["created"]) is not bool):
                    raise OdooJson2Error("RECEIPT_COUNT_INVALID_IMPORT_RESPONSE", outcome_unknown=True)
            except OdooJson2Error as exc:
                raise CommandError(
                    f"Fila {ordinal} del lote: {exc.code}. "
                    f"Recepciones verificadas en esta ejecución: {sum(result.values())}. "
                    "Revise Odoo y reintente el mismo lote. El cursor permanece en el lote anterior."
                ) from None
            result["created" if response["created"] else "unchanged"] += 1
    finally:
        client.close()
    return result


class Command(BaseCommand):
    help = "Previsualiza o importa una página de ingresos SGA ya aplicados para el tablero de conteo."

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--check", action="store_true", help="Comprueba sólo permisos de lectura y esquema SGA.")
        modes.add_argument("--apply", action="store_true", help="Entrega el lote a Odoo y guarda el cursor al completarlo.")
        parser.add_argument("--since", type=date.fromisoformat, help="Fecha inicial de recepción en la zona horaria SGA.")
        parser.add_argument("--page-size", type=int, default=50, help=f"Recepciones por ejecución. Máximo {MAX_PAGE}.")
        parser.add_argument("--checkpoint", type=Path, help="Archivo privado de avance exigido al aplicar.")

    def handle(self, *args, **options):
        try:
            source = configuration("SGA_RECEIPT_COUNT_SOURCE")
            target = configuration("ODOO_RECEIPT_COUNT_TARGET")
            since = options["since"].isoformat() if options["since"] else None
            warehouses, orders, zone, overlap, scope = _scope(source, target, since)

            if options["check"]:
                with ReceiptCountSource(source) as reader:
                    reader.check_schema()
                self.stdout.write(json.dumps({"mode": "check", "source_select_only": True, "schema_verified": True}))
                return

            checkpoint = options["checkpoint"]
            limit = options["page_size"]
            apply = options["apply"]
            if since is None or not 1 <= limit <= MAX_PAGE or (apply and not checkpoint):
                raise ReceiptCountError("RECEIPT_COUNT_DATE_OR_CHECKPOINT_REQUIRED")

            with _locked_checkpoint(checkpoint) if apply else nullcontext():
                state = _load_checkpoint(checkpoint, scope)
                page, lower, upper, prepared, skipped = _prepare(
                    source, state, since, warehouses, orders, zone, overlap, limit,
                )
                applied = Counter()
                if apply:
                    if prepared:
                        applied = _send_page(target, prepared)
                    try:
                        _save_checkpoint(checkpoint, {"scope": scope, "lower": lower, "upper": upper,
                                                      "after": page.after if page.more else None})
                    except OSError:
                        raise CommandError(
                            f"No se pudo guardar el cursor. Recepciones verificadas: {sum(applied.values())}. "
                            "Corrija los permisos y vuelva a ejecutar la página."
                        ) from None
                self.stdout.write(json.dumps({
                    "mode": "applied" if apply else "preview",
                    "read": len(page.rows), "eligible": len(prepared), "skipped": dict(skipped),
                    "created": applied["created"], "unchanged": applied["unchanged"], "more": page.more,
                }, sort_keys=True))
        except CommandError:
            raise
        except (ReceiptCountError, OdooJson2Error) as exc:
            raise CommandError(str(exc)) from None
        except Exception as exc:
            raise CommandError(
                f"RECEIPT_COUNT_RUN_FAILED ({type(exc).__name__}). Revise la configuración y los permisos."
            ) from None
