"""Short MariaDB card transactions and opt-in initial article setup; no balances.

The integration schema must be provisioned separately. Nothing here creates or
alters SGA tables. An operation UUID and its result commit with the business rows.
"""
import json
import re

import pymysql

from .article_contract import ArticleError, article_code, digest, identity, merge, next_article, request
from .catalog_source import bit, text
from .article_initialization import INITIAL_COLUMNS, initialize, profile


WRITE_COLUMNS = {
    'artsservs': {'INSERT': {'Articulo','ArticuloNomb','NombreCorto','Servicio','Usuario','Estacion','FechaActualizacion'},
                 'UPDATE': {'ArticuloNomb','NombreCorto','Usuario','Estacion','FechaActualizacion'}},
    'artsservsrefer': {'INSERT': {'Articulo','Division','Categoria','SubCategoria','Marca','FechaCreacion','Usuario','Estacion','FechaActualizacion'},
                      'UPDATE': {'Division','Categoria','SubCategoria','Marca','Usuario','Estacion','FechaActualizacion'}},
    'articuloscodbarras': {'INSERT': {'Articulo','Tipo','CodBarra','Usuario','Estacion','FechaActualizacion'},
                          'UPDATE': {'CodBarra','Usuario','Estacion','FechaActualizacion'}, 'DELETE': None},
}
INTEGRATION_PRIVILEGES = {'configuration': {'SELECT'},'allocator_lock': {'SELECT','UPDATE'},
                        'article_receipt': {'SELECT','INSERT','UPDATE'},'article_binding': {'SELECT','INSERT'}}


def assert_writer_grants(rows, database, integration, allow_initialization=False):
    """Fail closed on global, DDL, delegation or writes to non-card columns."""
    for row in rows:
        grant=str(next(iter(row.values())))
        match=re.match(r'^GRANT (.*?) ON ([^ ]+) TO ',grant,re.I)
        if not match or 'WITH GRANT OPTION' in grant.upper():
            raise ArticleError('ARTICLE_WRITER_ACCOUNT_TOO_BROAD')
        privileges,scope=match.groups()
        if privileges=='USAGE' and scope=='*.*':continue
        parts=scope.replace('`','').split('.')
        if len(parts)!=2:raise ArticleError('ARTICLE_WRITER_ACCOUNT_TOO_BROAD')
        schema,table=parts
        parsed=list(re.finditer(r'(SELECT|INSERT|UPDATE|DELETE)(?: \(([^)]*)\))?(?:, |$)',privileges))
        if ''.join(m.group(0) for m in parsed)!=privileges:
            raise ArticleError('ARTICLE_WRITER_ACCOUNT_TOO_BROAD')
        for item in parsed:
            privilege,columns=item.groups()
            if schema==database and privilege=='SELECT':continue
            if schema==integration and privilege in INTEGRATION_PRIVILEGES.get(table,set()) and columns is None:continue
            allowed=WRITE_COLUMNS.get(table,{}).get(privilege,set()) if schema==database else set()
            if schema==database and allow_initialization:
                extra=INITIAL_COLUMNS.get(table,set())
                if privilege=='INSERT':allowed=(allowed or set())|extra
                elif privilege=='UPDATE' and table in ('artsservs','artsservsrefer','articulosproveedor','artsservsprecios'):
                    if table=='articulosproveedor':extra={'CodigoArticulo','PrecioCotiz','PrecioCotizDet','Usuario','Estacion','FechaActualizacion'}
                    if table=='artsservsprecios':extra={'PorcDescto','PorcDesctoDet','Usuario','Estacion','FechaActualizacion'}
                    allowed=(allowed or set())|(extra-{'Articulo','Proveedor','Bodega','Nivel','Prioridad'})
            if allowed is None and columns is None:continue
            actual={c.strip().strip('`') for c in columns.split(',')} if columns else set()
            if not actual or not actual<=allowed:
                raise ArticleError('ARTICLE_WRITER_ACCOUNT_TOO_BROAD')


class ArticleCommitUnknown(RuntimeError):
    """Retry the same operation UUID; never allocate a new request identity."""


class ArticleRetry(RuntimeError):
    pass


