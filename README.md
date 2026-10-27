# Odoo 20 Servirepuestos

Base de integración entre Odoo 20 y SGA con un gateway Django en Docker.
El gateway conserva operaciones, revisiones, deduplicación y auditoría en
PostgreSQL. El módulo Odoo envía por API inmediatamente después del commit,
con una bandeja persistente para recuperar fallos. Los adaptadores que modifican SGA quedan bloqueados hasta
certificar sus reglas de aplicación.

## Estructura

```text
contracts/v1/              Contrato versionado, acciones y estados
 gateway/                 Django, API, cola PostgreSQL, migraciones y pruebas
 odoo/addons/              Módulos propios para Odoo 20
 scripts/                  Cifrado, secretos, respaldos y restauración
 docs/                     Arquitectura, API, despliegue y migración
 .github/workflows/        Verificación automatizada
 compose.yaml              API, worker, migraciones y PostgreSQL persistente
```

El repositorio complementario **odoo20_servirepuestos_secrets** es privado y
contiene únicamente configuración cifrada con age. La clave privada de
recuperación se conserva fuera de ambos repositorios. Los archivos descifrados
se montan como secretos Docker; no forman parte de la imagen ni de Git.

## Inicio

Requisitos: Docker Engine/Compose, Python 3.10 o posterior y age. Crear o
recuperar los secretos siguiendo [Configuración cifrada](docs/secrets.md).

```bash
cp .env.example .env
# Ajustar SECRETS_DIR a los archivos materializados del entorno.
docker compose config --quiet
docker compose build api
docker compose up -d api worker
docker compose ps
```

La API se publica por defecto en `127.0.0.1:8000`. Para acceso entre servidores,
configurar un dominio HTTPS mediante un proxy de confianza y el acceso de red
correspondiente. PostgreSQL no publica puertos al host.

```bash
curl http://127.0.0.1:8000/health/ready/
docker compose --profile test run --rm test
```

`docker compose down` conserva la base persistente. No utilizar `down -v` en
una operación normal. El volumen no sustituye al respaldo.

## Alcance actual

- Autenticación de servicios por tokens, separados por cliente, origen y empresa.
- API de recepción/lectura y cola durable con identidad de operación y revisión.
- Reintentos con el mismo contenido devuelven la operación existente; un contenido
  distinto bajo la misma identidad produce conflicto.
- La API procesa cada operación en la misma llamada, con bloqueo PostgreSQL
  y bitácora de estados; el worker solo recupera entregas abandonadas.
- El procesamiento actual registra `BLOQUEADO / SGA_NATIVE_ADAPTER_NOT_CERTIFIED`.
  No escribe inventario, costos, facturas ni tablas operativas de SGA.
- Addon Odoo con envío inmediato al confirmar su transacción. La respuesta
  actualiza el estado sin esperar un cron. Si Odoo revierte, no se envía.
- Cron opcional de recuperación de comunicaciones fallidas, desactivado por defecto.
- La conexión de los botones comerciales de compras, recepciones, ventas y
  precios a esa bandeja sigue pendiente, al igual que el adaptador nativo SGA.

No se incluye el core de Odoo, bases productivas, MDE, SQL adquirido, contraseñas
anteriores ni evidencias internas en este repositorio público.

## Documentación

- [Arquitectura y límites](docs/architecture.md)
- [API y estados](docs/api.md)
- [Instalación del addon Odoo](docs/odoo.md)
- [Configuración cifrada](docs/secrets.md)
- [Migración rápida y recuperación](docs/migration.md)
- [Validación y alcance comprobado](docs/validation.md)

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r gateway/requirements.lock
GATEWAY_TESTING=1 DATABASE_ENGINE=sqlite .venv/bin/python gateway/manage.py test bridge.tests
.venv/bin/python scripts/check_repository.py
```

La prueba SQLite omite cuatro casos de concurrencia; la prueba Docker usa
PostgreSQL real. Actualizar las dependencias desde `gateway/requirements.in`
mediante pip-tools, regenerar sus hashes y ejecutar la suite PostgreSQL.

`make test-odoo` instala el addon en Odoo 20 con PostgreSQL desechable y prueba
commits y rollbacks reales, sustituyendo únicamente el servidor HTTP remoto.
Finalizar con `docker compose -p servirepuestos_odoo_test -f compose.odoo-test.yaml down`: ese proyecto de
pruebas no comparte red, secretos ni volúmenes con el gateway del entorno.
