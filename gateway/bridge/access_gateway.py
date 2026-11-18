"""Optional local Access reader. Java is a short-lived child, never a service.

No SQL, linked-source traversal, Access COM, or write mode is exposed here.
"""
import argparse
import json
from pathlib import Path
import subprocess


class AccessError(Exception):
    pass


class AccessGateway:
    def __init__(self, jar, *, java='java', timeout=15):
        self.jar = Path(jar).resolve(strict=True)
        self.java = str(java)
        if not 1 <= timeout <= 30:
            raise ValueError('timeout must be 1..30 seconds')
        self.timeout = timeout

    def _call(self, action, database, *arguments):
        path = Path(database).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != '.mdb':
            raise AccessError('INVALID_DATABASE')
        command = [self.java, '-cp', str(self.jar),
                   'cr.servirepuestos.gateway.AccessGateway', action, str(path),
                   *map(str, arguments)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout,
                                    check=False, encoding='utf-8')
            if result.returncode or len(result.stdout) > 2_500_000:
                raise AccessError('ACCESS_READ_FAILED')
            data = json.loads(result.stdout)
            if data.get('status') != 'ok':
                raise AccessError('ACCESS_READ_FAILED')
            return data
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            raise AccessError('ACCESS_READ_FAILED') from error

    def describe(self, database):
        return self._call('describe', database)

    def read_rows(self, database, table, fields, *, offset=0, limit=100):
        if (not isinstance(table, str) or not 1 <= len(table) <= 128
                or not isinstance(fields, (tuple, list)) or not 1 <= len(fields) <= 32
                or any(not isinstance(field, str) or not 1 <= len(field) <= 128
                       or ',' in field for field in fields)
                or type(offset) is not int or not 0 <= offset <= 10000
                or type(limit) is not int or not 1 <= limit <= 500):
            raise AccessError('INVALID_READ_REQUEST')
        return self._call('read', database, table, ','.join(fields), offset, limit)

    def lab_layout(self, database):
        return self._call('lab-layout', database)


def main():
    parser = argparse.ArgumentParser(description='Read Access tables without changing Java or SGA')
    parser.add_argument('--jar', required=True)
    parser.add_argument('--java', default='java')
    parser.add_argument('--database', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('describe')
    read = sub.add_parser('read')
    read.add_argument('--table', required=True)
    read.add_argument('--field', action='append', required=True)
    read.add_argument('--offset', type=int, default=0)
    read.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    try:
        gateway = AccessGateway(args.jar, java=args.java)
        data = (gateway.describe(args.database) if args.action == 'describe' else
                gateway.read_rows(args.database, args.table, args.field,
                                  offset=args.offset, limit=args.limit))
        print(json.dumps(data, ensure_ascii=False, allow_nan=False))
    except (AccessError, OSError, ValueError):
        print(json.dumps({'status': 'error', 'code': 'ACCESS_REQUEST_FAILED'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
