"""Load the optional supplier relation writer independently of other adapters."""

import os
from pathlib import Path

from .catalog_source import configuration
from .supplier_update_sql import SupplierUpdateSql


def configured_supplier_writer(client):
    if not os.environ.get('SGA_SUPPLIER_WRITER_FILE'):
        return None
    config = configuration('SGA_SUPPLIER_WRITER')
    if config.get('enabled') is not True:
        return None
    source_file = os.environ.get('SGA_CATALOG_SOURCE_FILE')
    if not source_file or not Path(source_file).is_absolute():
        raise ValueError('Supplier update writer requires a pinned SELECT-source configuration')
    return SupplierUpdateSql(config, configuration('SGA_CATALOG_SOURCE'), client.config.get('application_id'))
