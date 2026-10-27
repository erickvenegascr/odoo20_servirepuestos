# Configuración cifrada y repositorio privado

El repositorio de aplicación contiene código y ejemplos. El repositorio privado
`odoo20_servirepuestos_secrets` conserva únicamente configuración cifrada con
age, más su clave pública. La identidad privada de recuperación se guarda por
un canal independiente y fuera de ambos repositorios.

## Crear un entorno nuevo

```bash
python3 scripts/manage_secrets.py bootstrap \
  --repo ../odoo20_servirepuestos_secrets \
  --identity /ruta/externa/gateway-age-identity.txt \
  --environment local --origin odoo-local --company default
```

Genera Django SECRET_KEY, contraseña PostgreSQL y tokens distintos para Odoo y
el operador. No reutiliza ni lee credenciales del sistema SGA. Rechaza regenerar
un entorno existente para impedir que se pierda el acceso a una base persistente.
La identidad se crea con permisos 0600. Respaldarla en un gestor de contraseñas
u otro canal controlado: Git por sí solo no permite recuperar estos secretos.

## Materializar archivos para Docker

```bash
python3 scripts/manage_secrets.py materialize \
  --input ../odoo20_servirepuestos_secrets/environments/local.json.age \
  --identity /ruta/externa/gateway-age-identity.txt \
  --output ../odoo20_servirepuestos_secrets/runtime/local
```

Configurar `SECRETS_DIR` en la copia local de `.env`. La materialización no imprime
valores y rechaza un directorio no vacío. El directorio padre queda 0700; los
archivos son de solo lectura y permiten que el UID no root del contenedor use
los montajes individuales de Compose en Linux. El directorio runtime se ignora
en Git y nunca debe incluirse en una copia de código o imagen Docker.

Los archivos principales son:

- `django_secret_key.txt`, `postgres_password.txt`: credenciales del gateway.
- `gateway_clients.json`: hashes de tokens, roles, origen y empresas permitidas.
- `odoo_gateway_key.txt`, `operator_gateway_key.txt`: claves entregadas solo a
  sus consumidores autorizados.
- `odoo_origin_instance_id.txt`, `odoo_gateway_url.txt`: configuración del addon.
- `sga_connection.json`: conexión pendiente y escrituras deshabilitadas; no se
  han incorporado credenciales de la base operativa.

Un montaje Compose no cifra el archivo en disco. Mantener el disco/host
protegido y otorgar acceso de Docker solo a los administradores necesarios.

## Cambiar configuración

Descifrar a un archivo temporal externo a los repositorios, con `umask 077`,
editarlo localmente y cifrar a una revisión nueva:

```bash
python3 scripts/manage_secrets.py encrypt \
  --input /ruta/privada/configuracion.json \
  --identity /ruta/externa/gateway-age-identity.txt \
  --output ../odoo20_servirepuestos_secrets/environments/local.next.json.age
```

Revisar la configuración sin publicarla en logs, sustituir el archivo cifrado
que corresponda y materializar a otro directorio runtime. Cambiar `SECRETS_DIR`
y recrear los contenedores tras comprobar el contenido. No cambiar únicamente
el archivo de contraseña PostgreSQL de una base ya inicializada: la rotación
debe coordinarse con el cambio de credencial dentro de esa base.

No guardar en Git archivos descifrados, claves age privadas, certificados
privados, `.env` reales ni respaldos. El repositorio privado tiene una lista
permitida de archivos para reforzar esta separación.
