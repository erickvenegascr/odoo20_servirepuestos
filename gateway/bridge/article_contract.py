"""Versioned article-card contract. Pricing, taxes and inventory are not writable."""
import hashlib
import json
import uuid

VERSION = 1
MAX_ARTICLE = 2147483647
FIELDS = frozenset({'name', 'reference', 'service', 'classification', 'barcodes'})
CLASS_FIELDS = frozenset({'division', 'category', 'subcategory', 'brand'})


class ArticleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def identity(value):
    if not isinstance(value, str):
        raise ArticleError('INVALID_ARTICLE_IDENTITY')
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ArticleError('INVALID_ARTICLE_IDENTITY') from None


def text(value, maximum, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ArticleError('INVALID_ARTICLE_TEXT')
    value = value.strip()
    if len(value) > maximum or any(ord(c) < 32 or ord(c) > 65535 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ArticleError('ARTICLE_TEXT_EXCEEDS_SGA_FORMAT')
    if not value:
        if required:
            raise ArticleError('ARTICLE_TEXT_REQUIRED')
        return None
    return value


def classification(value):
    if not isinstance(value, dict) or set(value) != CLASS_FIELDS:
        raise ArticleError('INVALID_ARTICLE_CLASSIFICATION')
    result = {'division': text(value['division'], 3), 'brand': text(value['brand'], 3)}
    for name in ('category', 'subcategory'):
        item = value[name]
        if item is not None and (type(item) is not int or not -32768 <= item <= 32767):
            raise ArticleError('INVALID_ARTICLE_CLASSIFICATION')
        result[name] = item
    if result['subcategory'] is not None and result['category'] is None:
        raise ArticleError('ARTICLE_SUBCATEGORY_REQUIRES_CATEGORY')
    return result


def values(value, *, legacy=False):
    if not isinstance(value, dict) or set(value) != FIELDS or type(value['service']) is not bool:
        raise ArticleError('INVALID_ARTICLE_FIELDS')
    if not isinstance(value['barcodes'], dict) or set(value['barcodes']) != {'0', '1'}:
        raise ArticleError('INVALID_ARTICLE_BARCODES')
    return {'name': text(value['name'], 70, required=True),
            'reference': text(value['reference'], 20, required=not legacy),
            'service': value['service'],
            'classification': classification(value['classification']),
            'barcodes': {key: text(value['barcodes'][key], 15) for key in ('0', '1')}}


def request(value):
    required = {'contract', 'action', 'operation_id', 'product_uuid', 'source_instance',
                'article_id', 'values', 'expected'}
    if (not isinstance(value, dict) or set(value) != required or type(value['contract']) is not int
            or value['contract'] != VERSION or value['action'] not in ('article.create', 'article.update')):
        raise ArticleError('INVALID_ARTICLE_REQUEST')
    result = dict(value)
    result['operation_id'] = identity(value['operation_id'])
    result['product_uuid'] = identity(value['product_uuid'])
    result['source_instance'] = text(value['source_instance'], 80, required=True)
    if value['action'] == 'article.create':
        result['values'] = values(value['values'])
        if value['article_id'] is not None or value['expected'] is not None:
            raise ArticleError('CREATE_CANNOT_SUPPLY_ARTICLE_NUMBER')
    else:
        result['values'] = values(value['values'], legacy=True)
        if type(value['article_id']) is not int or not 1 <= value['article_id'] <= MAX_ARTICLE:
            raise ArticleError('INVALID_SGA_ARTICLE_NUMBER')
        result['expected'] = values(value['expected'], legacy=True)
        if result['values']['reference'] is None and result['expected']['reference'] is not None:
            raise ArticleError('ARTICLE_REFERENCE_CANNOT_BE_CLEARED')
        if result['values']['service'] != result['expected']['service']:
            raise ArticleError('SGA_ARTICLE_TYPE_CHANGE_REQUIRES_SEPARATE_OPERATION')
    return result


def next_article(maximum):
    """Traced SGA PGProxNum: Nz(MAX(Articulo), 0)+1; the INSERT reserves it."""
    if maximum is None:
        maximum = 0
    if type(maximum) is not int or not 0 <= maximum < MAX_ARTICLE:
        raise ArticleError('SGA_ARTICLE_NUMBER_EXHAUSTED_OR_INVALID')
    return maximum + 1


def merge(base, local, remote):
    """Merge independent fields; competing changes are never silently discarded."""
    result, conflicts = {}, []
    for key in FIELDS:
        if key in ('classification', 'barcodes'):
            result[key] = {}
            for child in sorted(base[key]):
                old, left, right = base[key][child], local[key][child], remote[key][child]
                if left != old and right != old and left != right:
                    conflicts.append(key + '.' + child)
                result[key][child] = left if left != old else right
        else:
            old, left, right = base[key], local[key], remote[key]
            if left != old and right != old and left != right:
                conflicts.append(key)
            result[key] = left if left != old else right
    # A category/subcategory pair is one hierarchy; independent edits can still
    # produce an invalid pair. Validate it against SGA masters before writing.
    return result, sorted(conflicts)
