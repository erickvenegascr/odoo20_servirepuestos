#!/usr/bin/env python3
"""Check only code/configuration boundaries and static contract consistency."""
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
paths=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines()
for name in paths:
    p=ROOT/name
    if not p.is_file():continue
    if any(part in {'runtime','secrets','backups','.venv','evidencia','originales'} for part in p.relative_to(ROOT).parts):
        raise SystemExit('Forbidden path in application repository: '+name)
    if p.name.startswith('.env') and p.name!='.env.example':raise SystemExit('Environment file included: '+name)
    if p.suffix in {'.key','.pem','.agekey','.dump','.sqlite3','.age'}:raise SystemExit('Sensitive artifact included: '+name)
    text=p.read_text(errors='ignore')
    patterns=[r'AGE-SECRET-KEY-[A-Z0-9]{40,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',r'gh[pousr]_[A-Za-z0-9]{30,}']
    for pattern in patterns:
        if re.search(pattern,text):raise SystemExit('Credential pattern in '+name)
    if p.suffix=='.py':ast.parse(text,filename=name)
    if p.suffix=='.xml':ET.fromstring(text)

contracts=json.loads((ROOT/'contracts/v1/definitions.json').read_text())
schema=json.loads((ROOT/'contracts/v1/operation.schema.json').read_text())
assert schema['properties']['action']['enum']==contracts['actions']
assert set(contracts['transitions'])==set(contracts['states'])
for edges in contracts['transitions'].values():assert set(edges)<=set(contracts['states'])
def assigned_constants(path):
    tree=ast.parse(path.read_text());return {n.targets[0].id:ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and n.targets[0].id in {'ACTIONS','STATES'}}
for path in [ROOT/'gateway/bridge/domain.py',ROOT/'odoo/addons/sga_gateway_connector/constants.py']:
    c=assigned_constants(path);assert list(c['ACTIONS'])==contracts['actions'];assert list(c['STATES'])==contracts['states']
manifest=ast.literal_eval((ROOT/'odoo/addons/sga_gateway_connector/__manifest__.py').read_text())
for path in manifest['data']:assert (ROOT/'odoo/addons/sga_gateway_connector'/path).exists()
assert manifest['version'].startswith('20.0.')
assert 'PGPASSWORD=' not in (ROOT/'compose.yaml').read_text()
print(json.dumps({'static_checks':'passed','files_checked':len(paths),'actions':len(contracts['actions']),'states':len(contracts['states'])}))
