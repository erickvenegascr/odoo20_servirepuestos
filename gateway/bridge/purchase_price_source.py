"""Read the Normal retail price and bind the native state used for approval."""

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from .catalog_source import CatalogSource, bit, text
from .purchase_effects_contract import MAX_ID, VAT_RATES, digest


LEVEL = 1
QUANTUM = Decimal('.0001')
CENT = Decimal('.01')
MAX_PRICE = Decimal('999999999.9999')
MAINTAIN_PRICE = 1
FIXED_MARGIN = 0
MAINTAIN_PROFILE = 'crc_normal_maintain_retail'
FIXED_MARGIN_PROFILE = 'crc_normal_fixed_margin_retail'
ARTICLE_COLUMNS = ('Articulo', 'Trabajado', 'Retenido', 'MateriaPrima', 'Servicio', 'NumeroUnidades',
                   'CostoUnitDet', 'IVA', 'TipoIVA', 'Exento')
PRICE_COLUMNS = ('Articulo', 'Nivel', 'PorcGanancia', 'VariacionDet', 'PorcDescto', 'PorcDesctoDet',
                 'PorcDesctoPromo', 'AgotaExistPromo', 'FechaIniPromo', 'FechaFinPromo', 'CalculoPrecio',
                 'MargenMin', 'MargenMinDet', 'FechaTopePrecio', 'PrecioSugerido', 'ConImpto',
                 'PrecioSugeridoDet', 'ConImptoDet', 'PorcSugerido', 'TipoCambio', 'DesctoEsperado',
                 'BonifEsperada', 'PorcAporte', 'NoVender', 'FechaActualizacion', 'PGTS')
LEVEL_COLUMNS = ('Nivel', 'DescripcionNivel', 'NivelRespaldo', 'Export', 'EnDolares', 'Bodega')
READ_COLUMNS = {'artsservs': set(ARTICLE_COLUMNS), 'artsservsprecios': set(PRICE_COLUMNS),
                'preciosniveles': set(LEVEL_COLUMNS)}


def valid_scope(scope):
    if (not isinstance(scope, dict) or set(scope) != {'article_ids', 'level'}
            or type(scope['level']) is not int or scope['level'] != LEVEL):
        return False
    ids = scope['article_ids']
    return (isinstance(ids, list) and 1 <= len(ids) <= 50
            and all(type(article) is int and 1 <= article <= MAX_ID for article in ids)
            and len(ids) == len(set(ids)))


def four(value):
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def native_number(value):
    return Decimal(str(value))


def price_query(placeholders):
    article = ','.join(f'a.{name}' for name in ARTICLE_COLUMNS)
    rules = ','.join(f'p.{name}' for name in PRICE_COLUMNS
                     if name not in ('Articulo', 'PGTS', 'PorcGanancia', 'VariacionDet'))
    level = ','.join(f'n.{name}' for name in LEVEL_COLUMNS if name != 'Nivel')
    # FLOAT output otherwise loses digits before the effective price is rounded.
    return (f'SELECT {article},{rules},{level},UNIX_TIMESTAMP(p.PGTS) AS PriceRevision,'
            'CAST(p.PorcGanancia+0E0 AS DECIMAL(36,18)) AS PorcGanancia,'
            'CAST(p.VariacionDet+0E0 AS DECIMAL(36,18)) AS VariacionDet '
            'FROM artsservs a JOIN artsservsprecios p ON a.Articulo=p.Articulo '
            'JOIN preciosniveles n ON n.Nivel=p.Nivel '
            f'WHERE a.Articulo IN ({placeholders}) AND p.Nivel=%s ORDER BY a.Articulo')


def _json_value(value):
    if isinstance(value, bytes):
        return bit(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (float, Decimal)):
        return str(value)
    return value


def native_snapshot(source_instance, row):
    return {'source_instance': source_instance, 'native': {key: _json_value(value) for key, value in row.items()}}


def compute_vat(row):
    code = text(row['TipoIVA'])
    if code not in VAT_RATES:
        return None
    rate = native_number(row['IVA']) * 100
    expected = Decimal(str(VAT_RATES[code]))
    if (not rate.is_finite() or abs(rate - expected) > Decimal('.0001')
            or bit(row['Exento']) != (expected == 0)):
        return None
    return code, VAT_RATES[code]


