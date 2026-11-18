# Conector Access del gateway

El componente tiene dos superficies separadas: lector general de tablas y
escritor binario Jet4 experimental para una copia privada de laboratorio.
No es todavía un motor Jet completo ni habilita escrituras sobre el MDB activo.
No necesita licencia comercial. No cambia Java/Tomcat ni instala otro runtime.

## Lectura general implementada

`bridge.access_gateway.AccessGateway` ofrece:

- `describe(database)`: formato, tablas, columnas, tipos, longitud, precisión,
  escala, autonumeración, claves/índices y relaciones locales.
- `read_rows(database, table, fields, offset=0, limit=100)`: proyección explícita
  de hasta 32 campos, páginas de hasta 500 filas y desplazamiento hasta 10.000.
- Marcado de tablas vinculadas. No abre sus archivos/ODBC ni divulga conexiones;
  una tabla vinculada a MariaDB necesita el adaptador de ese origen.

El lector usa Jackcess 4.0.10 con apertura de solo lectura, cierre al terminar,
fechas locales sin zona y decimales representados como texto para conservar
precisión. Rechaza tablas de sistema, binarios/OLE y columnas complejas. Los
valores de texto se limitan a 4.096 caracteres. La lectura de un archivo que
cambia no es una instantánea: detecta cambios de tamaño/fecha y rechaza ese
resultado, sin afirmar que esos indicadores detecten todas las modificaciones.

```sh
mvn -f gateway/access-probe/pom.xml clean package
cd gateway
python -m bridge.access_gateway --jar access-probe/target/access-readonly-probe-1.0.0.jar \
  --database /ruta/copia.mdb describe
python -m bridge.access_gateway --jar access-probe/target/access-readonly-probe-1.0.0.jar \
  --database /ruta/copia.mdb read --table PGPropiedades --field Nombre --field Valor --limit 50
```

Se ejecuta Java como proceso hijo corto con argumentos separados, sin shell.
El timeout afecta exclusivamente a ese hijo. No se buscan ni terminan otros
procesos Java. La API es local; no se publica un endpoint HTTP de consulta
arbitraria ni se amplían permisos de los clientes actuales.

## Escritor propio implementado para laboratorio

`bridge.jet_lab` implementa lectura binaria de las filas Jet4 y actualización de
un número en un Memo inline de `PGPropiedades.Valor`. Jackcess aporta únicamente
la descripción de esquema y el generador/lector independiente de las pruebas;
la actualización usa el parser y las escrituras de página propios en Python.

`init` exige una carpeta nueva, crea una copia privada `data.mdb` y guarda la
descripción del esquema. La API de actualización solo acepta esa carpeta;
rechaza enlazar el original mediante symlinks o hardlinks. El fichero fuente
se abre para lectura. El límite del laboratorio es 64 MiB: no admite el MDB
activo de SGA de aproximadamente 326 MiB.

```sh
python -m bridge.jet_lab init --source /ruta/fixture.mdb \
  --jar access-probe/target/access-readonly-probe-1.0.0.jar --root /ruta/laboratorio-nuevo
python -m bridge.jet_lab update-numeric --root /ruta/laboratorio-nuevo \
  --operation-id 750a0448-c4bb-4876-b86a-738b33084740 \
  --name CounterFixture --expected 100 --replacement 101
python -m bridge.jet_lab recover --root /ruta/laboratorio-nuevo
```

La escritura comprueba el valor anterior, unicidad, límites de filas/campos,
identidad de la tabla y ausencia de un índice sobre `Valor`. Solo cambia los
bytes del número: no reubica filas, cambia tamaños, asigna páginas ni modifica
índices. Rechaza Memo externo, crecimiento de dígitos, tablas con columnas
eliminadas, ciertas compresiones de texto y filas eliminadas/desbordadas. El
subconjunto no permite aún crear artículos ni actualizar propiedades DAO.

Antes de escribir persiste una imagen original de la página y su identidad.
Tras verificar la escritura persiste la decisión de commit y luego el recibo
del UUID. La recuperación revierte una operación sin decisión o reconstruye
el recibo de una operación confirmada. Cambios ajenos fuera de la página o
conflictos de identidad conservan la evidencia y rechazan la recuperación.
El diario es propio, no es interpretable por Access y no está destinado a
convivir con una sesión nativa sobre ese archivo.

El bloqueo `flock` utilizado coordina **solo este laboratorio** y se libera al
cerrar el descriptor. No implementa el protocolo `.ldb` de Access. Las pruebas
de contención entre nuestros procesos no certifican convivencia con SGA/Samba.
La recuperación se prueba frente a terminación de procesos; no se garantiza
durabilidad frente a cortes físicos de energía.

## Validación y trabajo que falta

```sh
PYTHONPATH=gateway python -m unittest discover -s gateway/access-probe/tests -v
```

Las pruebas crean MDB Jet4 reales con datos sintéticos: comprueban esquema,
proyección, paginación, Unicode, precisión decimal, cambios, consulta posterior
por clave primaria, comparación del valor anterior, UUID repetido/conflictivo,
fallo/rotura de escritura, terminación de procesos antes/después del commit,
recuperación y liberación/contención de bloqueos propios. La lectura posterior
usa Jackcess, no el parser que escribió el dato. Esto todavía no es una prueba
de aceptación con Microsoft Jet/DAO.

`tools/jet/verify_lab_dao.ps1` prepara esa comprobación de lectura posterior con
DAO 3.6, en PowerShell de 32 bits del Windows que ya tenga el motor. Solo abre
la copia marcada de laboratorio en modo compartido y de solo lectura, verifica
las dos filas sintéticas por la clave primaria y contrasta SHA-256 antes/después.
El script está preparado pero todavía no se ejecutó en Windows. Esa prueba
también es distinta de certificar dos escritores concurrentes por SMB.

Faltan el protocolo de bloqueos nativo validado contra Jet4 real por SMB, su
coherencia de caché y recuperación, soporte de crecimiento/Memo externo,
inserción e índices, propiedades DAO y el asignador nativo del artículo. El
trabajo previo de VBA validó 18 ejemplos y recuperó dos cuerpos sencillos de SGA;
no recuperó el motor Jet ni el cuerpo de `PGProxNum`.

La cola comercial conserva su bloqueo de escritura. Desconectar este componente
no cambia los productos Odoo ni el lector MariaDB existente. El alcance del
gateway debe ampliarse por operaciones verificadas, con el alta de artículos
como primera operación de negocio pendiente.
