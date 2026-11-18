# Inspector Access de solo lectura

Herramienta libre para localizar candidatos al consecutivo de artículos en un
MDB desde Linux, sin instalar Access, ODBC comercial ni un agente Windows.
**No crea artículos ni actualiza consecutivos.** El gateway conserva deshabilitada
la escritura en SGA.

Lee propiedades de la base, propiedades personalizadas y las filas pertinentes
de `PGPropiedades`. Solo devuelve nombres relacionados con artículos o secuencias
y valores numéricos o booleanos. Omite nombres sensibles y oculta otros valores.
No resuelve tablas vinculadas a otros archivos o servidores.

Requiere Java 8 o posterior; compilar requiere Maven y JDK 8 o posterior:

```sh
mvn -f gateway/access-probe/pom.xml clean package
timeout 15s java -jar gateway/access-probe/target/access-readonly-probe-1.0.0.jar /ruta/SGADataLoc.mdb
```

El archivo se abre expresamente con `setReadOnly(true)` y se cierra al terminar,
incluso ante errores. No solicita bloqueo exclusivo ni modifica el `.ldb`.
La prueba crea un MDB Jet 4 de muestra y comprueba SHA-256 idéntico tras la
inspección y ausencia de archivos de bloqueo nuevos.

Una lectura de un archivo activo no constituye una instantánea transaccional.
El resultado indica si cambiaron tamaño o fecha de modificación durante la
lectura, pero esos indicadores no demuestran consistencia completa. Un candidato
no confirma por sí solo el contador: `counter_confirmed` siempre es `false`.
Debe contrastarse con el alta nativa de SGA en una copia aislada.

## Límite de la escritura compartida

El requisito vigente es Linux directo, sin licencia comercial, con SGA abierto y
respetando los bloqueos nativos. Jackcess **no satisface la escritura concurrente**:
su documentación advierte que editar desde varias instancias o junto con Access
puede corromper el archivo. No añadir `updateRow`, `addRow` ni escrituras de
propiedades al archivo activo usando este inspector.

UCanAccess emplea Jackcess y su recarga de archivos modificados no certifica
compatibilidad con los bloqueos Jet de páginas y registros. MDBTools documenta
su escritura como experimental y advierte sobre las tablas con índices. Un
`flock`, mutex de Linux o archivo `.ldb` inventado tampoco coordina a los usuarios
Windows de SGA. No sustituir el requisito por acceso exclusivo si hay usuarios.

La propuesta inicial con Easysoft quedó descartada por requerir licencia
comercial. Este inspector no incluye esa dependencia ni instala controladores.
Falta validar un motor libre compatible con los bloqueos nativos antes de
implementar el asignador y habilitar el alta Odoo → SGA.

Fuentes primarias:

- [Jackcess: concurrencia y transacciones](https://jackcess.sourceforge.io/apidocs/com/healthmarketscience/jackcess/Database.html)
- [MDBTools: límites de escritura](https://github.com/mdbtools/mdbtools)
- [UCanAccess: parámetros de apertura](https://spannm.github.io/ucanaccess/20-getting-started.html)
- [DAO: apertura compartida](https://learn.microsoft.com/en-us/office/client-developer/access/desktop-database-reference/dbengine-opendatabase-method-dao)
- [DAO: bloqueo al actualizar](https://learn.microsoft.com/en-us/office/client-developer/access/desktop-database-reference/recordset-lockedits-property-dao)
