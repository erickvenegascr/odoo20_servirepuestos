# API del gateway v1

Todas las operaciones requieren `Authorization: Bearer <token>` salvo salud.
Los tokens no se envían como parámetros de URL. El archivo montado del gateway
contiene hashes de tokens y sus ámbitos; el token original se entrega al
servicio autorizado mediante un archivo de acceso separado.

| Método y ruta | Función |
| --- | --- |
| GET `/health/live/` | Proceso vivo, sin datos internos |
| GET `/health/ready/` | Conectividad con PostgreSQL del gateway |
| POST `/api/v1/operations/` | Persistir y procesar inmediatamente una solicitud inmutable |
| GET `/api/v1/operations/?limit=50` | Listar hasta 100 operaciones del ámbito autorizado |
| GET `/api/v1/operations/{id}/` | Leer estado y correspondencias |
| GET `/api/v1/operations/{id}/events/?after=0` | Leer hasta 200 eventos; continuar desde `next_after` |
| POST `/api/v1/operations/{id}/retry/` | Reintentar inmediatamente un bloqueo o reintento seguro; requiere rol retry |

El esquema está en `contracts/v1/operation.schema.json`. El digest de payload
es opcional para clientes de v1: el servidor lo calcula y, si se suministra,
exige coincidencia. Se usa JSON con claves ordenadas, separadores compactos,
UTF-8, sin NaN/Infinity. Para cantidades y monedas se recomiendan cadenas
decimales normalizadas hasta implementar los esquemas de cada acción.

La identidad de deduplicación es origen + operation_id + acción + revisión.
Un nuevo timestamp/evento de entrega puede repetir el mismo negocio. Cambiar
payload, actor declarado, empresa, agregado o precondición bajo esa identidad
produce 409. La identidad de evento de primera entrega también es única por
cliente. Una revisión nueva necesita otra identidad de evento.

- 201: solicitud nueva persistida y procesada en la misma llamada hasta el estado
  devuelto; **no implica aplicación en SGA**. Actualmente devuelve `BLOQUEADO`.
- 200 en reenvío: operación existente con el mismo contenido.
- 400: contrato inválido, hash incorrecto o fecha sin zona horaria.
- 401/403: autenticación, rol, origen o empresa no permitidos.
- 404: identidad no encontrada dentro del ámbito del cliente.
- 409: conflicto de identidad, revisión o estado de reintento.
- 413: solicitud superior a 256 KiB.
- 503: persistencia o procesamiento del gateway no disponible. Una solicitud
  puede haber quedado persistida antes de fallar el procesamiento: reenviar
  exactamente la misma identidad y contenido permite recuperar su resultado.

No es necesario consultar periódicamente para conocer el resultado de una
llamada que respondió correctamente. Consultar o reenviar la misma operación
resuelve una respuesta perdida. La API espera el procesamiento actual; no ofrece
un plazo máximo de aplicación nativa ni una garantía de transacción distribuida.

El cliente normal solo lee sus propias operaciones y empresas. Un operador
con `read_any` puede consultar otras operaciones, siempre dentro de sus
empresas explícitas. No existe un endpoint para asignar arbitrariamente
Sincronizado, activar escrituras nativas ni ejecutar SQL.

La bitácora identifica al servicio autenticado y al operador declarado por
Odoo. Esa declaración requiere que el addon preserve la identidad del usuario;
no demuestra por sí sola la historia de usuarios del sistema SGA.
