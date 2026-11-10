"""Bounded catalog reads. This adapter has no SGA mutation interface."""
import json
import os
import re
from pathlib import Path

import pymysql


class SourceReadOnlyError(ValueError):
    code = 'SGA_SELECT_ONLY_ACCOUNT_REQUIRED'


def configuration(name):
    return json.loads(Path(os.environ[name + '_FILE']).read_text())


def code(value):
    return str(value).strip() if value not in (None, '') else '_'


def path_key(*parts):
    return json.dumps([code(p) for p in parts], separators=(',', ':'), ensure_ascii=False)


def text(value):
    value = str(value or '').strip()
    try:
        return value.encode('latin1').decode('utf8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def bit(value):
    return bool(int.from_bytes(value, 'big')) if isinstance(value, bytes) else bool(value)


def assert_read_only_grants(grants):
    """MariaDB 5.5 cannot enforce READ ONLY transactions: reject write grants."""
    has_select = False
    for row in grants:
        grant = str(next(iter(row.values())))
        match = re.match(r'^GRANT (.*?) ON ', grant, re.I)
        if not match or 'WITH GRANT OPTION' in grant.upper():
            raise SourceReadOnlyError('SGA requires a dedicated SELECT-only database account')
        privileges = {p.strip().upper() for p in match[1].split(',')}
        if not privileges <= {'USAGE', 'SELECT', 'SHOW VIEW'}:
            raise SourceReadOnlyError('SGA account has privileges beyond read-only access')
        has_select |= 'SELECT' in privileges
    if not has_select:
        raise SourceReadOnlyError('SGA account has no SELECT grant')


class CatalogSource:
    def __init__(self, config):
        self.config = config
        self.connection = None

    def __enter__(self):
        self.connection = pymysql.connect(
            host=self.config['host'], port=int(self.config.get('port', 3306)),
            user=self.config['user'], password=self.config['password'],
            database=self.config.get('database', 'sgadata'), charset='latin1',
            cursorclass=pymysql.cursors.DictCursor, autocommit=False,
            connect_timeout=10, read_timeout=60,
        )
        self.connection.encoding = 'latin-1'
        try:
            with self.connection.cursor() as cursor:
                cursor.execute('SHOW GRANTS FOR CURRENT_USER()')
                assert_read_only_grants(cursor.fetchall())
                try:
                    cursor.execute('SET SESSION TRANSACTION READ ONLY')
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] not in (1064, 1193):
                        raise
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            return self
        except Exception:
            self.connection.close()
            raise

    def __exit__(self, *args):
        self.connection.rollback()
        self.connection.close()

    def rows(self, sql, params=()):
        if not sql.startswith('SELECT ') or ';' in sql:
            raise ValueError('Only a single SELECT is permitted')
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def categories(self):
        """Include unused catalog entries, as well as all assigned paths."""
        divisions = {code(r['Division']): text(r['DivisionNomb']) for r in self.rows(
            'SELECT Division, DivisionNomb FROM divisiones ORDER BY Division')}
        categories = {code(r['Categoria']): text(r['CategoriaNomb']) for r in self.rows(
            'SELECT Categoria, CategoriaNomb FROM categorias ORDER BY Categoria')}
        subcategories = {(code(r['Categoria']), code(r['SubCategoria'])): text(r['SubCategoriaNomb'])
                         for r in self.rows('SELECT Categoria, SubCategoria, SubCategoriaNomb '
                                            'FROM categoriassub ORDER BY Categoria, SubCategoria')}
        paths = self.rows('SELECT DISTINCT Division, Categoria, SubCategoria FROM artsservsrefer '
                          'WHERE Articulo > 0 ORDER BY Division, Categoria, SubCategoria')
        nodes = {'root': {'key': 'root', 'parent': None, 'name': 'SGA'}}

        def division(d):
            key = path_key('division', d)
            nodes[key] = {'key': key, 'parent': 'root',
                          'name': f'[{d}] {divisions.get(d) or "SIN DIVISION"}'}
            return key

        def category(d, c):
            key = path_key('category', d, c)
            parent = division(d)
            nodes[key] = {'key': key, 'parent': parent,
                          'name': f'[{c}] {categories.get(c) or "SIN CATEGORIA"}'}
            return key

        pairs = {(code(r['Division']), code(r['Categoria'])) for r in paths}
        # Unassigned catalog entries remain visible under SIN DIVISION.
        pairs |= {('_', c) for c in categories if not any(p[1] == c for p in pairs)}
        pairs |= {('_', c) for c, _ in subcategories if not any(p[1] == c for p in pairs)}
        pairs.add(('_', '_'))
        for d in divisions:
            division(d)
        for d, c in sorted(pairs):
            parent = category(d, c)
            subs = {s for cat, s in subcategories if cat == c}
            subs |= {code(r['SubCategoria']) for r in paths
                     if code(r['Division']) == d and code(r['Categoria']) == c
                     and r['SubCategoria'] not in (None, '')}
            for s in sorted(subs):
                key = path_key('subcategory', d, c, s)
                nodes[key] = {'key': key, 'parent': parent,
                              'name': f'[{s}] {subcategories.get((c, s)) or "SIN SUBCATEGORIA"}'}
        # Insertion order always places each parent before its children.
        return list(nodes.values())

    def products(self, after=0, limit=100):
        rows = self.rows(
            'SELECT a.Articulo, a.NombreCorto, a.ArticuloNomb, a.Servicio, a.Trabajado, '
            'a.NumeroUnidades, a.Unidad, a.UnidadDet, a.CodHacienda, a.Atributos, a.NoPedir, '
            'r.Division, r.Categoria, r.SubCategoria, r.Marca '
            'FROM artsservs a LEFT JOIN artsservsrefer r ON r.Articulo=a.Articulo '
            'WHERE a.Articulo > %s ORDER BY a.Articulo LIMIT %s', (after, limit))
        result = []
        for row in rows:
            key = (path_key('category', row['Division'], row['Categoria'])
                   if row['SubCategoria'] in (None, '') else
                   path_key('subcategory', row['Division'], row['Categoria'], row['SubCategoria']))
            result.append({
                'article_id': int(row['Articulo']), 'name': text(row['ArticuloNomb']) or str(row['Articulo']),
                'default_code': text(row['NombreCorto']) or str(row['Articulo']),
                'service': bit(row['Servicio']), 'active': int(row['Trabajado'] or 0) == 255,
                'category_key': key, 'units_per_pack': int(row['NumeroUnidades'] or 1),
                'pack_uom': text(row['Unidad']), 'detail_uom': text(row['UnidadDet']),
                'cabys': text(row['CodHacienda']), 'keywords': text(row['Atributos']),
                'no_order': bit(row['NoPedir']), 'brand_code': text(row['Marca']),
            })
        return result
