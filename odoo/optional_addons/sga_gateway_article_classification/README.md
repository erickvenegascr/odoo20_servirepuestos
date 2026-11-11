# SGA en Clasificaciones de artículos

Este módulo conecta el catálogo ya importado por `sga_gateway_catalog_details`
con el módulo independiente `product_classification`. Completa su menú
**Inventario → Clasificaciones de artículos**, y la pestaña **Clasificaciones** de
cada producto y variante. La carga anterior solo completaba Categorización.

Crea cuatro dimensiones: División, Categoría, Subcategoría y Marca. Conserva los
códigos originales, deduplica las categorías reutilizadas entre divisiones y usa
código categoría/subcategoría para no confundir códigos de subcategoría iguales.
No crea una clasificación ficticia para un campo vacío de SGA.

La carga inicial usa las vinculaciones y evidencia SGA existentes en Odoo,
incluidos artículos archivados. No se conecta a SGA ni cambia precios, impuestos,
costos, existencias o documentos. El método privado `_backfill_batch` admite
simulación y lotes de hasta 500 artículos. Cada lote debe ejecutarse en una
transacción; una repetición es idempotente. El receptor existente invoca esta
integración al completar las cargas comerciales posteriores, usando el mismo
token y alcance por compañía/origen.

Las identidades y huellas protegen las ediciones locales de dimensiones, valores
y asignaciones. Si SGA deja de asignar un valor, el vínculo de integración se
archiva; las clasificaciones locales y las demás dimensiones se conservan.
Los permisos y filtros de compañía son los del módulo de clasificaciones.

Carga inicial en el shell nativo de Odoo, con la base y compañía verificadas:

```sh
SGA_CLASSIFICATION_DB=<base> SGA_CLASSIFICATION_COMPANY=<compañía> \
SGA_CLASSIFICATION_SOURCE=<origen> SGA_CLASSIFICATION_MODE=dry \
  odoo-bin shell --no-http < addons/sga_gateway_article_classification/scripts/backfill_odoo.py
```

Tras validar la simulación, cambiar el modo a `apply`. El proceso guarda un
resumen privado por ejecución en el directorio de datos de Odoo. Puede repetirse
para verificar que no crea asignaciones duplicadas.
