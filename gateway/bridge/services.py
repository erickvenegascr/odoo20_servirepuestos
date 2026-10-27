import hashlib
import json
import uuid
from django.db import IntegrityError, transaction
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from .domain import NATIVE_BLOCK_REASON
from .models import Operation, OperationEvent

class Conflict(APIException):
    status_code = 409
    default_detail = 'Operation identity conflicts with an existing request'
    default_code = 'conflict'

def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError('Content cannot be canonically encoded') from exc

def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def ensure_role(user, role):
    if role not in user.roles:
        raise PermissionDenied('Role required for this operation')

def submit(data, user):
    ensure_role(user, 'submit')
    if data['origin_instance_id'] != user.origin_instance_id or data['target_company_key'] not in user.companies:
        raise PermissionDenied('Origin or company outside authenticated scope')
    payload_hash = sha(data['payload'])
    if data.get('payload_sha256', payload_hash) != payload_hash:
        raise ValidationError({'payload_sha256': 'Payload digest does not match'})
    context = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in data.items()
               if k not in ('event_id', 'occurred_at_utc', 'payload_sha256')}
    # A retry may carry a new delivery timestamp/event; business content cannot change.
    request_hash = sha(context)
    identity = dict(origin_instance_id=data['origin_instance_id'], operation_id=data['operation_id'],
                    action=data['action'], revision=data['revision'])
    try:
        with transaction.atomic():
            op = Operation.objects.create(
                **identity, client_id=user.client_id, submit_event_id=data['event_id'],
                target_company_key=data['target_company_key'], aggregate_id=data['aggregate_id'],
                actor_user_id=data['actor_user_id'], occurred_at=data['occurred_at_utc'],
                expected_target_fingerprint=data['expected_target_fingerprint'], payload=data['payload'],
                payload_sha256=payload_hash, request_sha256=request_hash)
            OperationEvent.objects.create(operation=op, state=op.state, actor=user.client_id, code='SUBMITTED')
            return op, True
    except IntegrityError:
        existing = Operation.objects.filter(**identity).first()
        if existing is None or existing.client_id != user.client_id or existing.request_sha256 != request_hash:
            raise Conflict()
        return existing, False

def scope(user):
    ensure_role(user, 'read')
    qs = Operation.objects.filter(target_company_key__in=user.companies)
    if 'read_any' not in user.roles:
        qs = qs.filter(client_id=user.client_id)
    return qs

def retry(operation_id, user):
    ensure_role(user, 'retry')
    with transaction.atomic():
        op = scope(user).select_for_update().get(pk=operation_id)
        if op.state not in ('BLOQUEADO', 'REINTENTO_SEGURO'):
            raise Conflict('Only blocked or provably safe-to-retry operations can be queued')
        previous = op.state
        op.state, op.last_error_code = 'EN_COLA', ''
        op.save(update_fields=['state', 'last_error_code', 'updated_at'])
        OperationEvent.objects.create(operation=op, previous_state=previous, state=op.state,
                                      actor=user.client_id, code='REQUEUED')
        return op

def process_next():
    """Durable PostgreSQL queue. No native SGA effects are implemented here."""
    with transaction.atomic():
        op = Operation.objects.select_for_update(skip_locked=True).filter(state='EN_COLA').order_by('created_at', 'id').first()
        if op is None:
            return False
        OperationEvent.objects.create(operation=op, previous_state='EN_COLA', state='VALIDANDO', actor='worker', code='VALIDATION_STARTED')
        # Do not pretend a stored request has applied inventory, costs or invoices.
        op.state = 'BLOQUEADO'
        op.attempts += 1
        op.last_error_code = NATIVE_BLOCK_REASON
        op.save(update_fields=['state', 'attempts', 'last_error_code', 'updated_at'])
        OperationEvent.objects.create(operation=op, previous_state='VALIDANDO', state=op.state,
                                      actor='worker', code=NATIVE_BLOCK_REASON)
        return True
