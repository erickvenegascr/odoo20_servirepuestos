from unittest.mock import Mock, patch

from django.test import TestCase

from bridge.catalog_client import CatalogRemoteError
from bridge.models import SupplierPullEntry, SupplierPullState
from bridge.supplier_pull import checksum, reconcile_suppliers


def supplier(code='039', name='Supplier', active=True):
    return {'code': code, 'name': name, 'tax_id': '', 'active': active}


def relation(article=2410, code='039', product_code='46010107', priority=1, active=True):
    return {'article_id': article, 'vendor_code': code, 'product_code': product_code, 'priority': priority,
            'disabled': False, 'vendor_active': active}


class SupplierPullTests(TestCase):
    source_config = {'instance': 'fixture-sga', 'host': 'localhost', 'database': 'sgadata'}
    target = {'url': 'https://fixture.invalid', 'database': 'fixture', 'company_id': 1, 'application_id': 'fixture-app'}

    def setUp(self):
        self.client = Mock()
        self.client.call.side_effect = self.receive
        self.source = Mock()
        self.snapshot = patch('bridge.supplier_pull.SupplierSource', return_value=self.source)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)
        self.source.__enter__ = Mock(return_value=self.source)
        self.source.__exit__ = Mock(return_value=False)
        self.rows = [relation(), relation(2800, product_code='002800')]
        self.vendors = [supplier()]
        self.source.vendors.side_effect = lambda: list(self.vendors)
        self.source.relation_count.side_effect = lambda: len(self.rows)
        self.source.iter_snapshot_relations.side_effect = lambda: iter(self.rows)
        self.remote_vendors = {}
        self.remote_relations = {}
        self.remote_missing = set()
        self.reject = set()
        self.send_failed = False

    def receive(self, method, **payload):
        if method == 'suppliers_info':
            return {'suppliers_contract': 1, 'database': self.target['database'], 'company_id': self.target['company_id'],
                    'application_id': self.target['application_id'], 'source_instance': self.source_config['instance'],
                    'authentication': 'application_token', 'sga_writes_enabled': False, 'missing_supported': True}
        rows = payload['relations']
        keys = [(row['article_id'], row['vendor_code']) for row in rows]
        if self.reject.intersection(keys):
            raise CatalogRemoteError('ODOO_CATALOG_HTTP_409')
        counts = {'vendors': {}, 'relations': {}}
        for row in payload.get('vendors', []):
            code = row['code']
            action = 'unchanged' if self.remote_vendors.get(code) == row else 'updated'
            counts['vendors'][action] = counts['vendors'].get(action, 0) + 1
            self.remote_vendors[code] = row
        for key, row in zip(keys, rows):
            if method == 'apply_suppliers_missing':
                self.assertEqual(row['source_checksum'], checksum(self.remote_relations[key]))
                action = 'unchanged' if key in self.remote_missing else 'missing'
                self.remote_missing.add(key)
            else:
                action = 'unchanged' if self.remote_relations.get(key) == row and key not in self.remote_missing else 'updated'
                self.remote_relations[key] = row
                self.remote_missing.discard(key)
            counts['relations'][action] = counts['relations'].get(action, 0) + 1
        if rows and self.send_failed:
            raise TimeoutError('synthetic lost response')
        return {**counts, 'count': len(rows), 'last_key': list(keys[-1]) if rows else [0, ''],
                'dry_run': False, 'sga_writes_enabled': False}

    def sync(self, *, apply=True, **options):
        return reconcile_suppliers(self.source_config, self.target, self.client, apply=apply,
                                   max_relations=100, batch_size=50, budget=15, **options)

    def payloads(self, method):
        return [call.kwargs for call in self.client.call.call_args_list if call.args[0] == method]

    def test_complete_snapshot_sends_nothing_for_unchanged_bucket(self):
        self.assertTrue(self.sync(apply=False)['dry_run'])
        self.assertFalse(SupplierPullState.objects.exists())
        self.assertEqual(self.payloads('apply_suppliers_batch'), [])
        self.assertEqual(self.sync()['relation_updated'], 2)
        saved_hash = SupplierPullState.objects.get().last_snapshot_sha256
        self.client.reset_mock()
        self.assertEqual(self.sync()['changed_buckets'], 0)
        self.assertEqual(self.payloads('apply_suppliers_batch'), [])
        self.assertEqual(SupplierPullState.objects.get().last_snapshot_sha256, saved_hash)
        self.rows[0] = relation(product_code='046010107', priority=2)
        change = self.sync()
        self.assertEqual((change['changed_buckets'], change['relation_updated']), (1, 1))
        self.assertEqual([p['relations'] for p in self.payloads('apply_suppliers_batch')], [[self.rows[0]]])

    def test_only_complete_source_can_mark_missing_and_reappearance_restores_it(self):
        self.sync()
        removed = self.rows.pop(0)
        self.client.reset_mock()
        self.source.relation_count.side_effect = lambda: 2
        with self.assertRaisesRegex(ValueError, 'SGA_SUPPLIER_SNAPSHOT_COUNT_MISMATCH'):
            self.sync()
        self.assertTrue(SupplierPullEntry.objects.get(article_id=2410).present)
        self.assertEqual(self.payloads('apply_suppliers_missing'), [])
        self.source.relation_count.side_effect = lambda: 1
        self.assertEqual(self.sync()['missing_missing'], 1)
        self.assertEqual(self.payloads('apply_suppliers_missing')[0]['relations'], [
            {'article_id': 2410, 'vendor_code': '039', 'source_checksum': checksum(removed)},
        ])
        self.assertIn((2410, '039'), self.remote_missing)
        self.rows.insert(0, removed)
        self.source.relation_count.side_effect = lambda: 2
        self.assertEqual(self.sync()['relation_updated'], 1)
        self.assertFalse(self.remote_missing)

    def test_lost_remote_response_replays_without_marking_locally_confirmed(self):
        self.send_failed = True
        with self.assertRaises(TimeoutError):
            self.sync()
        self.assertTrue(all(not row.acknowledged_checksum and row.next_attempt_at for row in
                            SupplierPullEntry.objects.filter(article_id__gt=0)))
        self.send_failed = False
        self.assertEqual(self.sync()['relation_unchanged'], 2)
        self.assertFalse(SupplierPullEntry.objects.filter(next_attempt_at__isnull=False).exists())

    def test_single_conflict_does_not_block_other_source_identity(self):
        self.reject = {(2410, '039')}
        result = self.sync()
        self.assertEqual((result['conflicts'], result['relation_updated'], result['pending']), (1, 1, 1))
        self.assertEqual(set(self.remote_relations), {(2800, '039')})
        self.assertEqual(SupplierPullEntry.objects.get(article_id=2410).error_code, 'ODOO_CATALOG_HTTP_409')
        self.reject.clear()
        self.assertEqual(self.sync()['pending'], 1)
        self.rows[0] = relation(product_code='replacement')
        self.assertEqual(self.sync()['relation_updated'], 1)

    def test_changed_vendor_is_delivered_with_each_affected_exact_relation(self):
        self.sync()
        self.client.reset_mock()
        self.vendors = [supplier(active=False)]
        self.rows = [relation(active=False), relation(2800, product_code='002800', active=False)]
        result = self.sync()
        self.assertEqual((result['vendor_updated'], result['relation_updated']), (1, 2))
        vendor, entries = self.payloads('apply_suppliers_batch')
        self.assertEqual(vendor['vendors'], self.vendors)
        self.assertEqual(entries['relations'], self.rows)
