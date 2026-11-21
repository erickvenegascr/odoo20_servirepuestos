"""Deliver SGA payable and supplier snapshots to one configured Odoo company."""

import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import configuration
from bridge.payables_source import MAX_BATCH, PayablesSource, document_key


def _tracked_keys(path):
    if path is None:
        return []
    keys = json.loads(Path(path).read_text())
    if not isinstance(keys, list):
        raise ValueError("Invalid payable key file")
    return list(dict.fromkeys(document_key(key) for key in keys))


class Command(BaseCommand):
    help = "Reconcile SGA open payables and supplier payment details; dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--batch-size", type=int, default=MAX_BATCH)
        parser.add_argument(
            "--document-key-file",
            help="Private JSON array of previously tracked SGA keys to re-read, including closures.",
        )

    def handle(self, *args, **options):
        size = options["batch_size"]
        if not 1 <= size <= MAX_BATCH:
            raise CommandError(f"Batch size must be in 1..{MAX_BATCH}")
        try:
            source_config = configuration("SGA_PAYABLES_SOURCE")
            target = configuration("ODOO_PAYABLES_TARGET")
            instance = source_config["instance"]
            if not isinstance(instance, str) or not 1 <= len(instance) <= 80:
                raise ValueError("Invalid source instance")
            tracked = dict.fromkeys(_tracked_keys(options["document_key_file"]))
            company_id = int(target["company_id"])
            target_database = target["database"]
            application_id = target["application_id"]
            if company_id <= 0 or not target_database or not application_id:
                raise ValueError("Invalid destination")
        except (OSError, KeyError, TypeError, ValueError):
            raise CommandError("Payables configuration or tracked key file is invalid") from None

        # Prevent batches of an older snapshot being delivered after a newer
        # scan for the same source/company. Release the lock on every outcome.
        identity = json.dumps([target_database, company_id, instance], ensure_ascii=False)
        lock = int(hashlib.sha256(("payables:" + identity).encode()).hexdigest()[:15], 16)
        locked = False
        client = None
        dry_run = not options["apply"]
        counts = {"suppliers": 0, "documents": 0, "quarantined": 0, "missing_tracked": 0}
        try:
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock])
                    locked = cursor.fetchone()[0]
                    if not locked:
                        raise CommandError("A payables scan is already active for this destination")

            client = CatalogOdoo(target)
            info = client.call("catalog_info")
            if (info.get("contract") != 1 or info.get("database") != target_database
                    or info.get("company_id") != company_id or info.get("application_id") != application_id
                    or info.get("source_instance") != instance
                    or info.get("authentication") != "application_token"):
                raise CommandError("Odoo payables source, company or application mismatch")

            after_id = 0
            while after_id is not None:
                page = client.call("payables_keys", instance=instance, after_id=after_id)
                next_id = page["next_id"]
                if next_id is not None and (type(next_id) is not int or next_id <= after_id):
                    raise CommandError("Odoo payables key cursor did not advance")
                if not isinstance(page["keys"], list):
                    raise CommandError("Odoo payables key list is invalid")
                for key in page["keys"]:
                    tracked[document_key(key)] = None
                after_id = next_id

            def deliver(*, documents=(), suppliers=(), missing_keys=()):
                payload = {
                    "instance": instance, "documents": list(documents),
                    "suppliers": list(suppliers), "dry_run": dry_run,
                }
                if missing_keys:
                    payload["missing_keys"] = [list(key) for key in missing_keys]
                ack = client.call("apply_payables_snapshot", **payload)
                quarantined = ack.get("quarantined")
                if (ack.get("documents") != len(documents) or ack.get("suppliers") != len(suppliers)
                        or ack.get("dry_run") is not dry_run
                        or ack.get("missing_keys", 0) != len(missing_keys)
                        or type(quarantined) is not int or not 0 <= quarantined <= len(documents)):
                    raise CommandError("Odoo did not acknowledge the complete payables snapshot batch")
                counts["documents"] += len(documents)
                counts["suppliers"] += len(suppliers)
                counts["quarantined"] += quarantined
                counts["missing_tracked"] += len(missing_keys)

            with PayablesSource(source_config) as source:
                after_supplier = None
                while suppliers := source.contacts(after=after_supplier, limit=size):
                    deliver(suppliers=suppliers)
                    after_supplier = suppliers[-1]["code"]
                after_document = None
                while documents := source.open_documents(after=after_document, limit=size):
                    deliver(documents=documents)
                    for document in documents:
                        tracked.pop(tuple(document["key"]), None)
                    after_document = documents[-1]["key"]

                reconcile = list(tracked)
                for offset in range(0, len(reconcile), size):
                    requested = reconcile[offset:offset + size]
                    documents = source.documents_by_keys(requested)
                    found = {tuple(document["key"]) for document in documents}
                    missing = [key for key in requested if key not in found]
                    deliver(documents=documents, missing_keys=missing)
        except CommandError:
            raise
        except Exception as exc:
            # Driver messages, file paths, HTTP bodies and request payloads
            # can include a supplier email, IBAN or secret.
            raise CommandError(f"Payables scan failed ({type(exc).__name__})") from None
        finally:
            if client:
                client.close()
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock])

        mode = "SIMULATION" if dry_run else "APPLIED"
        self.stdout.write(
            f"{mode} suppliers={counts['suppliers']} documents={counts['documents']} "
            f"quarantined={counts['quarantined']} missing_tracked={counts['missing_tracked']}"
        )
