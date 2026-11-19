import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from psycopg2.extras import execute_values


def article_code(article):
    if type(article) is not int or not 1 <= article <= 2147483647:
        raise ValidationError('Número de artículo SGA inválido.')
    return str(article).zfill(6)


def commercial_reference(value, article):
    return value if value and value not in (str(article),article_code(article)) else False


class Product(models.Model):
    _inherit='product.template'

    sga_commercial_reference=fields.Char(string='Referencia comercial en SGA',size=20,copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        clean=[]
        for incoming in vals_list:
            vals=dict(incoming)
            article=vals.get('sga_catalog_article_id')
            if article:
                vals.setdefault('sga_commercial_reference',commercial_reference(vals.get('default_code'),article))
                vals['default_code']=article_code(article)
            clean.append(vals)
        return super().create(clean)

    def write(self, vals):
        if vals.get('sga_catalog_article_id'):
            vals={**vals,'default_code':article_code(vals['sga_catalog_article_id'])}
        if 'default_code' in vals and not self.env.context.get('sga_card_internal'):
            for product in self:
                if product.sga_catalog_article_id and vals['default_code']!=article_code(product.sga_catalog_article_id):
                    raise ValidationError('El código del artículo SGA conserva su número y formato de seis dígitos. Use la referencia comercial para otros códigos.')
        return super().write(vals)

    @api.model
    def _normalize_sga_article_codes(self, after_id=0, limit=500, dry_run=True):
        if type(after_id) is not int or type(limit) is not int or not 1<=limit<=500 or type(dry_run) is not bool:
            raise ValidationError('Lote de normalización inválido.')
        products=self.sudo().with_context(active_test=False).search([
            ('id','>',after_id),('sga_catalog_instance','!=',False),('sga_catalog_article_id','>',0)],order='id',limit=limit)
        plans=[]
        for product in products:
            variants=product.with_context(active_test=False).product_variant_ids
            if len(variants)!=1:
                raise ValidationError('El artículo SGA %s tiene varias variantes; revise su identidad antes de normalizar.' % product.sga_catalog_article_id)
            code=article_code(product.sga_catalog_article_id)
            reference=(product.sga_commercial_reference or commercial_reference(variants.default_code,product.sga_catalog_article_id)
                or commercial_reference(product.default_code,product.sga_catalog_article_id))
            tracked=dict(product.sga_catalog_values or {})
            if tracked:
                expected=(tracked.get('sga_commercial_reference') if 'sga_commercial_reference' in tracked
                    else commercial_reference(tracked.get('default_code'),product.sga_catalog_article_id))
                tracked.update(default_code=code,sga_commercial_reference=expected)
            if product.default_code!=code or variants.default_code!=code or product.sga_commercial_reference!=reference or (tracked or False)!=(product.sga_catalog_values or False):
                plans.append({'template':product.id,'variant':variants.id,'old_code':variants.default_code,'old_template_code':product.default_code,
                    'old_reference':product.sga_commercial_reference,'old_tracked':product.sga_catalog_values,
                    'reference':reference,'code':code,'tracked':tracked or False})
        if not dry_run and plans:
            self.env.flush_all()
            execute_values(self.env.cr._obj,'UPDATE product_product AS p SET default_code=v.code,write_date=NOW() AT TIME ZONE \'UTC\',write_uid=%s FROM (VALUES %%s) AS v(id,code) WHERE p.id=v.id' % self.env.uid,
                [(r['variant'],r['code']) for r in plans])
            execute_values(self.env.cr._obj,'UPDATE product_template AS p SET default_code=v.code,sga_commercial_reference=v.ref,sga_catalog_values=v.tracked::jsonb,write_date=NOW() AT TIME ZONE \'UTC\',write_uid=%s FROM (VALUES %%s) AS v(id,code,ref,tracked) WHERE p.id=v.id' % self.env.uid,
                [(r['template'],r['code'],r['reference'] or None,json.dumps(r['tracked'])) for r in plans])
            self.env['product.product'].browse([r['variant'] for r in plans]).invalidate_recordset(['default_code'])
            products.invalidate_recordset(['default_code','sga_commercial_reference','sga_catalog_values'])
        return {'count':len(products),'last_id':products[-1].id if products else after_id,'changed':len(plans),'plans':plans}


class Variant(models.Model):
    _inherit='product.product'

    def write(self, vals):
        if 'default_code' in vals and not self.env.context.get('sga_card_internal'):
            for product in self:
                article=product.product_tmpl_id.sga_catalog_article_id
                if article and vals['default_code']!=article_code(article):
                    raise ValidationError('El código definitivo corresponde al artículo SGA con seis dígitos.')
        return super().write(vals)
