import hashlib
import json
import uuid

from django.utils import timezone
from bridge.models import GatewayInbox


def exchange_once(client):
    envelope = client.call('poll')
    application = uuid.UUID(envelope['application_id'])
    expected = client.config.get('application_id') if isinstance(getattr(client, 'config', None), dict) else None
    if expected and str(application) != expected:
        raise ValueError('Gateway application scope mismatch')
    count = 0
    for message in envelope['messages']:
        message_id = uuid.UUID(message['message_id'])
        digest = hashlib.sha256(json.dumps({'kind': message['kind'], 'payload': message['payload']},
                                           sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        # There is deliberately no SGA mutation adapter on this path.
        result = ({'status': 'pong', 'sga_mode': 'read_only'} if message['kind'] == 'ping'
                  else {'status': 'blocked', 'code': 'SGA_READ_ONLY', 'sga_mode': 'read_only'})
        inbox, _created = GatewayInbox.objects.get_or_create(
            application_uuid=application, message_uuid=message_id,
            defaults={'request_checksum': digest, 'result': result})
        if inbox.request_checksum != digest:
            raise ValueError('Gateway message identity/content conflict')
        client.call('ack', message_id=str(message_id), lease=message['lease'], result=inbox.result)
        inbox.acknowledged_at = timezone.now()
        inbox.save(update_fields=['acknowledged_at'])
        count += 1
    return count
