# API de aplicación: receptor Odoo y comunicación inversa

`sga_gateway_connector` 20.0.1.3.2 incorpora un receptor HTTP propio, siguiendo
el patrón de credenciales de Device Hub. No crea usuarios de Odoo ni utiliza
claves API personales o JSON-2 para comunicar las aplicaciones.

Cada conexión vincula un UUID, una compañía y una instancia SGA. El gateway
conserva el token `sgw1_...` en un archivo privado; Odoo guarda únicamente su
SHA-256. Una credencial no puede elegir otra compañía, otro origen ni métodos
ORM arbitrarios. Revocar la conexión o rotar su token invalida el anterior.
Las rutas requieren HTTPS y `Authorization: Bearer <token>`. Las cookies o una
sesión de usuario no sustituyen al token de aplicación.

| POST | Función |
| --- | --- |
| `/sga_gateway/api/v1/info` | Comprueba aplicación, compañía, base, origen y capacidades |
| `/sga_gateway/api/v1/catalog/batch` | Recibe artículos/categorías; simulación por defecto |
| `/sga_gateway/api/v1/exchange/poll` | Entrega mensajes Odoo → gateway con lease de 120 segundos |
| `/sga_gateway/api/v1/exchange/ack` | Confirma el resultado del mensaje y su lease |

Los cuerpos son objetos JSON de hasta 256 KiB. El catálogo admite hasta 200
elementos por transacción. Cualquier conflicto o dato inválido revierte el lote
completo. El cliente verifica base, compañía, aplicación y origen antes de leer
SGA. No sigue redirecciones ni acepta claves API personales como fallback.

Los errores de concurrencia de PostgreSQL se propagan al mecanismo nativo de
reintento de Odoo: se revierte y repite la petición completa. La fecha de última
conexión se actualiza como máximo cada 30 segundos para evitar conflictos entre
los lotes del catálogo y el sondeo de la bandeja de regreso.

Ejemplo del cuerpo de catálogo:

```json
{
  "instance": "servirepuestos-sgadata",
  "categories": [{"key":"root","parent":null,"name":"SGA"}],
  "products": [],
  "dry_run": true
}
```

La autenticación fija la compañía; no se recibe `context`, `company_id`, modelo
ni método del cliente. Se reutiliza el contrato de catálogo existente para
identidades, normalización e idempotencia. Los endpoints administrativos del
addon siguen limitados a administradores.

## Comunicación inversa sin puertos entrantes en SGA

Odoo conserva su bandeja `sga.gateway.message`. El proceso local
`exchange_odoo_gateway` consulta la bandeja, persiste el mensaje y su resultado
en `GatewayInbox` PostgreSQL y después confirma a Odoo. Si se pierde una
respuesta, se reutilizan el UUID y el resultado durable. Un lease caducado
permite redelivery; un ack de otro lease o aplicación se rechaza. Dos lectores
no reclaman simultáneamente el mismo mensaje.

La prueba `ping` responde `pong` y confirma el modo `read_only`. Las solicitudes
de tipo `request` quedan `blocked / SGA_READ_ONLY`; no ejecutan SQL, comandos,
documentos ni escrituras SGA. Esta fase habilita el transporte de regreso; las
operaciones comerciales inversas requieren contratos y adaptadores posteriores.
La bandeja comercial anterior conserva su bloqueo de escritura SGA.

## Configuración

Odoo: Ajustes técnicos → Aplicaciones SGA Gateway. Crear una conexión para la
compañía y origen correctos. El botón de generar/rotar muestra el nuevo token
una sola vez; Odoo guarda su huella. También se puede generar el token en el
gateway y registrar únicamente su SHA-256 mediante la consola administrativa,
para que el secreto no pase por pantalla o portapapeles.

`catalog_target.json`, externo al repositorio:

```json
{
  "url": "https://servirepuestos.odoo.com",
  "database": "BASE_ODOO20_CONFIRMADA",
  "company_id": 1,
  "application_id": "UUID_DE_LA_CONEXION",
  "token_file": "/etc/sga-gateway/odoo_application_token.txt"
}
```

El archivo de token es legible únicamente para root y el grupo del gateway.
Docker puede montar un archivo equivalente como secreto. No reutilizar el token
de la API Django entrante para la API receptora Odoo; son credenciales distintas.

```bash
python gateway/manage.py sync_catalog                 # simulación
python gateway/manage.py sync_catalog --apply         # carga Odoo
python gateway/manage.py exchange_odoo_gateway --once # ida/vuelta de mensajes
python gateway/manage.py exchange_odoo_gateway        # sondeo cada 15 segundos
```

El proceso de intercambio no dispara una migración histórica ni una escritura
SGA. La carga real del catálogo se ejecuta por separado. Los respaldos cifrados
deben incluir el token privado y `catalog_target.json`; Odoo conserva la huella.
