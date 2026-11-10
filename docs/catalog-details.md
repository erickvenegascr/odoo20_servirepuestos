# Categorización nueva, datos fiscales y tarifas

El complemento opcional `sga_gateway_catalog_details` está separado del receptor
base y de las fotografías. Su código canónico está en `odoo/optional_addons`;
en la compilación Odoo se distribuye como un addon normal. Requiere clasificación
y CRFE de Servirepuestos. Las pruebas nativas se ejecutan con esas dependencias.

Preparación: crear un caché privado JSON de consulta oficial CABYS con `source`
`https://api.hacienda.go.cr/fe/cabys` y un diccionario `codes`, cada entrada con
`status` (`found`, `not_found` o `rejected`), descripción si fue encontrada y la
respuesta de IVA como evidencia. El impuesto operacional proviene de SGA, no se
sustituye por la tasa del catálogo CABYS. Instalar el archivo como
`/etc/sga-gateway/cabys-official.json`, propietario root:sgagateway, modo 0640.
No contiene credenciales ni datos de clientes. El comando exige cobertura de
todos los códigos del catálogo actual antes de enviar cambios.

Aplicar migración 0005 exclusivamente en PostgreSQL del gateway, instalar el
addon receptor en Odoo y ejecutar:

```
python manage.py sync_catalog_details --cabys-file /etc/sga-gateway/cabys-official.json --pricing formula
python manage.py sync_catalog_details --apply --cabys-file /etc/sga-gateway/cabys-official.json --pricing formula
```

Sin `--apply` no se escribe el catálogo en Odoo. La auditoría local sí se guarda.
Cada ejecución recorre una instantánea consistente de SGA, en lotes de 100,
valida identidad de Odoo antes de abrir el origen y guarda el cursor solo después
de recibir la confirmación completa. Una repetición completa es idempotente.
El bloqueo por destino evita dos cargas simultáneas, incluso si usan estrategias
de precio distintas. Los errores no exponen tokens ni configuración privada.

La unidad `sga-gateway-catalog-details.service` es manual, sin temporizador. No
reconfigura el temporizador existente del catálogo base. La detección rápida por
PGTS y huellas persistentes sigue siendo una mejora independiente; no requiere
triggers ni escritura en SGA. Una nueva clave CABYS requiere ampliar el caché
validado antes de importar. Las escrituras de retorno hacia SGA siguen bloqueadas.

Las cifras iniciales verificadas por lectura de SGA el 10/11/2026 fueron 33.039
artículos, 33.019 precios en una lista Normal CRC, 9 divisiones originales, 71
categorías, 7 subcategorías y 273 marcas. Al conservar cada contexto por división,
el destino contiene 10 divisiones (incluida Sin división), 147 líneas y 21
subcategorías. Hay 28 artículos sin CABYS, 23 con nueve códigos no confirmados por
la API oficial y 20 sin regla de precio; deben permanecer visibles para revisión.
