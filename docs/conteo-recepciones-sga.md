# Alimentación del tablero de conteo desde SGA

`sync_receipt_counts` lee una página de ingresos confirmados y, al aplicar, entrega cada ingreso al método `receipt.count.import_receipt` de Odoo. Usa `recibos` y `recibosart` como evidencia de recepción y `artsservs` para las etiquetas. El lector exige credenciales SELECT y una instantánea de tablas InnoDB. El tablero es el único destino de escritura.

## Configuración privada

Apunte `SGA_RECEIPT_COUNT_SOURCE_FILE` y `ODOO_RECEIPT_COUNT_TARGET_FILE` a archivos JSON propios de este comando. Mantenga las configuraciones, la clave y el cursor en almacenamiento privado del gateway.

- Origen SGA con `host`, `port` opcional, `database` opcional, `user`, `password`, `instance`, `timezone` IANA y lista `warehouses` de IDs nativos. Puede incluir `exclude_order_ids` para excluir pedidos concretos del alcance y `overlap_seconds` para el solape de lecturas. El valor inicial del solape es 120.
- Destino Odoo con `url` HTTPS, `database`, `company_id`, `source_instance` igual a `instance` y `api_key_file`. La clave debe poder invocar el método de importación del tablero en esa compañía.

## Comprobación, vista previa y carga

```sh
python manage.py sync_receipt_counts --check
python manage.py sync_receipt_counts --since 2026-11-21 --page-size 50
python manage.py sync_receipt_counts --since 2026-11-21 --page-size 50 --apply \
  --checkpoint /ruta/privada/receipt-count-checkpoint.json
```

Elija la fecha inicial de recepción aprobada. Mantenga esa fecha, el alcance y el mismo archivo de cursor en las ejecuciones posteriores. El comprobador y la vista previa leen SGA y muestran un resumen agregado. El modo de aplicación guarda el cursor después de verificar todos los resultados de la página. Si la salida indica `more=true`, ejecute el mismo comando para continuar. Cuando el recorrido se completa, la próxima ejecución abre una ventana incremental con solape.

El cursor usa el sello `PGTS` de cabecera y detalle junto al ID del recibo. Cada página se ordena por sello e ID y retiene el límite superior al reanudar. La marca puede compartirse entre recibos. Las eliminaciones físicas sin cambio en una fila relacionada requieren una revisión aparte.

La tarjeta muestra el ID de recibo, la referencia de factura cuando está disponible y el ID de la bodega. El detalle exige cantidad positiva y que SGA marque la línea como recibida y actualizada. Se conserva la cantidad en su presentación original de mayor o detalle. El documento requiere cabecera completa y al menos una línea aplicada.

Odoo deduplica por compañía, origen e ID de recibo. Si falla una operación, la salida muestra la posición de la fila y un código seguro. Tras revisar la discrepancia, vuelva a ejecutar el mismo comando y conserve el cursor. Las tarjetas ya confirmadas se reconocen en el reintento. Una diferencia en una tarjeta congelada, incluidas etiquetas de catálogo que cambiaron, requiere revisión de los dos orígenes.
