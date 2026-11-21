-- Optional migration for a writer with fiscalbot_bulk_enabled=true.
-- Install beside the existing purchase_order_receipt table in the integration schema.
CREATE TABLE sga_order_integration.purchase_order_fiscal_identity (
    application_uuid CHAR(36) NOT NULL,
    order_uuid CHAR(36) NOT NULL,
    odoo_order_id INT NOT NULL,
    fiscalbot_document_id INT NOT NULL,
    invoice_key CHAR(50) NOT NULL,
    vendor_code VARCHAR(3) NOT NULL,
    invoice_number VARCHAR(20) NOT NULL,
    PRIMARY KEY (application_uuid, order_uuid),
    UNIQUE KEY fiscal_order_electronic_invoice (invoice_key),
    UNIQUE KEY fiscal_order_vendor_invoice (vendor_code, invoice_number),
    UNIQUE KEY fiscal_order_odoo_order (application_uuid, odoo_order_id),
    UNIQUE KEY fiscal_order_odoo_document (application_uuid, fiscalbot_document_id),
    CONSTRAINT fiscal_order_receipt_fk FOREIGN KEY (application_uuid, order_uuid)
        REFERENCES sga_order_integration.purchase_order_receipt (application_uuid, order_uuid)
) ENGINE=InnoDB;

-- In addition to the existing order grants, the opted-in technical account needs:
-- GRANT SELECT, INSERT ON sga_order_integration.purchase_order_fiscal_identity TO 'sgagw_orders'@'localhost';
-- GRANT UPDATE (CantPedida) ON sgadata.artsservs TO 'sgagw_orders'@'localhost';
-- GRANT UPDATE (CantPedida) ON sgadata.articulosproveedor TO 'sgagw_orders'@'localhost';
-- GRANT SELECT (Proveedor, Factura) ON sgadata.recibos TO 'sgagw_orders'@'localhost';
-- GRANT SELECT (Proveedor, FacturaPrimaria) ON sgadata.cuentaspagar TO 'sgagw_orders'@'localhost';
-- GRANT SELECT (Proveedor, FacturaPrimaria, Acreedor, Factura) ON sgadata.cuentaspagardet TO 'sgagw_orders'@'localhost';
