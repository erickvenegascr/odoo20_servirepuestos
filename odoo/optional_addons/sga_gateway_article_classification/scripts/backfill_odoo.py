"""Run through `odoo-bin shell --no-http` in the intended database."""
import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from odoo.orm.environments import Environment
from odoo.orm.registry import Registry
from odoo.sql_db import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import config


def execute(shell_env):
    db = os.environ['SGA_CLASSIFICATION_DB']
    company = int(os.environ['SGA_CLASSIFICATION_COMPANY'])
    instance = os.environ['SGA_CLASSIFICATION_SOURCE']
    mode = os.environ.get('SGA_CLASSIFICATION_MODE', 'dry')
    if shell_env.cr.dbname != db or company <= 0 or not 1 <= len(instance) <= 80 or mode not in ('dry', 'apply'):
        raise ValueError('Invalid classification destination or mode.')
    dry = mode == 'dry'
    registry = Registry(db)
    run = {'id': str(uuid.uuid4()), 'started_at': datetime.now(timezone.utc).isoformat(),
           'status': 'running', 'mode': mode, 'company_id': company, 'source': instance,
           'products': 0, 'cursor': 0, 'assignments': {}, 'masters': {}, 'batches': 0}
    folder = Path(config['data_dir']) / 'sga-gateway-audit'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / f"article-classification-{run['id']}.json"
    latest = folder / f'article-classification-latest-{mode}.json'

    def save():
        for dest in (path, latest):
            temp = dest.with_name(f'.{dest.name}.{run["id"]}')
            with temp.open('x', encoding='utf8') as file:
                temp.chmod(0o600)
                json.dump(run, file, ensure_ascii=False)
            temp.replace(dest)

    lock = int.from_bytes(hashlib.sha256(f'article-classification-run:{company}:{instance}'.encode()).digest()[:8], 'big', signed=True)
    counts = Counter()
    with registry.cursor() as control:
        control.execute('SELECT pg_try_advisory_lock(%s)', [lock])
        if not control.fetchone()[0]:
            raise ValueError('A classification backfill is already running.')
        control.commit()
        try:
            with registry.cursor() as cr:
                e = Environment(cr, 1, {'allowed_company_ids': [company]})
                if not e['res.company'].browse(company).exists():
                    raise ValueError('The classification company does not exist.')
                run['expected_products'] = e['sga.catalog.detail.binding'].search_count([
                    ('company_id', '=', company), ('source_instance', '=', instance)])
                if not run['expected_products']:
                    raise ValueError('No source-bound products in the classification destination.')
            save()
            print('CLASSIFICATION_STARTED=' + json.dumps(run), flush=True)
            while True:
                for attempt in range(3):
                    try:
                        with registry.cursor() as cr:
                            e = Environment(cr, 1, {'allowed_company_ids': [company]})
                            result = e['sga.article.classification.import']._backfill_batch(
                                instance, after_article=run['cursor'], limit=500, dry_run=dry)
                            if dry:
                                cr.rollback()
                        break
                    except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                        if attempt == 2:
                            raise
                if not result['products']:
                    break
                if result['last_article'] <= run['cursor']:
                    raise ValueError('Classification cursor did not advance.')
                if not run['batches']:
                    run['masters'] = result['masters']
                run['products'] += result['products'];run['cursor'] = result['last_article'];run['batches'] += 1
                counts.update(result['assignments']);run['assignments'] = dict(counts)
                save()
                if run['batches'] % 10 == 0:
                    print('CLASSIFICATION_PROGRESS=' + json.dumps(run), flush=True)
            if run['products'] != run['expected_products']:
                raise ValueError('The source-bound catalog changed during classification backfill.')
            run['status'] = 'succeeded'
        except Exception as exc:
            run['status'] = 'failed';run['error'] = type(exc).__name__
            raise
        finally:
            run['finished_at'] = datetime.now(timezone.utc).isoformat()
            save()
            print('CLASSIFICATION_RESULT=' + json.dumps(run), flush=True)
            control.execute('SELECT pg_advisory_unlock(%s)', [lock])


execute(env)
