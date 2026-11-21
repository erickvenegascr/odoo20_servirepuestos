CREATE DATABASE IF NOT EXISTS sga_retail_price_integration;

CREATE TABLE IF NOT EXISTS sga_retail_price_integration.retail_price_application (
    application_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    operation_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    article_id INT NOT NULL,
    level SMALLINT NOT NULL,
    before_snapshot MEDIUMTEXT NOT NULL,
    after_snapshot MEDIUMTEXT NOT NULL,
    result MEDIUMTEXT NOT NULL,
    created_utc DATETIME NOT NULL,
    PRIMARY KEY (application_uuid, operation_uuid)
) ENGINE=InnoDB;
