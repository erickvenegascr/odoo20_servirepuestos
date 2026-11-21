import copy
import json
import unittest
from unittest.mock import Mock

import pymysql

from bridge.purchase_effects_contract import NativeResultUnverified, PurchaseEffectsError, digest, reconcile_price
from bridge.purchase_price_source import FIXED_MARGIN_PROFILE, MAINTAIN_PROFILE, describe_native_price
from bridge.purchase_price_sql import PurchasePriceSql
from bridge.tests.test_purchase_effects_contract import APP, SOURCE, price_request_v2


def native_price_row(**overrides):
    return {
        'Articulo': 41, 'Trabajado': 255, 'Retenido': b'\0', 'MateriaPrima': b'\0', 'Servicio': b'\0',
        'NumeroUnidades': 1, 'CostoUnitDet': 100.00, 'IVA': .13, 'TipoIVA': '08', 'Exento': b'\0',
        'Nivel': 1, 'PorcGanancia': .47, 'VariacionDet': 0.0, 'PorcDescto': .1, 'PorcDesctoDet': .1,
        'PorcDesctoPromo': 0.0, 'AgotaExistPromo': b'\0', 'FechaIniPromo': None, 'FechaFinPromo': None,
        'CalculoPrecio': 1, 'MargenMin': 0.0, 'MargenMinDet': 0.0, 'FechaTopePrecio': None,
        'PrecioSugerido': 166.11, 'ConImpto': b'\1', 'PrecioSugeridoDet': 166.11, 'ConImptoDet': b'\1',
        'PorcSugerido': 0.0, 'TipoCambio': 1.0, 'DesctoEsperado': None, 'BonifEsperada': None,
        'PorcAporte': 0.0, 'NoVender': b'\0', 'FechaActualizacion': None, 'PriceRevision': 1234567890,
        'DescripcionNivel': 'Normal', 'NivelRespaldo': None, 'Export': b'\0', 'EnDolares': b'\0', 'Bodega': None,
        **overrides,
    }


def approval(row, target=150.1234, **extra):
    operation = price_request_v2()
    operation['price'].update(unit_price_excluded_tax=target, **extra)
    operation['price']['expected_native_checksum'] = describe_native_price(SOURCE, 41, row)['native_checksum']
    operation['request_checksum'] = digest(operation['price'])
    return operation


