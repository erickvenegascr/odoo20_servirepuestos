import hashlib
import json
import logging
import uuid

from django.utils import timezone
from pymysql.err import MySQLError
from bridge.models import GatewayInbox
from bridge.article_contract import ArticleError
from bridge.purchase_exchange import ACTIONS as PURCHASE_ACTIONS
from bridge.supplier_update_contract import ACTION as SUPPLIER_UPDATE, SupplierResultUnverified


_logger = logging.getLogger(__name__)


def exchange_once(client, article_writer=None, purchase_bridge=None, *, supplier_writer=None):
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
        inbox = GatewayInbox.objects.filter(application_uuid=application, message_uuid=message_id).first()
        if inbox and inbox.request_checksum != digest:
            raise ValueError('Gateway message identity/content conflict')
        if not inbox:
            payload = message['payload']
            article = message['kind'] == 'request' and isinstance(payload, dict) and payload.get('action') in ('article.create','article.update','article.initialize')
            purchase = (message['kind'] == 'request' and isinstance(payload, dict)
                        and payload.get('action') in PURCHASE_ACTIONS)
            supplier_update = (message['kind'] == 'request' and isinstance(payload, dict)
                               and payload.get('action') == SUPPLIER_UPDATE)
            if article:
                # Disabled adapters leave the original message pending for its
                # next lease; a read-only inbox result must not poison retries.
                if article_writer is None:
                    continue
                if str(application) != article_writer.application_id or payload.get('operation_id') != str(message_id):
                    raise ValueError('Article operation/application identity mismatch')
                try:
                    result = article_writer.apply(payload)
                except ArticleError as exc:
                    if exc.code in ('SGA_ARTICLE_BUSY','ARTICLE_WRITER_SCHEMA_UNAVAILABLE','ARTICLE_WRITER_REQUIRES_INNODB'):
                        raise
                    result = {'status':'rejected','code':exc.code,'operation_id':str(message_id),
                              'product_uuid':payload.get('product_uuid')}
            elif supplier_update:
                if supplier_writer is None:
                    continue
                if (str(application) != supplier_writer.application_id
                        or payload.get('operation_id') != str(message_id)):
                    raise ValueError('Supplier operation/application identity mismatch')
                try:
                    result = supplier_writer.apply(payload)
                except (SupplierResultUnverified, MySQLError, OSError) as exc:
                    # The next lease must reuse this UUID. Keep processing unrelated messages.
                    _logger.warning('Supplier update %s deferred (%s)', message_id, type(exc).__name__)
                    continue
                if result.get('status') == 'unavailable':
                    client.call('ack', message_id=str(message_id), lease=message['lease'], result=result)
                    count += 1
                    continue
            elif purchase:
                if purchase_bridge is None:
                    continue
                if (str(application) != purchase_bridge.application_id
                        or payload.get('operation_id') != str(message_id)):
                    raise ValueError('Purchase operation/application identity mismatch')
                result = purchase_bridge.apply(payload)
                if result.get('status') in ('blocked', 'unavailable'):
                    # Deliver the reason to the user without making a temporary
                    # capability block or read outage a permanent native receipt.
                    client.call('ack', message_id=str(message_id), lease=message['lease'], result=result)
                    count += 1
                    continue
            else:
                result = ({'status': 'pong', 'sga_mode': 'read_only'} if message['kind'] == 'ping'
                          else {'status': 'blocked', 'code': 'SGA_READ_ONLY', 'sga_mode': 'read_only'})
            # If this commit or the acknowledgement is lost, the MariaDB receipt
            # remains authoritative. Never generate a second operation UUID.
            inbox, _created = GatewayInbox.objects.get_or_create(
                application_uuid=application, message_uuid=message_id,
                defaults={'request_checksum': digest, 'result': result})
            if inbox.request_checksum != digest or inbox.result != result:
                raise ValueError('Gateway message identity/content conflict')
        client.call('ack', message_id=str(message_id), lease=message['lease'], result=inbox.result)
        inbox.acknowledged_at = timezone.now()
        inbox.save(update_fields=['acknowledged_at'])
        count += 1
    return count
