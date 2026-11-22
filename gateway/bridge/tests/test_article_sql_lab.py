"""Opt-in tests on an isolated native MariaDB. Never use a production socket.

SGA_ARTICLE_LAB_SOCKET and SGA_ARTICLE_LAB_SCHEMA point to a disposable lab and a
private schema-only JSON fixture. Guards run BEFORE creating synthetic data.
"""
import copy
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
import uuid

import pymysql
from bridge.article_contract import ArticleError, next_article
from bridge.article_sql import ArticleSql, ArticleCommitUnknown, WRITE_COLUMNS, INTEGRATION_PRIVILEGES
from bridge.article_initialization import INITIAL_COLUMNS

SOCKET = os.environ.get('SGA_ARTICLE_LAB_SOCKET')
APP = '212621ca-e0e0-41c9-b5f0-d6da7b496226'


@unittest.skipUnless(SOCKET, 'Isolated MariaDB laboratory not selected')
class ArticleSqlLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(SOCKET)
        marker = json.loads((path.parent / 'marker.json').read_text())
        if marker.get('marker') != 'sga-product-sql-lab-v1' or marker.get('socket') != SOCKET:
            raise RuntimeError('Not a marked disposable laboratory')
        cls.db = pymysql.connect(unix_socket=SOCKET, user='root', password='', autocommit=True,
                                 charset='utf8', cursorclass=pymysql.cursors.DictCursor)
        with cls.db.cursor() as c:
            c.execute('SELECT @@datadir AS d,@@skip_networking AS n')
            guard=c.fetchone()
            if Path(guard['d']).resolve()!=Path(marker['data']).resolve() or not guard['n']:
                raise RuntimeError('Private laboratory isolation guard failed')
            c.execute('CREATE DATABASE article_fixture CHARACTER SET utf8')
            c.execute('USE article_fixture')
            c.execute('SET FOREIGN_KEY_CHECKS=0')
            schema=json.loads(Path(os.environ['SGA_ARTICLE_LAB_SCHEMA']).read_text())['tables']
            for ddl in schema.values():
                c.execute(ddl)
            # Minimal synthetic parent tables absent from the metadata export.
            c.execute('CREATE TABLE comisiones(Comision VARCHAR(3) PRIMARY KEY) ENGINE=InnoDB CHARSET=utf8')
            c.execute('CREATE TABLE componentes(Componente SMALLINT PRIMARY KEY) ENGINE=InnoDB CHARSET=utf8')
            c.execute('CREATE TABLE proveedores(Proveedor VARCHAR(3) PRIMARY KEY,Activo BIT NOT NULL DEFAULT 1) ENGINE=InnoDB CHARSET=utf8')
            c.execute('CREATE TABLE preciosniveles(Nivel SMALLINT PRIMARY KEY,EnDolares BIT NOT NULL DEFAULT 0) ENGINE=InnoDB CHARSET=utf8')
            c.execute('SET FOREIGN_KEY_CHECKS=1')
            c.execute("INSERT INTO divisiones(Division,DivisionNomb) VALUES('LAB','Laboratorio')")
            c.execute("INSERT INTO categorias(Categoria,CategoriaNomb) VALUES(1,'Categoria'),(2,'Otra')")
            c.execute("INSERT INTO categoriassub(Categoria,SubCategoria,SubCategoriaNomb) VALUES(1,1,'Sub')")
            c.execute("INSERT INTO marcas(Marca,MarcaNomb) VALUES('LAB','Marca')")
            c.execute("INSERT INTO bodegas(Bodega,BodegaNomb) VALUES(1,'Laboratorio')")
            c.execute("INSERT INTO proveedores(Proveedor) VALUES('LAB')")
            c.execute('INSERT INTO preciosniveles(Nivel) VALUES(1)')
            c.execute("INSERT INTO artsservs(Articulo,ArticuloNomb) VALUES(900000,'SYNTHETIC PROFILE')")
            c.execute("INSERT INTO artsservsrefer(Articulo,CodActividad,BodegaDevolucion) VALUES(900000,'123456',1)")
            c.execute('INSERT INTO artsservsprecios(Articulo,Nivel,PorcDescto,PorcDesctoDet) VALUES(900000,1,.1,.1)')
            c.execute('CREATE DATABASE article_integration CHARACTER SET utf8')
            c.execute('USE article_integration')
            ddl=Path(__file__).parents[1].joinpath('article_schema.sql').read_text()
            ddl=ddl.replace('CONFIGURE_SOURCE_INSTANCE','laboratory').replace('CONFIGURE_SOURCE_DATABASE','article_fixture')
            ddl='\n'.join(line for line in ddl.splitlines() if not line.lstrip().startswith('--'))
            for statement in ddl.split(';'):
                if statement.strip(): c.execute(statement)
            cls.writer_user='a_'+uuid.uuid4().hex[:8]
            c.execute('CREATE USER `%s`@`localhost`' % cls.writer_user)
            c.execute('GRANT SELECT ON article_fixture.* TO `%s`@`localhost`' % cls.writer_user)
            for table,permissions in WRITE_COLUMNS.items():
                for privilege,columns in permissions.items():
                    expression=privilege+' ('+','.join('`'+n+'`' for n in sorted(columns))+')' if columns else privilege
                    c.execute('GRANT '+expression+' ON article_fixture.'+table+' TO `'+cls.writer_user+'`@`localhost`')
            for table,permissions in INTEGRATION_PRIVILEGES.items():
                c.execute('GRANT '+','.join(sorted(permissions))+' ON article_integration.'+table+' TO `'+cls.writer_user+'`@`localhost`')
            cls.complete_user='i_'+uuid.uuid4().hex[:8]
            c.execute('CREATE USER `%s`@`localhost`' % cls.complete_user)
            c.execute('GRANT SELECT ON article_fixture.* TO `%s`@`localhost`' % cls.complete_user)
            for table,permissions in WRITE_COLUMNS.items():
                for privilege,columns in permissions.items():
                    expression=privilege+' ('+','.join('`'+n+'`' for n in sorted(columns))+')' if columns else privilege
                    c.execute('GRANT '+expression+' ON article_fixture.'+table+' TO `'+cls.complete_user+'`@`localhost`')
            for table,columns in INITIAL_COLUMNS.items():
                c.execute('GRANT INSERT ('+','.join('`'+n+'`' for n in sorted(columns))+') ON article_fixture.'+table+' TO `'+cls.complete_user+'`@`localhost`')
                update=columns-{'Articulo','Proveedor','Bodega','Nivel','Prioridad'}
                if table=='articulosbodegas':continue
                if table=='artsservsprecios':update={'PorcDescto','PorcDesctoDet','Usuario','Estacion','FechaActualizacion'}
                c.execute('GRANT UPDATE ('+','.join('`'+n+'`' for n in sorted(update))+') ON article_fixture.'+table+' TO `'+cls.complete_user+'`@`localhost`')
            for table,permissions in INTEGRATION_PRIVILEGES.items():
                c.execute('GRANT '+','.join(sorted(permissions))+' ON article_integration.'+table+' TO `'+cls.complete_user+'`@`localhost`')
        cls.config={'enabled':True,'application_id':APP,'source_instance':'laboratory',
                    'database':'article_fixture','integration_schema':'article_integration',
                    'audit_mode':'transaction_receipt','audit_user':'LAB','audit_station':1,
                    'unix_socket':SOCKET,'host':'localhost','user':cls.writer_user,'password':''}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()  # Preserve all synthetic rows for inspection; no DROP.

    def setUp(self):
        self.writer=ArticleSql(self.config,APP)

    def request(self, **changes):
        op={'contract':1,'action':'article.create','operation_id':str(uuid.uuid4()),
            'product_uuid':str(uuid.uuid4()),'source_instance':'laboratory','article_id':None,'expected':None,
            'values':{'name':'ARTICULO LABORATORIO','reference':uuid.uuid4().hex[:16], 'service':False,
                      'classification':{'division':'LAB','category':1,'subcategory':1,'brand':'LAB'},
                      'barcodes':{'0':None,'1':None}}}
        op.update(changes)
        return op

    def update(self, created):
        return self.request(action='article.update',article_id=created['article_id'],
                            product_uuid=created['product_uuid'],expected=copy.deepcopy(created['values']),
                            values=copy.deepcopy(created['values']))

    def scalar(self,sql,params=()):
        with self.db.cursor() as c:
            c.execute(sql,params);return next(iter(c.fetchone().values()))

    def test_create_and_uuid_redelivery(self):
        op=self.request(); first=self.writer.apply(op)
        self.assertEqual(first,self.writer.apply(op))
        self.assertEqual(1,self.scalar('SELECT COUNT(*) FROM article_fixture.artsservs WHERE Articulo=%s',(first['article_id'],)))
        altered=copy.deepcopy(op);altered['values']['name']='OTHER'
        with self.assertRaisesRegex(ArticleError,'ARTICLE_OPERATION_IDENTITY_CONFLICT'):self.writer.apply(altered)

    def test_new_uuid_cannot_create_another_article_for_product(self):
        op=self.request();self.writer.apply(op);op['operation_id']=str(uuid.uuid4())
        with self.assertRaisesRegex(ArticleError,'ARTICLE_PRODUCT_ALREADY_BOUND'):self.writer.apply(op)

    def test_concurrent_allocations(self):
        ops=[self.request() for _ in range(5)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(self.writer.apply,ops))
        self.assertEqual(5,len({r['article_id'] for r in results}))

    def test_concurrent_same_operation(self):
        op=self.request()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(self.writer.apply,[op,op]))
        self.assertEqual(results[0],results[1])

    def test_native_allocator_collision_retries(self):
        native=self.db
        triggered=[]
        def connect(**kwargs):
            conn=pymysql.connect(**kwargs);original=conn.cursor
            def cursor():
                cur=original(); execute=cur.execute
                def run(sql,args=None):
                    if sql.startswith('INSERT INTO artsservs ') and not triggered:
                        triggered.append(True)
                        with native.cursor() as n:
                            n.execute("INSERT INTO article_fixture.artsservs(Articulo,ArticuloNomb) VALUES(%s,'NATIVE LAB')",(args[0],))
                    return execute(sql,args)
                cur.execute=run;return cur
            conn.cursor=cursor;return conn
        result=ArticleSql(self.config,APP,connect=connect).apply(self.request())
        self.assertTrue(triggered)
        self.assertEqual('NATIVE LAB',self.scalar('SELECT ArticuloNomb FROM article_fixture.artsservs WHERE Articulo=%s',(result['article_id']-1,)))

    def test_lost_commit_response_reconciles_same_uuid(self):
        def connect(**kwargs):
            conn=pymysql.connect(**kwargs);commit=conn.commit
            def lose():
                commit();raise pymysql.err.OperationalError(2013,'synthetic lost response')
            conn.commit=lose;return conn
        op=self.request()
        with self.assertRaises(ArticleCommitUnknown):ArticleSql(self.config,APP,connect=connect).apply(op)
        self.assertEqual('synchronized',self.writer.apply(op)['status'])

    def test_barcode_collision_rolls_back_entire_create(self):
        op=self.request();op['values']['barcodes']['1']='LAB-UNIQUE';self.writer.apply(op)
        other=self.request();other['values']['barcodes']['1']='LAB-UNIQUE'
        with self.assertRaisesRegex(ArticleError,'SGA_ARTICLE_CODE_OR_BINDING_ALREADY_USED'):self.writer.apply(other)
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_fixture.artsservs WHERE NombreCorto=%s',(other['values']['reference'],)))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_integration.article_receipt WHERE operation_uuid=%s',(other['operation_id'],)))

    def test_null_barcode_row_is_updated(self):
        result=self.writer.apply(self.request());article=result['article_id']
        with self.db.cursor() as c:c.execute('INSERT INTO article_fixture.articuloscodbarras(Articulo,Tipo,CodBarra) VALUES(%s,1,NULL)',(article,))
        op=self.update(result);op['values']['barcodes']['1']='LAB-NULL'
        self.assertEqual('LAB-NULL',self.writer.apply(op)['values']['barcodes']['1'])

    def test_same_field_conflict_preserves_native_edit(self):
        result=self.writer.apply(self.request());op=self.update(result)
        with self.db.cursor() as c:c.execute("UPDATE article_fixture.artsservs SET ArticuloNomb='NATIVE EDIT' WHERE Articulo=%s",(result['article_id'],))
        op['values']['name']='ODOO EDIT';actual=self.writer.apply(op)
        self.assertEqual(['name'],actual['fields']);self.assertEqual('NATIVE EDIT',actual['values']['name'])
        self.assertEqual(actual,self.writer.apply(op))

    def test_independent_edits_merge(self):
        result=self.writer.apply(self.request());op=self.update(result)
        with self.db.cursor() as c:c.execute("UPDATE article_fixture.artsservs SET ArticuloNomb='NATIVE MERGED' WHERE Articulo=%s",(result['article_id'],))
        op['values']['barcodes']['0']='LAB-MERGE'
        actual=self.writer.apply(op)['values'];self.assertEqual('NATIVE MERGED',actual['name']);self.assertEqual('LAB-MERGE',actual['barcodes']['0'])

    def test_cost_stock_and_auxiliary_fields_are_preserved(self):
        result=self.writer.apply(self.request());article=result['article_id']
        with self.db.cursor() as c:
            c.execute('UPDATE article_fixture.artsservs SET CostoUnit=123.4567,PrecioCotiz=567,CantDisponible=77 WHERE Articulo=%s',(article,))
            c.execute("UPDATE article_fixture.artsservsrefer SET CodActividad='123456',BodegaDevolucion=1 WHERE Articulo=%s",(article,))
            c.execute('INSERT INTO article_fixture.articulosbodegas(Articulo,Bodega,Cant) VALUES(%s,1,44)',(article,))
        op=self.update(result);op['values']['name']='NEW NAME';op['values']['classification']['brand']=None
        self.writer.apply(op)
        self.assertEqual(123.4567,self.scalar('SELECT CostoUnit FROM article_fixture.artsservs WHERE Articulo=%s',(article,)))
        self.assertEqual(567,self.scalar('SELECT PrecioCotiz FROM article_fixture.artsservs WHERE Articulo=%s',(article,)))
        self.assertEqual(77,self.scalar('SELECT CantDisponible FROM article_fixture.artsservs WHERE Articulo=%s',(article,)))
        self.assertEqual(44,self.scalar('SELECT Cant FROM article_fixture.articulosbodegas WHERE Articulo=%s',(article,)))
        self.assertEqual('123456',self.scalar('SELECT CodActividad FROM article_fixture.artsservsrefer WHERE Articulo=%s',(article,)))

    def test_invalid_classification_rolls_back(self):
        op=self.request();op['values']['classification']['category']=2
        with self.assertRaisesRegex(ArticleError,'SGA_SUBCATEGORY_PARENT_MISMATCH'):self.writer.apply(op)

    def test_forbidden_pricing_fields_rejected(self):
        op=self.request();op['values']['cost']=10
        with self.assertRaisesRegex(ArticleError,'INVALID_ARTICLE_FIELDS'):self.writer.apply(op)

    def test_scope_cannot_change(self):
        op=self.request(source_instance='other')
        with self.assertRaisesRegex(ArticleError,'ARTICLE_SOURCE_SCOPE_MISMATCH'):self.writer.apply(op)

    def test_format_overflow_rejected(self):
        op=self.request();op['values']['name']='a'*71
        with self.assertRaises(ArticleError):self.writer.apply(op)
        with self.assertRaises(ArticleError):next_article(2147483647)

    def test_reference_collision_rejected(self):
        op=self.request();self.writer.apply(op);other=self.request();other['values']['reference']=op['values']['reference']
        with self.assertRaisesRegex(ArticleError,'SGA_ARTICLE_REFERENCE_ALREADY_USED'):self.writer.apply(other)

    def test_reader_sweep_reports_uuid_and_native_inactive(self):
        result=self.writer.apply(self.request())
        with self.db.cursor() as c:c.execute('UPDATE article_fixture.artsservs SET Trabajado=1 WHERE Articulo=%s',(result['article_id'],))
        cards=self.writer.cards(result['article_id']-1,1)
        self.assertEqual(result['product_uuid'],cards[0]['product_uuid'])
        self.assertTrue(cards[0]['source_inactive'])
        self.assertEqual(result['values'],cards[0]['values'])

    def test_writer_account_cannot_update_cost_or_stock(self):
        connection=pymysql.connect(unix_socket=SOCKET,user=self.writer_user,password='',database='article_fixture')
        try:
            with connection.cursor() as c:
                for column in ('CostoUnit','CantDisponible','IVA'):
                    with self.assertRaises(pymysql.err.OperationalError):c.execute('UPDATE artsservs SET '+column+'=0')
        finally:connection.close()

    def test_v2_allocates_native_code_without_local_reference(self):
        from bridge.article_contract import article_code
        op=self.request(contract=2);op['values']['reference']=None
        result=self.writer.apply(op)
        self.assertEqual(article_code(result['article_id']),result['article_code'])
        self.assertTrue(result['references_confirmed'])
        self.assertIsNone(result['values']['reference'])
        self.assertEqual(result,self.writer.apply(op))
        self.assertEqual(1,self.scalar('SELECT COUNT(*) FROM article_fixture.artsservsrefer WHERE Articulo=%s',(result['article_id'],)))

    def test_missing_auxiliary_reference_rolls_back_master_and_receipt(self):
        op=self.request();self.writer._save_classification=lambda *args:None
        with self.assertRaisesRegex(ArticleError,'SGA_ARTICLE_REFERENCES_REQUIRED'):self.writer.apply(op)
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_fixture.artsservs WHERE NombreCorto=%s',(op['values']['reference'],)))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_integration.article_receipt WHERE operation_uuid=%s',(op['operation_id'],)))

    def test_initial_activation_can_be_limited_to_one_product_uuid(self):
        op=self.request();restricted=ArticleSql({**self.config,'allowed_product_uuids':[str(uuid.uuid4())]},APP)
        with self.assertRaisesRegex(ArticleError,'ARTICLE_PRODUCT_NOT_AUTHORIZED'):restricted.apply(op)

    def full_writer(self):
        return ArticleSql({**self.config,'user':self.complete_user,'initialization':{
            'reference_article':900000,'activity':'123456','warehouse':1,'discount':10,'price_level':1,'currency':'CRC'}},APP)

    def full_request(self, **changes):
        op=self.request(contract=3,**changes)
        op['initialization']={'cabys':'1234567890123','unit_cost':100.1234,'currency':'CRC','vat_code':'08',
            'vat_rate':13,'vendor_code':'LAB','vendor_product_code':'LAB-CODE','markup':.3}
        return op

    def test_full_create_advances_past_historical_consecutives(self):
        native_max = self.scalar('SELECT MAX(Articulo) FROM article_fixture.artsservs')
        historical = (native_max + 1, native_max + 3)
        other_app = str(uuid.uuid4())
        old_product = str(uuid.uuid4())
        with self.db.cursor() as cursor:
            cursor.execute(
                'INSERT INTO article_integration.article_binding (application_uuid,product_uuid,article_id) '
                'VALUES (%s,%s,%s),(%s,%s,%s)',
                (APP, str(uuid.uuid4()), historical[0], other_app, old_product, historical[1]),
            )

        writer = self.full_writer()
        operation = self.full_request()
        operation['initialization']['vendor_product_code'] = 'LAB-HISTORY'
        confirmed = writer.apply(operation)

        self.assertEqual(historical[1] + 1, confirmed['article_id'])
        self.assertTrue(confirmed['initialization_confirmed'])
        self.assertEqual(confirmed, writer.apply(operation))
        self.assertEqual(0, self.scalar(
            'SELECT COUNT(*) FROM article_fixture.artsservs WHERE Articulo IN (%s,%s)', historical,
        ))
        self.assertEqual(old_product, self.scalar(
            'SELECT product_uuid FROM article_integration.article_binding WHERE application_uuid=%s AND article_id=%s',
            (other_app, historical[1]),
        ))
        self.assertEqual(1, self.scalar(
            'SELECT COUNT(*) FROM article_integration.article_binding WHERE application_uuid=%s AND product_uuid=%s',
            (APP, operation['product_uuid']),
        ))

    def test_full_create_commits_all_required_rows_without_inventory(self):
        writer=self.full_writer();op=self.full_request();result=writer.apply(op);article=result['article_id']
        self.assertTrue(result['initialization_confirmed']);self.assertEqual(result,writer.apply(op))
        for column,value in [('CodHacienda','1234567890123'),('UltProveedor','LAB'),('CostoUnit',100.1234),('TipoIVA','08'),('CantDisponible',0)]:
            self.assertEqual(value,self.scalar('SELECT '+column+' FROM article_fixture.artsservs WHERE Articulo=%s',(article,)))
        self.assertEqual('123456',self.scalar('SELECT CodActividad FROM article_fixture.artsservsrefer WHERE Articulo=%s',(article,)))
        self.assertEqual(1,self.scalar('SELECT BodegaDevolucion FROM article_fixture.artsservsrefer WHERE Articulo=%s',(article,)))
        self.assertEqual('LAB-CODE',self.scalar('SELECT CodigoArticulo FROM article_fixture.articulosproveedor WHERE Articulo=%s',(article,)))
        self.assertEqual(0,self.scalar('SELECT Cant FROM article_fixture.articulosbodegas WHERE Articulo=%s',(article,)))
        self.assertEqual(.1,self.scalar('SELECT PorcDesctoDet FROM article_fixture.artsservsprecios WHERE Articulo=%s',(article,)))

    def test_full_create_late_failure_rolls_back_master_and_receipt(self):
        writer=self.full_writer();op=self.full_request();op['initialization']['vendor_code']='BAD'
        with self.assertRaisesRegex(ArticleError,'ARTICLE_INITIALIZATION_VENDOR_MISSING'):writer.apply(op)
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_fixture.artsservs WHERE NombreCorto=%s',(op['values']['reference'],)))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_integration.article_receipt WHERE operation_uuid=%s',(op['operation_id'],)))

    def test_full_completion_preserves_native_price_and_identity(self):
        created=self.writer.apply(self.request(contract=2));article=created['article_id']
        with self.db.cursor() as c:
            c.execute('INSERT INTO article_fixture.artsservsprecios(Articulo,Nivel,PorcGanancia,PrecioSugerido) VALUES(%s,1,.47,1234)',(article,))
        op=self.full_request(action='article.initialize',article_id=article,product_uuid=created['product_uuid'],expected=created['values'])
        op['values']=created['values'];result=self.full_writer().apply(op)
        self.assertEqual(article,result['article_id']);self.assertTrue(result['existing_price_preserved'])
        self.assertAlmostEqual(.47,self.scalar('SELECT PorcGanancia FROM article_fixture.artsservsprecios WHERE Articulo=%s',(article,)),places=6)
        self.assertEqual(1234,self.scalar('SELECT PrecioSugerido FROM article_fixture.artsservsprecios WHERE Articulo=%s',(article,)))

    def test_full_completion_refuses_used_article_and_imported_identity(self):
        created=self.writer.apply(self.request());article=created['article_id']
        op=self.full_request(action='article.initialize',article_id=article,product_uuid=created['product_uuid'],expected=created['values'])
        op['values']=created['values']
        with self.db.cursor() as c:c.execute('UPDATE article_fixture.artsservs SET CantDisponible=1 WHERE Articulo=%s',(article,))
        with self.assertRaisesRegex(ArticleError,'ARTICLE_INITIALIZATION_REQUIRES_UNUSED_ARTICLE'):self.full_writer().apply(op)
        with self.db.cursor() as c:
            c.execute('UPDATE article_fixture.artsservs SET CantDisponible=0 WHERE Articulo=%s',(article,))
            c.execute('UPDATE article_integration.article_binding SET origin_operation_uuid=NULL WHERE article_id=%s',(article,))
        with self.assertRaisesRegex(ArticleError,'ARTICLE_INITIALIZATION_REQUIRES_GATEWAY_CREATED_ARTICLE'):self.full_writer().apply(op)

    def test_full_price_conflict_rolls_back_earlier_cost_and_vendor_writes(self):
        created=self.writer.apply(self.request());article=created['article_id']
        with self.db.cursor() as c:
            c.execute('INSERT INTO article_fixture.artsservsprecios(Articulo,Nivel,PorcDescto) VALUES(%s,1,.2)',(article,))
        op=self.full_request(action='article.initialize',article_id=article,product_uuid=created['product_uuid'],expected=created['values'])
        op['values']=created['values']
        with self.assertRaisesRegex(ArticleError,'ARTICLE_INITIALIZATION_LOCAL_CONFLICT'):self.full_writer().apply(op)
        self.assertEqual(0,self.scalar('SELECT CostoUnit FROM article_fixture.artsservs WHERE Articulo=%s',(article,)))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_fixture.articulosproveedor WHERE Articulo=%s',(article,)))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM article_integration.article_receipt WHERE operation_uuid=%s',(op['operation_id'],)))

    def test_full_profile_replays_old_receipt_but_rejects_new_partial_create(self):
        old=self.request(contract=2);receipt=self.writer.apply(old);writer=self.full_writer()
        self.assertEqual(receipt,writer.apply(old))
        with self.assertRaisesRegex(ArticleError,'ARTICLE_COMPLETE_CREATION_REQUIRED'):writer.apply(self.request(contract=2))

    def test_full_writer_cannot_update_stock_or_be_used_without_profile(self):
        connection=pymysql.connect(unix_socket=SOCKET,user=self.complete_user,password='',database='article_fixture')
        try:
            with connection.cursor() as c:
                for table,column in [('artsservs','CantDisponible'),('articulosbodegas','Cant')]:
                    with self.assertRaises(pymysql.err.OperationalError):c.execute('UPDATE '+table+' SET '+column+'=1')
        finally:connection.close()
        with self.assertRaisesRegex(ArticleError,'ARTICLE_WRITER_ACCOUNT_TOO_BROAD'):
            ArticleSql({**self.config,'user':self.complete_user},APP).apply(self.request())
