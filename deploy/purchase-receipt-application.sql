CREATE DATABASE IF NOT EXISTS sga_receipt_integration;

CREATE TABLE IF NOT EXISTS sga_receipt_integration.purchase_receipt_application (
    application_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    operation_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    invoice_key CHAR(50) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    vendor VARCHAR(3) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    invoice_number VARCHAR(20) CHARACTER SET latin1 COLLATE latin1_bin NOT NULL,
    recibo INT NOT NULL,
    result MEDIUMTEXT NOT NULL,
    created_utc DATETIME NOT NULL,
    PRIMARY KEY (application_uuid, operation_uuid),
    UNIQUE KEY receipt_fiscal_key (invoice_key),
    UNIQUE KEY receipt_vendor_invoice (vendor, invoice_number),
    UNIQUE KEY receipt_native_number (recibo)
) ENGINE=InnoDB;
