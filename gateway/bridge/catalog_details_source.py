"""SELECT-only enrichment of already-bound articles; no SGA write interface."""
import datetime
import json
import math
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

from .catalog_source import CatalogSource, bit, code, path_key, text

VAT = {'01': 0, '02': 1, '03': 2, '04': 4, '05': 0, '06': 4, '07': 8, '08': 13, '09': .5, '10': 0, '11': 0}


def numeric(value):
    value = float(value or 0)
    if not math.isfinite(value):
        raise ValueError('Non-finite SGA catalog value')
    return value


def safe(value):
    if isinstance(value, bytes):
        return bit(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def vat(rate, source_code, exempt):
    rate = round(numeric(rate) * 100 if abs(numeric(rate)) <= 1 else numeric(rate), 4)
    original = text(source_code).zfill(2)
    if exempt and rate == 0:
        target = '01'
    elif original in VAT and abs(VAT[original] - rate) < .00001:
        target = original
    else:
        target = {0: '01', .5: '09', 1: '02', 2: '03', 4: '04', 8: '07', 13: '08'}.get(rate)
    if target is None:
        raise ValueError('Unsupported SGA VAT rate')
    return rate, target, target != original


def commercial_unit(value):
    key = text(value).casefold()
    units = {'': 'Unid', 'unidad': 'Unid', 'unidades': 'Unid', 'unid': 'Unid',
             'centim': 'cm', 'centimetro': 'cm', 'cm': 'cm', 'metro': 'm', 'm': 'm'}
    if key not in units:
        raise ValueError('Unsupported SGA detail unit')
    return units[key]


def price_row(row, rate, strategy):
    if bit(row.get('NoVender')):
        raise ValueError('An SGA sale restriction requires an explicit catalog rule')
    today = datetime.datetime.now(ZoneInfo('America/Costa_Rica')).date()
    start, end = row.get('FechaIniPromo'), row.get('FechaFinPromo')
    start = start.date() if isinstance(start, datetime.datetime) else start
    end = end.date() if isinstance(end, datetime.datetime) else end
    if numeric(row.get('PorcDesctoPromo')) and end and end >= today:
        raise ValueError('A live or future SGA promotion requires a validated pricing adapter')
    calculation = int(row.get('CalculoPrecio') or 0)
    if calculation not in (0, 1):
        raise ValueError('Unsupported SGA price calculation mode')
    quote = numeric(row.get('PrecioSugeridoDet'))
    net = quote / (1 + rate / 100) if bit(row.get('ConImptoDet')) else quote
    markup = (numeric(row.get('PorcGanancia')) + numeric(row.get('VariacionDet'))) * 100
    return {'level': int(row['Nivel']), 'mode': 'fixed' if strategy == 'fixed' or calculation == 1 else 'markup',
            'markup': round(markup, 8), 'fixed_price': net,
            'source': {k: safe(v) for k, v in row.items() if k != 'Articulo'}}


class CatalogDetailsSource(CatalogSource):
    def masters(self):
        result = []
        for node in self.categories():
            if node['key'] == 'root':
                continue
            parts = json.loads(node['key'])
            kind = {'division': 'division', 'category': 'line', 'subcategory': 'subcategory'}[parts[0]]
            if kind == 'line' and parts[-1] == '_':
                continue
            if kind == 'subcategory' and parts[-2] == '_':
                continue
            result.append({'kind': kind, 'key': node['key'],
                           'code': 'SGA:_' if parts[-1] == '_' else parts[-1],
                           'name': re.sub(r'^\[[^]]+\]\s*', '', node['name']),
                           'parent': None if kind == 'division' else node['parent'], 'currency': None})
        for row in self.rows('SELECT Marca,MarcaNomb FROM marcas ORDER BY Marca'):
            result.append({'kind': 'brand', 'key': path_key('brand', row['Marca']), 'code': code(row['Marca']),
                           'name': text(row['MarcaNomb']) or code(row['Marca']), 'parent': None, 'currency': None})
        for row in self.rows('SELECT Nivel,DescripcionNivel,EnDolares FROM preciosniveles ORDER BY Nivel'):
            result.append({'kind': 'pricelist', 'key': str(row['Nivel']), 'code': str(row['Nivel']),
                           'name': (text(row['DescripcionNivel']) or str(row['Nivel'])) + ' (SGA)',
                           'parent': None, 'currency': 'USD' if bit(row['EnDolares']) else 'CRC'})
        return result

    def details(self, official, strategy='formula', after=0, limit=100):
        rows = self.rows(
            'SELECT a.Articulo,a.CodHacienda,a.Unidad,a.UnidadDet,a.NumeroUnidades,a.CostoUnit,a.CostoUnitDet,'
            'a.TipoIVA,a.IVA,a.Exento,r.Division,r.Categoria,r.SubCategoria,r.Marca,m.Marca CatalogMarca '
            'FROM artsservs a LEFT JOIN artsservsrefer r ON r.Articulo=a.Articulo '
            'LEFT JOIN marcas m ON m.Marca=r.Marca '
            'WHERE a.Articulo>%s ORDER BY a.Articulo LIMIT %s', (after, limit))
        if not rows:
            return []
        ids = [int(r['Articulo']) for r in rows]
        prices = self.rows(
            'SELECT Articulo,Nivel,PorcGanancia,VariacionDet,PorcDesctoDet,PorcDesctoPromo,AgotaExistPromo,'
            'FechaIniPromo,FechaFinPromo,CalculoPrecio,MargenMinDet,FechaTopePrecio,PrecioSugeridoDet,ConImptoDet,'
            'TipoCambio,NoVender FROM artsservsprecios WHERE Articulo IN (' + ','.join(['%s'] * len(ids)) + ') '
            'ORDER BY Articulo,Nivel', ids)
        by_article = {}
        for row in prices:
            by_article.setdefault(int(row['Articulo']), []).append(row)
        result = []
        for row in rows:
            cabys = text(row['CodHacienda'])
            if cabys and not re.fullmatch(r'[0-9]{13}', cabys):
                raise ValueError('Invalid CABYS in SGA source')
            if cabys and cabys not in official['codes']:
                raise ValueError('CABYS has not been checked against the official catalogue')
            entry = official['codes'].get(cabys, {'status': 'missing'})
            rate, rate_code, corrected = vat(row['IVA'], row['TipoIVA'], bit(row['Exento']))
            factor = int(row['NumeroUnidades'] or 1)
            if factor <= 0:
                raise ValueError('Invalid SGA pack factor')
            cost = numeric(row['CostoUnit']) / factor
            division = path_key('division', row['Division']) if row['Division'] is not None or row['Categoria'] is not None else None
            line = path_key('category', row['Division'], row['Categoria']) if row['Categoria'] is not None else None
            sub = path_key('subcategory', row['Division'], row['Categoria'], row['SubCategoria']) if row['SubCategoria'] is not None else None
            result.append({'article_id': int(row['Articulo']), 'division': division, 'line': line, 'subcategory': sub,
                'brand': path_key('brand', row.get('CatalogMarca') or row['Marca']) if text(row['Marca']) else None,
                'cabys': cabys, 'cabys_description': entry.get('description', ''), 'cabys_status': entry['status'],
                'commercial_unit': commercial_unit(row['UnidadDet']), 'unit_cost': cost,
                'vat_rate': rate, 'vat_code': rate_code, 'tax_corrected': corrected,
                'prices': [price_row(p, rate, strategy) for p in by_article.get(int(row['Articulo']), [])],
                'snapshot': {k: safe(v) for k, v in row.items()}})
        return result
