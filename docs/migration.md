# Migración rápida del gateway

Se necesitan dos repositorios, una identidad age conservada fuera de Git y un
respaldo cifrado de PostgreSQL. Conservar los mismos orígenes de cliente y UUID
de operación: cambiarlos al mover el servidor rompería la deduplicación histórica.

## Servidor de origen

1. Detener la entrada al gateway y sus trabajadores. La outbox de Odoo mantiene
   lo todavía no enviado y conserva las identidades de reintento.
2. Mantener PostgreSQL activo y crear un respaldo consistente cifrado:

```bash
docker compose stop api worker
export AGE_RECIPIENT="$(cat ../odoo20_servirepuestos_secrets/recipients.txt)"
export BACKUP_DIR=/ruta/externa/backups
./scripts/backup.sh
```

El script usa `pg_dump` en formato custom y cifra el flujo sin escribir un dump
sin cifrar. Guarda un checksum del archivo cifrado. Transferir el respaldo y su
checksum al servidor nuevo por un canal autorizado. Los respaldos no van a Git.
No reactivar la instancia anterior tras completar el cambio de servicio.

## Servidor nuevo

Clonar la aplicación y su repositorio privado en directorios hermanos. Recuperar
la identidad age por su canal separado e instalar Docker/Compose, Python y age.

```bash
cp .env.example .env
python3 scripts/manage_secrets.py materialize \
  --input ../odoo20_servirepuestos_secrets/environments/local.json.age \
  --identity /ruta/externa/gateway-age-identity.txt \
  --output ../odoo20_servirepuestos_secrets/runtime/local
```

Ajustar `.env` con el proyecto, ruta de secretos, hostname y puerto del nuevo
host. Cambiar la URL del gateway del addon mediante la configuración cifrada si
corresponde. Mantener las identidades de origen, credenciales y datos del entorno;
no ejecutar bootstrap para sustituir un entorno que ya existía.

Iniciar únicamente la base nueva antes de restaurar:

```bash
docker compose build api
docker compose up -d --wait db
export AGE_IDENTITY=/ruta/externa/gateway-age-identity.txt
./scripts/restore.sh /ruta/externa/backups/gateway-fecha.dump.age
docker compose up -d api worker
curl http://127.0.0.1:8000/health/ready/
```

El script de restauración rechaza bases con tablas existentes. No elimina
volúmenes ni utiliza `--clean`: usar un proyecto/volumen nuevo para ensayar o
recuperar. La restauración usa una transacción y elimina propietarios/ACL del
dump para que los datos pertenezcan al usuario configurado del entorno destino.
Las migraciones del servicio `migrate` se ejecutan antes de API y worker.

## Validación del cambio

- PostgreSQL y API saludables; una operación anterior conserva UUID, revisión,
  estado, hashes y eventos.
- Un reenvío desde Odoo devuelve la operación existente sin duplicarla.
- Los tokens y ámbitos de cliente/empresa se conservan; una empresa ajena sigue
  sin acceder al documento.
- Solo la instancia elegida está activa; comprobar las solicitudes y respuestas
  pendientes antes de reanudar el intercambio.
- Las escrituras SGA siguen deshabilitadas mientras no exista el adaptador validado.

## Qué persiste y qué se mueve por separado

`docker compose down` conserva el volumen PostgreSQL. Esto permite recrear
contenedores en el mismo host, pero para otro servidor se requiere el respaldo
lógico o una migración de datos compatible. Mantener el mismo nombre de proyecto
no transporta automáticamente un volumen a otra máquina.

El gateway no contiene la base ni el filestore de Odoo. Para trasladar Odoo
completo se necesita su respaldo PostgreSQL y el filestore correspondiente,
además del addon y sus archivos de acceso. El sistema SGA y sus bases se
mantienen como un entorno independiente; este procedimiento no los modifica.
