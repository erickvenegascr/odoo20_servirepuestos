# Catálogo comercial y fiscal de SGA

Módulo nuevo para Odoo 20. Requiere los módulos de la compilación Servirepuestos
`product_catalog_classification` y `crfe_connector`, además del receptor existente
`sga_gateway_connector`. Instalar desde `odoo/optional_addons` junto con esas
 dependencias; no forma parte del perfil mínimo independiente del gateway.

Importa los catálogos completos, incluso marcas y categorías sin artículos. Las
categorías de SGA se convierten en líneas dentro de su división; las subcategorías
conservan su línea. Un código reutilizado en varias divisiones mantiene cada
contexto. «Sin división» representa referencias que SGA tiene sin división.
Se conservan las categorías nativas contables importadas en la primera fase.

El contrato fijo `/sga_gateway/api/v1/catalog/details/{info,batch}` usa el mismo
token entre aplicaciones, compañía y origen. No admite modelos arbitrarios ni
contextos de compañía. No crea artículos nuevos: exige la vinculación previa.

Importa CABYS y descripción oficial, código interno, unidad comercial, IVA de
compra/venta con códigos CRFE, costo por unidad de detalle, precio de venta y
reglas de listas. El código CABYS de SGA se conserva si la API oficial no lo
confirma y se marca para revisión. Los datos ausentes no se inventan.

Los márgenes usan `compute_price=markup`, base costo y ocho decimales. Los tres
casos «mantener precio» de SGA usan precio fijo neto. Los descuentos máximos y
promociones sin fechas se conservan como evidencia, sin activarlos como rebajas.
El costo por unidad es `CostoUnit / NumeroUnidades`, según la decisión de
migración previa. Un cambio de costo/unidad con inventario se bloquea; también
se bloquean cambios de unidad si ya hay ventas o facturación.

Cada vinculación y regla guarda huellas del origen y destino; una edición local
posterior detiene la importación hasta conciliarla. Los lotes HTTP son atómicos.
La revisión de catálogo muestra faltantes sin revelar los costos del registro
crudo a usuarios de inventario. No configura certificados, credenciales fiscales,
actividad económica de compañía, plan contable ni documentos históricos.

El lector solo usa SELECT con una cuenta verificada sin permisos de escritura.
No instala disparadores, cambia permisos ni modifica tablas o archivos SGA.
