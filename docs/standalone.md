# Instalación separada en el servidor SGA

El 10 de noviembre de 2026 se instaló el gateway en `192.168.2.2` (ClearOS 7)
sin Docker y sin cambiar MariaDB ni los archivos de SGA. Se reutilizó Python
3.11.16 y se creó un entorno virtual independiente. Todas las dependencias
se instalaron con las versiones y hashes de `gateway/requirements.lock`.

PostgreSQL **17.11** se compiló desde la distribución oficial y su SHA-256
`5367f6fb2ec97efe1eb2e0c7926bb33438e51b0bd3a9733b88498056a7dc9a7e`.
Se añadieron únicamente m4, Bison y Flex, con firmas RPM verificadas. No se
actualizaron paquetes del motor MariaDB ni dependencias de los servicios SGA.
La compilación usó dos procesos con prioridad reducida. Opciones: sin readline,
zlib, ICU, LZ4 ni Zstandard. Los dumps de este PostgreSQL requieren `pg_dump -Z0`.

| Componente | Ruta o servicio |
| --- | --- |
| Aplicación | `/opt/sga-gateway/app/gateway` |
| Python | `/opt/sga-gateway/venv/bin/python` |
| PostgreSQL | `/opt/sga-gateway/postgresql-17.11` |
| Datos PostgreSQL | `/var/lib/sga-gateway/postgresql` |
| Configuración privada | `/etc/sga-gateway` |
| PostgreSQL | `sga-gateway-postgresql.service`, solo `127.0.0.1:5548` |
| API | `sga-gateway-api.service`, solo `127.0.0.1:8096` |
| Recuperación | `sga-gateway-worker.service` |
| Comunicación de regreso | `sga-gateway-exchange.service`, sondeo saliente cada 15 segundos |
| Catálogo | `sga-gateway-catalog.service`, ejecución separada del histórico |
| Conciliación periódica | `sga-gateway-catalog.timer`, cada hora tras validar la carga |
| Fotografías | `sga-gateway-images.service`, ejecución manual y carpeta fuente montada solo lectura |

El usuario `sgapg` posee los datos PostgreSQL en modo 0700. `sgagateway`
ejecuta Django con un rol PostgreSQL distinto, propietario únicamente de la
base `gateway`. Los archivos de configuración pertenecen a root, con lectura
solo para el grupo del servicio; las credenciales nunca se imprimieron.
La conexión SGA reutiliza `sgamigrate_ro`; SHOW GRANTS confirmó solo lectura.
La clave Odoo anterior fue rechazada con HTTP 401 y no se reutilizó.

Las migraciones `bridge.0001_initial`, `bridge.0002_catalogrun`,
`bridge.0003_gatewayinbox`, `bridge.0004_productimagerun` y contenttypes
se aplicaron únicamente en PostgreSQL del gateway. `/health/ready/` devolvió
`{"status":"ready"}`. La API no se publicó fuera de localhost. El adaptador
de escritura SGA permanece deshabilitado. El timer de catálogo se habilita
solo después de validar la carga y su repetición idempotente.

El worker y timer históricos `sgadata-migration-worker.service` y
`sgadata-migration-incremental.timer` quedaron detenidos y deshabilitados.
La cola no tenía trabajos activos. Sus cursores, pendientes y auditoría se
conservaron; el monitor web anterior sigue disponible para consulta.

## Operación

```bash
systemctl status sga-gateway-postgresql sga-gateway-api sga-gateway-worker
systemctl status sga-gateway-exchange sga-gateway-catalog
curl --fail http://127.0.0.1:8096/health/ready/
journalctl -u sga-gateway-api -u sga-gateway-worker --since today
```

Para ejecutar `sync_catalog`, cargar `/etc/sga-gateway/gateway.env` en un
proceso del usuario `sgagateway`, situado en la carpeta de aplicación. La
primera ejecución es simulación; `--apply` habilita únicamente las escrituras
del catálogo en Odoo. No reactivar el worker o timer históricos para usar este
gateway. La [guía del catálogo](catalog.md) define el alcance y la conciliación.

El receptor Odoo usa un token de aplicación dedicado en
`/etc/sga-gateway/odoo_application_token.txt`; Odoo guarda solo su SHA-256.
`catalog_target.json` fija además base, compañía y UUID de aplicación. No se
creó una cuenta Odoo ni se reutilizaron claves API personales. El intercambio
inverso conserva mensajes y confirmaciones en PostgreSQL; responde ping/pong y
bloquea todas las solicitudes comerciales con `SGA_READ_ONLY`.

Las unidades de catálogo e intercambio están en `deploy/systemd`. Después de
simular, cargar y repetir la conciliación sin nuevas creaciones:

```bash
install -m 0644 deploy/systemd/sga-gateway-catalog.service /etc/systemd/system/
install -m 0644 deploy/systemd/sga-gateway-catalog.timer /etc/systemd/system/
install -m 0644 deploy/systemd/sga-gateway-exchange.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sga-gateway-exchange.service sga-gateway-catalog.timer
systemctl list-timers sga-gateway-catalog.timer
```

El servicio de catálogo realiza `sync_catalog --apply` y puede invocarse con
`systemctl start sga-gateway-catalog`. El timer es horario, con hasta 120 segundos
de dispersión y recuperación tras reinicio. Una unidad activa no se solapa con
otra ejecución; el bloqueo PostgreSQL protege además contra procesos manuales.
Una falla queda auditada y la siguiente pasada comienza desde cero sin duplicar.

Para fotos, instalar primero el complemento Odoo `sga_gateway_product_media`.
La inspección confirmó que la cuenta OS del servicio general no puede leer la
carpeta privada de fotos. Sus permisos permanecen intactos: la unidad manual
de imágenes usa un lector sin puertos entrantes, monta esa carpeta como solo
lectura y limita capacidades a `CAP_DAC_READ_SEARCH`. Envía archivos únicamente
al receptor HTTPS ya autenticado y validado. La migración histórica sigue parada.
El comando sin `--apply` solo simula la correspondencia y compara checksums;
el servidor Odoo decodifica y valida el contenido antes de aplicar cada archivo.

El respaldo manual `/opt/sga-gateway/backup.sh` (fuente en
`scripts/backup_standalone.sh`) envía `pg_dump` y el tar de configuración
directamente a age 1.3.2, sin crear copias en texto claro. Usa el destinatario
público previamente configurado; la identidad privada queda fuera del servidor
y de ambos repositorios. Los archivos cifrados quedan en
`/var/backups/sga-gateway` con modo 0600 y tienen checksums SHA-256. Se conserva
una copia externa cifrada y se verificó su descifrado e inventario de tablas.
No se habilitó programación automática de respaldos en esta etapa.

Se debe conservar la configuración privada y
un dump de la base `gateway`, cifrados fuera del repositorio. Los scripts Docker
de respaldo del proyecto original no corresponden a este despliegue standalone.
El volumen o directorio de datos no sustituye a esos respaldos.
