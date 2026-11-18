# Acceso directo de Linux al formato Access/Jet

Implementación opcional en `gateway/bridge/access_jet.py`, con CLI independiente
en `bridge.access_jet_cli`. Linux abre el MDB mediante **Easysoft ODBC-ACCESS** y
coordina los bloqueos con Access por SMB. No necesita un proceso Windows, Wine,
Jet COM ni abrir los formularios de SGA.

El adaptador implementa dos operaciones concretas:

- Inspección SELECT acotada de candidatos en `PGPropiedades`.
- Lectura/simulación y actualización condicional de una propiedad numérica
  existente, incluida explícitamente en la configuración privada. Compara el
  texto anterior, verifica una sola fila afectada y el valor posterior y confirma
  la transacción. No crea la fila si no existe.

**No es todavía un asignador certificado de artículos SGA.** Sigue pendiente
identificar el contador real y su protocolo. `PGPropiedades.Valor` es una
ubicación candidata; no se debe confundir con `Database.Properties` de DAO.
La inspección ODBC no declara haber inspeccionado las propiedades DAO.
No se implementan escrituras en tablas de sistema Access para suplir esa falta.

## Preparación

El ejemplo `deploy/access-jet/config.example.json` apunta a un entorno ficticio
y tiene la lista de propiedades permitidas vacía. Copiarlo a configuración privada
fuera de Git, ajustar ruta y SMB al **mismo archivo**, y usar una cuenta existente
con los permisos necesarios. Mantener el archivo de contraseña restringido al
usuario de servicio; no pasar secretos como argumentos de proceso.

Linux necesita unixODBC, `pyodbc` y el driver comercial instalado/licenciado.
Las dependencias Python opcionales están en `gateway/requirements-access.in`;
no se modificó el conjunto de dependencias del gateway existente.

No se instala ni acepta una licencia del proveedor con este cambio. Antes de
usar el MDB activo hay que verificar compatibilidad con su seguridad/grupo de
trabajo, transacciones, compare-and-set sobre `Valor` (Memo), Unicode y SMB.
El código rechaza el driver ausente, el destino distinto o un driver que no
declare soporte transaccional para las actualizaciones.

## Uso

Ejecutar desde `gateway/` con el entorno Python del job:

```sh
python -m bridge.access_jet_cli --config-file /etc/sga-gateway/access-jet.json inspect
```

Después de identificar y probar una propiedad **en una copia**, incluir su nombre
exacto en `numeric_properties`. Los nombres y números siguientes son sintéticos:

```sh
python -m bridge.access_jet_cli --config-file /etc/sga-gateway/access-jet.json numeric-property \
  --name CONTADOR_CONFIRMADO_EN_LA_COPIA --expected 100 --replacement 101
```

La operación anterior solo simula. `--apply` solicita la actualización real de
esa fila. No hay API genérica de SQL ni fallback a MAX+1, Jackcess, otra base,
apertura exclusiva o agente Windows. La operación solo admite números positivos
o cero dentro de un Long con signo, y exige que el valor aumente.

Los resultados son JSON sin trazas de conexión ni contraseñas. Si otro proceso
cambió el valor, devuelve conflicto y no reintenta. Si falla el commit o no se
puede confirmar un rollback después de intentar DML, devuelve
`ACCESS_RESULT_UNKNOWN`: no interpretar la repetición del valor como prueba de
que nuestra operación se aplicó. Hay que conciliar antes de reenviar.

## Bloqueos y cierre

La cadena se construye desde campos controlados e impone `exclusive=no`, el
canal `smb*` y el modo de acceso solicitado. No admite un `.ldb` alternativo.
Comprueba también la identidad de la base que devuelve ODBC. Desactiva pooling
antes de abrir conexiones y cierra cursor y conexión al terminar, incluso si
falla la consulta. Debe ejecutarse como job corto separado del servidor web.

Se solicitan límites de 1–10 segundos para conexión y consultas, sin reintentos
internos ni esperas del usuario dentro de una transacción. Estos tiempos dependen
de que el driver los respete: todavía falta validarlo con el driver real. No se
mata un proceso en mitad de la escritura, no se elimina el `.ldb` ni se cierran
sesiones ajenas. No se puede prometer ausencia absoluta de bloqueos o corrupción
solo mediante estas pruebas unitarias.

El cliente SMB y Samba deben compartir el mecanismo de bloqueo y los ajustes de
caché documentados por el proveedor, incluso cuando Linux abre una ruta local
que Samba ofrece a Windows. Este cambio no modifica montajes, permisos ni la
configuración de Samba.

El [manual del driver](https://www.easysoft.com/products/data_access/odbc-access-driver/manual/access/1/getting-started.html)
describe `smbpath`, `smblib`, `smbuser`, `smbauth`, `readonly`, `exclusive` y el
único archivo `.ldb`. El driver es acceso al formato Jet desde Linux, no una
versión Linux del motor COM de Microsoft.

## Integración con el alta de artículos

El gateway instalado conserva la protección `SGA_WRITES_ENABLED=0`; no se conecta
este utilitario a la cola comercial ni al sondeo inverso con este cambio.
El futuro contrato `article.create` debe identificar la operación, reservar
mediante un mecanismo compatible con SGA, crear/verificar sus filas MariaDB y
vincular el producto Odoo existente. Una transacción del MDB no abarca MariaDB,
la bitácora del gateway ni Odoo.

La biblioteca independiente se puede retirar junto con el adaptador SGA. Los
productos y sus referencias comerciales seguirán siendo datos nativos de Odoo.

## Validación realizada y pendiente

```sh
cd gateway
python -m unittest bridge.tests.test_access_jet -v
```

20 pruebas con contrato ODBC sintético: simulación sin DML, comparación exacta,
cambio concurrente, fila afectada desconocida, verificación, rollback, timeout
de consulta, commit incierto, cierre, destino equivocado, SMB, límites y secretos.
No equivalen a una prueba nativa del driver ni a una certificación de los bloqueos
de Access. No se modificó un MDB real.

Antes de producción faltan: driver y licencia, lectura del contador real,
comprobación de la seguridad Access, prueba de rollback en copia, dos escritores
concurrentes (incluido SGA), prueba de caída/red y liberación de bloqueos, y
recuperación sin duplicar el alta entre MDB, MariaDB y Odoo.
