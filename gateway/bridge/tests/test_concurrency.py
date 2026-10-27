from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from django.db import connection, close_old_connections, connections
from django.test import TransactionTestCase
from bridge.authentication import ServicePrincipal
from bridge.models import Operation, OperationEvent
from bridge.serializers import SubmissionSerializer
from bridge.services import submit, process_next, process_operation
from .test_api import envelope

@skipUnless(connection.vendor == 'postgresql', 'Row-lock and uniqueness races require PostgreSQL')
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def validated(self):
        serializer = SubmissionSerializer(data=envelope()); serializer.is_valid(raise_exception=True)
        return serializer.validated_data
    def user(self):
        return ServicePrincipal('odoo', 'odoo-test', frozenset(['submit', 'read']), frozenset(['company-a']))
    def test_concurrent_submission_has_one_operation_and_one_initial_event(self):
        data, user = self.validated(), self.user()
        def run(_):
            close_old_connections()
            try: return submit(data, user)[1]
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(Operation.objects.count(), 1)
        self.assertEqual(OperationEvent.objects.count(), 1)
    def test_two_workers_cannot_consume_the_same_operation(self):
        submit(self.validated(), self.user())
        def run(_):
            close_old_connections()
            try: return process_next(min_age_seconds=0)
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(Operation.objects.get().attempts, 1)
        self.assertEqual(OperationEvent.objects.count(), 3)

    def test_immediate_delivery_racing_recovery_does_not_apply_twice(self):
        op, _ = submit(self.validated(), self.user())
        def run(kind):
            close_old_connections()
            try:
                if kind == 'api':
                    return process_operation(op.pk).state
                return process_next(min_age_seconds=0)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, ['api', 'worker']))
        self.assertEqual(Operation.objects.get().attempts, 1)
        self.assertEqual(OperationEvent.objects.count(), 3)

    def test_duplicate_immediate_deliveries_only_process_once(self):
        op, _ = submit(self.validated(), self.user())
        def run(_):
            close_old_connections()
            try: return process_operation(op.pk).state
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(run, range(2))), ['BLOQUEADO', 'BLOQUEADO'])
        self.assertEqual(Operation.objects.get().attempts, 1)
        self.assertEqual(OperationEvent.objects.count(), 3)
