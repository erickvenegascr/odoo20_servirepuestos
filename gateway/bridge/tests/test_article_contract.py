import copy
import unittest
import uuid
from bridge.article_contract import ArticleError, merge, next_article, request


class ArticleContractTests(unittest.TestCase):
    def request(self):
        return {'contract':1,'action':'article.create','operation_id':str(uuid.uuid4()),
                'product_uuid':str(uuid.uuid4()),'source_instance':'lab','article_id':None,'expected':None,
                'values':{'name':'A','reference':'R','service':False,
                          'classification':{'division':None,'category':None,'subcategory':None,'brand':None},
                          'barcodes':{'0':None,'1':None}}}

    def test_allocator_null_and_overflow(self):
        self.assertEqual(1,next_article(None));self.assertEqual(42,next_article(41))
        for value in (2147483647,-1,True):
            with self.assertRaises(ArticleError):next_article(value)

    def test_create_has_no_user_supplied_number(self):
        op=self.request();op['article_id']=42
        with self.assertRaises(ArticleError):request(op)

    def test_fields_outside_card_contract_are_forbidden(self):
        for name in ('price','cost','stock','cabys','vat'):
            op=self.request();op['values'][name]=1
            with self.assertRaises(ArticleError):request(op)

    def test_utf8mb3_and_control_character_limits(self):
        for value in ('x'*71,'bad\ntext','emoji \U0001f680','\ud800'):
            op=self.request();op['values']['name']=value
            with self.assertRaises(ArticleError):request(op)

    def test_hierarchy_requires_category(self):
        op=self.request();op['values']['classification']['subcategory']=1
        with self.assertRaises(ArticleError):request(op)

    def test_merge_independent_leaves(self):
        base=self.request()['values'];local=copy.deepcopy(base);remote=copy.deepcopy(base)
        local['barcodes']['0']='ABC';remote['classification']['brand']='LAB'
        actual,conflicts=merge(base,local,remote)
        self.assertFalse(conflicts);self.assertEqual('ABC',actual['barcodes']['0']);self.assertEqual('LAB',actual['classification']['brand'])

    def test_merge_competing_leaf_is_visible(self):
        base=self.request()['values'];local=copy.deepcopy(base);remote=copy.deepcopy(base)
        local['name']='LEFT';remote['name']='RIGHT'
        actual,conflicts=merge(base,local,remote)
        self.assertEqual(['name'],conflicts)


class ArticleGrantTests(unittest.TestCase):
    def test_global_and_stock_write_grants_are_rejected(self):
        from bridge.article_sql import assert_writer_grants
        for grant in ["GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost'",
                      "GRANT UPDATE ON `sga`.* TO 'w'@'localhost'",
                      "GRANT UPDATE (`CostoUnit`) ON `sga`.`artsservs` TO 'w'@'localhost'",
                      "GRANT SELECT ON `sga`.* TO 'w'@'localhost' WITH GRANT OPTION"]:
            with self.assertRaises(ArticleError):assert_writer_grants([{'g':grant}],'sga','adapter')

    def test_granular_card_columns_are_accepted(self):
        from bridge.article_sql import assert_writer_grants
        assert_writer_grants([{'g':"GRANT UPDATE (`ArticuloNomb`, `NombreCorto`) ON `sga`.`artsservs` TO 'w'@'localhost'"},
                              {'g':"GRANT SELECT ON `sga`.* TO 'w'@'localhost'"},
                              {'g':"GRANT SELECT, INSERT ON `adapter`.`article_receipt` TO 'w'@'localhost'"}],'sga','adapter')
