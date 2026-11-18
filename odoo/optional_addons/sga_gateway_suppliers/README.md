# Proveedores por artículo de SGA

Importa `articulosproveedor` con proveedores identificados por su código textual SGA y artículos por su identidad del catálogo. Conserva ceros iniciales. `Prioridad` pasa sin transformación a `sequence`: menor número, mayor preferencia; cero es válido. Los empates no se renumeran.

El contrato solo importa relaciones, códigos, prioridad y las marcas de exclusión. No importa cotizaciones, saldos, cuentas bancarias ni condiciones de pago. Reutiliza contactos únicamente por identificación fiscal normalizada única, sin coincidencias aproximadas de nombre. Las identidades fiscales originales quedan en una instantánea privada para revisión; no se crean configuraciones fiscales mediante este contrato.

Las tarifas existentes conservan precio, moneda, unidad, descuentos y mínimos. Las relaciones nuevas muestran precio pendiente (cero) y quedan fuera de la selección automática hasta tener precio positivo. También se excluyen proveedores inactivos y relaciones deshabilitadas en SGA. En artículos SGA, la elección automática sin proveedor explícito respeta primero `sequence`, después el desempate de precios nativo de Odoo. La API no elimina relaciones ausentes: las bajas requieren una reconciliación explícita.

API de aplicación existente: `POST /sga_gateway/api/v1/catalog/suppliers/{info,batch}`. Mismo token, instancia y compañía autorizados para catálogo; sin acceso ORM genérico. Lotes de hasta 800 proveedores y 1000 relaciones, cuerpo máximo 512 KiB. Simulación sin escrituras; identidad única y huellas detectan cambios manuales en códigos/prioridad antes de sobrescribirlos. SGA permanece exclusivamente en lectura.
