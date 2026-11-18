"""Direct Linux → Access/Jet-format storage through Easysoft ODBC-ACCESS.

No Windows agent, Access.Application automation, file-format patching, generic
SQL endpoint, or article-number allocation is implemented here. The numeric
property API only operates on an explicitly configured PGPropiedades row.
"""
from contextlib import contextmanager, suppress
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


DRIVER = "Easysoft ODBC-ACCESS"
READ_SQL = "SELECT [Valor] FROM [PGPropiedades] WHERE [Nombre] = ?"
UPDATE_SQL = "UPDATE [PGPropiedades] SET [Valor] = ? WHERE [Nombre] = ? AND [Valor] = ?"
MAX_INT = 2147483647
MAX_BYTES = 16384


class AccessJetError(Exception):
    def __init__(self, code, *, outcome_unknown=False):
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


def read_private_file(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = handle.read(MAX_BYTES + 1)
        if len(value) > MAX_BYTES:
            raise ValueError()
        return value.rstrip("\r\n")
    except (OSError, ValueError):
        raise AccessJetError("ACCESS_PRIVATE_FILE_UNAVAILABLE") from None


def load_config(path):
    try:
        config = json.loads(read_private_file(path))
    except (ValueError, TypeError):
        raise AccessJetError("ACCESS_INVALID_CONFIG") from None
    validate_config(config)
    return config


def validate_config(config):
    fields = {"mdb_file", "smb_path", "smb_library", "smb_user", "smb_password_file",
              "timeout_seconds", "numeric_properties"}
    if not isinstance(config, dict) or set(config) - fields:
        raise AccessJetError("ACCESS_INVALID_CONFIG")
    for key in fields - {"numeric_properties", "timeout_seconds"}:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise AccessJetError("ACCESS_INVALID_CONFIG")
    file = Path(config["mdb_file"])
    try:
        url = urlsplit(config["smb_path"])
    except ValueError:
        raise AccessJetError("ACCESS_INVALID_SMB_TARGET") from None
    if (not file.is_absolute() or file.suffix.lower() != ".mdb"
            or not Path(config["smb_library"]).is_absolute()
            or not Path(config["smb_password_file"]).is_absolute()
            or url.scheme != "smb" or not url.hostname or url.username or url.password
            or url.query or url.fragment or len(url.path.strip("/").split("/")) < 2
            or Path(unquote(url.path)).name != file.name):
        raise AccessJetError("ACCESS_INVALID_SMB_TARGET")
    timeout = config.get("timeout_seconds", 5)
    if type(timeout) is not int or not 1 <= timeout <= 10:
        raise AccessJetError("ACCESS_INVALID_TIMEOUT")
    names = config.get("numeric_properties", [])
    if (not isinstance(names, list) or len(names) > 10
            or any(not isinstance(n, str) or not 1 <= len(n) <= 50
                   or re.search(r"[\x00-\x1f]", n) for n in names)
            or len(set(names)) != len(names)):
        raise AccessJetError("ACCESS_INVALID_PROPERTY_ALLOWLIST")


def quoted(value):
    return "{" + value.replace("}", "}}") + "}"


def connection_string(config, *, write=False):
    validate_config(config)
    values = {"Driver": DRIVER, "mdbfile": config["mdb_file"],
              "smbpath": config["smb_path"], "smblib": config["smb_library"],
              "smbuser": config["smb_user"],
              "smbauth": read_private_file(config["smb_password_file"]),
              "readonly": "no" if write else "yes", "exclusive": "no",
              "unicode_map": "2"}
    # Deliberately no DSN, custom lockfile, exclusive mode or raw extra attributes.
    return ";".join(key + "=" + quoted(value) for key, value in values.items())


def driver_module():
    if sys.platform != "linux":
        raise AccessJetError("ACCESS_DIRECT_ADAPTER_REQUIRES_LINUX")
    try:
        import pyodbc
    except ImportError:
        raise AccessJetError("ACCESS_PYODBC_NOT_INSTALLED") from None
    if DRIVER not in pyodbc.drivers():
        raise AccessJetError("ACCESS_EASYSOFT_DRIVER_NOT_INSTALLED")
    # This optional adapter must run in its own worker, before the first ODBC
    # connection. Retaining pooled handles would defeat close-after-operation.
    pyodbc.pooling = False
    return pyodbc


@contextmanager
def session(config, *, write=False):
    validate_config(config)
    connection = cursor = None
    try:
        driver = driver_module()
        connection = driver.connect(connection_string(config, write=write),
                                    readonly=not write, autocommit=not write,
                                    timeout=config.get("timeout_seconds", 5))
        connection.timeout = config.get("timeout_seconds", 5)
        expected_mode = "N" if write else "Y"
        if connection.getinfo(driver.SQL_DATA_SOURCE_READ_ONLY) != expected_mode:
            raise AccessJetError("ACCESS_MODE_NOT_CONFIRMED")
        # Refuse a redirected/wrong database rather than updating an identically
        # named property in another company or a development copy.
        actual = connection.getinfo(driver.SQL_DATABASE_NAME)
        if not isinstance(actual, str) or Path(actual).resolve() != Path(config["mdb_file"]).resolve():
            raise AccessJetError("ACCESS_DATABASE_MISMATCH")
        if write:
            if connection.autocommit or connection.getinfo(driver.SQL_TXN_CAPABLE) not in (1, 2, 3, 4):
                raise AccessJetError("ACCESS_TRANSACTIONS_REQUIRED")
            # Warnings about missing SMB coordination are fatal before any DML.
            if any("smb" in str(message).lower() or "corrupt" in str(message).lower()
                   for message in getattr(connection, "messages", [])):
                raise AccessJetError("ACCESS_SMB_LOCKING_NOT_CONFIRMED")
        cursor = connection.cursor()
        yield connection, cursor
    except AccessJetError:
        raise
    except Exception:
        raise AccessJetError("ACCESS_CONNECTION_OR_QUERY_FAILED") from None
    finally:
        if cursor is not None:
            with suppress(Exception):
                cursor.close()
        if connection is not None:
            with suppress(Exception):
                connection.close()


def checked_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value):
        raise AccessJetError("ACCESS_VALUE_IS_NOT_A_NUMERIC_COUNTER")
    number = int(value)
    if number > MAX_INT:
        raise AccessJetError("ACCESS_COUNTER_OUT_OF_RANGE")
    return number


