# Motores Jet publicados en GitHub y escritura compartida en SGA

Investigación del 18 de noviembre de 2026. Requisito vigente: escribir desde Linux
sin licencia comercial, con SGA abierto, respetando los bloqueos nativos y sin
interrumpir la herramienta Java de precios.

## Resultado

Los proyectos encontrados no permiten habilitar ese modo de escritura. Esta
investigación no implementa un escritor ni habilita escrituras en producción.
El PR contiene un inspector de solo lectura. Ningún proyecto revisado se instaló
ni se ejecutó contra la base de SGA durante esta investigación.

| Proyecto | Capacidad documentada | Límite para SGA abierto |
| --- | --- | --- |
| Microsoft ESE / Jet Blue | Motor publicado por Microsoft | Es una implementación distinta de Jet Red, el motor de Access. No son intercambiables |
| LibRed | Implementación libre que lee y escribe MDB/ACCDB desde Linux | Declara un solo escritor y ausencia del protocolo de bloqueos `.ldb`/`.laccdb`; advierte de corrupción con escritores concurrentes |
| JetDatabaseWriter | Lectura/escritura libre, transacciones y bloqueos entre procesos que cooperan | Su contrato exige un escritor exclusivo durante toda su sesión; los bloqueos por página no sustituyen esa exclusividad |
| Jackcess | Lectura/escritura del formato Access | No ofrece transacciones y advierte de corrupción al editar junto con Access |

## Fuentes y revisiones

Microsoft explica en su [repositorio ESE](https://github.com/microsoft/Extensible-Storage-Engine)
que Jet Blue y el Jet Red de Access son implementaciones diferentes. La
[documentación de Microsoft](https://learn.microsoft.com/en-us/windows/win32/extensible-storage-engine/extensible-storage-engine)
también dice que no son intercambiables. Referencia `main` observada:
`7030fe7407615160e54d152e4ef704eede2fdd7e`.

El [README de LibRed](https://github.com/CirrusRedOrg/EntityFrameworkCore.Jet/blob/460a46054f83169a9c6752d21cf5fdd0f608479d/src/LibRed/README.md),
en el apartado de concurrencia, distingue admitir varios handles abiertos de
coordinar varias escrituras. Abrir con `FileShare.ReadWrite` no implementa la
coordinación nativa. Referencia `master` observada:
`460a46054f83169a9c6752d21cf5fdd0f608479d`.

Se revisaron el [contrato de JetDatabaseWriter](https://github.com/symbiogenesis/JetDatabaseWriter/blob/b0ea07337be6663253651cc3dbf7f07e70c6d379/docs/support-contract.md),
su [guía de operación](https://github.com/symbiogenesis/JetDatabaseWriter/blob/b0ea07337be6663253651cc3dbf7f07e70c6d379/docs/operations.md)
y el código de `Transactions/JetByteRangeLock.cs` y
`Transactions/LockFileCoordinator.cs`. La guía requiere conservar la exclusividad
también entre llamadas. El diario de recuperación es propio y Access no lo
reproduce. `UseLockFile` y `RespectExistingLockFile` deben permanecer activos;
ignorar el `.ldb` no habilita una modalidad compartida admitida. Referencia
`main` observada: `b0ea07337be6663253651cc3dbf7f07e70c6d379`.

La [API de Jackcess](https://jackcess.sourceforge.io/apidocs/com/healthmarketscience/jackcess/Database.html)
mantiene la advertencia sobre edición concurrente. El inspector existente lo
abre expresamente en modo de solo lectura.

## Implicaciones de implementación

1. Mantener deshabilitado el escritor de producción hasta disponer de un motor
   compatible. En el código actual `SGA_WRITES_ENABLED=1` hace fallar la carga de
   configuración: no selecciona ni instala un adaptador.
2. Un mutex, `flock`, conexión breve o cierre correcto de recursos no coordina
   por sí solo las escrituras de los usuarios Access a través de Samba. Una
   prueba entre dos procesos de la misma biblioteca tampoco prueba esa convivencia.
3. No borrar ni ignorar el `.ldb`, forzar sesiones, reemplazar el MDB activo
   por una copia editada ni adoptar acceso exclusivo como sustituto del requisito.
4. La lectura anterior identificó `Articulo Secuencial=true`, no el contador ni
   el algoritmo nativo. El alta Odoo → SGA también necesita resolver ese asignador
   y sus efectos auxiliares antes de implementar una reserva de números.
5. La herramienta Java de precios conserva su runtime y proceso. Esta
   investigación no cambia Java, Tomcat, servicios ni configuración de Samba.

Para avanzar al alta hay que resolver una incompatibilidad concreta del diseño:
usar un motor con concurrencia Jet compatible, o cambiar la condición de acceso.
Las bibliotecas libres encontradas requieren serializar/excluir a los demás
escritores; no cumplen actualmente la modalidad compartida solicitada.
