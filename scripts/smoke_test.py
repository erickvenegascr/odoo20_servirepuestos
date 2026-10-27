#!/usr/bin/env python3
"""Exercise only the new gateway using synthetic data, never SGA."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import uuid

p=argparse.ArgumentParser();p.add_argument('--secrets-dir',type=Path,required=True);p.add_argument('--base-url',default='http://127.0.0.1:8000');p.add_argument('--output',type=Path,required=True);p.add_argument('--verify',type=Path);args=p.parse_args()
token=(args.secrets_dir/'odoo_gateway_key.txt').read_text().strip()
origin=(args.secrets_dir/'odoo_origin_instance_id.txt').read_text().strip()
clients=json.loads((args.secrets_dir/'gateway_clients.json').read_text());company=next(c for c in clients if c['client_id']=='odoo')['companies'][0]
def call(path,data=None):
 req=urllib.request.Request(args.base_url+path,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=10) as r:return r.status,json.load(r)
for i in range(30):
 try:call('/health/ready/');break
 except OSError:
  if i==29:raise
  time.sleep(1)
if args.verify:
 source=json.loads(args.verify.read_text());op_id=source['gateway_id'];status,value=call('/api/v1/operations/'+op_id+'/')
 events=call('/api/v1/operations/'+op_id+'/events/')[1]['results']
 assert value['operation_id']==source['operation_id'] and value['state']=='BLOQUEADO'
 assert len(events)>=3
 result={'gateway_id':op_id,'operation_id':value['operation_id'],'state':value['state'],'events':len(events),'persistence_verified':True}
else:
 body={'schema_version':'1','operation_id':str(uuid.uuid4()),'event_id':str(uuid.uuid4()),'aggregate_id':str(uuid.uuid4()),'origin_instance_id':origin,'target_company_key':company,'revision':1,'action':'receipt.apply','origin_system':'odoo','actor_user_id':'synthetic-smoke-test','occurred_at_utc':'2026-10-27T12:00:00Z','expected_target_fingerprint':'UNVERIFIED','payload':{'synthetic':True}}
 status,first=call('/api/v1/operations/',body);assert status==201
 assert first['state']=='BLOQUEADO' and first['last_error_code']=='SGA_NATIVE_ADAPTER_NOT_CERTIFIED'
 status,second=call('/api/v1/operations/',body);assert status==200 and first['id']==second['id']
 op_id=first['id']
 _,value=call('/api/v1/operations/'+op_id+'/')
 assert value['state']=='BLOQUEADO' and value['native_keys']=={}
 result={'gateway_id':op_id,'operation_id':body['operation_id'],'state':value['state'],'duplicate_reused':True,'delivery':'immediate_http_response','native_writes_executed':False}
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
