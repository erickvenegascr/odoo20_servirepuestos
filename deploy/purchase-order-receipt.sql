CREATE DATABASE IF NOT EXISTS sga_order_integration;

CREATE TABLE sga_order_integration.purchase_order_receipt (
    application_uuid CHAR(36) NOT NULL,
    order_uuid CHAR(36) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    pedido INT NOT NULL,
    result MEDIUMTEXT NOT NULL,
    created_utc DATETIME NOT NULL,
    PRIMARY KEY (application_uuid, order_uuid),
    UNIQUE KEY purchase_order_native_identity (pedido)
) ENGINE=InnoDB;
