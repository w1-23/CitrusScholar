"""Strict Corpus-wide selection and candidate allocation contract."""
import re
from dataclasses import dataclass

from .auth import BASE_SOURCES
from .errors import CorpusError
from .sources import normalize_doi

EVOLUTION = 'evolution'
SOURCE_IDS = (*BASE_SOURCES, EVOLUTION)
SCOPE_ID = re.compile(r'[A-Za-z0-9._@-]{1,128}')
TOPIC_ALIASES = {
    'agriculture/ecology': '农业与生态', 'pathology/resistance': '病理与抗性',
    'physiology/metabolism': '生理与代谢', 'nutrition/health': '营养与健康',
    'breeding/biotechnology': '育种与生物技术', 'genomics/evolution': '基因组与进化',
}


@dataclass(frozen=True)
class Budget:
    max_candidates: int = 240
    max_results: int = 12
    max_chars: int = 24000
    timeout_ms: int = 4000

    @classmethod
    def parse(cls, payload):
        defaults = cls()
        limits = dict(max_candidates=600, max_results=50, max_chars=64000, timeout_ms=12000)
        fields(payload, optional=limits)
        values = {name: payload.get(name, getattr(defaults, name)) for name in limits}
        if any(type(val) is not int or not 1 <= val <= limits[name] for name, val in values.items()):
            raise CorpusError('invalid_budget', 'Invalid retrieval budget.')
        return cls(**values)

    def caps(self, lanes):
        """Allocate the sum once across a canonical ordering of source/scope lanes."""
        count = len(lanes) * 2
        if not count:
            return {}
        size, spill = divmod(self.max_candidates, count)
        return {lane: {channel: size + (2 * n + i < spill)
                       for i, channel in enumerate(('lexical', 'dense'))}
                for n, lane in enumerate(lanes)}


def fields(obj, required=(), optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        raise CorpusError('invalid_request', 'Unexpected or missing request fields.')


def _denied():
    return CorpusError('forbidden', 'Requested scope is not authorized.', 403)


def _scopes(actor, selection):
    fields(selection, optional=('personal', 'shared', 'projects'))
    personal, shared = (selection.get(key, False) for key in ('personal', 'shared'))
    projects = selection.get('projects', [])
    if (type(personal) is not bool or type(shared) is not bool or not isinstance(projects, list) or
            len(projects) > 32 or any(not isinstance(v, str) or not SCOPE_ID.fullmatch(v) for v in projects) or
            len(set(projects)) != len(projects)):
        raise CorpusError('invalid_scope', 'Invalid Evolution scope.')
    scopes = sorted([f'project:{p}' for p in projects] + ([f'personal:{actor.subject}'] if personal else []) +
                    (['shared'] if shared else []))
    if not scopes:
        raise CorpusError('invalid_scope', 'Select at least one Evolution scope.')
    if not all(actor.can_read(s) for s in scopes):
        raise _denied()
    return scopes


def requests(actor, payload):
    fields(payload, ('query', 'sources'), ('evolution_scope', 'filters', 'budget'))
    query, sources = payload['query'], payload['sources']
    if not isinstance(query, str) or not query.strip() or len(query) > 4096:
        raise CorpusError('invalid_query', 'Query must contain 1 to 4096 characters.')
    if not isinstance(sources, list) or not 1 <= len(sources) <= len(SOURCE_IDS):
        raise CorpusError('invalid_sources', 'Choose one or more Corpus sources.')
    choices = {}
    for item in sources:
        fields(item, ('source_id',), ('topics',))
        source, topics = item['source_id'], item.get('topics', [])
        if not isinstance(source, str) or source not in SOURCE_IDS or source in choices:
            raise CorpusError('invalid_sources', 'Unknown or duplicate Corpus source.')
        if source != EVOLUTION and source not in actor.base_sources:
            raise _denied()
        if (not isinstance(topics, list) or len(topics) > 32 or
                any(not isinstance(t, str) or not t.strip() or len(t) > 128 for t in topics)):
            raise CorpusError('invalid_topics', 'Invalid collection topic selector.')
        canonical = sorted({TOPIC_ALIASES.get(t.strip().casefold(), t.strip()) for t in topics})
        if any(t not in TOPIC_ALIASES.values() for t in canonical) or (source == EVOLUTION and canonical):
            raise CorpusError('invalid_topics', 'Unknown or unsupported collection topic.')
        choices[source] = canonical
    if 'evolution_scope' in payload and EVOLUTION not in choices:
        raise CorpusError('invalid_scope', 'Evolution scope requires the Evolution source.')
    scopes = _scopes(actor, payload.get('evolution_scope', {'personal': True, 'shared': actor.shared})) if EVOLUTION in choices else []
    raw = payload.get('filters', {})
    fields(raw, optional=('doi', 'year', 'document_ref'))
    filters = dict(raw)
    if 'doi' in filters:
        filters['doi'] = normalize_doi(filters['doi'])
    if 'year' in filters and (type(filters['year']) is not int or not 1000 <= filters['year'] <= 2999):
        raise CorpusError('invalid_filters', 'Invalid year filter.')
    if 'document_ref' in filters and (not isinstance(filters['document_ref'], str) or
            not re.fullmatch(r'[A-Za-z0-9._:@-]{1,200}', filters['document_ref'])):
        raise CorpusError('invalid_filters', 'Invalid document reference filter.')
    return query.strip(), choices, scopes, filters, Budget.parse(payload.get('budget', {}))
