"""Suggest supplier matches from business names without transmitting identifiers."""

import math
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Iterable

import requests


EMBEDDING_PROFILE = 'sga-supplier-business-name-v1'
OPENAI_EMBEDDINGS_URL = 'https://api.openai.com/v1/embeddings'
DEFAULT_MODEL = 'text-embedding-3-small'
DEFAULT_DIMENSIONS = 256
MAX_RANK_CANDIDATES = 200
_CACHE_LIMIT = 256
_MODEL_DIMENSIONS = {DEFAULT_MODEL: 1536, 'text-embedding-3-large': 3072}
_RESTRICTED_TEXT = re.compile(
    r'@|https?\s*://|\bwww\.|\b(?:iban|sinpe|e-?mail|correo|cedula|bban)\b|'
    r'\b(?:cuenta|referencia)\s+(?:bancaria|banco|destino|de\s+pago)\b|\bsk-proj-',
    re.IGNORECASE,
)


class SupplierEmbeddingInputError(ValueError):
    """A source name or commercial alias cannot safely be embedded."""


class SupplierEmbeddingUnavailable(RuntimeError):
    """Embedding credentials, service or response were unavailable."""


@dataclass(frozen=True)
class SupplierEmbeddingText:
    legal_name: str = field(repr=False)
    references: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class SupplierCandidate:
    key: str
    text: SupplierEmbeddingText
    code: str = field(default='', repr=False)
    tax_id: str = field(default='', repr=False)


@dataclass(frozen=True)
class SupplierRankedCandidate:
    key: str
    score: float | None = None
    input_hash: str | None = None


@dataclass(frozen=True)
class SupplierMatchReview:
    status: str
    candidates: tuple[SupplierRankedCandidate, ...] = ()
    source_hash: str | None = None
    profile: str = EMBEDDING_PROFILE
    model: str | None = None
    dimensions: int | None = None
    basis: tuple[str, ...] = ()
    excluded: int = 0
    reason: str = ''


