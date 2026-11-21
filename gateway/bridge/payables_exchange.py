"""Persist and acknowledge payment instructions independently of the purchase exchange."""

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from .catalog_client import CatalogRemoteError
from .models import PayablesInbox


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,12})\.[0-9]{2}")


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _key(value):
    if (not isinstance(value, list) or len(value) != 4
            or any(not isinstance(part, str) or not part or len(part) > 100 for part in value)):
        raise ValueError("PAYABLES_NATIVE_KEY_INVALID")
    return tuple(value)


def _amount(value):
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise ValueError("PAYABLES_AMOUNT_INVALID")
    return Decimal(value)


def validate_delivery(delivery, instance):
    if not isinstance(delivery, dict) or set(delivery) != {"operation_id", "lease", "payload_hash", "payload"}:
        raise ValueError("PAYABLES_DELIVERY_INVALID")
    operation_id, lease = UUID(delivery["operation_id"]), UUID(delivery["lease"])
    payload = delivery["payload"]
    fields = {"schema_version", "operation_id", "instance", "operation", "currency", "bank_reference",
              "date", "original_operation_id", "allocations"}
    if (not isinstance(payload, dict) or set(payload) != fields or payload["schema_version"] != 1
            or payload["operation_id"] != str(operation_id) or payload["instance"] != instance
            or payload["currency"] not in ("CRC", "USD")
            or not isinstance(payload["bank_reference"], str) or not _ID.fullmatch(payload["bank_reference"])
            or not isinstance(payload["allocations"], list) or not 1 <= len(payload["allocations"]) <= 400
            or not isinstance(payload["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["date"])
            or not isinstance(delivery["payload_hash"], str) or not _SHA256.fullmatch(delivery["payload_hash"])
            or delivery["payload_hash"] != _digest(payload)):
        raise ValueError("PAYABLES_PAYLOAD_INVALID")
    date.fromisoformat(payload["date"])
    operation = payload["operation"]
    original_id = payload["original_operation_id"]
    if operation == "reverse":
        if not isinstance(original_id, str) or str(UUID(original_id)) != original_id or original_id == str(operation_id):
            raise ValueError("PAYABLES_ORIGINAL_OPERATION_INVALID")
    elif operation != "apply" or original_id is not None:
        raise ValueError("PAYABLES_OPERATION_INVALID")
    keys = set()
    for allocation in payload["allocations"]:
        if not isinstance(allocation, dict) or set(allocation) != {
            "key", "amount", "source_fingerprint", "before", "expected_after",
        }:
            raise ValueError("PAYABLES_ALLOCATION_INVALID")
        native_key = _key(allocation["key"])
        amount = _amount(allocation["amount"])
        before = _amount(allocation["before"])
        after = _amount(allocation["expected_after"])
        direction = 1 if operation == "reverse" else -1
        fingerprint = allocation["source_fingerprint"]
        if (native_key in keys or amount <= 0 or before + direction * amount != after
                or not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint)):
            raise ValueError("PAYABLES_ALLOCATION_INVALID")
        keys.add(native_key)
    return operation_id, lease, payload


def validate_readback(payload, payload_hash, result):
    if not isinstance(result, dict) or result.get("payload_hash") != payload_hash:
        raise ValueError("PAYABLES_READBACK_INVALID")
    state = result.get("state")
    if state in ("rejected", "unknown"):
        if set(result) != {"state", "payload_hash", "reason"} or result["reason"] not in (
            "stale_balance", "native_validation", "effect_unknown", "unsupported",
        ):
            raise ValueError("PAYABLES_READBACK_INVALID")
        return
    if (state != "confirmed" or set(result) != {"state", "payload_hash", "native_receipt", "allocations"}
            or not isinstance(result["native_receipt"], str) or not _ID.fullmatch(result["native_receipt"])
            or not isinstance(result["allocations"], list)):
        raise ValueError("PAYABLES_READBACK_INVALID")
    expected = {_key(row["key"]): _amount(row["expected_after"]) for row in payload["allocations"]}
    observed = {}
    for row in result["allocations"]:
        if not isinstance(row, dict) or set(row) != {"key", "after"}:
            raise ValueError("PAYABLES_READBACK_INVALID")
        key = _key(row["key"])
        if key in observed:
            raise ValueError("PAYABLES_READBACK_INVALID")
        observed[key] = _amount(row["after"])
    if observed != expected:
        raise ValueError("PAYABLES_READBACK_BALANCE_MISMATCH")


def receive(application_id, instance, delivery):
    operation_id, lease, payload = validate_delivery(delivery, instance)
    with transaction.atomic():
        row, _created = PayablesInbox.objects.select_for_update().get_or_create(
            application_id=application_id, operation_id=operation_id,
            defaults={"source_instance": instance, "payload": payload,
                      "payload_hash": delivery["payload_hash"], "lease": lease},
        )
        if row.source_instance != instance or row.payload_hash != delivery["payload_hash"] or row.payload != payload:
            raise ValueError("PAYABLES_OPERATION_CONFLICT")
        if row.lease != lease:
            row.lease = lease
            row.acknowledged_at = None
            row.save(update_fields=["lease", "acknowledged_at"])
    return row


def record_readback(application_id, operation_id, result):
    """Commit an attested receipt delivered by the native adapter after readback."""
    with transaction.atomic():
        row = PayablesInbox.objects.select_for_update().get(
            application_id=application_id, operation_id=operation_id,
        )
        validate_readback(row.payload, row.payload_hash, result)
        if row.result is not None and row.result != result:
            raise ValueError("PAYABLES_READBACK_CONFLICT")
        if row.result is None:
            row.result = result
            row.save(update_fields=["result"])
        return row


def _acknowledge(client, row):
    response = client.call(
        "payables_ack", instance=row.source_instance, operation_id=str(row.operation_id),
        lease=str(row.lease), result=row.result,
    )
    if response != {"operation_id": str(row.operation_id), "state": row.result["state"]}:
        raise ValueError("PAYABLES_ACK_INVALID")
    return PayablesInbox.objects.filter(pk=row.pk, lease=row.lease).update(acknowledged_at=timezone.now())


def exchange_once(client, application_id, instance, *, adapter=None):
    envelope = client.call("payables_poll", instance=instance)
    if (not isinstance(envelope, dict) or set(envelope) != {"events"}
            or not isinstance(envelope["events"], list) or len(envelope["events"]) > 10):
        raise ValueError("PAYABLES_POLL_INVALID")
    counts = dict(received=0, awaiting_native=0, acknowledged=0)
    for delivery in envelope["events"]:
        row = receive(application_id, instance, delivery)
        counts["received"] += 1
        if row.result is None and adapter is not None:
            # An adapter must atomically store the native operation receipt with
            # its effect and return that same receipt when this UUID is replayed.
            row = record_readback(application_id, row.operation_id,
                                  adapter.apply_and_readback(row.payload, row.payload_hash))
        if row.result is None:
            counts["awaiting_native"] += 1
            continue
        counts["acknowledged"] += _acknowledge(client, row)

    # Terminal events no longer appear in poll if Odoo saved the ack but its
    # response was lost. Try the stored receipt with the stored lease; by
    # polling first we already used any replacement lease in this delivery.
    unacknowledged = PayablesInbox.objects.filter(
        application_id=application_id, source_instance=instance,
        result__isnull=False, acknowledged_at__isnull=True,
    ).order_by("pk")
    for row in unacknowledged:
        try:
            counts["acknowledged"] += _acknowledge(client, row)
        except CatalogRemoteError as error:
            if error.code != "ODOO_CATALOG_HTTP_409":
                raise
            # An expired or replaced lease may be offered by a later poll.
    return counts
