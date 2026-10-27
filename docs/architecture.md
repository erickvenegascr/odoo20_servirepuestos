# Arquitectura del gateway

```text
Odoo 20 (outbox persistente)
       | API autenticada, operación y revisión estables
       v
Django API ---- PostgreSQL persistente ---- Worker
       ^              |                    |
       |       Auditoría y deduplicación    v
       +------ consulta de estados     Adaptador SGA pendiente
```

La aplicación usa Django 5.2 LTS, REST Framework, Gunicorn y PostgreSQL 16.
El worker consume una cola de base de datos: no se necesita un Redis efímero
para conservar solicitudes. Cada toma usa bloqueo de fila y `skip_locked`
en PostgreSQL. La transición actual a Bloqueado y sus eventos se confirman en
una misma transacción local, sin efectos externos.

Los estados y 18 acciones están definidos en `contracts/v1`. El contrato
separa una solicitud recibida, un resultado nativo confirmado y un resultado
conciliado. Actualmente solo están implementadas recepción, lectura,
bloqueo preventivo y reencolado autorizado de estados seguros.

Cuando se implemente el adaptador SGA, debe resolver la ventana de fallo entre
aplicar el negocio y guardar su confirmación. Una fila externa de deduplicación
no demuestra una aplicación única en Access. Un resultado nativo desconocido
no se reintenta automáticamente. Si SGA confirmó y falla el espejo Odoo,
se repara solamente el espejo.

La base del gateway no es la base de Odoo ni la de SGA. Sus migraciones no
modifican estas últimas. El repositorio de código no contiene las adquisiciones
internas del análisis de SGA; esas reglas deben validarse en su entorno privado.

## Persistencia

- `gateway_postgres`: volumen nombrado con operaciones, eventos y migraciones.
- Configuración cifrada: repositorio privado independiente.
- Identidad age: canal independiente y fuera de Git.
- Secretos runtime: directorio externo con permisos restrictivos, ignorado.
- Respaldos: archivos cifrados externos, no versionados en Git.

Compose monta secretos por archivo. En Compose local son montajes de archivos;
no equivalen a un almacén de secretos cifrado en memoria. El disco y el acceso
al host deben protegerse. Cada servicio recibe únicamente los archivos que
necesita y el contenedor de aplicación ejecuta sin root, sin capacidades y con
sistema de archivos de solo lectura.

Referencias: [Django LTS](https://www.djangoproject.com/download/),
[secretos Compose](https://docs.docker.com/compose/how-tos/use-secrets/) y
[age](https://github.com/FiloSottile/age).
