# Fotografías SGA por token de aplicación

Complemento de `sga_gateway_connector` y `product_image_gallery`. Recibe la foto
principal y todas las secundarias sin instalar eCommerce. Conserva identidades
por instancia, compañía, artículo y nombre de archivo, junto con SHA-256 del
original y de la versión normalizada por Odoo. Las ediciones locales de imagen
producen conflicto antes de sobrescribirse; las fotos manuales se conservan.

POST autenticados con el mismo token de catálogo:

- `/sga_gateway/api/v1/media/check`: hasta 200 metadatos; pide únicamente los
  archivos nuevos/cambiados y reconoce los ya importados sin transferirlos.
- `/sga_gateway/api/v1/media/image`: un archivo de hasta 6 MiB; JPEG, PNG, WebP
  o BMP, máximo 40 megapíxeles. El cuerpo JSON tiene límite de 10 MiB.

El contrato fija compañía y origen desde la aplicación autenticada. No admite
modelos ni contextos del cliente. `info` anuncia `media_contract: 1` solo cuando
el complemento está instalado. No tiene ninguna interfaz de escritura SGA.

Las imágenes usan `BinaryValue` nativo de Odoo 20. Los checksums separan los
bytes originales de la imagen redimensionada para que la normalización nativa
no invalide la idempotencia. Los reintentos de serialización siguen a cargo de
Odoo y cada cambio se confirma en una transacción, con bloqueo por artículo.
