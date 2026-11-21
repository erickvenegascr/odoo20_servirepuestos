import json
import os
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from requests import RequestException

from ...catalog_client import CatalogOdoo, CatalogRemoteError
from ...payables_exchange import exchange_once


class Command(BaseCommand):
    help = "Receive CxP payment operations or return stored native receipts to the same Odoo application."

    def add_arguments(self, parser):
        parser.add_argument("--target-file", default=os.environ.get("ODOO_PAYABLES_TARGET_FILE"))
        parser.add_argument("--receive", action="store_true", help="Claim payment instructions in the configured application.")

    def handle(self, *args, **options):
        path = options["target_file"]
        if not path:
            raise CommandError("PAYABLES_TARGET_FILE_REQUIRED")
        client = None
        try:
            target = json.loads(Path(path).read_text(encoding="utf-8"))
            instance = target["source_instance"]
            target_id = UUID(target["application_id"])
            client = CatalogOdoo(target)
            info = client.call("catalog_info")
            if (info.get("database") != target["database"] or info.get("source_instance") != instance
                    or UUID(info["application_id"]) != target_id):
                raise ValueError
            result = (exchange_once(client, target_id, instance) if options["receive"]
                      else {"preflight": "ok", "native_writer": "unavailable"})
            self.stdout.write(json.dumps(result, sort_keys=True))
        except (TypeError, ValueError, KeyError, AttributeError, OSError, RequestException, CatalogRemoteError,
                DatabaseError):
            raise CommandError("PAYABLES_EXCHANGE_FAILED") from None
        finally:
            if client:
                client.close()
