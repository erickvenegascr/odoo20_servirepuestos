import uuid
from django.db import models
from .domain import ACTIONS, STATES

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
