"""One-time initialization of integration-owned articles; no inventory moves."""
import re
from decimal import Decimal, ROUND_HALF_UP

from .article_contract import ArticleError, digest
from .catalog_source import bit


INITIAL_COLUMNS={
 'artsservs':{'CodHacienda','UltProveedor','CostoUnit','CostoUnitDet','TipoIVA','IVA','Exento'},
 'artsservsrefer':{'CodActividad','BodegaDevolucion'},
 'articulosproveedor':{'Articulo','Proveedor','Prioridad','CodigoArticulo','PrecioCotiz','PrecioCotizDet','Usuario','Estacion','FechaActualizacion'},
 'articulosbodegas':{'Articulo','Bodega','Usuario','Estacion','FechaActualizacion'},
 'artsservsprecios':{'Articulo','Nivel','PorcGanancia','PorcDescto','PorcDesctoDet','PrecioSugerido','PrecioSugeridoDet','ConImpto','ConImptoDet','CambioPrecio','Usuario','Estacion','FechaActualizacion'},
}


def profile(value):
    keys={'reference_article','activity','warehouse','discount','price_level','currency'}
    if (not isinstance(value,dict) or set(value)!=keys or type(value['reference_article']) is not int
            or value['reference_article']<=0 or not re.fullmatch(r'[0-9]{6}',str(value['activity']))
            or any(type(value[k]) is not int or not 1<=value[k]<=32767 for k in ('warehouse','price_level'))
            or type(value['discount']) not in (int,float) or not 0<=value['discount']<100
            or not re.fullmatch(r'[A-Z]{3}',str(value['currency']))):
        raise ArticleError('ARTICLE_INITIALIZATION_PROFILE_INVALID')
    return dict(value)


def rounded(value):
    return float(Decimal(str(value)).quantize(Decimal('.0001'),rounding=ROUND_HALF_UP))