def _component(value: str, max_length: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length or not value.isprintable():
        raise SupplierEmbeddingInputError('Nombre comercial no apto para embeddings.')
    decomposed = unicodedata.normalize('NFKD', value.casefold())
    plain = ''.join(character for character in decomposed if not unicodedata.combining(character))
    if _RESTRICTED_TEXT.search(plain) or sum(character.isdecimal() for character in plain) > 4:
        raise SupplierEmbeddingInputError('Nombre comercial no apto para embeddings.')
    normalized = ' '.join(''.join(character if character.isalnum() else ' ' for character in plain).split())
    if not normalized or not any(character.isalpha() for character in normalized):
        raise SupplierEmbeddingInputError('Nombre comercial no apto para embeddings.')
    return normalized


def _embedding_input(value: SupplierEmbeddingText) -> str:
    if not isinstance(value, SupplierEmbeddingText) or not isinstance(value.references, tuple) or len(value.references) > 2:
        raise SupplierEmbeddingInputError('Se requiere un nombre legal y hasta dos alias comerciales.')
    name = _component(value.legal_name, 160)
    aliases = {_component(alias, 80) for alias in value.references} - {name}
    return '\n'.join((name, *sorted(aliases)))


def _input_hash(normalized: str) -> str:
    return sha256(f'{EMBEDDING_PROFILE}\n{normalized}'.encode('utf-8')).hexdigest()


def supplier_embedding_input_hash(value: SupplierEmbeddingText) -> str:
    """Fingerprint the exact profile input without the embedding or identity fields."""
    return _input_hash(_embedding_input(value))


def _try_normalize(value: SupplierEmbeddingText) -> str | None:
    try:
        return _embedding_input(value)
    except SupplierEmbeddingInputError:
        return None


def _code_key(value: str) -> str:
    key = value.strip().upper() if isinstance(value, str) else ''
    return key if re.fullmatch(r'[0-9A-Z]{1,3}', key) else ''


def _tax_key(value: str) -> str:
    key = re.sub(r'[ .-]', '', unicodedata.normalize('NFKC', value).upper()) if isinstance(value, str) else ''
    if re.fullmatch(r'CR[0-9]{9,12}', key):
        return key[2:]
    return key if re.fullmatch(r'[0-9A-Z]{6,20}', key) else ''


def _unit_vector(value, dimensions: int) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != dimensions:
        raise SupplierEmbeddingUnavailable('Dimensiones inválidas en la respuesta de embeddings.')
    if any(type(item) not in (int, float) or not -1 <= item <= 1 for item in value):
        raise SupplierEmbeddingUnavailable('Vector inválido en la respuesta de embeddings.')
    norm = math.hypot(*value)
    if not norm:
        raise SupplierEmbeddingUnavailable('Vector vacío en la respuesta de embeddings.')
    return tuple(item / norm for item in value)


class OpenAISupplierEmbedder:
    """A narrowly scoped client with no prompt, response or header logging."""

    def __init__(self, *, api_key: str, model=DEFAULT_MODEL, dimensions=DEFAULT_DIMENSIONS, session=None):
        if not isinstance(api_key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,512}', api_key):
            raise SupplierEmbeddingUnavailable('No hay una credencial de embeddings válida.')
        if (not isinstance(model, str) or model not in _MODEL_DIMENSIONS
                or type(dimensions) is not int or not 1 <= dimensions <= _MODEL_DIMENSIONS[model]):
            raise ValueError('Configuración del modelo de embeddings inválida.')
        self.model = model
        self.dimensions = dimensions
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None
        self._cache = OrderedDict()

    @classmethod
    def from_key_file(cls, path, **options):
        try:
            with Path(path).open(encoding='utf-8') as key_file:
                api_key = key_file.read(516).strip()
        except (OSError, TypeError, UnicodeError):
            raise SupplierEmbeddingUnavailable('No se pudo leer la credencial de embeddings.') from None
        return cls(api_key=api_key, **options)

    def embed(self, values: Iterable[SupplierEmbeddingText]) -> tuple[tuple[float, ...], ...]:
        if self._api_key is None:
            raise SupplierEmbeddingUnavailable('El cliente de embeddings está cerrado.')
        normalized = [_embedding_input(value) for value in values]
        if len(normalized) > MAX_RANK_CANDIDATES + 1:
            raise SupplierEmbeddingInputError('Demasiados nombres para revisar en una solicitud.')
        if not normalized:
            return ()
        keys = {value: _input_hash(value) for value in dict.fromkeys(normalized)}
        by_input = {}
        for value, key in keys.items():
            if key in self._cache:
                by_input[value] = self._cache[key]
                self._cache.move_to_end(key)
        missing = tuple(value for value in keys if value not in by_input)
        if missing:
            if self._session is None:
                self._session = requests.Session()
            vectors = self._request(self._session, missing)
            for value, vector in zip(missing, vectors, strict=True):
                by_input[value] = vector
                self._cache[keys[value]] = vector
            while len(self._cache) > _CACHE_LIMIT:
                self._cache.popitem(last=False)
        return tuple(by_input[value] for value in normalized)

    def close(self) -> None:
        """Release the owned transport and discard this run's cache."""
        if self._owns_session and self._session is not None:
            self._session.close()
        self._session = None
        self._api_key = None
        self._cache.clear()

    def _request(self, session, inputs):
        try:
            response = session.post(
                OPENAI_EMBEDDINGS_URL,
                headers={'Authorization': f'Bearer {self._api_key}'},
                json={
                    'model': self.model, 'dimensions': self.dimensions,
                    'encoding_format': 'float', 'input': list(inputs),
                },
                timeout=(3, 20),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise SupplierEmbeddingUnavailable('El servicio de embeddings no está disponible.')
            body = response.json()
        except (requests.RequestException, ValueError):
            raise SupplierEmbeddingUnavailable('El servicio de embeddings no está disponible.') from None
        if not isinstance(body, dict) or body.get('model') != self.model or not isinstance(body.get('data'), list):
            raise SupplierEmbeddingUnavailable('La respuesta de embeddings no corresponde al modelo solicitado.')
        indexed = {}
        for item in body['data']:
            index = item.get('index') if isinstance(item, dict) else None
            if type(index) is not int or not 0 <= index < len(inputs) or index in indexed:
                raise SupplierEmbeddingUnavailable('Índices inválidos en la respuesta de embeddings.')
            indexed[index] = _unit_vector(item.get('embedding'), self.dimensions)
        if len(indexed) != len(inputs):
            raise SupplierEmbeddingUnavailable('La respuesta de embeddings está incompleta.')
        return tuple(indexed[index] for index in range(len(inputs)))


def review_supplier_match(
    source: SupplierCandidate,
    candidates: Iterable[SupplierCandidate],
    *,
    embedder=None,
    limit=5,
) -> SupplierMatchReview:
    """Compare records from one source/company before suggesting similarity-only matches.

    Candidate keys are opaque, stable caller identifiers. A source code is
    authoritative only within the same SGA source instance. The result is a
    review record, with no side effects on the supplier master.
    """
    candidates = tuple(candidates)
    if type(limit) is not int or not 1 <= limit <= MAX_RANK_CANDIDATES:
        raise ValueError('Límite de candidatos inválido.')
    keys = {candidate.key for candidate in candidates}
    if len(keys) != len(candidates) or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError('Cada candidato necesita una clave única.')
    source_text = _try_normalize(source.text)
    source_hash = _input_hash(source_text) if source_text else None
    code, tax = _code_key(source.code), _tax_key(source.tax_id)
    by_code = tuple(candidate for candidate in candidates if code and _code_key(candidate.code) == code)
    by_tax = tuple(candidate for candidate in candidates if tax and _tax_key(candidate.tax_id) == tax)
    if by_code or by_tax:
        exact = {candidate.key: candidate for candidate in (*by_code, *by_tax)}
        basis = tuple(name for name, matching in (('code', by_code), ('tax_id', by_tax)) if matching)
        conflict = len(exact) != 1 or any(
            code and _code_key(candidate.code) not in ('', code)
            or tax and _tax_key(candidate.tax_id) not in ('', tax)
            for candidate in exact.values()
        )
        matches = []
        for key, candidate in sorted(exact.items()):
            candidate_text = _try_normalize(candidate.text)
            matches.append(SupplierRankedCandidate(key, input_hash=_input_hash(candidate_text) if candidate_text else None))
        return SupplierMatchReview('conflict' if conflict else 'exact', tuple(matches), source_hash, basis=basis)

    if not candidates:
        return SupplierMatchReview('no_candidates', source_hash=source_hash)
    if not source_text or not embedder or len(candidates) > MAX_RANK_CANDIDATES:
        reason = 'source_input_unavailable' if not source_text else (
            'candidate_limit' if len(candidates) > MAX_RANK_CANDIDATES else 'provider_unavailable'
        )
        return SupplierMatchReview('unavailable', source_hash=source_hash, reason=reason)

    eligible = []
    for candidate in candidates:
        candidate_code, candidate_tax = _code_key(candidate.code), _tax_key(candidate.tax_id)
        if (code and candidate_code and code != candidate_code) or (tax and candidate_tax and tax != candidate_tax):
            continue
        normalized = _try_normalize(candidate.text)
        if normalized:
            eligible.append((candidate, _input_hash(normalized)))
    excluded = len(candidates) - len(eligible)
    metadata = dict(source_hash=source_hash, model=embedder.model, dimensions=embedder.dimensions, excluded=excluded)
    if not eligible:
        return SupplierMatchReview('no_candidates', **metadata)
    try:
        inputs = (source.text, *(candidate.text for candidate, _ in eligible))
        vectors = tuple(_unit_vector(vector, embedder.dimensions) for vector in embedder.embed(inputs))
        if len(vectors) != len(inputs):
            raise SupplierEmbeddingUnavailable('La respuesta de embeddings está incompleta.')
    except SupplierEmbeddingUnavailable:
        return SupplierMatchReview('unavailable', reason='provider_unavailable', **metadata)

    scores = []
    for (candidate, fingerprint), vector in zip(eligible, vectors[1:], strict=True):
        similarity = math.fsum(left * right for left, right in zip(vectors[0], vector, strict=True))
        scores.append(SupplierRankedCandidate(candidate.key, round(min(1.0, max(-1.0, similarity)), 6), fingerprint))
    scores.sort(key=lambda item: (-item.score, item.key))
    return SupplierMatchReview('ranked', tuple(scores[:limit]), basis=('name_similarity',), **metadata)
