"""Request and rank a company-scoped shortlist of supplier names."""

from collections import Counter

from .supplier_embeddings import SupplierCandidate, SupplierEmbeddingText, review_supplier_match


def supplier_advisories(client, instance, suppliers, embedder):
    identities = [{field: row[field] for field in ('code', 'name', 'tax_id')} for row in suppliers]
    response = client.call('payables_supplier_options', instance=instance, suppliers=identities)
    if not isinstance(response, dict) or set(response) != {'suppliers'} or not isinstance(response['suppliers'], list):
        raise ValueError('PAYABLES_SUPPLIER_OPTIONS_INVALID')
    by_code = {row['code']: row['name'] for row in identities}
    seen = set()
    advisories = {}
    counts = Counter()
    for option in response['suppliers']:
        if (not isinstance(option, dict) or set(option) != {'code', 'known', 'candidates'}
                or not isinstance(option['code'], str) or option['code'] not in by_code or option['code'] in seen
                or type(option['known']) is not bool or not isinstance(option['candidates'], list)
                or len(option['candidates']) > 8):
            raise ValueError('PAYABLES_SUPPLIER_OPTIONS_INVALID')
        code = option['code']
        seen.add(code)
        candidates = []
        for row in option['candidates']:
            if (not isinstance(row, dict) or set(row) != {'partner_id', 'name'}
                    or type(row['partner_id']) is not int or row['partner_id'] <= 0
                    or not isinstance(row['name'], str)):
                raise ValueError('PAYABLES_SUPPLIER_CANDIDATE_INVALID')
            candidates.append(SupplierCandidate(str(row['partner_id']), SupplierEmbeddingText(row['name'])))
        if not candidates:
            counts['no_candidates'] += 1
            continue
        # Identity already belongs to Odoo/SGA. Compare labels only so a
        # verified code match does not skip an actual name change.
        review = review_supplier_match(
            SupplierCandidate(code, SupplierEmbeddingText(by_code[code])), candidates, embedder=embedder,
        )
        needs_review = not option['known'] and review.status in ('unavailable', 'no_candidates')
        status = 'unavailable' if needs_review else review.status
        counts[status] += 1
        if status != 'ranked' and not needs_review:
            continue
        advisories[code] = {
            'status': status, 'profile': review.profile, 'model': review.model or embedder.model,
            'source_hash': review.source_hash,
            'candidates': [
                {'partner_id': int(row.key), 'score': row.score, 'input_hash': row.input_hash}
                for row in review.candidates
            ] if status == 'ranked' else [],
        }
    if seen != set(by_code):
        raise ValueError('PAYABLES_SUPPLIER_OPTIONS_INCOMPLETE')
    return advisories, counts