def read_row(cursor, name):
    cursor.execute(READ_SQL, name)
    rows = cursor.fetchmany(2)
    if len(rows) != 1:
        raise AccessJetError("ACCESS_PROPERTY_NOT_UNIQUE_OR_MISSING")
    value = rows[0][0]
    checked_number(value)
    return value


def compare_and_set_numeric_property(config, name, expected, replacement, *, apply=False):
    """Compare exact stored text and update one configured numeric property.

    This is not a certified SGA article allocator. A changed value is a conflict,
    including after a lost response: it must not be assumed to be our own write.
    No retries, MAX+1, decrements, inserts, deletes, DDL or lock-file manipulation.
    """
    validate_config(config)
    if name not in config.get("numeric_properties", []):
        raise AccessJetError("ACCESS_PROPERTY_NOT_ALLOWED")
    if checked_number(replacement) <= checked_number(expected):
        raise AccessJetError("ACCESS_COUNTER_MUST_ADVANCE")
    if type(apply) is not bool:
        raise AccessJetError("ACCESS_INVALID_APPLY_FLAG")
    with session(config, write=apply) as (connection, cursor):
        mutation_started = commit_started = committed = False
        try:
            if read_row(cursor, name) != expected:
                raise AccessJetError("ACCESS_COUNTER_CONFLICT")
            if not apply:
                return {"status": "simulated", "property": name, "expected": expected,
                        "replacement": replacement, "business_writes": False}
            mutation_started = True
            cursor.execute(UPDATE_SQL, replacement, name, expected)
            if cursor.rowcount != 1:
                raise AccessJetError("ACCESS_COUNTER_CONFLICT_OR_UNKNOWN_ROWCOUNT")
            if read_row(cursor, name) != replacement:
                raise AccessJetError("ACCESS_COUNTER_VERIFICATION_FAILED")
            commit_started = True
            connection.commit()
            committed = True
            return {"status": "committed", "property": name, "expected": expected,
                    "replacement": replacement, "business_writes": True,
                    "article_created": False}
        except Exception as exc:
            rollback_failed = False
            if apply and not committed:
                try:
                    connection.rollback()
                except Exception:
                    rollback_failed = True
            if commit_started or (mutation_started and rollback_failed):
                raise AccessJetError("ACCESS_RESULT_UNKNOWN", outcome_unknown=True) from None
            if isinstance(exc, AccessJetError):
                raise
            raise AccessJetError("ACCESS_UPDATE_ROLLED_BACK") from None


def inspect_numbering(config):
    """Bounded SELECT of likely parameter names; DAO custom properties are unknown."""
    sql = ("SELECT [Nombre], [Valor] FROM [PGPropiedades] WHERE "
           "[Nombre] LIKE '%Art%' OR [Nombre] LIKE '%Consecut%' OR "
           "[Nombre] LIKE '%Secuenc%' OR [Nombre] LIKE '%ProxNum%' ORDER BY [Nombre]")
    with session(config) as (_, cursor):
        cursor.execute(sql)
        records = cursor.fetchmany(65)
        candidates = []
        for name, value in records[:64]:
            if not isinstance(name, str) or re.search(r"password|clave|token|secret|encript", name, re.I):
                continue
            try:
                checked_number(value)
                safe_value = value
            except AccessJetError:
                safe_value = None
            candidates.append({"name": name, "value": safe_value,
                               "value_redacted": value is not None and safe_value is None})
        return {"status": "inspected", "business_writes": False,
                "counter_confirmed": False, "atomic_snapshot": False,
                "dao_properties_inspected": False, "candidates": candidates,
                "truncated": len(records) > 64}
