from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from bridge.catalog_client import CatalogOdoo
from bridge.catalog_source import CatalogSource, assert_read_only_grants, path_key
from bridge.models import CatalogRun
import pymysql


class CatalogSafetyTests(SimpleTestCase):
    def test_only_select_account_accepted(self):
        assert_read_only_grants([{'grant': 'GRANT USAGE ON *.* TO reader'},
                                 {'grant': 'GRANT SELECT, SHOW VIEW ON sgadata.* TO reader'}])
        for grant in ['GRANT ALL PRIVILEGES ON *.* TO root',
                      'GRANT SELECT, UPDATE ON sgadata.* TO reader',
                      'GRANT EXECUTE ON *.* TO reader',
                      'GRANT SELECT ON sgadata.* TO reader WITH GRANT OPTION']:
            with self.assertRaises(ValueError):
                assert_read_only_grants([{'grant': grant}])

    def test_arbitrary_sql_rejected(self):
        source = CatalogSource({})
        for sql in ['UPDATE artsservs SET Articulo=1', 'SELECT 1; DELETE FROM artsservs']:
            with self.assertRaises(ValueError):
                source.rows(sql)

    def test_legacy_read_only_fallback_still_requires_select_grants(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'grant': 'GRANT SELECT ON sgadata.* TO reader'}]
        def execute(sql, *args):
            if sql == 'SET SESSION TRANSACTION READ ONLY':
                raise pymysql.err.OperationalError(1064, 'Legacy MariaDB syntax')
        cursor.execute.side_effect = execute
        with patch('bridge.catalog_source.pymysql.connect', return_value=connection):
            with CatalogSource({'host': 'sga.invalid', 'user': 'reader', 'password': 'synthetic'}):
                pass
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()
        self.assertEqual(cursor.execute.call_args_list[-1].args[0], 'START TRANSACTION WITH CONSISTENT SNAPSHOT')

    def test_privileged_connection_closes_before_any_catalog_read(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'grant': 'GRANT ALL PRIVILEGES ON *.* TO root'}]
        with patch('bridge.catalog_source.pymysql.connect', return_value=connection):
            with self.assertRaises(ValueError):
                CatalogSource({'host': 'sga.invalid', 'user': 'root', 'password': 'synthetic'}).__enter__()
        self.assertEqual(cursor.execute.call_count, 1)
        connection.close.assert_called_once()

    def test_unused_categories_preserved_with_parent_order(self):
        source = CatalogSource({})
        source.rows = MagicMock(side_effect=[
            [{'Division': 'D', 'DivisionNomb': 'DIVISION'}],
            [{'Categoria': 1, 'CategoriaNomb': 'USADA'}, {'Categoria': 2, 'CategoriaNomb': 'VACIA'}],
            [{'Categoria': 2, 'SubCategoria': 3, 'SubCategoriaNomb': 'VACIA TAMBIEN'}],
            [{'Division': 'D', 'Categoria': 1, 'SubCategoria': None}],
        ])
        nodes = source.categories()
        seen = set()
        for node in nodes:
            self.assertTrue(node['parent'] is None or node['parent'] in seen)
            seen.add(node['key'])
        self.assertIn(path_key('category', '_', 2), seen)
        self.assertIn(path_key('subcategory', '_', 2, 3), seen)

    def test_https_required_and_redirects_disabled(self):
        for url in ['http://odoo.invalid', 'https://user:password@odoo.invalid', 'https://odoo.invalid/path']:
            with self.assertRaises(ValueError):
                CatalogOdoo({'url': url})
        client = CatalogOdoo({'url': 'https://odoo.invalid', 'api_key': 'synthetic',
                              'database': 'synthetic-db', 'company_id': 1})
        with patch.object(client.session, 'post') as post:
            post.return_value.status_code = 302
            with self.assertRaises(RuntimeError):
                client.call('catalog_info')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
        client.close()


class CatalogCommandTests(TestCase):
    def config(self, name):
        return ({'instance': 'synthetic-sga', 'host': 'sga.invalid', 'database': 'sgadata'}
                if name == 'SGA_CATALOG_SOURCE' else
                {'url': 'https://odoo.invalid', 'database': 'synthetic-db', 'company_id': 1})

    def run_command(self, fail=False, apply=False, wrong_database=False):
        with patch('bridge.management.commands.sync_catalog.configuration', side_effect=self.config), \
             patch('bridge.management.commands.sync_catalog.CatalogSource') as source, \
             patch('bridge.management.commands.sync_catalog.CatalogOdoo') as target:
            source.return_value.__enter__.return_value.categories.return_value = [
                {'key': 'root', 'parent': None, 'name': 'SGA'}]
            source.return_value.__enter__.return_value.products.side_effect = [[{'article_id': 10}], []]
            def call(method, **kwargs):
                if method == 'catalog_info':
                    return {'contract': 1, 'database': 'wrong' if wrong_database else 'synthetic-db',
                            'company_id': 1, 'sga_writes_enabled': False}
                if kwargs['products'] and fail:
                    raise TimeoutError('synthetic lost acknowledgement')
                return {'categories': len(kwargs['categories']), 'products': len(kwargs['products'])}
            target.return_value.call.side_effect = call
            call_command('sync_catalog', apply=apply, stdout=StringIO())
            return target.return_value

    def test_default_simulation_and_successful_audit(self):
        target = self.run_command()
        run = CatalogRun.objects.get()
        self.assertTrue(run.dry_run)
        self.assertEqual((run.status, run.categories, run.products, run.cursor), ('succeeded', 1, 1, 10))
        self.assertTrue(all(c.kwargs.get('dry_run', True) for c in target.call.call_args_list))

    def test_failure_does_not_advance_cursor_and_retry_starts_at_zero(self):
        with self.assertRaises(CommandError):
            self.run_command(fail=True, apply=True)
        run = CatalogRun.objects.get()
        self.assertEqual((run.status, run.cursor, run.products), ('failed', 0, 0))
        self.assertEqual(run.error_code, 'TimeoutError')
        self.run_command(apply=True)
        self.assertEqual(CatalogRun.objects.filter(status='succeeded').count(), 1)

    def test_wrong_destination_blocks_before_source_read(self):
        with self.assertRaises(CommandError):
            self.run_command(wrong_database=True)
        self.assertEqual(CatalogRun.objects.get().cursor, 0)
