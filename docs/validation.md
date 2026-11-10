# Validación de la base del proyecto

## Catálogo, 10 de noviembre de 2026

- Gateway: **41 pruebas aprobadas contra PostgreSQL 17 temporal**, incluidas
  las pruebas existentes de concurrencia y nueve verificaciones del catálogo.
- Addon Odoo 20: **12 pruebas aprobadas**, instalado y actualizado en una base
  temporal con el core nativo; creación, repetición, archivo/reactivación,
  separación de compañías, conflictos locales, permisos y rollback del lote.
  La cuenta técnica puede importar por el contrato y no puede modificar
  productos por ORM general ni crear existencias.
- Instalación del gateway desde dependencias fijadas con hashes completada.
- `makemigrations --check --dry-run`, `check`, `check_repository.py`,
  `git diff --check` y configuración Compose sin errores.
- Las pruebas usan datos sintéticos. La cuenta MariaDB debe validarse en el
  servidor real antes de cualquier lectura del catálogo. El usuario autorizó expresamente el acceso SSH y la instalación. El gateway
  quedó instalado con PostgreSQL 17.11, Python 3.11 y dependencias fijadas.
  Salud local `ready`; servicios históricos deshabilitados. La cuenta SELECT
  se verificó y se contaron 33.039 artículos y 189 nodos de clasificación.
  La conexión Odoo 20 espera la clave API dedicada; la clave antigua responde 401.
- No se reanudó la migración histórica ni se escribió en SGA. No se ha
  certificado una sincronización comercial real con estos resultados.

Código: `sga_gateway_connector` **20.0.1.2.1** y migración gateway
`0002_catalogrun`. [Alcance y despliegue](catalog.md).

## Base del gateway, octubre de 2026

Comprobaciones realizadas el 27 de octubre de 2026:

| Comprobación | Resultado |
| --- | --- |
| Imagen Docker | Construida; API sin root y sistema de archivos de solo lectura |
| PostgreSQL y API | Saludables en el proyecto local del gateway |
| Suite Django contra PostgreSQL real | 32 pruebas satisfactorias, incluidos cuatro casos concurrentes |
| Idempotencia concurrente | Una solicitud y un evento inicial para dos envíos simultáneos |
| Exclusión de trabajadores | Una sola toma de una operación por dos trabajadores |
| API inmediata contra recuperación | Una sola ejecución cuando API y worker compiten por la misma operación |
| Entrega inmediata duplicada | Una sola ejecución para dos llamadas simultáneas |
| Interrupción después de persistir | El reenvío inmediato o el worker de recuperación conservan identidad y auditoría |
| Separación de ámbito | Autenticación, origen, empresa y visibilidad por cliente comprobados |
| Fallo seguro | Sin secretos válidos no arranca; activar escrituras SGA por variable produce error |
| Prueba sintética HTTP | Con el worker detenido, el POST devuelve ya Bloqueado; reenvío conserva identidad |
| Persistencia | Identidad, estado y tres eventos conservados tras reiniciar base/API/worker |
| Copia cifrada | pg_dump enviado directamente a age, sin dump sin cifrar en disco |
| Restauración | Identidad, estado y auditoría coinciden en una base nueva separada |
| Protección de restauración | Rechaza una base no vacía |
| Configuración cifrada | Descifra correctamente; hashes runtime coinciden y valores no aparecen en Git |
| Repositorio de secretos | Remoto privado; únicamente documentación, clave pública y configuración cifrada |
| Addon Odoo 20 real | Instalado en contenedor oficial con PostgreSQL aislado; ocho comprobaciones satisfactorias |
| Commit Odoo | Envío inmediato desde el callback real; otro cursor confirma que el outbox ya era visible |
| Rollback Odoo | Cero envíos para la operación revertida |
| Respuesta HTTP perdida | Outbox pendiente; recuperación con el mismo sobre y la misma identidad remota |
| Recuperación Odoo | No recoge intentos recientes; cron desactivado y reservado a fallos |
| Permisos Odoo 20 | ir.access permite al operador y restringe otras empresas y usuarios ordinarios |

Las ocho comprobaciones Odoo usan el ORM y los commits reales de Odoo 20,
con el servidor HTTP sustituido por un mock. La prueba HTTP del gateway es real
y separada. No se instaló el addon en la instancia comercial del cliente.
Ambas suites se ejecutan también en CI.

Las pruebas usan únicamente datos sintéticos del gateway nuevo. No se conectó
ni modificó la base SGA y no se validaron todavía sus procesos de compras,
recepción, valoración, precios o facturación.

Python y PostgreSQL tienen referencias de imagen fijadas por digest y las
dependencias Python conservan versiones y hashes. Las actualizaciones requieren
reconstrucción y repetición de las pruebas; no cambiar la versión mayor de una
base persistente mediante un simple cambio de etiqueta.

El repositorio público no contiene SQL adquirido, MDE, contraseñas anteriores
ni evidencia interna. Los escenarios completos del negocio y los botones
comerciales de Odoo deben cerrarse antes de habilitar el adaptador SGA.
