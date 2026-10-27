#!/usr/bin/env python3
"""Encrypt configuration for a separate repository; materialize Docker secret files.
The age identity must be stored outside both application and secrets repositories.
No secret values are printed, passed as command-line arguments or committed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def within(path, directory):
    return path == directory or directory in path.parents

def require_age():
    for binary in ('age', 'age-keygen'):
        if not shutil.which(binary):
            raise ValueError('Install age from the official package before using secret commands')

def check_identity(identity, repo):
    if within(identity, ROOT) or within(identity, repo):
        raise ValueError('Identity must be outside application and configuration repositories')
    if identity.exists() and identity.stat().st_mode & 0o077:
        raise ValueError('Identity permissions must be 0600 or more restrictive')

def recipient(identity):
    return subprocess.check_output(['age-keygen', '-y', str(identity)], text=True).strip()

def validate(config):
    if config.get('schema_version') != 1:
        raise ValueError('Unsupported secrets schema')
    for name in ('django_secret_key', 'postgres_password'):
        if not isinstance(config.get(name), str) or len(config[name]) < 32:
            raise ValueError(f'{name} must be a generated value of at least 32 characters')
    clients = config.get('clients', [])
    if not isinstance(clients, list) or not clients:
        raise ValueError('At least one API client is required')
    ids, origins, tokens = set(), set(), set()
    for c in clients:
        if not isinstance(c, dict) or not all(k in c for k in ('client_id', 'origin_instance_id', 'token', 'roles', 'companies')):
            raise ValueError('Client configuration is incomplete')
        if not isinstance(c['client_id'], str) or not re.fullmatch(r'[a-z0-9_-]{1,80}', c['client_id']):
            raise ValueError('Invalid client identifier')
        if not isinstance(c['origin_instance_id'], str) or not 1 <= len(c['origin_instance_id']) <= 80:
            raise ValueError('Invalid origin identifier')
        if not isinstance(c['roles'], list) or not set(c['roles']) <= {'submit', 'read', 'read_any', 'retry'}:
            raise ValueError('Invalid client roles')
        if not isinstance(c['companies'], list) or not all(isinstance(v, str) and 1 <= len(v) <= 64 for v in c['companies']):
            raise ValueError('Invalid client companies')
        if not c.get('companies') or not c.get('roles'):
            raise ValueError('Each client needs explicit roles and companies')
        if len(c.get('token', '')) < 32:
            raise ValueError('API tokens must contain at least 32 characters')
        if c['client_id'] in ids or c['origin_instance_id'] in origins or c['token'] in tokens:
            raise ValueError('Client identities, origins and API tokens must be unique')
        ids.add(c['client_id']); origins.add(c['origin_instance_id']); tokens.add(c['token'])
    if config.get('sga', {}).get('writes_enabled', False):
        raise ValueError('No native SGA write adapter is certified')
    return config

def bootstrap(args):
    repo, identity = args.repo.resolve(), args.identity.resolve()
    check_identity(identity, repo)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,40}', args.environment):
        raise ValueError('Invalid environment name')
    output = repo/'environments'/(args.environment+'.json.age')
    if output.exists():
        raise ValueError('Environment already exists; never regenerate credentials for an existing database')
    identity.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not identity.exists():
        subprocess.run(['age-keygen', '-o', str(identity)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        identity.chmod(0o600)
    pub = recipient(identity)
    roles = [('odoo', ['submit', 'read']), ('operator', ['read', 'read_any', 'retry'])]
    clients = [{'client_id': name, 'origin_instance_id': args.origin if name == 'odoo' else args.origin+'-operator',
                'roles': permissions, 'companies': [args.company], 'token': secrets.token_urlsafe(48)} for name, permissions in roles]
    config = validate({'schema_version': 1, 'django_secret_key': secrets.token_urlsafe(64),
                       'postgres_password': secrets.token_urlsafe(48), 'clients': clients,
                       'odoo': {'gateway_url': 'https://gateway.example.invalid'},
                       'sga': {'writes_enabled': False, 'connection': None}})
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['age', '-r', pub, '-o', str(output)], input=json.dumps(config).encode(), check=True)
    (repo/'recipients.txt').write_text(pub+'\n')
    print(f'Encrypted environment created: {output}')
    print(f'Recovery identity retained outside repositories: {identity}')

def materialize(args):
    source, identity, output = args.input.resolve(), args.identity.resolve(), args.output.resolve()
    repo = source.parent.parent
    check_identity(identity, repo)
    if within(output, ROOT) or within(identity, output) or output == repo:
        raise ValueError('Runtime secrets must be outside the application repository and identity directory')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Runtime directory is not empty; choose a new one, then switch SECRETS_DIR')
    raw = subprocess.check_output(['age', '-d', '-i', str(identity), str(source)])
    config = validate(json.loads(raw))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    clients = [{k: v for k, v in c.items() if k != 'token'} | {'token_sha256': hashlib.sha256(c['token'].encode()).hexdigest()} for c in config['clients']]
    files = {'django_secret_key.txt': config['django_secret_key'], 'postgres_password.txt': config['postgres_password'],
             'gateway_clients.json': json.dumps(clients, indent=2), 'sga_connection.json': json.dumps(config.get('sga', {}))}
    for c in config['clients']:
        if not re.fullmatch(r'[a-z0-9_-]+', c['client_id']):
            raise ValueError('Client id cannot be used as a secret filename')
        files[c['client_id']+'_gateway_key.txt'] = c['token']
        files[c['client_id']+'_origin_instance_id.txt'] = c['origin_instance_id']
    files['odoo_gateway_url.txt'] = config.get('odoo', {}).get('gateway_url', '')
    for name, value in files.items():
        p = output/name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(value+'\n')
        # Parent is 0700. Read-only files allow a non-root container UID to
        # consume explicit per-file mounts on Linux Compose, which retains
        # host file ownership for file-backed secrets.
        p.chmod(0o444)
    print(f'Runtime secret files materialized: {output}')

def encrypt(args):
    source, identity, output = args.input.resolve(), args.identity.resolve(), args.output.resolve()
    check_identity(identity, output.parent.parent)
    if within(source, ROOT):
        raise ValueError('Do not place plaintext configuration inside the application repository')
    config = validate(json.loads(source.read_text()))
    if output.exists():
        raise ValueError('Choose a new encrypted output, review it, then replace the intended revision')
    subprocess.run(['age', '-r', recipient(identity), '-o', str(output)], input=json.dumps(config).encode(), check=True)
    print(f'Encrypted configuration written: {output}')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('bootstrap'); p.add_argument('--repo', type=Path, required=True); p.add_argument('--identity', type=Path, required=True)
    p.add_argument('--environment', default='local'); p.add_argument('--company', default='default'); p.add_argument('--origin', default='odoo-local'); p.set_defaults(func=bootstrap)
    p = sub.add_parser('materialize'); p.add_argument('--input', type=Path, required=True); p.add_argument('--identity', type=Path, required=True); p.add_argument('--output', type=Path, required=True); p.set_defaults(func=materialize)
    p = sub.add_parser('encrypt'); p.add_argument('--input', type=Path, required=True); p.add_argument('--identity', type=Path, required=True); p.add_argument('--output', type=Path, required=True); p.set_defaults(func=encrypt)
    args = parser.parse_args()
    try:
        require_age(); args.func(args)
    except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        # Command errors never include the decrypted input or API tokens.
        print(f'Secrets operation failed: {exc}', file=sys.stderr)
        return 1
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
