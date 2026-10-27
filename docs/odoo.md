# Addon Odoo 20

`odoo/addons/sga_gateway_connector` contiene un addon de base para la bandeja
de salida y entrega inmediata después del commit. Añadir ese directorio al addons_path,
actualizar la lista e instalar el módulo en una instancia Odoo 20 de pruebas.
Los permisos utilizan `ir.access` de Odoo 20, con una autorización para
administradores y una restricción global por las empresas activas.

El proceso Odoo necesita estos archivos externos, montados de solo lectura:

```text
SGA_GATEWAY_URL_FILE=/run/secrets/odoo_gateway_url
SGA_GATEWAY_API_KEY_FILE=/run/secrets/odoo_gateway_key
SGA_GATEWAY_ORIGIN_FILE=/run/secrets/odoo_origin_instance_id
```

Proceden de la configuración cifrada materializada. Configurar una URL HTTPS
real antes del primer envío. Para una red Docker local explícita puede
usarse `SGA_GATEWAY_ALLOW_HTTP=1`; fuera de ese caso se exige HTTPS. No seguir
redirecciones al transmitir el token. No guardar claves en ir.config_parameter.

La bandeja técnica aparece solo para administradores de sistema y respeta
empresas permitidas. Guardar la operación y pulsar «Enviar ahora» registra la
identidad, el usuario y la hora de envío en la transacción Odoo. Al confirmarse,
un callback hace inmediatamente el POST y guarda la respuesta con otro cursor.
Si la transacción se revierte, no hay envío. El formulario se recarga para
mostrar el resultado. Las solicitudes enviadas son inmutables; un cambio de
negocio usa otra revisión.

El cron no participa en la entrega normal. Se instala desactivado y sirve solo
para recuperar fallos después de cinco minutos sin actualización. El botón
«Consultar / recuperar» permite hacerlo manualmente: consulta una operación
conocida o programa el reenvío inmediato de la misma identidad después del commit.
Un timeout deja la operación pendiente con su error; no se informa de éxito.
El callback no puede deshacer el movimiento local ya confirmado.

Al actualizar desde 20.0.1.0.0, ejecutar la actualización del módulo para crear
`queued_at`. El cron anterior usa un registro `noupdate`: mantiene su configuración
previa; `_cron_exchange()` ahora delega a la recuperación con el mismo plazo de
gracia. Si estaba habilitado, cambiar su nombre e intervalo a recuperación cada
cinco minutos o desactivarlo. No vuelve a ser el camino de entrega normal.

El addon todavía no engancha los botones comerciales de Compras, Inventario,
Ventas o Tarifas ni valida movimientos de stock. Esos enlaces requieren las
reglas de negocio y roles que se certifiquen. Tampoco emite facturas fiscales:
ese proceso continúa en SGA.

Los futuros hooks comerciales deberán crear la operación y llamar a
`action_queue()` dentro de la misma transacción que el cambio autorizado del
documento; no hacer commits manuales en esos hooks. La selección de eventos,
sus payloads y la política de stock pendiente de confirmación SGA siguen por
certificar. El estado de una operación técnica no valida por sí solo el stock
ni el estado comercial del documento Odoo.

El repositorio no incluye el core ni el despliegue de Odoo. Al trasladar una
instancia Odoo hay que mover también su base PostgreSQL y su filestore; el
respaldo del gateway no reemplaza esas dos piezas. Mantener además persistente
la base de SGA y sus archivos requeridos por el adaptador.
