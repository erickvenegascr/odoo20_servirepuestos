# Addon Odoo 20

`odoo/addons/sga_gateway_connector` contiene un addon de base para la bandeja
de salida y la consulta de estados. Añadir ese directorio al addons_path,
actualizar la lista e instalar el módulo en una instancia Odoo 20 de pruebas.

El proceso Odoo necesita estos archivos externos, montados de solo lectura:

```text
SGA_GATEWAY_URL_FILE=/run/secrets/odoo_gateway_url
SGA_GATEWAY_API_KEY_FILE=/run/secrets/odoo_gateway_key
SGA_GATEWAY_ORIGIN_FILE=/run/secrets/odoo_origin_instance_id
```

Proceden de la configuración cifrada materializada. Configurar una URL HTTPS
real antes de habilitar el cron. Para una red Docker local explícita puede
usarse `SGA_GATEWAY_ALLOW_HTTP=1`; fuera de ese caso se exige HTTPS. No seguir
redirecciones al transmitir el token. No guardar claves en ir.config_parameter.

La bandeja técnica aparece solo para administradores de sistema y respeta
empresas permitidas. Guardar la operación y pulsar «Enviar a la cola» conserva
su identidad antes de la comunicación. El cron realiza el envío y consulta el
estado; se instala desactivado para evitar llamadas sin configuración. Las
solicitudes ya enviadas son inmutables; un cambio de negocio usa otra revisión.

El addon todavía no engancha los botones comerciales de Compras, Inventario,
Ventas o Tarifas ni valida movimientos de stock. Esos enlaces requieren las
reglas de negocio y roles que se certifiquen. Tampoco emite facturas fiscales:
ese proceso continúa en SGA.

El repositorio no incluye el core ni el despliegue de Odoo. Al trasladar una
instancia Odoo hay que mover también su base PostgreSQL y su filestore; el
respaldo del gateway no reemplaza esas dos piezas. Mantener además persistente
la base de SGA y sus archivos requeridos por el adaptador.
