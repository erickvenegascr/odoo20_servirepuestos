"""Supplier relationships only. No source quotes or SGA writes."""
from pymysql.cursors import SSDictCursor

from .catalog_source import CatalogSource, bit, text


def supplier_relation(row):
    return {'article_id': int(row['Articulo']), 'vendor_code': str(row['Proveedor']).strip(),
            'product_code': text(row['CodigoArticulo']), 'priority': int(row['Prioridad']),
            'disabled': bit(row['Deshabilitado']), 'vendor_active': bit(row['Activo'])}


class SupplierSource(CatalogSource):
    def vendors(self):
        return [{'code': str(r['Proveedor']).strip(), 'name': text(r['ProveedorNomb']),
                 'tax_id': str(r['Cedula'] or '').strip(), 'active': bit(r['Activo'])}
                for r in self.rows('SELECT Proveedor,ProveedorNomb,Cedula,Activo FROM proveedores '
                    'WHERE Proveedor IN (SELECT DISTINCT Proveedor FROM articulosproveedor) ORDER BY Proveedor')]

    def relations(self, after=(0, ''), limit=1000):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid supplier batch size')
        rows = self.rows('SELECT r.Articulo,r.Proveedor,r.CodigoArticulo,r.Prioridad,r.Deshabilitado,p.Activo '
            'FROM articulosproveedor r JOIN proveedores p ON p.Proveedor=r.Proveedor '
            'WHERE (r.Articulo>%s OR (r.Articulo=%s AND r.Proveedor>%s)) '
            'ORDER BY r.Articulo,r.Proveedor LIMIT %s', (after[0], after[0], after[1], limit))
        return [supplier_relation(row) for row in rows]

    def relation_count(self):
        return self.rows('SELECT COUNT(*) AS relation_count FROM articulosproveedor')[0]['relation_count']

    def iter_snapshot_relations(self):
        """Read every primary key, including an orphan so it cannot look like a deletion."""
        with self.connection.cursor(SSDictCursor) as cursor:
            cursor.execute('SELECT r.Articulo,r.Proveedor,r.CodigoArticulo,r.Prioridad,r.Deshabilitado,p.Activo '
                           'FROM articulosproveedor r LEFT JOIN proveedores p ON p.Proveedor=r.Proveedor '
                           'ORDER BY r.Articulo,r.Proveedor')
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    if row['Activo'] is None:
                        raise ValueError('SGA_SUPPLIER_SNAPSHOT_ORPHAN')
                    yield supplier_relation(row)
