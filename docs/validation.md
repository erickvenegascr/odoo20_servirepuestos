# Validación de la base del proyecto

Comprobaciones realizadas el 27 de octubre de 2026:

| Comprobación | Resultado |
| --- | --- |
| Imagen Docker | Construida; API sin root y sistema de archivos de solo lectura |
| PostgreSQL y API | Saludables en el proyecto local del gateway |
| Suite Django contra PostgreSQL real | 28 pruebas satisfactorias, sin omitir los casos concurrentes |
| Idempotencia concurrente | Una solicitud y un evento inicial para dos envíos simultáneos |
| Exclusión de trabajadores | Una sola toma de una operación por dos trabajadores |
| Separación de ámbito | Autenticación, origen, empresa y visibilidad por cliente comprobados |
| Fallo seguro | Sin secretos válidos no arranca; activar escrituras SGA por variable produce error |
| Prueba sintética HTTP | Reenvío devuelve la misma identidad; worker bloquea la aplicación nativa |
| Persistencia | Identidad, estado y tres eventos conservados tras reiniciar base/API/worker |
| Copia cifrada | pg_dump enviado directamente a age, sin dump sin cifrar en disco |
| Restauración | Identidad, estado y auditoría coinciden en una base nueva separada |
| Protección de restauración | Rechaza una base no vacía |
| Configuración cifrada | Descifra correctamente; hashes runtime coinciden y valores no aparecen en Git |
| Repositorio de secretos | Remoto privado; únicamente documentación, clave pública y configuración cifrada |
| Addon Odoo | Sintaxis Python y XML revisadas; no instalado en una instancia Odoo 20 para esta entrega |

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
