import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from django.test import SimpleTestCase

class ConfigurationTests(SimpleTestCase):
    def probe(self, *, clients=None, writes='0', omit_key=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);canary='test-canary-must-not-appear-in-errors-'+'x'*32
            for name in ['django','postgres']:(root/name).write_text(canary)
            default=[{'client_id':'test','origin_instance_id':'test-origin','token_sha256':hashlib.sha256(b'test-only-token').hexdigest(),'roles':['submit','read'],'companies':['company-a']}]
            (root/'clients').write_text(json.dumps(default if clients is None else clients))
            env=os.environ.copy();env.pop('GATEWAY_TESTING',None);env.pop('DATABASE_ENGINE',None)
            env.update({'DJANGO_SECRET_KEY_FILE':str(root/('missing' if omit_key else 'django')),
                        'POSTGRES_PASSWORD_FILE':str(root/'postgres'),'GATEWAY_CLIENTS_FILE':str(root/'clients'),'SGA_WRITES_ENABLED':writes})
            result=subprocess.run([sys.executable,'-c','from config import settings; print(settings.DEBUG)'],env=env,cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True)
            self.assertNotIn(canary,result.stdout+result.stderr)
            return result
    def test_real_mode_needs_files_and_disables_debug(self):
        result=self.probe();self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'False')
    def test_missing_secret_does_not_fall_back_to_a_default(self):
        result=self.probe(omit_key=True);self.assertNotEqual(result.returncode,0);self.assertIn('Cannot read configured secret file',result.stderr)
    def test_empty_client_configuration_fails_closed(self):
        result=self.probe(clients=[]);self.assertNotEqual(result.returncode,0);self.assertIn('At least one authenticated',result.stderr)
    def test_native_writes_cannot_be_enabled_by_an_environment_flag(self):
        result=self.probe(writes='1');self.assertNotEqual(result.returncode,0);self.assertIn('No certified SGA write adapter',result.stderr)
