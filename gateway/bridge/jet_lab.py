"""Own Jet4 in-place numeric MEMO writer, confined to a newly created lab copy.

The file lock below coordinates ONLY this laboratory. It is NOT Jet shared
locking, does NOT certify native interoperability, and never opens SGA to write.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import time
import uuid
from .access_gateway import AccessGateway

PAGE = 4096
MAX_SIZE = 64 * 1024 * 1024
MARKER = 'servirepuestos-jet4-lab-v1'


class JetRefusal(Exception):
    pass


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _hash_fd(fd, skip_page=None):
    digest = hashlib.sha256()
    for offset in range(0, os.fstat(fd).st_size, PAGE):
        if skip_page is None or offset != skip_page * PAGE:
            digest.update(os.pread(fd, PAGE, offset))
    return digest.hexdigest()


def _sync_directory(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _json(root, name, value):
    temporary = root / ('.pending-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / name)
        _sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def create_lab(source, root, gateway):
    """Create a new private copy. Existing directories are never adopted."""
    source, root = Path(source).resolve(strict=True), Path(root)
    if source.suffix.lower() != '.mdb':
        raise JetRefusal('MDB_REQUIRED')
    root.mkdir(mode=0o700)  # Fail if it exists; no overwrite or adoption.
    target = root / 'data.mdb'
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode) or not PAGE <= initial.st_size <= MAX_SIZE:
            raise JetRefusal('LAB_SIZE_UNSUPPORTED')
        with os.fdopen(os.dup(fd), 'rb') as incoming, target.open('xb') as outgoing:
            os.chmod(target, 0o600)
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        final = os.fstat(fd)
        if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise JetRefusal('SOURCE_CHANGED')
        with target.open('rb') as copied:
            header = copied.read(PAGE)
        if header[4:20] != b'Standard Jet DB\0' or header[20:24] != b'\x01\0\0\0':
            raise JetRefusal('ONLY_JET4_SUPPORTED')
        layout = gateway.lab_layout(target)
        if layout['value_indexed'] or not layout['raw_pages']:
            raise JetRefusal('LAYOUT_UNSUPPORTED')
        columns = layout['columns']
        if sorted(col['number'] for col in columns) != list(range(len(columns))):
            raise JetRefusal('DELETED_COLUMNS_UNSUPPORTED')
        lookup = {col['name']: col for col in columns}
        if (lookup['Nombre']['type'], lookup['Valor']['type']) != ('TEXT', 'MEMO'):
            raise JetRefusal('LAYOUT_UNSUPPORTED')
        copied_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            tdef = os.pread(copied_fd, PAGE, layout['table_definition_page'] * PAGE)
            if len(tdef) != PAGE or tdef[:2] != b'\x02\x01':
                raise JetRefusal('INVALID_TABLE_DEFINITION')
            manifest = {'marker': MARKER, 'layout': layout, 'source_sha256': _hash_fd(copied_fd),
                        'table_definition_sha256': _digest(tdef), 'source_snapshot_certified': False}
        finally:
            os.close(copied_fd)
        (root / 'receipts').mkdir(mode=0o700)
        _json(root, 'manifest.json', manifest)
        return {'status': 'lab_created', 'production_writer': False}
    finally:
        os.close(fd)


@contextmanager
def _session(root, timeout=2):
    root = Path(root)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise JetRefusal('PRIVATE_LAB_REQUIRED')
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('marker') != MARKER:
        raise JetRefusal('LAB_MARKER_REQUIRED')
    lock = os.open(root / 'lab.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fd = None
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JetRefusal('LAB_BUSY')
                time.sleep(0.02)
        fd = os.open(root / 'data.mdb', os.O_RDWR | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not PAGE <= info.st_size <= MAX_SIZE:
            raise JetRefusal('LAB_FILE_REQUIRED')
        layout = manifest['layout']
        if _digest(os.pread(fd, PAGE, layout['table_definition_page'] * PAGE)) != manifest['table_definition_sha256']:
            raise JetRefusal('SCHEMA_CHANGED')
        yield root, fd, manifest
    finally:
        if fd is not None:
            os.close(fd)
        os.close(lock)  # Releases this lab's lock on every exit.


def _variable(row, column, column_count):
    if len(row) < 2 or struct.unpack_from('<H', row)[0] != column_count:
        raise JetRefusal('ROW_LAYOUT_UNSUPPORTED')
    null_size = (column_count + 7) // 8
    if len(row) < 4 + null_size:
        raise JetRefusal('ROW_TRUNCATED')
    number = column['number']
    if not row[-null_size + number // 8] & (1 << (number % 8)):
        return None
    count_position = len(row) - null_size - 2
    count = struct.unpack_from('<H', row, count_position)[0]
    index = column['variable_index']
    if not column['variable'] or not 0 <= index < count <= column_count:
        raise JetRefusal('VARIABLE_LAYOUT_UNSUPPORTED')
    table_position = count_position - 2 * (count + 1)
    if table_position < 2:
        raise JetRefusal('ROW_TRUNCATED')
    offsets = [struct.unpack_from('<H', row, count_position - 2 * (i + 1))[0]
               for i in range(count + 1)]
    if offsets != sorted(offsets) or offsets[0] < 2 or offsets[-1] != table_position:
        raise JetRefusal('VARIABLE_BOUNDS_INVALID')
    return offsets[index], offsets[index + 1]


def _text(raw):
    if raw.startswith(b'\xff\xfe'):
        if any(byte < 32 or byte > 126 for byte in raw[2:]):
            raise JetRefusal('COMPRESSED_TEXT_UNSUPPORTED')
        return raw[2:].decode('ascii')
    try:
        return raw.decode('utf-16le')
    except UnicodeError as error:
        raise JetRefusal('TEXT_ENCODING_UNSUPPORTED') from error


def _plan(fd, layout, name, expected, replacement):
    columns = {col['name']: col for col in layout['columns']}
    candidate = None
    for page_number in range(1, os.fstat(fd).st_size // PAGE):
        page = os.pread(fd, PAGE, page_number * PAGE)
        if page[:2] != b'\x01\x01' or struct.unpack_from('<I', page, 4)[0] != layout['table_definition_page']:
            continue
        count = struct.unpack_from('<H', page, 12)[0]
        if count > (PAGE - 14) // 2:
            raise JetRefusal('ROW_DIRECTORY_INVALID')
        end = PAGE
        for index in range(count):
            entry = struct.unpack_from('<H', page, 14 + index * 2)[0]
            if entry & 0xE000:
                raise JetRefusal('DELETED_OR_OVERFLOW_ROWS_UNSUPPORTED')
            start = entry & 0x1FFF
            if not 14 + 2 * count <= start < end <= PAGE:
                raise JetRefusal('ROW_DIRECTORY_INVALID')
            row = page[start:end]
            end = start
            key = _variable(row, columns['Nombre'], len(columns))
            if key is None or _text(row[slice(*key)]) != name:
                continue
            if candidate is not None:
                raise JetRefusal('DUPLICATE_PROPERTY')
            value = _variable(row, columns['Valor'], len(columns))
            if value is None:
                raise JetRefusal('NULL_VALUE_UNSUPPORTED')
            field = row[slice(*value)]
            if len(field) < 12:
                raise JetRefusal('MEMO_TRUNCATED')
            descriptor = struct.unpack_from('<I', field)[0]
            if descriptor & 0xC0000000 != 0x80000000 or descriptor & 0x3FFFFFFF != len(field) - 12:
                raise JetRefusal('EXTERNAL_MEMO_UNSUPPORTED')
            raw = field[12:]
            if _text(raw) != expected:
                raise JetRefusal('EXPECTED_VALUE_CONFLICT')
            encoded = ((b'\xff\xfe' + replacement.encode('ascii')) if raw.startswith(b'\xff\xfe')
                       else replacement.encode('utf-16le'))
            if len(encoded) != len(raw):
                raise JetRefusal('VALUE_GROWTH_UNSUPPORTED')
            position = start + value[0] + 12
            after = page[:position] + encoded + page[position + len(raw):]
            candidate = (page_number, page, after)
    if candidate is None:
        raise JetRefusal('PROPERTY_NOT_FOUND')
    return candidate


def _write_page(fd, page, data):
    written = 0
    while written < PAGE:
        count = os.pwrite(fd, data[written:], page * PAGE + written)
        if count <= 0:
            raise OSError('SHORT_WRITE')
        written += count
    os.fsync(fd)


def _recover(root, fd):
    path = root / 'transaction.json'
    if not path.exists():
        return False
    if path.stat().st_size > 20000:
        raise JetRefusal('RECOVERY_LOG_INVALID')
    log = json.loads(path.read_text())
    info = os.fstat(fd)
    if (type(log.get('page')) is not int or not 1 <= log['page'] < info.st_size // PAGE
            or str(uuid.UUID(log['operation_id'])) != log['operation_id']):
        raise JetRefusal('RECOVERY_LOG_INVALID')
    if ([info.st_dev, info.st_ino, info.st_size] != log['identity']
            or _hash_fd(fd, log['page']) != log['outside_sha256']):
        raise JetRefusal('RECOVERY_IDENTITY_CONFLICT')
    before = base64.b64decode(log['before'], validate=True)
    if len(before) != PAGE or _digest(before) != log['before_sha256']:
        raise JetRefusal('RECOVERY_LOG_INVALID')
    if log['state'] == 'prepared':
        _write_page(fd, log['page'], before)
    elif log['state'] == 'committed':
        if _digest(os.pread(fd, PAGE, log['page'] * PAGE)) != log['after_sha256']:
            raise JetRefusal('RECOVERY_COMMIT_CONFLICT')
        receipt_path = root / 'receipts' / (log['operation_id'] + '.json')
        if receipt_path.exists() and json.loads(receipt_path.read_text()) != log['receipt']:
            raise JetRefusal('RECEIPT_CONFLICT')
        _json(root / 'receipts', receipt_path.name, log['receipt'])
    else:
        raise JetRefusal('RECOVERY_LOG_INVALID')
    path.unlink()
    _sync_directory(root)
    return True


def recover(root):
    with _session(root) as (root, fd, _manifest):
        return {'status': 'recovered' if _recover(root, fd) else 'clean', 'production_writer': False}


def update_numeric(root, operation_id, name, expected, replacement, *, fault=None):
    """Compare-and-set on the PRIVATE COPY. Same-width numeric inline Memo only."""
    operation_id = str(uuid.UUID(str(operation_id)))
    if (not isinstance(name, str) or not 1 <= len(name) <= 120
            or re.search(r'password|pwd|clave|token|secret', name, re.I)
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value)
                   for value in (expected, replacement))
            or not 0 <= int(expected) < int(replacement) <= 2147483647):
        raise JetRefusal('INVALID_NUMERIC_UPDATE')
    request_sha256 = _digest(json.dumps([name, expected, replacement]).encode())
    with _session(root) as (root, fd, manifest):
        _recover(root, fd)
        receipt_path = root / 'receipts' / (operation_id + '.json')
        if receipt_path.exists():
            result = json.loads(receipt_path.read_text())
            if result['request_sha256'] != request_sha256:
                raise JetRefusal('OPERATION_ID_CONFLICT')
            return result
        page, before, after = _plan(fd, manifest['layout'], name, expected, replacement)
        info = os.fstat(fd)
        result = {'status': 'applied_to_lab_copy', 'operation_id': operation_id,
                  'request_sha256': request_sha256, 'production_writer': False}
        log = {'state': 'prepared', 'identity': [info.st_dev, info.st_ino, info.st_size],
               'page': page, 'before': base64.b64encode(before).decode(),
               'before_sha256': _digest(before), 'after_sha256': _digest(after),
               'outside_sha256': _hash_fd(fd, page), 'operation_id': operation_id, 'receipt': result}
        _json(root, 'transaction.json', log)
        try:
            if fault:
                fault('prepared')
            if os.pread(fd, PAGE, page * PAGE) != before:
                raise JetRefusal('PAGE_CHANGED')
            _write_page(fd, page, after)
            if fault:
                fault('written')
            if os.pread(fd, PAGE, page * PAGE) != after or _hash_fd(fd, page) != log['outside_sha256']:
                raise JetRefusal('WRITE_VERIFICATION_FAILED')
            log['state'] = 'committed'
            _json(root, 'transaction.json', log)
            if fault:
                fault('committed')
            _recover(root, fd)  # Persist receipt before removing the commit decision.
            return result
        except Exception:
            _recover(root, fd)
            raise


def main():
    parser = argparse.ArgumentParser(description='Experimental Jet writer on a new private lab copy only')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('init')
    create.add_argument('--source', required=True)
    create.add_argument('--jar', required=True)
    create.add_argument('--root', required=True)
    update = sub.add_parser('update-numeric')
    update.add_argument('--root', required=True)
    update.add_argument('--operation-id', required=True)
    update.add_argument('--name', required=True)
    update.add_argument('--expected', required=True)
    update.add_argument('--replacement', required=True)
    restore = sub.add_parser('recover')
    restore.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'init':
            result = create_lab(args.source, args.root, AccessGateway(args.jar))
        elif args.action == 'recover':
            result = recover(args.root)
        else:
            result = update_numeric(args.root, args.operation_id, args.name, args.expected, args.replacement)
        print(json.dumps(result))
    except Exception:
        print(json.dumps({'status': 'refused', 'code': 'JET_LAB_OPERATION_REFUSED', 'production_writer': False}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