def _retail_block(row):
    if (row['Trabajado'] != 255 or bit(row['Retenido']) or bit(row['MateriaPrima'])
            or bit(row['Servicio']) or bit(row['NoVender'])):
        return 'SGA_PRICE_ARTICLE_UNAVAILABLE'
    if (row['Nivel'] != LEVEL or text(row['DescripcionNivel']).casefold() != 'normal'
            or bit(row['Export']) or bit(row['EnDolares']) or row['TipoCambio'] != 1):
        return 'SGA_PRICE_LEVEL_UNSUPPORTED'
    if row['NumeroUnidades'] != 1:
        return 'SGA_PRICE_PRESENTATION_UNSUPPORTED'
    if compute_vat(row) is None:
        return 'SGA_PRICE_VAT_UNSUPPORTED'
    if row['PorcDesctoPromo'] != 0 or bit(row['AgotaExistPromo']):
        return 'SGA_PRICE_PROMOTION_UNSUPPORTED'
    if row['CalculoPrecio'] not in (FIXED_MARGIN, MAINTAIN_PRICE):
        return 'SGA_PRICE_MODE_UNSUPPORTED'
    return None


def net_suggested(row, rate):
    value = native_number(row['PrecioSugeridoDet'])
    if bit(row['ConImptoDet']):
        value /= 1 + native_number(rate) / 100
    return four(value)


def net_fixed_margin(row):
    cost = native_number(row['CostoUnitDet'])
    margin = native_number(row['PorcGanancia']) + native_number(row['VariacionDet'])
    return (cost * (1 + margin)).quantize(CENT, rounding=ROUND_HALF_UP) if cost > 0 else Decimal(0)


def _maintained_in_effect(row, net):
    cost = native_number(row['CostoUnitDet'])
    return (row['CalculoPrecio'] == MAINTAIN_PRICE and row['FechaTopePrecio'] is None
            and row['MargenMinDet'] == 0 and 0 < cost <= net)


def describe_native_price(source_instance, article, row, *, profile=None):
    price = {'article_id': article, 'level': LEVEL, 'currency': 'CRC', 'exchange_rate': 1,
             'vat_code': None, 'vat_rate': None, 'unit_price_excluded_tax': None, 'native_checksum': None,
             'publication_supported': False, 'publication_block_code': 'SGA_PRICE_NATIVE_ROW_NOT_FOUND'}
    if row is None:
        return price
    price['native_checksum'] = digest(native_snapshot(source_instance, row))
    blocker = _retail_block(row)
    if blocker:
        price['publication_block_code'] = blocker
        return price
    vat_code, rate = compute_vat(row)
    price.update(vat_code=vat_code, vat_rate=rate)
    fixed_margin = row['CalculoPrecio'] == FIXED_MARGIN
    expected_profile = FIXED_MARGIN_PROFILE if fixed_margin else MAINTAIN_PROFILE
    net = net_fixed_margin(row) if fixed_margin else net_suggested(row, rate)
    if not 0 < net <= MAX_PRICE:
        price['publication_block_code'] = 'SGA_PRICE_NATIVE_EFFECTIVE_UNAVAILABLE'
        return price
    if not fixed_margin and not _maintained_in_effect(row, net):
        price['publication_block_code'] = 'SGA_PRICE_MAINTAIN_TERMS_UNSUPPORTED'
        return price
    price['unit_price_excluded_tax'] = float(net)
    if row['NivelRespaldo'] not in (None, 0, LEVEL):
        price['publication_block_code'] = 'SGA_PRICE_BACKUP_LEVEL_UNSUPPORTED'
    elif fixed_margin and (row['MargenMinDet'] != 0 or row['FechaTopePrecio'] is not None):
        price['publication_block_code'] = 'SGA_PRICE_FIXED_MARGIN_TERMS_UNSUPPORTED'
    elif profile != expected_profile:
        price['publication_block_code'] = ('SGA_PRICE_FIXED_MARGIN_REQUIRES_NATIVE_POLICY' if fixed_margin
                                          else 'BLOCKED_NATIVE_PRICE_NOT_CERTIFIED')
    else:
        price.update(publication_supported=True, publication_block_code=None)
    return price


class PurchasePriceSource(CatalogSource):
    def lookup(self, article_ids, level, *, profile=None):
        rows = self.rows(price_query(','.join(['%s'] * len(article_ids))), (*article_ids, level))
        by_article = {row['Articulo']: row for row in rows}
        if len(rows) != len(by_article) or not set(by_article) <= set(article_ids):
            raise ValueError('SGA Normal retail rows do not have a unique requested identity')
        source = self.config.get('instance') or self.config.get('source_instance')
        return [describe_native_price(source, article, by_article.get(article), profile=profile)
                for article in article_ids]
