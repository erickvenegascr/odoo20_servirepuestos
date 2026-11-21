CREATE DATABASE IF NOT EXISTS sga_supplier_integration;

CREATE TABLE IF NOT EXISTS sga_supplier_integration.supplier_relation_application (
    application_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    operation_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    article_id INT NOT NULL,
    vendor_code VARCHAR(3) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    result MEDIUMTEXT NOT NULL,
    created_utc DATETIME NOT NULL,
    PRIMARY KEY (application_uuid, operation_uuid)
) ENGINE=InnoDB;

-- Template for a separate runtime account; provisioning is independent of this DDL.
-- GRANT SELECT (Articulo,Proveedor,CodigoArticulo,Prioridad,Deshabilitado),
--       UPDATE (CodigoArticulo,Prioridad,Usuario,Estacion,FechaActualizacion)
--       ON sgadata.articulosproveedor TO 'sgagw_supplier_updates'@'localhost';
-- GRANT SELECT (Proveedor,Activo) ON sgadata.proveedores TO 'sgagw_supplier_updates'@'localhost';
-- GRANT SELECT (Articulo,Trabajado) ON sgadata.artsservs TO 'sgagw_supplier_updates'@'localhost';
-- GRANT SELECT,INSERT ON sga_supplier_integration.supplier_relation_application
--       TO 'sgagw_supplier_updates'@'localhost';
