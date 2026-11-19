-- Provision in a NEW integration database on the same MariaDB server as SGA.
-- MariaDB 5.5 compatible. Do not run this file against the SGA schema.
CREATE TABLE configuration (
 singleton TINYINT NOT NULL PRIMARY KEY,
 contract_version INT NOT NULL,
 source_instance VARCHAR(80) NOT NULL,
 source_database VARCHAR(64) NOT NULL
) ENGINE=InnoDB;
-- Replace BOTH markers with the verified source identity before provisioning.
INSERT INTO configuration VALUES (1,2,'CONFIGURE_SOURCE_INSTANCE','CONFIGURE_SOURCE_DATABASE');
CREATE TABLE allocator_lock (
 lock_name VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY
) ENGINE=InnoDB;
INSERT INTO allocator_lock VALUES ('article');
CREATE TABLE article_receipt (
 application_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 operation_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 result LONGTEXT CHARACTER SET utf8 NOT NULL,
 created_utc DATETIME NOT NULL,
 PRIMARY KEY (application_uuid,operation_uuid)
) ENGINE=InnoDB;
CREATE TABLE article_binding (
 application_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 product_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 article_id INT NOT NULL,
 created_at_sga DATETIME NULL,
 origin_operation_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
 PRIMARY KEY (application_uuid,product_uuid),
 UNIQUE KEY article_application (application_uuid,article_id),
 KEY article_id (article_id)
) ENGINE=InnoDB;
