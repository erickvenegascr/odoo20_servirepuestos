"""Integration tests against actual Jet4 files, with an independent readback."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from bridge.access_gateway import AccessError, AccessGateway
from bridge import jet_lab


class SimulatedTermination(BaseException):
    pass


class JetGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = Path(__file__).resolve().parents[1]
        cls.jar = cls.probe / 'target/access-readonly-probe-1.0.0.jar'
        cls.fixture_classpath = os.pathsep.join([str(cls.probe / 'target/test-classes'), str(cls.jar)])
        if not cls.jar.is_file():
            raise RuntimeError('Build access-probe with Maven before this test module')
        cls.java = os.environ.get('JET_TEST_JAVA', 'java')
        cls.gateway = AccessGateway(cls.jar, java=cls.java)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'fixture.mdb'
        self.fixture('create', self.source)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.lab = self.base / 'lab'

    def fixture(self, action, path):
        result = subprocess.run([self.java, '-cp', self.fixture_classpath,
                                 'cr.servirepuestos.gateway.JetLabFixture', action, str(path)],
                                capture_output=True, text=True, check=True, timeout=15)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def create(self):
        jet_lab.create_lab(self.source, self.lab, self.gateway)

    def apply(self, *, operation=None, expected='100', replacement='101', fault=None):
        return jet_lab.update_numeric(self.lab, operation or uuid.uuid4(), 'CounterFixture',
                                      expected, replacement, fault=fault)

    def values(self):
        return {row['Nombre']: (row['Valor'], row['Guard'])
                for row in self.fixture('read', self.lab / 'data.mdb')}

    def assert_original_unchanged(self):
        self.assertEqual(self.source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_describes_tables_columns_indexes_and_links_without_resolving(self):
        data = self.gateway.describe(self.source)
        tables = {table['name']: table for table in data['tables']}
        self.assertTrue(tables['LinkedProducts']['linked'])
        self.assertTrue(tables['PGPropiedades']['indexes'][0]['primary'])
        self.assertIn('MEMO', [col['type'] for col in tables['PGPropiedades']['columns']])
        self.assert_original_unchanged()

    def test_projects_and_paginates_typed_rows_without_private_columns(self):
        data = self.gateway.read_rows(self.source, 'Catalog', ['Id', 'Description', 'Price', 'Moment'], limit=1)
        self.assertEqual(data['rows'], [{'Id': 1, 'Description': 'Artículo ñ', 'Price': '123.4500',
                                        'Moment': '2026-11-18T09:30'}])
        self.assertEqual(data['next_offset'], 1)
        second = self.gateway.read_rows(self.source, 'Catalog', ['Id', 'Description'], offset=1, limit=1)
        self.assertEqual(second['rows'], [{'Id': 2, 'Description': None}])
        self.assertIsNone(second['next_offset'])
        self.assertNotIn('private-canary', json.dumps(data))
        self.assert_original_unchanged()

    def test_refuses_linked_and_system_table_reads(self):
        for table in ['LinkedProducts', 'MSysObjects']:
            with self.assertRaises(AccessError):
                self.gateway.read_rows(self.source, table, ['Id'])

    def test_read_limits_are_enforced(self):
        for arguments in [{'limit': 501}, {'offset': -1}, {'offset': 10001}]:
            with self.assertRaises(AccessError):
                self.gateway.read_rows(self.source, 'Catalog', ['Id'], **arguments)

    def test_own_writer_updates_real_jet_and_preserves_other_data_and_index(self):
        self.create()
        before = (self.lab / 'data.mdb').read_bytes()
        result = self.apply()
        self.assertEqual(result['status'], 'applied_to_lab_copy')
        self.assertEqual(self.values(), {'CounterFixture': ('101', 777), 'OtherFixture': ('666', 888)})
        after = (self.lab / 'data.mdb').read_bytes()
        self.assertEqual(len(before), len(after))
        self.assertEqual(sum(a != b for a, b in zip(before, after)), 1)
        self.assertFalse((self.lab / 'transaction.json').exists())
        self.assert_original_unchanged()

    def test_idempotent_operation_and_conflicting_identity(self):
        self.create()
        operation = uuid.uuid4()
        first = self.apply(operation=operation)
        self.assertEqual(self.apply(operation=operation), first)
        with self.assertRaisesRegex(jet_lab.JetRefusal, 'OPERATION_ID_CONFLICT'):
            self.apply(operation=operation, expected='101', replacement='102')
        self.assertEqual(self.values()['CounterFixture'][0], '101')

    def test_conflicting_previous_value_does_not_change_any_bytes(self):
        self.create()
        before = (self.lab / 'data.mdb').read_bytes()
        with self.assertRaisesRegex(jet_lab.JetRefusal, 'EXPECTED_VALUE_CONFLICT'):
            self.apply(expected='099')
        self.assertEqual((self.lab / 'data.mdb').read_bytes(), before)

    def test_size_growth_is_refused_without_mutation(self):
        self.create()
        before = (self.lab / 'data.mdb').read_bytes()
        with self.assertRaisesRegex(jet_lab.JetRefusal, 'VALUE_GROWTH_UNSUPPORTED'):
            self.apply(replacement='1000')
        self.assertEqual((self.lab / 'data.mdb').read_bytes(), before)

    def test_io_failure_rolls_back_and_releases_own_lock(self):
        self.create()
        def fail(stage):
            if stage == 'written':
                raise OSError('simulated disk failure')
        with self.assertRaises(OSError):
            self.apply(fault=fail)
        self.assertEqual(self.values()['CounterFixture'][0], '100')
        self.apply()
        self.assertEqual(self.values()['CounterFixture'][0], '101')

    def test_interrupted_uncommitted_write_is_recovered_exactly(self):
        self.create()
        before = (self.lab / 'data.mdb').read_bytes()
        def terminate(stage):
            if stage == 'written':
                raise SimulatedTermination()
        with self.assertRaises(SimulatedTermination):
            self.apply(fault=terminate)
        self.assertTrue((self.lab / 'transaction.json').exists())
        self.assertEqual(jet_lab.recover(self.lab)['status'], 'recovered')
        self.assertEqual((self.lab / 'data.mdb').read_bytes(), before)

    def test_actual_process_exit_before_and_after_commit_decision(self):
        self.create()
        before = (self.lab / 'data.mdb').read_bytes()
        program = ('import os,sys; from bridge.jet_lab import update_numeric; '
                   'update_numeric(sys.argv[1],sys.argv[2],"CounterFixture","100","101",'
                   'fault=lambda stage: os._exit(73) if stage==sys.argv[3] else None)')
        for stage in ['written', 'committed']:
            operation = uuid.uuid4()
            result = subprocess.run([sys.executable, '-c', program, str(self.lab), str(operation), stage],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 73, result.stderr.decode())
            self.assertTrue((self.lab / 'transaction.json').exists())
            jet_lab.recover(self.lab)
            if stage == 'written':
                self.assertEqual((self.lab / 'data.mdb').read_bytes(), before)
            else:
                self.assertEqual(self.apply(operation=operation)['operation_id'], str(operation))
                self.assertEqual(self.values()['CounterFixture'][0], '101')

    def test_partial_page_write_is_rolled_back_before_retry(self):
        self.create()
        before = (self.lab / 'data.mdb').read_bytes()
        original = jet_lab._write_page
        calls = []
        def partial(fd, page, data):
            if not calls:
                calls.append(True)
                # Write a torn version of the page, then inject a storage failure.
                os.pwrite(fd, data[:4090], page * 4096)
                os.fsync(fd)
                raise OSError('partial write')
            original(fd, page, data)
        with patch.object(jet_lab, '_write_page', partial):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual((self.lab / 'data.mdb').read_bytes(), before)
        self.apply()
        self.assertEqual(self.values()['CounterFixture'][0], '101')

    def test_lost_response_after_commit_reconciles_without_reapplying(self):
        self.create()
        operation = uuid.uuid4()
        def terminate(stage):
            if stage == 'committed':
                raise SimulatedTermination()
        with self.assertRaises(SimulatedTermination):
            self.apply(operation=operation, fault=terminate)
        result = self.apply(operation=operation)
        self.assertEqual(result['operation_id'], str(operation))
        self.assertEqual(self.values()['CounterFixture'][0], '101')

    def test_recovery_refuses_changes_outside_owned_page(self):
        self.create()
        def terminate(stage):
            if stage == 'written':
                raise SimulatedTermination()
        with self.assertRaises(SimulatedTermination):
            self.apply(fault=terminate)
        with (self.lab / 'data.mdb').open('r+b') as output:
            output.seek(4096 + 100)
            original = output.read(1)
            output.seek(4096 + 100)
            output.write(bytes([original[0] ^ 1]))
        with self.assertRaisesRegex(jet_lab.JetRefusal, 'RECOVERY_IDENTITY_CONFLICT'):
            jet_lab.recover(self.lab)
        self.assertTrue((self.lab / 'transaction.json').exists())

    def test_symlink_or_hardlink_target_is_refused(self):
        self.create()
        target = self.lab / 'data.mdb'
        target.unlink()
        target.symlink_to(self.source)
        with self.assertRaises(OSError):
            self.apply()
        target.unlink()
        os.link(self.source, target)
        with self.assertRaisesRegex(jet_lab.JetRefusal, 'LAB_FILE_REQUIRED'):
            self.apply()
        self.assert_original_unchanged()

    def test_source_or_existing_folder_cannot_be_used_as_lab(self):
        with self.assertRaises(FileExistsError):
            jet_lab.create_lab(self.source, self.base, self.gateway)
        with self.assertRaises((OSError, ValueError, jet_lab.JetRefusal)):
            jet_lab.update_numeric(self.source, uuid.uuid4(), 'CounterFixture', '100', '101')
        self.assert_original_unchanged()

    def test_contending_lab_process_is_refused_and_can_retry_after_close(self):
        self.create()
        process = subprocess.Popen([sys.executable, '-c',
            'import fcntl,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX); '
            'print("ready",flush=True); sys.stdin.read()', str(self.lab / 'lab.lock')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaisesRegex(jet_lab.JetRefusal, 'LAB_BUSY'):
                self.apply()
        finally:
            process.communicate('', timeout=5)
        self.apply()


if __name__ == '__main__':
    unittest.main()
