"""Run the gateway task for native Odoo vendor bills."""

import hashlib
import json
from contextlib import contextmanager
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from ...catalog_source import configuration
from ...odoo_json2 import OdooJson2Client, OdooJson2Error
from ...payables_source import PayablesSource
from ...vendor_bill_balances import Opening, _digest, collect_documents, load_drafts, make_plan


@contextmanager
def _exclusive_run(database, company, instance):
    # Reuse the existing CxP scan lock for this destination and source.
    if connection.vendor != "postgresql":
        yield
        return
    identity = json.dumps([database, company, instance], ensure_ascii=False)
    key = int(hashlib.sha256(("payables:" + identity).encode()).hexdigest()[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        if not cursor.fetchone()[0]:
            raise CommandError("Ya existe una tarea de CxP para este destino.")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def _exclusions(target):
    orders = target["exclude_order_ids"]
    references = target["exclude_invoice_references"]
    fiscalbot = target["exclude_fiscalbot_ids"]
    if any(not isinstance(values, list) or any(type(item) is not int or item <= 0 for item in values)
           for values in (orders, fiscalbot)):
        raise ValueError("Invalid protected identifiers")
    if not isinstance(references, list) or any(not isinstance(item, str) or not item.strip() for item in references):
        raise ValueError("Invalid protected references")
    return sorted(set(orders)), references, sorted(set(fiscalbot))


class Command(BaseCommand):
    help = "Auditar CxP de SGA en Odoo estándar y, previa aprobación del plan, importar borradores de apertura."

    def add_arguments(self, parser):
        parser.add_argument("--apply-drafts", metavar="PLAN_SHA256", help="Crear borradores para una simulación vigente.")

    def handle(self, *args, **options):
        requested = options["apply_drafts"]
        run_id = str(uuid4())
        client = None
        applied = 0
        try:
            source_config = configuration("SGA_PAYABLES_SOURCE")
            target = configuration("ODOO_VENDOR_BILLS_TARGET")
            instance = source_config["instance"]
            company = target["company_id"]
            if (not isinstance(instance, str) or not 1 <= len(instance) <= 80 or instance != target["source_instance"]
                    or type(company) is not int or company <= 0):
                raise ValueError("Source or company mismatch")
            orders, references, fiscalbot = _exclusions(target)
            opening = Opening.configured(target)
            if requested and (not opening or connection.vendor != "postgresql"):
                raise CommandError("La carga requiere fecha, diario, cuenta puente aprobados y PostgreSQL del gateway.")

            client = OdooJson2Client(target)
            with _exclusive_run(target["database"], company, instance):
                with PayablesSource(source_config) as source:
                    documents, excluded = collect_documents(source, orders, references)
                currency, counts, drafts = make_plan(client, company, instance, documents, fiscalbot, opening)
                plan = _digest([
                    target["url"], target["database"], company, instance, excluded,
                    sorted((key, row["fingerprint"]) for key, row in documents.items()),
                    dict(counts), [draft.row(company, currency["id"], opening) for draft in drafts],
                ])
                self.stdout.write(json.dumps({
                    "run": run_id, "mode": "simulation", "plan_sha256": plan, "open": len(documents),
                    "excluded": excluded, "states": dict(counts), "currency": currency["name"],
                    "draft_amount": format(sum(draft.amount for draft in drafts), ".2f"),
                }, sort_keys=True))
                if requested and requested != plan:
                    raise CommandError("La simulación cambió. Revisa la huella vigente antes de crear borradores.")
                if requested:
                    for offset in range(0, len(drafts), 50):
                        applied += load_drafts(client, company, currency["id"], opening, drafts[offset:offset + 50])
                    self.stdout.write(json.dumps({"run": run_id, "mode": "applied", "drafts_verified": applied}))
        except OdooJson2Error as error:
            raise CommandError(
                f"Tarea {run_id}: {error.code}. Borradores verificados en esta ejecución: {applied}. "
                "Vuelve a simular y coteja los borradores antes de volver a aplicar."
            ) from None
        except CommandError:
            raise
        except Exception as error:
            # Remote responses and driver exceptions can contain references and credentials.
            raise CommandError(
                f"Tarea {run_id}: {type(error).__name__}. Borradores verificados en esta ejecución: {applied}. "
                "Revisa la configuración y vuelve a simular antes de repetir."
            ) from None
        finally:
            if client:
                client.close()
