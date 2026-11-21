"""Reconcile SGA suppliers using complete native snapshots and acknowledged deliveries."""

import hashlib
import json
import re
import time
from collections import Counter
from datetime import timedelta
from itertools import groupby
from tempfile import TemporaryFile

from django.db import connection, transaction
from django.utils import timezone

from .catalog_client import CatalogRemoteError
from .models import SupplierPullBucket, SupplierPullEntry, SupplierPullState
from .supplier_source import SupplierSource


BUCKET_SIZE = 512
VENDOR_BUCKET = -1
RETRY_INTERVAL = timedelta(minutes=5)
VENDOR_CODE = re.compile(r'[0-9A-Za-z]{1,3}\Z')
ENTRY_REFRESH_FIELDS = ('source_checksum', 'payload', 'present', 'changed_at', 'next_attempt_at', 'error_code')
ENTRY_ACK_FIELDS = ('acknowledged_checksum', 'next_attempt_at', 'error_code')


def checksum(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def target_fingerprint(target, instance):
    # Match the existing manual supplier command's lock identity.
    identity = {key: target[key] for key in ('url', 'database', 'company_id', 'application_id')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode() + instance.encode()).hexdigest()


def validate_target(client, target, instance):
    expected = {'suppliers_contract': 1, 'database': target['database'], 'company_id': target['company_id'],
                'application_id': target['application_id'], 'source_instance': instance,
                'authentication': 'application_token', 'sga_writes_enabled': False}
    if not target['application_id'] or type(target['company_id']) is not int or target['company_id'] < 1:
        raise ValueError('SGA_SUPPLIER_TARGET_IDENTITY_REQUIRED')
    info = client.call('suppliers_info')
    if (not isinstance(info, dict) or info.get('sga_writes_enabled') is not False
            or any(info.get(key) != value for key, value in expected.items())):
        raise ValueError('SGA_SUPPLIER_TARGET_MISMATCH')
    return info.get('missing_supported') is True


def entry_key(row, bucket):
    if bucket == VENDOR_BUCKET:
        return 0, row['code']
    return row['article_id'], row['vendor_code']


def stage_bucket(spool, observed, prior, bucket, rows):
    rows.sort(key=lambda row: entry_key(row, bucket))
    keys = [entry_key(row, bucket) for row in rows]
    if len(set(keys)) != len(keys) or any(
        type(article) is not int or not 0 <= article < 2**31
        or not VENDOR_CODE.fullmatch(vendor)
        or (article == 0) != (bucket == VENDOR_BUCKET)
        for article, vendor in keys
    ):
        raise ValueError('SGA_SUPPLIER_SNAPSHOT_KEY_INVALID')
    digest = checksum(rows)
    observed[bucket] = digest
    if prior.get(bucket) != digest:
        spool.write(json.dumps([bucket, rows], ensure_ascii=False) + '\n')


def read_snapshot(source_config, prior, spool):
    observed = {}
    count = 0
    with SupplierSource(source_config) as source:
        vendors = source.vendors()
        stage_bucket(spool, observed, prior, VENDOR_BUCKET, vendors)
        vendor_codes = {row['code'] for row in vendors}
        expected = source.relation_count()
        for bucket, relations in groupby(source.iter_snapshot_relations(), lambda row: row['article_id'] // BUCKET_SIZE):
            if bucket in observed:
                raise ValueError('SGA_SUPPLIER_SNAPSHOT_ORDER_INVALID')
            rows = list(relations)
            if any(row['vendor_code'] not in vendor_codes for row in rows):
                raise ValueError('SGA_SUPPLIER_SNAPSHOT_VENDOR_MISSING')
            stage_bucket(spool, observed, prior, bucket, rows)
            count += len(rows)
        if count != expected:
            raise ValueError('SGA_SUPPLIER_SNAPSHOT_COUNT_MISMATCH')

    # Native transaction has finished successfully. A previously populated bucket
    # missing from the complete scan is now empty; a failed read never gets here.
    for bucket in sorted(prior.keys() - observed.keys()):
        spool.write(json.dumps([bucket, []]) + '\n')
    spool.seek(0)
    return observed, count, len(vendors)


def refresh_bucket(state, bucket, rows, now):
    current = {(entry.article_id, entry.vendor_code): entry for entry in
               SupplierPullEntry.objects.filter(state=state, bucket=bucket)}
    additions = []
    changes = []
    for row in rows:
        article_id, vendor_code = entry_key(row, bucket)
        previous = current.pop((article_id, vendor_code), None)
        digest = checksum(row)
        if previous is None:
            additions.append(SupplierPullEntry(state=state, bucket=bucket, article_id=article_id,
                                              vendor_code=vendor_code, source_checksum=digest, payload=row,
                                              changed_at=now, next_attempt_at=now))
        elif not previous.present or previous.source_checksum != digest:
            previous.source_checksum = digest
            previous.payload = row
            previous.present = True
            previous.changed_at = now
            previous.next_attempt_at = now
            previous.error_code = ''
            changes.append(previous)

    for previous in current.values():
        if not previous.present:
            continue
        previous.present = False
        previous.changed_at = now
        previous.next_attempt_at = now if previous.article_id and previous.acknowledged_checksum else None
        previous.error_code = ('SGA_SUPPLIER_RELATION_MISSING_UNCONFIRMED'
                               if previous.article_id and not previous.acknowledged_checksum else '')
        changes.append(previous)
    SupplierPullEntry.objects.bulk_create(additions, batch_size=500)
    SupplierPullEntry.objects.bulk_update(changes, ENTRY_REFRESH_FIELDS, batch_size=500)
    return len(additions) + len(changes)


def acknowledge(reply, entries, mode):
    last_key = [0, ''] if mode == 'vendor' else [entries[-1].article_id, entries[-1].vendor_code]
    count = 0 if mode == 'vendor' else len(entries)
    if (not isinstance(reply, dict) or reply.get('last_key') != last_key or reply.get('count') != count
            or reply.get('dry_run') is not False or reply.get('sga_writes_enabled') is not False):
        raise ValueError('SGA_SUPPLIER_DELIVERY_UNCONFIRMED')
    key = 'vendors' if mode == 'vendor' else 'relations'
    allowed = {'created', 'updated', 'unchanged', 'adopted'} if mode != 'missing' else {'missing', 'unchanged'}
    counts = reply.get(key)
    if (not isinstance(counts, dict) or not set(counts).intersection(allowed)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or sum(counts.get(status, 0) for status in allowed) != len(entries)):
        raise ValueError('SGA_SUPPLIER_DELIVERY_UNCONFIRMED')
    return {f'{mode}_{status}': count for status, count in counts.items() if status in allowed}


def send_supplier_batch(client, instance, entries, mode):
    if mode == 'missing':
        relations = [{'article_id': entry.article_id, 'vendor_code': entry.vendor_code,
                      'source_checksum': entry.acknowledged_checksum} for entry in entries]
        return client.call('apply_suppliers_missing', instance=instance, relations=relations, dry_run=False)
    payload = [entry.payload for entry in entries]
    return client.call('apply_suppliers_batch', instance=instance, dry_run=False,
                       vendors=payload if mode == 'vendor' else [], relations=payload if mode == 'relation' else [])


def deliver(client, instance, entries, mode, deadline, summary):
    # Check the monotonic deadline again before every split. If a request/ACK is
    # lost, no local row is marked complete; the remote import supports replay.
    if not entries or time.monotonic() >= deadline:
        return
    try:
        reply = send_supplier_batch(client, instance, entries, mode)
    except CatalogRemoteError as exc:
        if exc.code != 'ODOO_CATALOG_HTTP_409':
            raise
        if len(entries) > 1:
            pivot = len(entries) // 2
            deliver(client, instance, entries[:pivot], mode, deadline, summary)
            deliver(client, instance, entries[pivot:], mode, deadline, summary)
            return
        entries[0].next_attempt_at = timezone.now() + RETRY_INTERVAL
        entries[0].error_code = exc.code
        entries[0].save(update_fields=('next_attempt_at', 'error_code'))
        summary['conflicts'] += 1
        return
    summary.update(acknowledge(reply, entries, mode))
    for entry in entries:
        if mode != 'missing':
            entry.acknowledged_checksum = entry.source_checksum
        entry.next_attempt_at = None
        entry.error_code = ''
    SupplierPullEntry.objects.bulk_update(entries, ENTRY_ACK_FIELDS, batch_size=500)


def due_entries(state, mode, maximum):
    entries = SupplierPullEntry.objects.filter(state=state, next_attempt_at__lte=timezone.now())
    if mode == 'vendor':
        entries = entries.filter(article_id=0, present=True)
    elif mode == 'missing':
        entries = entries.filter(article_id__gt=0, present=False)
    else:
        pending_vendors = SupplierPullEntry.objects.filter(state=state, article_id=0, next_attempt_at__isnull=False)
        entries = entries.filter(article_id__gt=0, present=True).exclude(vendor_code__in=pending_vendors.values('vendor_code'))
    return list(entries.order_by('-changed_at', 'article_id', 'vendor_code')[:maximum])


def persist_snapshot(state, target_key, physical_key, spool, observed, count):
    now = timezone.now()
    with transaction.atomic():
        if not state:
            state = SupplierPullState.objects.create(target_fingerprint=target_key, source_fingerprint=physical_key)
        changed = 0
        for line in spool:
            bucket, rows = json.loads(line)
            changed += refresh_bucket(state, bucket, rows, now)
            if bucket in observed:
                SupplierPullBucket.objects.update_or_create(state=state, bucket=bucket,
                                                            defaults={'source_checksum': observed[bucket]})
            else:
                SupplierPullBucket.objects.filter(state=state, bucket=bucket).delete()
        state.last_complete_at = now
        state.last_snapshot_count = count
        state.last_snapshot_sha256 = checksum(sorted(observed.items()))
        state.save(update_fields=('last_complete_at', 'last_snapshot_count', 'last_snapshot_sha256'))
    return state, changed


def reconcile_suppliers(source_config, target, client, *, apply, max_relations, batch_size, budget):
    deadline = time.monotonic() + budget
    instance = source_config['instance']
    if not isinstance(instance, str) or not 1 <= len(instance) <= 80:
        raise ValueError('SGA_SUPPLIER_SOURCE_IDENTITY_REQUIRED')
    target_key = target_fingerprint(target, instance)
    physical_key = checksum({**{key: source_config[key] for key in ('instance', 'host')},
                             'database': source_config.get('database', 'sgadata'),
                             'port': int(source_config.get('port', 3306))})
    lock = int(hashlib.sha256(('suppliers:' + target_key).encode()).hexdigest()[:15], 16)
    locked = False
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', [lock])
            locked = cursor.fetchone()[0]
        if not locked:
            raise ValueError('SGA_SUPPLIER_TARGET_BUSY')
    try:
        return _reconcile(source_config, target, client, target_key, physical_key, deadline, apply, max_relations, batch_size)
    finally:
        if locked:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [lock])


def _reconcile(source_config, target, client, target_key, physical_key, deadline, apply, max_relations, batch_size):
    instance = source_config['instance']
    missing_supported = validate_target(client, target, instance)
    state = SupplierPullState.objects.filter(pk=target_key).first()
    if state and state.source_fingerprint != physical_key:
        raise ValueError('SGA_SUPPLIER_SOURCE_IDENTITY_CHANGED')
    prior = dict(SupplierPullBucket.objects.filter(state=state).values_list('bucket', 'source_checksum')) if state else {}
    with TemporaryFile(mode='w+', encoding='utf-8') as spool:
        observed, count, vendors = read_snapshot(source_config, prior, spool)
        changed_buckets = sum(1 for bucket in prior.keys() | observed.keys() if prior.get(bucket) != observed.get(bucket))
        summary = Counter(relations=count, vendors=vendors, changed_buckets=changed_buckets)
        if not apply:
            return {**summary, 'dry_run': True}
        state, changed = persist_snapshot(state, target_key, physical_key, spool, observed, count)
    summary['changed_entries'] = changed
    try:
        modes = ['vendor'] + (['missing'] if missing_supported else []) + ['relation']
        for mode in modes:
            maximum = 800 if mode == 'vendor' else max_relations
            pending = due_entries(state, mode, maximum)
            for offset in range(0, len(pending), batch_size):
                if time.monotonic() >= deadline:
                    break
                batch = sorted(pending[offset:offset + batch_size], key=lambda entry: (entry.article_id, entry.vendor_code))
                deliver(client, instance, batch, mode, deadline, summary)
    except Exception as exc:
        state.last_result = {'status': 'delivery_error', 'error_code': getattr(exc, 'code', type(exc).__name__), **summary}
        state.save(update_fields=('last_result',))
        raise
    active = SupplierPullEntry.objects.filter(state=state)
    summary['pending'] = active.filter(next_attempt_at__isnull=False).count()
    summary['conflicts_pending'] = active.filter(error_code='ODOO_CATALOG_HTTP_409').count()
    summary['missing_review'] = active.filter(article_id__gt=0, present=False, acknowledged_checksum='').count()
    summary['missing_pending'] = active.filter(article_id__gt=0, present=False, next_attempt_at__isnull=False).count()
    state.last_result = {'status': 'succeeded', **summary}
    state.save(update_fields=('last_result',))
    return {**summary, 'dry_run': False}
