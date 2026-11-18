import copy
import uuid
from unittest.mock import MagicMock
from django.test import TestCase
from bridge.article_contract import ArticleError
from bridge.article_sql import ArticleCommitUnknown
from bridge.services_exchange import exchange_once
from bridge.article_exchange import sweep_once
from bridge.models import GatewayInbox, ArticleSweep


class ArticleExchangeTests(TestCase):
    def setUp(self):
        self.app=str(uuid.uuid4());self.op=str(uuid.uuid4())
        self.writer=MagicMock(application_id=self.app,config={'source_instance':'lab'})
        self.payload={'action':'article.create','operation_id':self.op,'product_uuid':str(uuid.uuid4())}
        self.message={'message_id':self.op,'lease':str(uuid.uuid4()),'kind':'request','payload':self.payload}
        self.envelope={'application_id':self.app,'messages':[self.message]}
        self.client=MagicMock(config={'application_id':self.app})
        self.result={'status':'synchronized','operation_id':self.op,'product_uuid':self.payload['product_uuid'],'article_id':42}
        self.writer.apply.return_value=self.result

    def test_disabled_article_is_deferred_without_poisoning_inbox(self):
        self.client.call.side_effect=[self.envelope]
        self.assertEqual(0,exchange_once(self.client))
        self.assertFalse(GatewayInbox.objects.exists())
        self.client.call.side_effect=[self.envelope,{}]
        self.assertEqual(1,exchange_once(self.client,self.writer))
        self.assertEqual(self.result,GatewayInbox.objects.get().result)

    def test_lost_ack_does_not_rewrite_sga(self):
        self.client.call.side_effect=[self.envelope,TimeoutError()]
        with self.assertRaises(TimeoutError):exchange_once(self.client,self.writer)
        self.client.call.side_effect=[self.envelope,{}]
        exchange_once(self.client,self.writer)
        self.writer.apply.assert_called_once_with(self.payload)

    def test_unknown_native_commit_does_not_create_final_inbox(self):
        self.writer.apply.side_effect=ArticleCommitUnknown()
        self.client.call.side_effect=[self.envelope]
        with self.assertRaises(ArticleCommitUnknown):exchange_once(self.client,self.writer)
        self.assertFalse(GatewayInbox.objects.exists())

    def test_busy_native_state_is_deferred(self):
        self.writer.apply.side_effect=ArticleError('SGA_ARTICLE_BUSY');self.client.call.side_effect=[self.envelope]
        with self.assertRaises(ArticleError):exchange_once(self.client,self.writer)
        self.assertFalse(GatewayInbox.objects.exists())

    def test_invalid_operation_is_rejected_with_identity(self):
        self.writer.apply.side_effect=ArticleError('INVALID_ARTICLE_FIELDS');self.client.call.side_effect=[self.envelope,{}]
        exchange_once(self.client,self.writer)
        result=GatewayInbox.objects.get().result
        self.assertEqual('rejected',result['status']);self.assertEqual(self.op,result['operation_id'])

    def test_operation_uuid_must_equal_message_uuid(self):
        self.payload['operation_id']=str(uuid.uuid4());self.client.call.side_effect=[self.envelope]
        with self.assertRaises(ValueError):exchange_once(self.client,self.writer)
        self.writer.apply.assert_not_called()

    def test_writer_application_cannot_change(self):
        self.writer.application_id=str(uuid.uuid4());self.client.call.side_effect=[self.envelope]
        with self.assertRaises(ValueError):exchange_once(self.client,self.writer)
        self.writer.apply.assert_not_called()

    def test_sweep_checkpoint_survives_lost_response(self):
        self.writer.cards.return_value=[{'article_id':42}]
        self.client.call.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):sweep_once(self.client,self.writer)
        self.assertFalse(ArticleSweep.objects.exists())
        self.client.call.side_effect=None
        self.client.call.return_value={'status':'accepted','application_id':self.app}
        self.assertEqual(1,sweep_once(self.client,self.writer))
        self.assertEqual(42,ArticleSweep.objects.get().after_article)
        self.writer.cards.return_value=[]
        self.assertEqual(0,sweep_once(self.client,self.writer))
        self.assertEqual(0,ArticleSweep.objects.get().after_article)