def initialize(cursor, article, values, settings, actor, station):
    p=profile(settings)
    if values['currency']!=p['currency']:
        raise ArticleError('ARTICLE_INITIALIZATION_CURRENCY_MISMATCH')
    cursor.execute('SELECT r.CodActividad,r.BodegaDevolucion,p.PorcDescto,p.PorcDesctoDet,n.EnDolares '
        'FROM artsservsrefer r JOIN artsservsprecios p ON p.Articulo=r.Articulo AND p.Nivel=%s '
        'JOIN preciosniveles n ON n.Nivel=p.Nivel WHERE r.Articulo=%s',
        (p['price_level'],p['reference_article']))
    reference=cursor.fetchone()
    discount=p['discount']/100
    if (not reference or reference['CodActividad']!=p['activity'] or reference['BodegaDevolucion']!=p['warehouse']
            or any(abs(float(reference[k])-discount)>.000001 for k in ('PorcDescto','PorcDesctoDet'))
            or bit(reference['EnDolares'])!=(p['currency']=='USD')):
        raise ArticleError('ARTICLE_INITIALIZATION_PROFILE_CHANGED')
    cursor.execute('SELECT Bodega FROM bodegas WHERE Bodega=%s',(p['warehouse'],))
    if not cursor.fetchone():raise ArticleError('ARTICLE_INITIALIZATION_WAREHOUSE_MISSING')
    cursor.execute('SELECT Proveedor,Activo FROM proveedores WHERE Proveedor=%s',(values['vendor_code'],))
    vendor=cursor.fetchone()
    if not vendor or not bit(vendor['Activo']):raise ArticleError('ARTICLE_INITIALIZATION_VENDOR_MISSING')
    cursor.execute('SELECT CodHacienda,UltProveedor,CostoUnit,CostoUnitDet,NumeroUnidades,TipoIVA,IVA,Exento,'
        'CantDisponible,CantDisponibleDet,CantPedida,CantPedidaDet,CantFacturada,CantFacturadaDet,FechaUltCompra,FechaUltVenta '
        'FROM artsservs WHERE Articulo=%s FOR UPDATE',(article,))
    old=cursor.fetchone()
    if (not old or old['NumeroUnidades']!=1 or old['FechaUltCompra'] or old['FechaUltVenta']
            or any(old[k] for k in ('CantDisponible','CantDisponibleDet','CantPedida','CantPedidaDet','CantFacturada','CantFacturadaDet'))):
        raise ArticleError('ARTICLE_INITIALIZATION_REQUIRES_UNUSED_ARTICLE')
    for column,wanted in [('CodHacienda',values['cabys']),('UltProveedor',values['vendor_code'])]:
        if old[column] not in (None,'',wanted):raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
    for column in ('CostoUnit','CostoUnitDet'):
        if abs(float(old[column]))>.00001 and abs(float(old[column])-values['unit_cost'])>.0001:
            raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
    if old['IVA'] is not None and float(old['IVA'])!=-1 and abs(float(old['IVA'])-values['vat_rate']/100)>.00001:
        raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
    if bit(old['Exento']) and values['vat_code']!='10':
        raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
    cursor.execute('SELECT Cant,CantDet,CantFacturada,CantFacturadaDet,FechaUltIngreso,FechaUltVenta FROM articulosbodegas WHERE Articulo=%s FOR UPDATE',(article,))
    if any(any(row[k] for k in ('Cant','CantDet','CantFacturada','CantFacturadaDet','FechaUltIngreso','FechaUltVenta')) for row in cursor.fetchall()):
        raise ArticleError('ARTICLE_INITIALIZATION_REQUIRES_UNUSED_ARTICLE')
    cursor.execute('SELECT CodActividad,BodegaDevolucion FROM artsservsrefer WHERE Articulo=%s FOR UPDATE',(article,))
    refs=cursor.fetchone()
    if not refs or refs['CodActividad'] not in (None,'',p['activity']) or refs['BodegaDevolucion'] not in (None,p['warehouse']):
        raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
    cursor.execute('UPDATE artsservs SET CodHacienda=%s,UltProveedor=%s,CostoUnit=%s,CostoUnitDet=%s,TipoIVA=%s,IVA=%s,Exento=%s,'
        'Usuario=%s,Estacion=%s,FechaActualizacion=NOW() WHERE Articulo=%s',
        (values['cabys'],values['vendor_code'],values['unit_cost'],values['unit_cost'],values['vat_code'],values['vat_rate']/100,
         int(values['vat_code']=='10'),actor,station,article))
    cursor.execute('UPDATE artsservsrefer SET CodActividad=%s,BodegaDevolucion=%s,Usuario=%s,Estacion=%s,FechaActualizacion=NOW() WHERE Articulo=%s',
        (p['activity'],p['warehouse'],actor,station,article))
    cursor.execute('SELECT CodigoArticulo,PrecioCotiz,PrecioCotizDet,FechaUltCompra,Deshabilitado,Prioridad FROM articulosproveedor WHERE Articulo=%s AND Proveedor=%s FOR UPDATE',
        (article,values['vendor_code']))
    relation=cursor.fetchone()
    if relation:
        if relation['CodigoArticulo'] not in (None,'',values['vendor_product_code']) or relation['FechaUltCompra'] or bit(relation['Deshabilitado']) or relation['Prioridad']!=1:
            raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
        if any(float(relation[k] or 0) not in (0,values['unit_cost']) for k in ('PrecioCotiz','PrecioCotizDet')):
            raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
        cursor.execute('UPDATE articulosproveedor SET CodigoArticulo=%s,PrecioCotiz=%s,PrecioCotizDet=%s,Usuario=%s,Estacion=%s,FechaActualizacion=NOW() WHERE Articulo=%s AND Proveedor=%s',
            (values['vendor_product_code'],values['unit_cost'],values['unit_cost'],actor,station,article,values['vendor_code']))
    else:
        cursor.execute('INSERT INTO articulosproveedor (Articulo,Proveedor,Prioridad,CodigoArticulo,PrecioCotiz,PrecioCotizDet,Usuario,Estacion,FechaActualizacion) '
            'VALUES (%s,%s,1,%s,%s,%s,%s,%s,NOW())',
            (article,values['vendor_code'],values['vendor_product_code'],values['unit_cost'],values['unit_cost'],actor,station))
    cursor.execute('SELECT Articulo FROM articulosbodegas WHERE Articulo=%s AND Bodega=%s FOR UPDATE',(article,p['warehouse']))
    if not cursor.fetchone():
        cursor.execute('INSERT INTO articulosbodegas (Articulo,Bodega,Usuario,Estacion,FechaActualizacion) VALUES (%s,%s,%s,%s,NOW())',
            (article,p['warehouse'],actor,station))
    cursor.execute('SELECT PorcDescto,PorcDesctoDet FROM artsservsprecios WHERE Articulo=%s AND Nivel=%s FOR UPDATE',(article,p['price_level']))
    price=cursor.fetchone()
    if price:
        if any(abs(float(price[k]))>.000001 and abs(float(price[k])-discount)>.000001 for k in ('PorcDescto','PorcDesctoDet')):
            raise ArticleError('ARTICLE_INITIALIZATION_LOCAL_CONFLICT')
        # A manually entered markup or price remains authoritative in SGA.
        cursor.execute('UPDATE artsservsprecios SET PorcDescto=%s,PorcDesctoDet=%s,Usuario=%s,Estacion=%s,FechaActualizacion=NOW() WHERE Articulo=%s AND Nivel=%s',
            (discount,discount,actor,station,article,p['price_level']))
    else:
        gross=rounded(values['unit_cost']*(1+values['markup'])*(1+values['vat_rate']/100))
        cursor.execute('INSERT INTO artsservsprecios (Articulo,Nivel,PorcGanancia,PorcDescto,PorcDesctoDet,PrecioSugerido,PrecioSugeridoDet,ConImpto,ConImptoDet,Usuario,Estacion,FechaActualizacion) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,1,1,%s,%s,NOW())',
            (article,p['price_level'],values['markup'],discount,discount,gross,gross,actor,station))
    cursor.execute('SELECT a.CodHacienda,a.UltProveedor,a.CostoUnit,a.CostoUnitDet,r.CodActividad,r.BodegaDevolucion,'
        'v.CodigoArticulo,w.Bodega,p.PorcDescto,p.PorcDesctoDet FROM artsservs a JOIN artsservsrefer r ON r.Articulo=a.Articulo '
        'JOIN articulosproveedor v ON v.Articulo=a.Articulo AND v.Proveedor=%s '
        'JOIN articulosbodegas w ON w.Articulo=a.Articulo AND w.Bodega=%s '
        'JOIN artsservsprecios p ON p.Articulo=a.Articulo AND p.Nivel=%s WHERE a.Articulo=%s',
        (values['vendor_code'],p['warehouse'],p['price_level'],article))
    actual=cursor.fetchone()
    if (not actual or actual['CodHacienda']!=values['cabys'] or actual['UltProveedor']!=values['vendor_code']
            or actual['CodigoArticulo']!=values['vendor_product_code'] or actual['CodActividad']!=p['activity']
            or actual['BodegaDevolucion']!=p['warehouse'] or actual['Bodega']!=p['warehouse']
            or any(abs(float(actual[k])-values['unit_cost'])>.0001 for k in ('CostoUnit','CostoUnitDet'))
            or any(abs(float(actual[k])-discount)>.000001 for k in ('PorcDescto','PorcDesctoDet'))):
        raise ArticleError('ARTICLE_INITIALIZATION_UNCONFIRMED')
    return {'initialization_confirmed':True,'initialization_checksum':digest(values),'initialization_profile':p,
            'existing_price_preserved':bool(price)}
