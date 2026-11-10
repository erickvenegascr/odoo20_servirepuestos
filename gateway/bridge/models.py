import uuid
from django.db import models
from .domain import ACTIONS, STATES

class CatalogRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_instance = models.CharField(max_length=80)
    target_fingerprint = models.CharField(max_length=64)
    dry_run = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default='running')
    cursor = models.PositiveBigIntegerField(default=0)
    categories = models.PositiveIntegerField(default=0)
    products = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    result_counts = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class ProductImageRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_instance = models.CharField(max_length=80)
    target_fingerprint = models.CharField(max_length=64)
    dry_run = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default='running')
    files = models.PositiveIntegerField(default=0)
    products = models.PositiveIntegerField(default=0)
    primary = models.PositiveIntegerField(default=0)
    secondary = models.PositiveIntegerField(default=0)
    result_counts = models.JSONField(default=dict)
    last_key = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CatalogDetailRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_instance = models.CharField(max_length=80)
    target_fingerprint = models.CharField(max_length=64)
    official_catalog_checksum = models.CharField(max_length=64)
    pricing_strategy = models.CharField(max_length=16, default='formula')
    dry_run = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default='running')
    products = models.PositiveIntegerField(default=0)
    prices = models.PositiveIntegerField(default=0)
    cursor = models.PositiveIntegerField(default=0)
    result_counts = models.JSONField(default=dict)
    price_result_counts = models.JSONField(default=dict)
    master_counts = models.JSONField(default=dict)
    issue_counts = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class GatewayInbox(models.Model):
    application_uuid = models.UUIDField()
    message_uuid = models.UUIDField()
    request_checksum = models.CharField(max_length=64)
    result = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['application_uuid', 'message_uuid'], name='unique_gateway_inbox_message')]

class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.CharField(max_length=80)
    origin_instance_id = models.CharField(max_length=80)
    target_company_key = models.CharField(max_length=64)
    operation_id = models.UUIDField()
    submit_event_id = models.UUIDField()
    aggregate_id = models.UUIDField()
    revision = models.PositiveIntegerField()
    action = models.CharField(max_length=64, choices=[(s, s) for s in ACTIONS])
    state = models.CharField(max_length=32, choices=[(s, s) for s in STATES], default='EN_COLA')
    actor_user_id = models.CharField(max_length=128)
    occurred_at = models.DateTimeField()
    expected_target_fingerprint = models.CharField(max_length=128)
    payload = models.JSONField()
    payload_sha256 = models.CharField(max_length=64)
    request_sha256 = models.CharField(max_length=64)
    native_keys = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['origin_instance_id', 'operation_id', 'action', 'revision'], name='unique_operation_revision'),
            models.UniqueConstraint(fields=['client_id', 'submit_event_id'], name='unique_client_submit_event'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='positive_business_revision'),
        ]
        indexes = [models.Index(fields=['state', 'created_at']), models.Index(fields=['target_company_key', 'updated_at'])]

class OperationEvent(models.Model):
    operation = models.ForeignKey(Operation, related_name='events', on_delete=models.PROTECT)
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    previous_state = models.CharField(max_length=32, blank=True)
    state = models.CharField(max_length=32)
    actor = models.CharField(max_length=160)
    code = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['id']
