"""Run via the Odoo 20 shell in compose.odoo-test.yaml, using synthetic data only.

Real PostgreSQL commits and rollbacks, with only the HTTP peer replaced by a mock.
Never run this harness against a business database.
"""
import json
import uuid
from unittest.mock import patch
import requests
from odoo import api, SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.orm.registry import Registry


def run():
    registry = Registry(env.cr.dbname)
    assert env.cr.dbname == 'connector_test', 'A disposable connector_test database is required'
    model_class = type(env['sga.gateway.operation'])
    config = {'SGA_GATEWAY_URL': 'https://gateway.invalid',
              'SGA_GATEWAY_API_KEY': 'test-only-synthetic-key', 'SGA_GATEWAY_ORIGIN': 'odoo-test'}
    delivered, remote = [], {}
    checks = []

    def new(cr):
        test_env = api.Environment(cr, SUPERUSER_ID, {})
        return test_env['sga.gateway.operation'].create({
            'action': 'receipt.apply', 'target_company_key': 'company-a',
            'payload': {'lines': [{'product': 'synthetic-product', 'quantity': '4'}]},
        })

    class Reply:
        status_code = 201
        def __init__(self, data): self.data = data
        def json(self): return self.data

    def peer(_session, method, url, **kwargs):
        assert method == 'POST'
        body = kwargs['json']
        # A different cursor must already see both the operation and its queued state.
        with registry.cursor() as other:
            other.execute('SELECT state FROM sga_gateway_operation WHERE operation_uuid = %s', [body['operation_id']])
            assert other.fetchone() == ('EN_COLA',), 'HTTP ran before the Odoo transaction committed'
        delivered.append(body)
        data = remote.setdefault(body['operation_id'], {
            **{k: body[k] for k in ('operation_id', 'revision', 'action', 'target_company_key', 'origin_instance_id')},
            'id': str(uuid.uuid4()), 'state': 'BLOQUEADO', 'native_keys': {},
            'last_error_code': 'SGA_NATIVE_ADAPTER_NOT_CERTIFIED',
        })
        return Reply(data)

    with patch.object(model_class, '_configuration', return_value=config), patch.object(requests.Session, 'request', peer):
        with registry.cursor() as cr:
            op = new(cr)
            operation_id = op.id
            action = op.action_queue()
            assert action['tag'] == 'reload'
            assert delivered == [], 'Enqueue must not emit uncommitted data'
            cr.commit()  # The real postcommit callback must perform delivery now.
        with registry.cursor() as cr:
            saved = api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation'].browse(operation_id)
            assert len(delivered) == 1 and saved.state == 'BLOQUEADO' and saved.gateway_id
            assert saved.submitted_by.id == SUPERUSER_ID and saved.queued_at
        checks.append('immediate_after_real_commit_without_cron')

        with registry.cursor() as cr:
            op = new(cr)
            rolled_back_id = op.id
            op.action_queue()
            cr.rollback()
        assert len(delivered) == 1
        with registry.cursor() as cr:
            assert not api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation'].browse(rolled_back_id).exists()
        checks.append('rollback_never_delivers')

        def lost_reply(*args, **kwargs):
            peer(*args, **kwargs)  # The remote accepted it; the reply was lost.
            raise requests.Timeout()

        with patch.object(requests.Session, 'request', lost_reply):
            with registry.cursor() as cr:
                op = new(cr)
                lost_id = op.id
                op.action_queue()
                cr.commit()
        with registry.cursor() as cr:
            saved = api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation'].browse(lost_id)
            assert saved.state == 'EN_COLA' and not saved.gateway_id
            assert saved.last_error == 'GATEWAY_CONNECTION_UNCONFIRMED'
            saved.action_refresh()
            cr.commit()
        with registry.cursor() as cr:
            saved = api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation'].browse(lost_id)
            assert saved.state == 'BLOQUEADO' and saved.gateway_id
        assert delivered[-1] == delivered[-2], 'A retry must preserve the complete envelope'
        assert len(remote) == 2, 'A lost response cannot create another business operation'
        checks.append('lost_reply_reuses_identity_and_preserves_outbox')

        with patch.object(requests.Session, 'request', side_effect=requests.Timeout()):
            with registry.cursor() as cr:
                op = new(cr)
                recovery_id = op.id
                op.action_queue()
                cr.commit()
        count = len(delivered)
        with registry.cursor() as cr:
            test_env = api.Environment(cr, SUPERUSER_ID, {})
            test_env['sga.gateway.operation']._cron_recover()
            assert len(delivered) == count, 'Recovery must not compete with recent immediate attempts'
            cr.execute("UPDATE sga_gateway_operation SET write_date = (now() AT TIME ZONE 'UTC') - interval '6 minutes' WHERE id = %s", [recovery_id])
        with registry.cursor() as cr:
            api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation']._cron_recover()
        assert len(delivered) == count + 1
        checks.append('optional_recovery_only_after_grace_period')

        with registry.cursor() as cr:
            saved = api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation'].browse(operation_id)
            try:
                saved.write({'payload': {'changed': True}})
                raise AssertionError('Submitted business content was editable')
            except UserError:
                pass
        checks.append('submitted_business_content_is_immutable')

    with registry.cursor() as cr:
        cron = api.Environment(cr, SUPERUSER_ID, {}).ref('sga_gateway_connector.cron_sga_gateway_exchange')
        assert not cron.active and cron.interval_number == 5 and '_cron_recover' in cron.code
    checks.append('cron_is_disabled_and_recovery_only')

    with registry.cursor() as cr:
        test_env = api.Environment(cr, SUPERUSER_ID, {})
        company = test_env.company
        other_company = test_env['res.company'].create({'name': 'Synthetic second company'})
        users = test_env['res.users']
        group_field = 'group_ids' if 'group_ids' in users._fields else 'groups_id'
        common = {'company_id': company.id, 'company_ids': [(6, 0, [company.id])]}
        operator = users.create({**common, 'name': 'Synthetic gateway operator',
            'login': 'gateway-operator-' + str(uuid.uuid4()),
            group_field: [(6, 0, [test_env.ref('base.group_system').id])]})
        regular = users.create({**common, 'name': 'Synthetic regular user',
            'login': 'gateway-regular-' + str(uuid.uuid4()),
            group_field: [(6, 0, [test_env.ref('base.group_user').id])]})
        foreign = new(cr)
        foreign.write({'company_id': other_company.id})
        try:
            foreign.with_user(operator).with_context(allowed_company_ids=[company.id]).read(['state'])
            raise AssertionError('Company scope allowed reading another company')
        except AccessError:
            pass
        try:
            new(cr).with_user(regular).read(['state'])
            raise AssertionError('A regular user could read the technical outbox')
        except AccessError:
            pass
        own = new(cr).with_user(operator).with_context(allowed_company_ids=[company.id])
        assert own.read(['state'])[0]['state'] == 'BORRADOR'
        with patch.object(model_class, '_configuration', return_value=config), patch.object(requests.Session, 'request', peer):
            own.action_queue()
            own_id, operator_id = own.id, operator.id
            cr.commit()
    with registry.cursor() as cr:
        saved = api.Environment(cr, SUPERUSER_ID, {})['sga.gateway.operation'].browse(own_id)
        assert saved.state == 'BLOQUEADO' and saved.submitted_by.id == operator_id
    checks.append('authorized_user_delivery_and_company_scope')

    with registry.cursor() as cr:
        op = new(cr)
        with patch.object(model_class, '_configuration', side_effect=UserError('Missing synthetic configuration')):
            try:
                op.action_queue()
                raise AssertionError('Missing configuration allowed delivery')
            except UserError:
                pass
        assert op.state == 'BORRADOR'
    checks.append('configuration_required_before_queue')
    print(json.dumps({'odoo20_immediate_tests': 'passed', 'checks': checks, 'http_peer': 'mocked', 'sga_writes': False}))


run()