class ArticleSql:
    def __init__(self, config, application_id, *, connect=pymysql.connect):
        self.config = dict(config)
        self.application_id = identity(application_id)
        self.connect = connect
        schema = config.get('integration_schema', '')
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', schema)
                or config.get('enabled') is not True
                or config.get('application_id') != self.application_id
                or config.get('audit_mode') != 'transaction_receipt'
                or not isinstance(config.get('source_instance'), str)
                or not 1 <= len(config['source_instance']) <= 80
                or not isinstance(config.get('database'), str)
                or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,63}', config['database'])
                or config['database'] == schema
                or not 1 <= len(config.get('audit_user', '')) <= 8
                or type(config.get('audit_station')) is not int
                or not 0 <= config['audit_station'] <= 32767):
            raise ArticleError('ARTICLE_WRITER_CONFIGURATION_INVALID')
        self.schema = '`' + schema + '`'
        self.initialization_profile=profile(config['initialization']) if config.get('initialization') else None

    def _open(self):
        options = {key: self.config[key] for key in ('host', 'user', 'password', 'database') if key in self.config}
        options['port'] = int(self.config.get('port', 3306))
        if self.config.get('unix_socket'):
            options['unix_socket'] = self.config['unix_socket']
        connection = self.connect(**options, charset='utf8', cursorclass=pymysql.cursors.DictCursor,
                                  connect_timeout=3, read_timeout=5, write_timeout=5, autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute('SHOW GRANTS FOR CURRENT_USER()')
                assert_writer_grants(cursor.fetchall(),self.config['database'],self.config['integration_schema'],bool(self.initialization_profile))
                cursor.execute("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                cursor.execute('SET SESSION innodb_lock_wait_timeout=2')
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
                cursor.execute('SELECT contract_version,source_instance,source_database FROM ' + self.schema + '.configuration WHERE singleton=1')
                row = cursor.fetchone()
                if (not row or row['contract_version'] != 2 or row['source_instance'] != self.config['source_instance']
                        or row['source_database'] != self.config['database']):
                    raise ArticleError('ARTICLE_WRITER_SCHEMA_UNAVAILABLE')
                cursor.execute('SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE FROM information_schema.TABLES '
                               'WHERE TABLE_SCHEMA IN (%s,%s)',
                               (self.config['database'], self.config['integration_schema']))
                engines = {(r['TABLE_SCHEMA'], r['TABLE_NAME']): r['ENGINE'] for r in cursor.fetchall()}
                for schema, names in [(self.config['database'], ('artsservs','artsservsrefer','articuloscodbarras')),
                                      (self.config['integration_schema'], ('configuration','allocator_lock',
                                                                         'article_receipt','article_binding'))]:
                    if any(engines.get((schema,n)) != 'InnoDB' for n in names):
                        raise ArticleError('ARTICLE_WRITER_REQUIRES_INNODB')
                if self.initialization_profile and any(engines.get((self.config['database'],n))!='InnoDB'
                        for n in ('articulosproveedor','articulosbodegas','artsservsprecios','proveedores','bodegas','preciosniveles')):
                    raise ArticleError('ARTICLE_WRITER_REQUIRES_INNODB')
            connection.rollback()
            return connection
        except Exception:
            connection.close()
            raise

    def cards(self, after=0, limit=100):
        """Bounded full sweeps also detect deleted barcode children, without tombstones.

        A consistent snapshot prevents mixing parent and child revisions. The
        caller advances its checkpoint only after Odoo commits the whole page.
        """
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ArticleError('INVALID_ARTICLE_CURSOR')
        connection = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
                cursor.execute('SELECT Articulo,Trabajado FROM artsservs WHERE Articulo>%s ORDER BY Articulo LIMIT %s',
                               (after, limit))
                native_rows = cursor.fetchall()
                result = []
                for native in native_rows:
                    article = native['Articulo']
                    card, created = self._current(cursor, article)
                    cursor.execute('SELECT product_uuid,created_at_sga FROM ' + self.schema +
                                   '.article_binding WHERE application_uuid=%s AND article_id=%s',
                                   (self.application_id, article))
                    binding = cursor.fetchone()
                    if not self._references_present(cursor,article):
                        raise ArticleError('SGA_ARTICLE_REFERENCES_REQUIRED')
                    if binding and binding['created_at_sga'] != created:
                        raise ArticleError('SGA_ARTICLE_CREATION_IDENTITY_CHANGED')
                    result.append({'article_id': article, 'values': card,
                                   'product_uuid': binding['product_uuid'] if binding else None,
                                   'source_inactive': native['Trabajado'] == 1,
                                   'references_present': self._references_present(cursor,article)})
                return result
        finally:
            connection.rollback()
            connection.close()

    def _receipt(self, cursor, operation):
        cursor.execute('SELECT request_hash, result FROM ' + self.schema +
                       '.article_receipt WHERE application_uuid=%s AND operation_uuid=%s FOR UPDATE',
                       (self.application_id, operation['operation_id']))
        row = cursor.fetchone()
        if row:
            if row['request_hash'] != digest(operation):
                raise ArticleError('ARTICLE_OPERATION_IDENTITY_CONFLICT')
            result = json.loads(row['result'])
            if result.get('operation_id') != operation['operation_id']:
                raise ArticleError('ARTICLE_RECEIPT_INVALID')
            return result

    def _current(self, cursor, article, *, lock=False):
        cursor.execute(
            'SELECT a.Articulo, a.ArticuloNomb, a.NombreCorto, a.Servicio, '
            'r.Division, r.Categoria, r.SubCategoria, r.Marca, r.FechaCreacion '
            'FROM artsservs a LEFT JOIN artsservsrefer r ON r.Articulo=a.Articulo '
            'WHERE a.Articulo=%s' + (' FOR UPDATE' if lock else ''), (article,))
        row = cursor.fetchone()
        if not row:
            return None, None
        cursor.execute('SELECT Tipo, CodBarra FROM articuloscodbarras WHERE Articulo=%s '
                       'AND Tipo IN (0,1)' + (' FOR UPDATE' if lock else ''), (article,))
        codes = {str(item['Tipo']): text(item['CodBarra']) or None for item in cursor.fetchall()}
        return {'name': text(row['ArticuloNomb']) or str(article),
                'reference': text(row['NombreCorto']) or None, 'service': bit(row['Servicio']),
                'classification': {'division': text(row['Division']) or None,
                                   'category': row['Categoria'], 'subcategory': row['SubCategoria'],
                                   'brand': text(row['Marca']) or None},
                'barcodes': {key: codes.get(key) for key in ('0', '1')}}, row['FechaCreacion']

    def _references_present(self, cursor, article):
        cursor.execute('SELECT Articulo FROM artsservsrefer WHERE Articulo=%s', (article,))
        row=cursor.fetchone()
        return bool(row and row['Articulo']==article)

    def _classification(self, cursor, values):
        for field, table, column in [('division', 'divisiones', 'Division'),
                                     ('category', 'categorias', 'Categoria'), ('brand', 'marcas', 'Marca')]:
            code = values[field]
            if code is not None:
                cursor.execute('SELECT ' + column + ' FROM ' + table + ' WHERE ' + column + '=%s', (code,))
                if not cursor.fetchone():
                    raise ArticleError('SGA_CLASSIFICATION_NOT_FOUND')
        if values['subcategory'] is not None:
            cursor.execute('SELECT SubCategoria FROM categoriassub WHERE Categoria=%s AND SubCategoria=%s',
                           (values['category'], values['subcategory']))
            if not cursor.fetchone():
                raise ArticleError('SGA_SUBCATEGORY_PARENT_MISMATCH')

    def _reference(self, cursor, article, reference):
        # Native NombreCorto is indexed but is not UNIQUE in the deployed schema.
        # Detect existing collisions; the adapter serializes its own allocations.
        if reference:
            cursor.execute('SELECT Articulo FROM artsservs WHERE NombreCorto=%s AND Articulo<>%s LIMIT 1',
                           (reference, article))
            if cursor.fetchone():
                raise ArticleError('SGA_ARTICLE_REFERENCE_ALREADY_USED')

    def _identity(self, cursor, operation):
        cursor.execute('SELECT article_id, created_at_sga, origin_operation_uuid FROM ' + self.schema +
                       '.article_binding WHERE application_uuid=%s AND product_uuid=%s FOR UPDATE',
                       (self.application_id, operation['product_uuid']))
        binding = cursor.fetchone()
        if binding and (operation['action'] == 'article.create' or binding['article_id'] != operation['article_id']):
            raise ArticleError('ARTICLE_PRODUCT_ALREADY_BOUND')
        if operation['action']=='article.initialize' and (not binding or not binding['origin_operation_uuid']):
            raise ArticleError('ARTICLE_INITIALIZATION_REQUIRES_GATEWAY_CREATED_ARTICLE')
        return binding

    def _save_card(self, cursor, article, before, after):
        actor = (self.config['audit_user'], self.config['audit_station'])
        if before['name'] != after['name'] or before['reference'] != after['reference']:
            cursor.execute('UPDATE artsservs SET ArticuloNomb=%s, NombreCorto=%s, Usuario=%s, '
                           'Estacion=%s, FechaActualizacion=NOW() WHERE Articulo=%s',
                           (after['name'], after['reference'], *actor, article))
        if before['classification'] != after['classification']:
            self._save_classification(cursor, article, after['classification'])
        for kind in ('0', '1'):
            if before['barcodes'][kind] == after['barcodes'][kind]:
                continue
            barcode = after['barcodes'][kind]
            if barcode is None:
                cursor.execute('DELETE FROM articuloscodbarras WHERE Articulo=%s AND Tipo=%s', (article, int(kind)))
            else:
                cursor.execute('SELECT Articulo FROM articuloscodbarras WHERE Articulo=%s AND Tipo=%s FOR UPDATE',
                               (article, int(kind)))
                if cursor.fetchone():
                    cursor.execute('UPDATE articuloscodbarras SET CodBarra=%s,Usuario=%s,Estacion=%s,'
                                   'FechaActualizacion=NOW() WHERE Articulo=%s AND Tipo=%s',
                                   (barcode, *actor, article, int(kind)))
                else:
                    cursor.execute('INSERT INTO articuloscodbarras '
                                   '(Articulo,Tipo,CodBarra,Usuario,Estacion,FechaActualizacion) '
                                   'VALUES (%s,%s,%s,%s,%s,NOW())',
                                   (article, int(kind), barcode, *actor))

    def _save_classification(self, cursor, article, classification):
        # Never update commission, return warehouse, activity or other SGA fields.
        cursor.execute('SELECT Articulo FROM artsservsrefer WHERE Articulo=%s FOR UPDATE', (article,))
        parameters=(*(classification[key] for key in ('division','category','subcategory','brand')),
                    self.config['audit_user'],self.config['audit_station'])
        if cursor.fetchone():
            cursor.execute('UPDATE artsservsrefer SET Division=%s,Categoria=%s,SubCategoria=%s,Marca=%s,'
                           'Usuario=%s,Estacion=%s,FechaActualizacion=NOW() WHERE Articulo=%s',
                           (*parameters,article))
        else:
            cursor.execute('INSERT INTO artsservsrefer '
                           '(Articulo,Division,Categoria,SubCategoria,Marca,Usuario,Estacion,FechaCreacion,FechaActualizacion) '
                           'VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())', (article,*parameters))

    def apply(self, payload):
        operation = request(payload)
        if operation['source_instance'] != self.config['source_instance']:
            raise ArticleError('ARTICLE_SOURCE_SCOPE_MISMATCH')
        allowed=self.config.get('allowed_product_uuids')
        if allowed is not None and (not isinstance(allowed,list) or operation['product_uuid'] not in allowed):
            raise ArticleError('ARTICLE_PRODUCT_NOT_AUTHORIZED')
        for attempt in range(3):
            try:
                return self._apply(operation)
            except ArticleRetry:
                if attempt == 2:
                    raise ArticleError('SGA_ARTICLE_BUSY') from None

    def _apply(self, operation):
        connection = self._open()
        committing = False
        try:
            connection.begin()
            with connection.cursor() as cursor:
                existing = self._receipt(cursor, operation)
                if existing:
                    connection.rollback()
                    return existing
                if operation['contract']==3 and not self.initialization_profile:
                    raise ArticleError('ARTICLE_INITIALIZATION_PROFILE_REQUIRED')
                if self.initialization_profile and operation['action']=='article.create' and operation['contract']!=3:
                    raise ArticleError('ARTICLE_COMPLETE_CREATION_REQUIRED')
                # A durable operation claim shares the SGA commit. Concurrent
                # deliveries wait on this unique key and then reuse the receipt.
                try:
                    cursor.execute('INSERT INTO ' + self.schema + '.article_receipt '
                                   '(application_uuid,operation_uuid,request_hash,result,created_utc) '
                                   'VALUES (%s,%s,%s,%s,UTC_TIMESTAMP())',
                                   (self.application_id, operation['operation_id'], digest(operation), '{}'))
                except pymysql.err.IntegrityError as exc:
                    if exc.args[0] == 1062:
                        raise ArticleRetry from None
                    raise
                binding = self._identity(cursor, operation)
                cursor.execute('SELECT lock_name FROM ' + self.schema + '.allocator_lock '
                               "WHERE lock_name='article' FOR UPDATE")
                if not cursor.fetchone():
                    raise ArticleError('ARTICLE_WRITER_SCHEMA_UNAVAILABLE')
                if operation['action'] == 'article.create':
                    cursor.execute('SELECT MAX(Articulo) AS maximum FROM artsservs')
                    article = next_article(cursor.fetchone()['maximum'])
                    # Refuse to reuse an identity previously exported by this
                    # adapter after somebody physically deleted its native row.
                    cursor.execute('SELECT article_id FROM ' + self.schema + '.article_binding WHERE article_id=%s', (article,))
                    if cursor.fetchone():
                        raise ArticleError('SGA_ARTICLE_NUMBER_REUSE_DETECTED')
                    self._classification(cursor, operation['values']['classification'])
                    self._reference(cursor, article, operation['values']['reference'])
                    try:
                        cursor.execute('INSERT INTO artsservs '
                                       '(Articulo,ArticuloNomb,NombreCorto,Servicio,Usuario,Estacion,FechaActualizacion) '
                                       'VALUES (%s,%s,%s,%s,%s,%s,NOW())',
                                       (article, operation['values']['name'], operation['values']['reference'],
                                        int(operation['values']['service']), self.config['audit_user'], self.config['audit_station']))
                    except pymysql.err.IntegrityError as exc:
                        if exc.args[0] == 1062:
                            raise ArticleRetry from None
                        raise
                    self._save_classification(cursor, article, operation['values']['classification'])
                    before, _created = self._current(cursor, article, lock=True)
                    self._save_card(cursor, article, before, operation['values'])
                else:
                    article = operation['article_id']
                    before, created = self._current(cursor, article, lock=True)
                    if before is None:
                        raise ArticleError('SGA_ARTICLE_NOT_FOUND')
                    if not self._references_present(cursor,article):
                        raise ArticleError('SGA_ARTICLE_REFERENCES_REQUIRED')
                    if binding and binding['created_at_sga'] != created:
                        raise ArticleError('SGA_ARTICLE_CREATION_IDENTITY_CHANGED')
                    after, conflicts = merge(operation['expected'], operation['values'], before)
                    if operation['action']=='article.initialize':after,conflicts=before,[]
                    if before['service'] != operation['expected']['service']:
                        conflicts = sorted(set(conflicts + ['service']))
                    if conflicts:
                        result = {'status': 'conflict', 'code': 'SGA_ARTICLE_EDIT_CONFLICT',
                                  'operation_id': operation['operation_id'], 'article_id': article,
                                  'product_uuid': operation['product_uuid'], 'fields': conflicts, 'values': before,
                                  'article_code': article_code(article)}
                        return self._finish(connection, cursor, operation, result)
                    if operation['action']!='article.initialize':
                        self._classification(cursor, after['classification'])
                        if before['reference'] != after['reference']:
                            self._reference(cursor, article, after['reference'])
                        self._save_card(cursor, article, before, after)
                initialized={}
                if operation['contract']==3:
                    initialized=initialize(cursor,article,operation['initialization'],self.initialization_profile,
                        self.config['audit_user'],self.config['audit_station'])
                actual, created = self._current(cursor, article, lock=True)
                if not self._references_present(cursor,article):
                    raise ArticleError('SGA_ARTICLE_REFERENCES_REQUIRED')
                if not binding:
                    cursor.execute('INSERT INTO ' + self.schema + '.article_binding '
                                   '(application_uuid,product_uuid,article_id,created_at_sga,origin_operation_uuid) VALUES (%s,%s,%s,%s,%s)',
                                   (self.application_id, operation['product_uuid'], article, created,
                                    operation['operation_id'] if operation['action']=='article.create' else None))
                result = {'status': 'synchronized', 'operation_id': operation['operation_id'],
                          'product_uuid': operation['product_uuid'], 'article_id': article,
                          'article_code': article_code(article), 'references_confirmed':True, 'values': actual, 'checksum': digest(actual),**initialized}
                committing = True
                return self._finish(connection, cursor, operation, result)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            if committing:
                raise ArticleCommitUnknown('Reconcile the same operation UUID') from None
            if exc.args and exc.args[0] in (1205, 1213):
                raise ArticleRetry from None
            raise
        except pymysql.err.IntegrityError as exc:
            if exc.args[0] == 1062:
                raise ArticleError('SGA_ARTICLE_CODE_OR_BINDING_ALREADY_USED') from None
            if exc.args[0] in (1451, 1452):
                raise ArticleError('SGA_ARTICLE_REFERENCE_CONFLICT') from None
            raise
        finally:
            try:
                if not committing:
                    try:
                        connection.rollback()
                    except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
                        pass
            finally:
                connection.close()

    def _finish(self, connection, cursor, operation, result):
        cursor.execute('UPDATE ' + self.schema + '.article_receipt SET result=%s '
                       'WHERE application_uuid=%s AND operation_uuid=%s',
                       (json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False),
                        self.application_id, operation['operation_id']))
        try:
            connection.commit()
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            raise ArticleCommitUnknown('Reconcile the same operation UUID') from None
        return result