class NativePriceTests(unittest.TestCase):
    def setUp(self):
        self.config = {'enabled': True, 'application_id': APP, 'source_instance': SOURCE, 'host': 'localhost',
            'port': 3306, 'database': 'sgadata', 'integration_schema': 'sga_price_lab',
            'user': 'price', 'password': 'lab', 'audit_user': 'ODOOPR', 'audit_station': 4,
            'profile': MAINTAIN_PROFILE, 'evidence_reference': 'Lab profile'}
        self.reader = {'instance': SOURCE, 'host': 'localhost', 'database': 'sgadata', 'user': 'read'}
        self.sql = PurchasePriceSql(self.config, self.reader, APP)
        self.connection = Mock()
        self.cursor = Mock(rowcount=1)
        self.cursor.fetchone.return_value = None

    def test_native_snapshot_detects_cost_rule_changes_and_blocks_unverified_policies(self):
        baseline = native_price_row()
        lookup = describe_native_price(SOURCE, 41, baseline)
        self.assertEqual((147.0, False, 'BLOCKED_NATIVE_PRICE_NOT_CERTIFIED'), (
            lookup['unit_price_excluded_tax'], lookup['publication_supported'], lookup['publication_block_code']))
        markup = describe_native_price(SOURCE, 41, native_price_row(CalculoPrecio=0), profile=MAINTAIN_PROFILE)
        self.assertEqual(147.0, markup['unit_price_excluded_tax'])
        self.assertFalse(markup['publication_supported'])
        for overrides, code in [({'CalculoPrecio': -1}, 'SGA_PRICE_MODE_UNSUPPORTED'),
                ({'FechaTopePrecio': '2020-01-01'}, 'SGA_PRICE_MAINTAIN_TERMS_UNSUPPORTED'),
                ({'NivelRespaldo': 2}, 'SGA_PRICE_BACKUP_LEVEL_UNSUPPORTED'),
                ({'MargenMinDet': .1}, 'SGA_PRICE_MAINTAIN_TERMS_UNSUPPORTED'),
                ({'PorcDesctoPromo': .1}, 'SGA_PRICE_PROMOTION_UNSUPPORTED'),
                ({'EnDolares': b'\1'}, 'SGA_PRICE_LEVEL_UNSUPPORTED'),
                ({'TipoIVA': '04'}, 'SGA_PRICE_VAT_UNSUPPORTED'),
                ({'NumeroUnidades': 2}, 'SGA_PRICE_PRESENTATION_UNSUPPORTED'),
                ({'NoVender': b'\1'}, 'SGA_PRICE_ARTICLE_UNAVAILABLE'),
                ({'CalculoPrecio': 0, 'CostoUnitDet': 0}, 'SGA_PRICE_NATIVE_EFFECTIVE_UNAVAILABLE')]:
            with self.subTest(overrides=overrides):
                native = describe_native_price(SOURCE, 41, native_price_row(**overrides), profile=MAINTAIN_PROFILE)
                self.assertNotEqual(lookup['native_checksum'], native['native_checksum'])
                self.assertEqual(code, native['publication_block_code'])

    def test_fixed_margin_uses_live_cost_to_match_native_consulta_de_precios(self):
        row = native_price_row(CalculoPrecio=0, CostoUnitDet=1347.18,
                               PrecioSugeridoDet=912.23, PorcGanancia=.469997)
        price = describe_native_price(SOURCE, 41, row, profile=FIXED_MARGIN_PROFILE)
        self.assertEqual((1980.35, True, None), (price['unit_price_excluded_tax'], price['publication_supported'],
                                                price['publication_block_code']))
        newer = describe_native_price(SOURCE, 41, {**row, 'CostoUnitDet': 1400}, profile=FIXED_MARGIN_PROFILE)
        self.assertEqual(2058.0, newer['unit_price_excluded_tax'])
        self.assertNotEqual(price['native_checksum'], newer['native_checksum'])

        retail = native_price_row(Articulo=42, CalculoPrecio=0, CostoUnitDet=353.33,
                                  PorcGanancia=.45, VariacionDet=.3)
        price = describe_native_price(SOURCE, 42, retail, profile=FIXED_MARGIN_PROFILE)
        self.assertEqual(618.33, price['unit_price_excluded_tax'])

    def test_fixed_margin_price_only_changes_detail_variance_and_coherent_suggestion(self):
        fixed = PurchasePriceSql({**self.config, 'profile': FIXED_MARGIN_PROFILE}, self.reader, APP)
        row = native_price_row(CalculoPrecio=0)
        change = native_price_row(CalculoPrecio=0, VariacionDet=.0312, PrecioSugeridoDet=169.6356)
        fixed._locked_price = Mock(side_effect=[row, change])
        result = fixed._publish(self.connection, self.cursor, approval(row, 150.12))
        update = self.cursor.execute.call_args_list[-2]
        self.assertIn('SET VariacionDet=%s,PrecioSugeridoDet=%s', update.args[0])
        self.assertEqual((150.12, .47, 0), (result['effective_price']['unit_price_excluded_tax'],
                                           change['PorcGanancia'], change['CalculoPrecio']))
        fixed._locked_price = Mock(return_value=row)
        self.assertEqual('SGA_PRICE_NATIVE_ROUNDING_MISMATCH',
                         fixed._publish(self.connection, self.cursor, approval(row, 150.1201))['code'])

        below_cost = native_price_row(CalculoPrecio=0, VariacionDet=-.57, PrecioSugeridoDet=101.70)
        fixed._locked_price = Mock(side_effect=[row, below_cost])
        self.assertEqual(90, fixed._publish(self.connection, self.cursor, approval(row, 90))['effective_price'][
            'unit_price_excluded_tax'])

    def test_exact_native_readback_and_idempotent_replay(self):
        row = native_price_row()
        operation = approval(row)
        changed = native_price_row(PrecioSugeridoDet=169.6394, PriceRevision=1234567900)
        self.sql._locked_price = Mock(side_effect=[row, changed])
        result = self.sql._publish(self.connection, self.cursor, operation)
        self.assertEqual('synchronized', result['status'])
        self.assertEqual(150.1234, result['effective_price']['unit_price_excluded_tax'])
        self.assertEqual(result['native_price']['native_checksum'], describe_native_price(SOURCE, 41, changed)['native_checksum'])
        self.assertNotEqual(operation['price']['expected_native_checksum'], result['native_price']['native_checksum'])
        self.connection.commit.assert_called_once()
        write, journal = self.cursor.execute.call_args_list[-2:]
        self.assertIn('SET PrecioSugeridoDet=%s,CambioPrecio=TRUE', write.args[0])
        self.assertNotIn('Costo', write.args[0])
        self.assertEqual(row['PrecioSugeridoDet'], float(json.loads(journal.args[1][-3])['native']['PrecioSugeridoDet']))
        self.assertEqual(changed['PrecioSugeridoDet'], float(json.loads(journal.args[1][-2])['native']['PrecioSugeridoDet']))

        self.cursor.fetchone.return_value = {'request_hash': operation['request_checksum'], 'result': json.dumps(result)}
        self.assertEqual(result, self.sql._publish(self.connection, self.cursor, operation))
        self.connection.commit.assert_called_once()
        self.assertEqual(2, self.sql._locked_price.call_count)
        self.assertEqual('SGA_PRICE_OPERATION_CONTENT_CONFLICT',
                         self.sql._publish(self.connection, self.cursor, approval(row, 155))['code'])
        invalid = copy.deepcopy(result)
        invalid.pop('native_price')
        with self.assertRaises(NativeResultUnverified):
            reconcile_price(operation, invalid)
        invalid = copy.deepcopy(result)
        invalid['effective_price']['unit_price_excluded_tax'] += .0001
        with self.assertRaises(NativeResultUnverified):
            reconcile_price(operation, invalid)

    def test_drift_taxes_and_below_cost_cannot_enter_mutation(self):
        baseline = native_price_row()
        cases = [
            (approval(baseline), native_price_row(CostoUnitDet=111), 'SGA_PRICE_SOURCE_CHANGED'),
            (approval(baseline, vat_code='04', vat_rate=4), baseline, 'SGA_PRICE_NATIVE_TAX_OR_CURRENCY_MISMATCH'),
            (approval(baseline, target=99), baseline, 'SGA_PRICE_BELOW_CURRENT_NATIVE_COST'),
            (approval(native_price_row(CalculoPrecio=0)), native_price_row(CalculoPrecio=0),
             'SGA_PRICE_FIXED_MARGIN_REQUIRES_NATIVE_POLICY'),
        ]
        for operation, row, code in cases:
            with self.subTest(code=code):
                self.sql._locked_price = Mock(return_value=row)
                self.cursor.execute.reset_mock()
                self.assertEqual(code, self.sql._publish(self.connection, self.cursor, operation)['code'])
                self.cursor.execute.assert_called_once()  # Journal lookup only.
        self.connection.commit.assert_not_called()

    def test_change_after_write_rolls_back_and_unconfirmed_commit_stays_pending(self):
        row = native_price_row()
        operation = approval(row)
        self.sql._locked_price = Mock(side_effect=[row, native_price_row(PrecioSugeridoDet=169.6394, PorcGanancia=.3)])
        with self.assertRaisesRegex(PurchaseEffectsError, 'SGA_PRICE_NATIVE_READBACK_MISMATCH'):
            self.sql._publish(self.connection, self.cursor, operation)
        self.connection.commit.assert_not_called()
        self.sql._locked_price = Mock(side_effect=[row, native_price_row(PrecioSugeridoDet=169.6394)])
        self.connection.commit.side_effect = pymysql.err.OperationalError(2013, 'Lost connection')
        with self.assertRaisesRegex(NativeResultUnverified, 'SGA_PRICE_NATIVE_COMMIT_UNCONFIRMED'):
            self.sql._publish(self.connection, self.cursor, operation)

    def test_unapproved_invalid_scope_or_precision_never_connects(self):
        with self.assertRaisesRegex(PurchaseEffectsError, 'CONFIGURATION_INVALID'):
            PurchasePriceSql({**self.config, 'user': self.reader['user']}, self.reader, APP)
        self.sql.connect = Mock()
        self.assertEqual('SGA_PRICE_APPROVED_NATIVE_BASELINE_REQUIRED', self.sql.apply(price_request_v2())['code'])
        self.assertEqual('SGA_PRICE_REQUEST_PROFILE_UNSUPPORTED', self.sql.apply(approval(native_price_row(), 1.12345))['code'])
        with self.assertRaisesRegex(PurchaseEffectsError, 'SCOPE_MISMATCH'):
            self.sql.apply({**approval(native_price_row()), 'source_instance': 'other'})
        self.sql.connect.assert_not_called()

    def test_lock_contention_keeps_the_same_operation_pending(self):
        self.sql._apply = Mock(side_effect=pymysql.err.OperationalError(1213, 'Deadlock found'))
        with self.assertRaisesRegex(NativeResultUnverified, 'SGA_PRICE_WRITER_BUSY'):
            self.sql.apply(approval(native_price_row()))
        self.assertEqual(3, self.sql._apply.call_count)
