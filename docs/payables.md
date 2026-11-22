# Cuentas por pagar SGA

El flujo usa una aplicación Odoo con alcance CxP independiente. El gateway lee el saldo y el contacto de pago en SGA con una cuenta de solo lectura y recibe instrucciones de conciliación de Odoo en su PostgreSQL. El adaptador físico de pagos/reversos requiere homologación antes de aplicar efectos en SGA.

## Lectura y carga inicial

1. Usar el mismo esquema de configuración de conexión que `catalog_source` y `supplier_source` con el archivo protegido `SGA_PAYABLES_SOURCE_FILE`. Añadir `instance`, `national_currency_code` y `currency_map` a partir del catálogo real de la copia SGA.
2. Fijar la URL HTTPS, base, UUID de aplicación, compañía, `source_instance` y archivo del token de la aplicación aislada en `ODOO_PAYABLES_TARGET_FILE`.
3. Ejecutar `python manage.py sync_payables`. La simulación valida la identidad de destino y devuelve conteos; en Odoo revierte todos los cambios de cada solicitud.
4. Ejecutar `python manage.py sync_payables --apply` solo contra el destino aislado revisado. Primero entrega proveedores y después partidas abiertas. Vuelve a consultar por clave las partidas rastreadas para reconocer cierres. `--document-key-file` admite claves explícitas adicionales con cuatro componentes. Una clave ausente se marca para revisión sin inferir el saldo.
5. Cotejar partidas vigentes, diferencias y cuarentenas en Odoo. El gateway no imprime identidades, documentos, PDFs, números de cuenta, correos ni respuestas HTTP con datos.

Las claves de partidas son `(Proveedor, FacturaPrimaria, Acreedor, Factura)`. El saldo fuente es `Monto-MontoCancelado+MontoNotas-Retencion`; `MontoPagosPend` se informa por separado. La fecha de vencimiento es `Fecha + TipoCredito` en días. Los importes de SGA conservan cuatro decimales. El tipo de cambio y el código de origen deben corresponder al mapa de moneda y a CRC o USD. Una moneda indeterminada o la falta de retención conocida se reporta en cuarentena.

## Intercambio de pagos

`python manage.py exchange_payables` comprueba la identidad sin tomar trabajos. `--receive` recibe eventos solo si el alcance de intercambio de CxP está habilitado en la aplicación Odoo. El comando conserva la instrucción y devuelve únicamente recibos ya registrados. La entrega pendiente no implica que SGA haya recibido el pago.

Un futuro adaptador deberá verificar la huella y el saldo anterior bajo el bloqueo nativo. La aplicación y la persistencia del resultado por UUID deben participar en la misma transacción nativa. El recibo incluye la identidad del efecto y la lectura posterior de cada saldo. El reverso referencia la operación original. Un desconecte ambiguo requiere lectura de la identidad antes de considerar un nuevo efecto. El contrato distingue saldo desactualizado, validación nativa rechazada y resultado desconocido.

Las pruebas de intercambio usan un libro ficticio transaccional con recibos persistentes, incluidos abonos en USD, reverso, moneda incompatible, respuesta perdida, reintento y saldo posterior. Las consultas de esquema y los QueryDefs de `SGAPago` y `SGARecalcProg` corroboran identidades y reglas de lectura. Se necesita una copia ejecutable de SGA y evidencia de su rutina nativa para certificar escrituras, saldos y reversos reales.

## Tarea hacia Facturas de proveedor estándar

`sync_vendor_bill_balances` toma una lectura consistente de saldos abiertos en SGA y coteja la referencia y el proveedor comercial con las facturas estándar de Odoo. Usa los vínculos de proveedores ya instalados y consulta FiscalBot para apartar posibles duplicados. La tarea no llama al importador de compras, no actualiza proveedores y no requiere un módulo Odoo nuevo. El saldo de una factura contabilizada se calcula en Odoo a partir de su conciliación. Una diferencia queda para revisión contable.

Configure el archivo protegido `SGA_PAYABLES_SOURCE_FILE` como en la lectura de CxP. Configure `ODOO_VENDOR_BILLS_TARGET_FILE` con:

- `url`, `database`, `company_id` y `source_instance` del destino específico.
- `api_key_file` para la clave de un usuario técnico de Odoo con API JSON-2 y compañía acotada. Debe poder leer facturas, cuentas, monedas, vínculos SGA y FiscalBot de esa compañía. Para crear borradores, debe tener acceso a la importación de facturas. No se usa el token del conector de compras ni una clave OpenAI.
- `exclude_order_ids`, `exclude_invoice_references` y `exclude_fiscalbot_ids` con las exclusiones revisadas para cada destino. Las tres listas deben estar presentes. Un pedido excluido sin una recepción comprobable detiene la corrida.
- `opening` opcional con `date`, `journal_id` de compras y `bridge_account_id` de la cuenta puente conciliable, aprobados antes de habilitar una carga. La fecha delimita las facturas históricas candidatas. Los saldos en otra moneda quedan para revisión.

La unidad `sga-gateway-vendor-bill-balances.service` ejecuta una simulación invocable mediante `systemctl start`. Cárguela desde su publicación aislada en `/opt/sga-gateway/cxp-native-release` con los archivos indicados en `/etc/sga-gateway/cxp-native.env`. El servicio no activa un timer. La simulación devuelve un identificador de ejecución, conteos y una huella. Para crear borradores con un plan revisado, ejecute el mismo comando de gateway con `--apply-drafts HUELLA`. Si cambian los saldos o las coincidencias, la tarea exige una simulación nueva.

Los borradores usan solo el importe pendiente, una línea de apertura sin IVA, referencia y fecha originales, y el vencimiento original. Reservan sus IDs externos y se importan con protección nativa contra actualizaciones de esos IDs. La tarea relee lo creado y verifica el importe y la cuenta. Ante una interrupción, vuelva a simular para identificar borradores ya existentes antes de aplicar otra vez. La contabilización y la conciliación se controlan en Facturas de proveedor de Odoo.

Los borradores de la tarea cuyo saldo cambió o que ya no aparecen abiertos en SGA se señalan para revisión. Coordine la carga con FiscalBot cuando se esté trabajando sobre el mismo histórico. JSON-2 confirma cada lote por separado y no comparte una transacción con el flujo de compras.
