import json
import os
from datetime import datetime, timezone
from pathlib import Path

from odoo import SUPERUSER_ID, api
from odoo.tools import config


def migrate(cr, version):
    model=api.Environment(cr,SUPERUSER_ID,{})['product.template']
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    path=Path(config['data_dir'])/('sga-article-code-mask-backup-'+stamp+'.jsonl')
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as backup:
        cursor=0
        while True:
            result=model._normalize_sga_article_codes(after_id=cursor,dry_run=False)
            for plan in result['plans']:
                backup.write(json.dumps(plan,ensure_ascii=False)+'\n')
            if not result['count']:break
            cursor=result['last_id']
