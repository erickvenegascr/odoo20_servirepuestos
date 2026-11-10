# Etapa 1: catálogo SGA → Odoo 20

El addon `sga_gateway_connector` 20.0.1.3.2 incorpora el contrato de entrada del
catálogo. El gateway mantiene su bandeja anterior y continúa rechazando toda
escritura hacia SGA. Este trabajo no reactiva el monitor de migración histórica
ni su temporizador, ni encola compras, ventas, pagos o inventario.

La primera etapa replica la identidad `Articulo`, nombre, referencia, estado,
tipo producto/servicio y clasificación nativa `product.category`. Conserva
división → categoría → subcategoría, incluidas las entradas del catálogo sin
artículos asignados. Un código de categoría usado por varias divisiones produce
una ruta por división. Los artículos sin clasificación usan SIN DIVISION /
SIN CATEGORIA. No identifica productos por SKU, descripción ni código de barras.

Empaque, unidades por empaque, CABYS, atributos, NO PEDIR y código de marca quedan
como evidencia descriptiva en `sga_catalog_details`; no representan unidades,
impuestos o reglas comerciales ya homologadas. Si está instalado
`product_smart_search`, los atributos alimentan además su campo de búsqueda.
Respeta la normalización de nombres de `product_name_uppercase` cuando existe.

Esta etapa no carga precios, costos, impuestos, existencias, imágenes ni
documentos. Los valores comerciales existentes se conservan. Los nuevos
productos usan los valores predeterminados nativos del entorno; deben permanecer
fuera de operación comercial hasta homologar tarifas, impuestos y unidades.
No se elimina ni archiva un artículo por desaparecer de una lectura. Solo se
replica la marca de actividad presente en SGA (`Trabajado=255`).

## Protección del origen

El lector exige una cuenta **ya existente de solo SELECT**. Comprueba sus permisos
mediante SHOW GRANTS y rechaza permisos de escritura, EXECUTE o GRANT OPTION;
no sirve una cuenta root aunque se prometa usarla solo para leer. No crea usuarios
ni modifica permisos, tablas, triggers, binlog o archivos SGA. MariaDB 5.5 no
soporta todas las protecciones READ ONLY modernas; por eso el permiso real de la
cuenta es obligatorio. Se abre una instantánea consistente y se cierra mediante
rollback. La única interfaz de consulta admite SELECT fijos y parametrizados.

Se recorren todos los artículos por clave, en lotes de hasta 200. Cada nueva
corrida reconcilia el catálogo completo: `artsservsrefer` y los catálogos de
clasificación no ofrecen un registro de cambios completo y confiable. Un cambio
concurrente a la instantánea se recoge en la siguiente corrida. El perfil Docker
es manual. En standalone, el timer separado permite conciliar cada hora después
de validar la carga inicial y una segunda pasada sin nuevas creaciones.

## Instalar y configurar

1. Incorporar `odoo/addons/sga_gateway_connector` a la compilación **Odoo 20**,
   instalar/actualizar el addon y verificar sus pruebas antes de cargar datos.
   El viejo `sgadata_migration` Odoo 19 no se instala sobre Odoo 20.
2. En el gateway, reconstruir la imagen con `gateway/requirements.lock` y aplicar
   las migraciones normales. `0002_catalogrun` y `0003_gatewayinbox` crean la
   auditoría y bandeja de recepción en PostgreSQL del gateway. No hay migraciones
   para MariaDB.
3. Materializar fuera de Git dos archivos con permisos 0600. Incorporarlos al
   procedimiento cifrado del repositorio privado sin publicar sus valores.

`catalog_source.json`:

```json
{
  "instance": "servirepuestos-sgadata",
  "host": "192.168.2.2",
  "port": 3306,
  "database": "sgadata",
  "user": "CUENTA_SELECT_EXISTENTE",
  "password": "SECRETO"
}
```

`catalog_target.json`:

```json
{
  "url": "https://servirepuestos.odoo.com",
  "database": "NOMBRE_REAL_CONFIRMADO_DE_ODOO20",
  "company_id": 1,
  "application_id": "UUID_DE_LA_APLICACION",
  "token_file": "/etc/sga-gateway/odoo_application_token.txt"
}
```

El receptor usa un token entre aplicaciones, asociado a una compañía y origen,
con SHA-256 en Odoo y el secreto solo en el gateway. No requiere un usuario
Odoo ni una clave API personal. Configurar `application_id` y `token_file` en
el destino; ver [API y comunicación inversa](application-api.md). Se exige
HTTPS verificado, sin redirecciones. Antes de leer SGA se verifica la identidad
completa del destino.

```bash
docker compose build api
docker compose run --rm migrate
docker compose --profile catalog run --rm catalog
# Después de revisar la simulación:
docker compose --profile catalog run --rm catalog python manage.py sync_catalog --apply
```

Fuera de Docker, los mismos archivos se seleccionan con
`SGA_CATALOG_SOURCE_FILE` y `ODOO_CATALOG_TARGET_FILE`; ejecutar
`python gateway/manage.py sync_catalog` y después `--apply`.

## Reintentos, conflictos y auditoría

Cada lote Odoo es una transacción completa. Los artículos nuevos se crean con
la operación nativa multi-create para compartir variantes y recomputaciones. Las identidades únicas incluyen
instancia y compañía; las categorías también incluyen su ruta. Repetir un lote
tras perder su respuesta no duplica productos ni categorías. El cursor de
`CatalogRun` solo avanza después de recibir la confirmación. Una falla queda
registrada sin secretos y la próxima corrida comienza desde cero, reconciliando
con idempotencia; no se salta silenciosamente un lote fallido. Un bloqueo
PostgreSQL impide dos recorridos simultáneos para el mismo origen/destino.

Los campos administrados se comparan con su último valor importado: una edición
local en Odoo produce conflicto y revierte el lote antes de sobrescribirla. Los
precios o costos editados en Odoo quedan fuera de este contrato y se conservan.
No se transmite un evento de retorno a SGA por crear el espejo.

El registro `CatalogRun` conserva UUID, origen, huella del destino, simulación,
estado, cantidades reconocidas, cursor confirmado y clase de error. Una corrida
interrumpida abruptamente puede quedar `running`; la siguiente crea otra auditoría
y el bloqueo de sesión anterior se libera cuando PostgreSQL cierra la conexión.

## Criterios de validación del entorno real

La ejecución comercial requiere: acceso autorizado al servidor, cuenta SELECT
vigente, addon instalado en la base Odoo 20 correcta y token de aplicación vigente. No se
puede declarar el catálogo migrado solo por pasar pruebas con datos sintéticos.
Comparar conteos, rutas y una segunda corrida sin nuevas creaciones antes de dar
por terminada esta etapa.

Referencias: [JSON-2 y transacciones Odoo 20](https://www.odoo.com/documentation/20.0/developer/reference/external_api.html),
[permisos efectivos en MariaDB](https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/show/show-grants).
