from datetime import timezone
import re
from rest_framework import serializers
from .domain import ACTIONS
from .models import Operation, OperationEvent

class AwareDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        if not isinstance(value, str) or len(value) > 40 or not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', value):
            raise serializers.ValidationError('An ISO 8601 timestamp with timezone is required')
        return super().to_internal_value(value)

class SubmissionSerializer(serializers.Serializer):
    schema_version = serializers.ChoiceField(choices=['1'])
    operation_id = serializers.UUIDField()
    event_id = serializers.UUIDField()
    origin_instance_id = serializers.CharField(max_length=80)
    target_company_key = serializers.CharField(max_length=64)
    aggregate_id = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1, max_value=2147483647)
    action = serializers.ChoiceField(choices=ACTIONS)
    origin_system = serializers.ChoiceField(choices=['odoo'])
    actor_user_id = serializers.CharField(max_length=128)
    occurred_at_utc = AwareDateTimeField()
    expected_target_fingerprint = serializers.CharField(max_length=128)
    payload = serializers.JSONField()
    payload_sha256 = serializers.RegexField(r'^[0-9a-f]{64}$', required=False)

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields) if isinstance(data, dict) else set()
        if unknown:
            raise serializers.ValidationError({'unknown_fields': sorted(unknown)})
        return super().to_internal_value(data)

    def validate_payload(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('payload must be a JSON object')
        return value

    def validate_occurred_at_utc(self, value):
        return value.astimezone(timezone.utc)

class OperationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Operation
        fields = ['id', 'operation_id', 'origin_instance_id', 'target_company_key', 'aggregate_id',
                  'revision', 'action', 'state', 'actor_user_id', 'occurred_at', 'payload_sha256',
                  'native_keys', 'attempts', 'last_error_code', 'created_at', 'updated_at']

class EventSerializer(serializers.ModelSerializer):
    sequence = serializers.IntegerField(source="id")
    class Meta:
        model = OperationEvent
        fields = ['sequence', 'event_id', 'previous_state', 'state', 'actor', 'code', 'created_at']
