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


class SupplierRelationRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_instance = models.CharField(max_length=80)
    target_fingerprint = models.CharField(max_length=64)
    dry_run = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default='running')
    relations = models.PositiveIntegerField(default=0)
    last_key = models.JSONField(default=list)
    source_checksum = models.CharField(max_length=64, blank=True)
    result_counts = models.JSONField(default=dict)
    vendor_counts = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ArticleSweep(models.Model):
    application_uuid = models.UUIDField(unique=True)
    source_instance = models.CharField(max_length=80)
    after_article = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class SupplierPullState(models.Model):
    target_fingerprint = models.CharField(max_length=64, primary_key=True)
    source_fingerprint = models.CharField(max_length=64)
    last_complete_at = models.DateTimeField(null=True)
    last_snapshot_count = models.PositiveIntegerField(default=0)
    last_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    last_result = models.JSONField(default=dict)


class SupplierPullBucket(models.Model):
    state = models.ForeignKey(SupplierPullState, on_delete=models.PROTECT)
    bucket = models.IntegerField()
    source_checksum = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['state', 'bucket'], name='supplier_pull_unique_bucket')]


class SupplierPullEntry(models.Model):
    state = models.ForeignKey(SupplierPullState, on_delete=models.PROTECT)
    bucket = models.IntegerField()
    # Article zero denotes the supplier metadata, separate from its item links.
    article_id = models.PositiveIntegerField()
    vendor_code = models.CharField(max_length=3)
    source_checksum = models.CharField(max_length=64)
    acknowledged_checksum = models.CharField(max_length=64, blank=True)
    payload = models.JSONField()
    present = models.BooleanField(default=True)
    changed_at = models.DateTimeField()
    next_attempt_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['state', 'article_id', 'vendor_code'],
                                               name='supplier_pull_unique_identity')]
        indexes = [models.Index(fields=['state', 'bucket'], name='supplier_pull_entry_bucket'),
                   models.Index(fields=['state', 'next_attempt_at'], name='supplier_pull_entry_due')]
