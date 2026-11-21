import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from bridge.supplier_embeddings import (
    EMBEDDING_PROFILE,
    OPENAI_EMBEDDINGS_URL,
    OpenAISupplierEmbedder,
    SupplierCandidate,
    SupplierEmbeddingInputError,
    SupplierEmbeddingText,
    SupplierEmbeddingUnavailable,
    review_supplier_match,
    supplier_embedding_input_hash,
)


MODEL = 'text-embedding-3-small'
TEST_KEY = 'fixture-no-real-key'


class Session:
    def __init__(self, vectors=(), status=200, error=None):
        self.vectors = vectors
        self.status_code = status
        self.error = error
        self.sent = []

    def post(self, endpoint, **options):
        self.sent.append((endpoint, options))
        if self.error:
            raise self.error
        return self

    def json(self):
        return {'model': MODEL, 'data': self.vectors}


def candidate(key, name, *, code='', tax_id='', aliases=()):
    return SupplierCandidate(key, SupplierEmbeddingText(name, aliases), code=code, tax_id=tax_id)


class SupplierEmbeddingsTest(TestCase):
    def test_exact_code_and_formatted_tax_id_avoid_the_api(self):
        source = candidate('source', 'Distribuidora Ficticia', code='001', tax_id='CR 3-101-000000')
        exact = candidate('dest', 'Distribuidora Antigua', code='001', tax_id='3101000000')
        session = Session()
        review = review_supplier_match(source, [exact], embedder=self._client(session))
        self.assertEqual(review.status, 'exact')
        self.assertEqual(review.basis, ('code', 'tax_id'))
        self.assertEqual([match.key for match in review.candidates], ['dest'])
        self.assertNotEqual(review.source_hash, review.candidates[0].input_hash)
        self.assertEqual(session.sent, [])
        self.assertNotIn('3101000000', repr(source))

    def test_duplicate_or_conflicting_deterministic_identities_never_rank(self):
        source = candidate('source', 'Casa Ficticia', code='021', tax_id='ABC123456')
        same_code = candidate('code', 'Casa Ficticia', code='021', tax_id='XYZ123456')
        same_tax = candidate('tax', 'Casa Ficticia', code='022', tax_id='ABC123456')
        duplicate_tax = candidate('duplicate', 'Casa Ficticia', tax_id='ABC123456')
        session = Session()
        self.assertEqual(
            review_supplier_match(source, [same_code], embedder=self._client(session)).status,
            'conflict',
        )
        split = review_supplier_match(source, [same_code, same_tax, duplicate_tax], embedder=self._client(session))
        self.assertEqual(split.status, 'conflict')
        self.assertEqual([match.key for match in split.candidates], ['code', 'duplicate', 'tax'])
        self.assertEqual(session.sent, [])

    def test_input_excludes_keys_identity_and_unsuitable_candidate_text(self):
        source = candidate('internal-source-key', '  Árbol Comercial 3R, S.A. ', code='021', tax_id='3000000000')
        candidates = [
            candidate('internal-other', 'Repuestos Simulados'),
            candidate('internal-near', 'Árbol Comercial', aliases=('Árbol Mayorista',)),
            candidate('internal-incompatible', 'Árbol Comercial', tax_id='3000000001'),
            candidate('internal-account', 'Proveedor CR76 0000 0000 0000 0000 01'),
            candidate('internal-email', 'Proveedor Ficticio', aliases=('contacto@example.invalid',)),
            candidate('internal-identity', '3-101-000001 S.A.'),
        ]
        session = Session([
            {'index': 2, 'embedding': [0.8, 0.6]},
            {'index': 0, 'embedding': [1, 0]},
            {'index': 1, 'embedding': [0.2, 0.98]},
        ])
        review = review_supplier_match(source, candidates, embedder=self._client(session))
        self.assertEqual((review.status, review.profile, review.model, review.dimensions, review.excluded),
                         ('ranked', EMBEDDING_PROFILE, MODEL, 2, 4))
        self.assertEqual([(match.key, match.score) for match in review.candidates], [
            ('internal-near', 0.8), ('internal-other', 0.19996),
        ])
        (endpoint, options), = session.sent
        self.assertEqual(endpoint, OPENAI_EMBEDDINGS_URL)
        self.assertEqual(options['json']['input'], ['arbol comercial 3r s a', 'repuestos simulados',
                                                    'arbol comercial\narbol mayorista'])
        self.assertEqual(options['headers'], {'Authorization': f'Bearer {TEST_KEY}'})
        self.assertFalse(options['allow_redirects'])
        self.assertNotIn(TEST_KEY, json.dumps(options['json']))
        self.assertNotIn('internal-', json.dumps(options['json']))

    def test_equivalent_inputs_share_a_hash_and_meaningful_changes_do_not(self):
        first = SupplierEmbeddingText('Ferretería Ficticia!', ('Marca Norte', 'Árbol'))
        equivalent = SupplierEmbeddingText('FERRETERIA FICTICIA', ('arbol', 'MARCA NORTE'))
        changed = SupplierEmbeddingText('Ferretería de Ensayo', ('Marca Norte', 'Árbol'))
        self.assertEqual(supplier_embedding_input_hash(first), supplier_embedding_input_hash(equivalent))
        self.assertNotEqual(supplier_embedding_input_hash(first), supplier_embedding_input_hash(changed))
        session = Session([{'index': 0, 'embedding': [1, 0]}])
        review = review_supplier_match(SupplierCandidate('origin', first), [
            SupplierCandidate('z', equivalent), SupplierCandidate('a', first),
        ], limit=1, embedder=self._client(session))
        self.assertEqual(review.candidates[0].key, 'a')
        self.assertEqual(review.candidates[0].input_hash, review.source_hash)
        self.assertEqual(len(session.sent[0][1]['json']['input']), 1)

    def test_unsafe_source_or_direct_client_input_never_calls_provider(self):
        for name in ('Ficticia contacto＠example.invalid', 'Cuenta bancaria Ficticia', 'Ficticia 1234-56789',
                     'https://example.invalid', '3-101-000001', 'Proveedor CF 0000 0000 0000 0000'):
            with self.subTest(name=name):
                session = Session()
                client = self._client(session)
                source = candidate('source', name)
                review = review_supplier_match(source, [candidate('dest', 'Proveedor Simulado')], embedder=client)
                self.assertEqual((review.status, review.reason, review.source_hash), ('unavailable', 'source_input_unavailable', None))
                with self.assertRaises(SupplierEmbeddingInputError):
                    client.embed((SupplierEmbeddingText('Nombre permitido'), source.text))
                self.assertEqual(session.sent, [])

    def test_malformed_responses_and_upstream_errors_have_no_raw_context(self):
        one = SupplierEmbeddingText('Nombre ficticio')
        invalid = (
            Session([], 301),
            Session([{'index': 0, 'embedding': [0.1, 0.2]}, {'index': 0, 'embedding': [0.3, 0.4]}]),
            Session([{'index': True, 'embedding': [0.1, 0.2]}]),
            Session([{'index': 0, 'embedding': [float('nan'), 0.2]}]),
            Session([{'index': 0, 'embedding': [0, 0]}]),
            Session([{'index': 0, 'embedding': [0.1]}]),
            Session(error=requests.Timeout('SENSITIVE-FIXTURE')),
        )
        for session in invalid:
            with self.subTest(status=session.status_code):
                client = self._client(session)
                with self.assertRaises(SupplierEmbeddingUnavailable) as error:
                    client.embed((one,))
                self.assertNotIn('SENSITIVE-FIXTURE', str(error.exception))
                self.assertNotIn(TEST_KEY, str(error.exception))
        blocked = review_supplier_match(SupplierCandidate('source', one), [candidate('dest', 'Otro nombre')],
                                        embedder=self._client(Session([], 429)))
        self.assertEqual((blocked.status, blocked.reason, blocked.candidates), ('unavailable', 'provider_unavailable', ()))

    def test_credential_is_loaded_only_from_the_explicit_file(self):
        session = Session([{'index': 0, 'embedding': [1, 0]}])
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture-key'
            with self.assertRaises(SupplierEmbeddingUnavailable):
                OpenAISupplierEmbedder.from_key_file(path)
            path.write_text(f'{TEST_KEY}\n', encoding='utf-8')
            client = OpenAISupplierEmbedder.from_key_file(path, dimensions=2, session=session)
        client.embed((SupplierEmbeddingText('Proveedor Ficticio'),))
        self.assertEqual(session.sent[0][1]['headers'], {'Authorization': f'Bearer {TEST_KEY}'})

    def test_reuses_and_bounds_run_cache_and_closes_its_transport(self):
        response = Session([{'index': 0, 'embedding': [1, 0]}])
        response.close = Mock()
        name = SupplierEmbeddingText('Nombre Comercial')
        with (
            patch('bridge.supplier_embeddings.requests.Session', return_value=response),
            patch('bridge.supplier_embeddings._CACHE_LIMIT', 1),
        ):
            client = OpenAISupplierEmbedder(api_key=TEST_KEY, dimensions=2)
            client.embed((name,))
            self.assertEqual(client.embed((SupplierEmbeddingText('Nómbre comercial'),)), ((1.0, 0.0),))
            self.assertEqual(len(response.sent), 1)
            client.embed((SupplierEmbeddingText('Almacén Simulado'),))
            client.embed((name,))
            self.assertEqual(len(response.sent), 3)
            client.close()
        response.close.assert_called_once_with()
        with self.assertRaises(SupplierEmbeddingUnavailable):
            client.embed((name,))

    @staticmethod
    def _client(session):
        return OpenAISupplierEmbedder(api_key=TEST_KEY, dimensions=2, session=session)
