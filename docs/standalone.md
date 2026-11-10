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

El usuario `sgapg` posee los datos PostgreSQL en modo 0700. `sgagateway`
ejecuta Django con un rol PostgreSQL distinto, propietario únicamente de la
base `gateway`. Los archivos de configuración pertenecen a root, con lectura
solo para el grupo del servicio; las credenciales nunca se imprimieron.
La conexión SGA reutiliza `sgamigrate_ro`; SHOW GRANTS confirmó solo lectura.
La clave Odoo anterior fue rechazada con HTTP 401 y no se reutilizó.

Las migraciones `bridge.0001_initial`, `bridge.0002_catalogrun` y contenttypes
se aplicaron únicamente en PostgreSQL del gateway. `/health/ready/` devolvió
`{"status":"ready"}`. La API no se publicó fuera de localhost. El adaptador
de escritura SGA permanece deshabilitado y no se habilitó un timer de catálogo.

El worker y timer históricos `sgadata-migration-worker.service` y
`sgadata-migration-incremental.timer` quedaron detenidos y deshabilitados.
La cola no tenía trabajos activos. Sus cursores, pendientes y auditoría se
conservaron; el monitor web anterior sigue disponible para consulta.

## Operación

```bash
systemctl status sga-gateway-postgresql sga-gateway-api sga-gateway-worker
curl --fail http://127.0.0.1:8096/health/ready/
journalctl -u sga-gateway-api -u sga-gateway-worker --since today
```

Para ejecutar `sync_catalog`, cargar `/etc/sga-gateway/gateway.env` en un
proceso del usuario `sgagateway`, situado en la carpeta de aplicación. La
primera ejecución es simulación; `--apply` habilita únicamente las escrituras
del catálogo en Odoo. No reactivar el worker o timer históricos para usar este
gateway. La [guía del catálogo](catalog.md) define el alcance y la conciliación.

Para respaldar esta instalación se debe conservar la configuración privada y
un dump de la base `gateway`, cifrados fuera del repositorio. Los scripts Docker
de respaldo del proyecto original no corresponden a este despliegue standalone.
El volumen o directorio de datos no sustituye a esos respaldos.
