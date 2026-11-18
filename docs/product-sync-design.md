# Alta y sincronización bidireccional de productos

Estado: asignador confirmado y adaptador opcional implementado y probado con datos ficticios; alta y edición Odoo → SGA aún no habilitadas en producción. El lector Access y el prototipo Jet privado no constituyen un adaptador comercial de productos.

## Alcance acordado

Odoo inicia el alta y puede enviar cambios de nombre, referencias, códigos y clasificación. SGA conserva el control de precios y costos. Las existencias cambian por sus documentos de inventario. La integración debe poder desactivarse conservando productos y referencias en Odoo.

La base de los cambios es el `main` remoto de este repositorio y de `erickvenegascr/v20_servirepuestos_motos`. Las correcciones de Odoo desplegadas hasta el PR #26 fueron incorporadas a su `main` mediante el PR #28.

## Asignador nativo

La revisión de los programas instalados el 18 de noviembre de 2026 identifica esta ruta:

1. `Edicion ArtsServs.Form_BeforeInsert` consulta `articulo secuencial`.
2. Si está activado, llama a `PGBiblio.PGProxNum("ArtsServs", "articulo")`.
3. La función consulta el máximo de la columna y devuelve `Nz(mx, 0) + 1`.

```sql
SELECT COALESCE(MAX(Articulo), 0) + 1 FROM artsservs;
```

Se resolvieron los argumentos, los textos SQL y los handles de los métodos del compilado. Se contrastaron 109 descriptores del formulario con 108 referencias propias, y 43 descriptores del módulo `Interfaz Datos` con 22 referencias propias. La llamada a `Nz` corresponde a la entrada virtual 300 de `_Application` en la biblioteca de tipos real de Access 2003. La suma de uno se contrastó con el intérprete VBE6 instalado.

Es una traza estática de la ruta de asignación, no una ejecución de un alta real ni una reconstrucción completa del programa. Los binarios y las trazas completas permanecen en el directorio local de análisis.

El siguiente número se deriva del catálogo persistido. `Articulo Secuencial` es una opción de generación. `pgconsecutivos` contiene contadores de documentos y no es el asignador de artículos de esta ruta. Durante la coexistencia no debe introducirse otra secuencia independiente en Odoo.

`Articulo` es la identidad numérica. `NombreCorto` es la referencia comercial que el conector importa como `default_code`. Se conservan ambos datos; el formato de seis dígitos del artículo no sustituye la referencia comercial.

## Escritura y recuperación

El catálogo y sus referencias comprobadas están en MariaDB. El cálculo del número descrito no requiere actualizar un contador Jet. El adaptador debe realizar transacciones cortas y cerrar la conexión al terminar, con espera por bloqueos acotada.

Flujo requerido:

1. Guardar producto y solicitud durable en Odoo, con UUID y valores inmutables.
2. Validar origen, compañía, longitudes, clasificación y ausencia de aplicación previa.
3. Asignar e insertar el artículo y los registros auxiliares necesarios en una transacción de MariaDB.
4. Conservar en ese mismo commit un recibo con UUID, checksum y claves nativas.
5. Leer la confirmación y vincular el producto Odoo original.
6. Hacer que el catálogo entrante adopte esa vinculación incluso si llega antes del ack.

Un recibo guardado únicamente en el PostgreSQL del gateway no hace atómico el commit con SGA. El recibo de aplicación debe participar en la transacción MariaDB; puede estar en un esquema propio del adaptador con tablas InnoDB.

La lectura de `MAX` no reserva el código. La clave primaria detecta colisiones; hay que validar rollback, reintentos y conciliación de commits con respuesta perdida. Un mutex del gateway no coordina por sí solo las sesiones nativas de SGA.

## Cambios en ambos sentidos

Cada vínculo debe conservar origen, compañía, identidad Odoo, artículo SGA y últimos valores confirmados por campo.

| Cambios frente a la última confirmación | Tratamiento |
| --- | --- |
| Solo Odoo | Enviar los campos autorizados a SGA |
| Solo SGA | Actualizar Odoo |
| Ambos tienen el mismo valor nuevo | Confirmar sin volver a emitirlo |
| Ambos tienen valores nuevos diferentes | Conservar un conflicto para resolverlo |

Los UUID y revisiones impiden duplicados y ciclos. La adopción se hace por el vínculo durable, no por coincidencia de nombre o referencia corta. Los mensajes de ficha no pueden escribir precios, costos ni cantidades de existencias.

## Restricciones verificadas y validaciones pendientes

- `ArticuloNomb`: hasta 70 caracteres; `NombreCorto`: hasta 20. No truncar silenciosamente.
- `articuloscodbarras`: clave `(Articulo, Tipo)`, código de hasta 15 caracteres. Los tipos documentados son 0 por mayor y 1 detalle; no representa una lista ilimitada de códigos.
- `artsservsrefer`: división y marca de tres caracteres, categoría y subcategoría numéricas; se deben validar las correspondencias y la jerarquía.
- El alta contiene lógica posterior en `Form_AfterInsert`, incluidos subformularios de bodegas y códigos y la auditoría `PGActBitac`. La envoltura `PGDataRecordset` expone `AddNew`, `Update` y `CloseRec`. Falta validar qué datos deben persistirse para cada selección de la ficha.
- Antes de activar el adaptador se necesita una comparación en una copia aislada: alta, edición, auxiliares, clasificación, códigos, auditoría, colisiones, reintentos y pérdida de respuesta.

La modificación experimental de Memo del gateway pasó una lectura independiente con DAO 3.6 sobre una base sintética: valor esperado, búsqueda por clave primaria, demás filas conservadas y hash sin cambios durante la lectura. Esa prueba no certifica bloqueos de escritura Jet compartidos ni un alta comercial.

El módulo de flujo Odoo debe mantenerse separado del adaptador SGA. Desconectar el adaptador exige resolver su cola y conservar productos, códigos y correspondencias. La herramienta Java de precios permanece fuera de estas escrituras de ficha.


## Implementación opcional

El módulo Odoo y el escritor de fichas están implementados, deshabilitados por defecto. La guía [product-sync-adapter.md](product-sync-adapter.md) describe el contrato, los permisos por columna, las pruebas y los límites. La certificación con el formulario nativo y su auditoría sigue pendiente antes de activar producción.
