import json
import re
from collections import defaultdict
from collections.abc import Callable
from heapq import nlargest

from .errors import CorpusError
from .generations import current
from .lexical import fts_query, tokens
from .relevance import Relevance
from .sources import normalize_doi
from .vector_index import ExactVectorIndex, encode_vector

_BASE = ''' FROM generation_members m JOIN versions v ON v.ref=m.version_ref
JOIN chunks c ON c.parsed_ref=m.parsed_ref AND c.chunker=? '''
_COLUMNS = '''m.document_ref,m.version_ref AS document_version_ref,c.ref AS chunk_ref,v.title,v.doi,
v.source_uri,v.source_hash,c.text AS content,c.heading_path,c.page_start,c.page_end,c.ordinal'''
_DOCUMENT_REF = re.compile(r'doc_[0-9a-f]{40}')


def evidence(row, scope, generation):
    headings = json.loads(row['heading_path'])
    base = {k:row[k] for k in ('document_ref','document_version_ref','chunk_ref','title','doi','source_uri',
                                'source_hash','content','page_start','page_end','ordinal')}
    return {**base, 'heading_path':headings, 'section':headings[-1] if headings else None,
            'page':row['page_start'], 'citation_ref':'corpus:' + row['chunk_ref'],
            'scope_ref':scope, 'generation_ref':generation}


def document_chunks(db, generation, document_ref):
    rows = db.execute('SELECT ' + _COLUMNS + _BASE + ' WHERE m.gen=? AND m.document_ref=? ORDER BY c.ordinal',
                      (generation['chunker'], generation['ref'], document_ref))
    return [evidence(row, generation['scope'], generation['ref']) for row in rows]


def _lexical(db, clause, params, match, query, cap, admit, *, extra_columns=''):
    if not match:
        return []
    # Avoid global BM25 document frequencies changing a private scope's scores.
    needle = set(list(dict.fromkeys(tokens(query)))[:64])
    rows = db.execute('SELECT ' + _COLUMNS + extra_columns + ',f.terms AS indexed_terms' + _BASE +
        ' JOIN chunk_fts f ON f.rowid=c.id WHERE m.gen=?' + clause + ' AND chunk_fts MATCH ?', [*params, match])
    def key(row):
        words = row['indexed_terms'].split()
        matched = needle.intersection(words)
        return (len(matched), len(matched) / max(1, len(words)), row['chunk_ref'])
    return nlargest(cap, (row for row in rows if admit(row['content'])), key=key)


def _filter(filters):
    if not isinstance(filters, dict) or filters.keys() - {'doi','year','document_ref'}:
        raise CorpusError('invalid_filters', 'Supported filters are doi, year and document_ref.')
    values, clauses = [], []
    for key, column in [('doi','lower(v.doi)'), ('year','v.year'), ('document_ref','m.document_ref')]:
        if key not in filters:
            continue
        val = filters[key]
        if (key == 'year' and type(val) is not int) or (key != 'year' and not isinstance(val, str)):
            raise CorpusError('invalid_filters', 'Invalid filter value.')
        clauses.append(' AND ' + column + '=?')
        values.append((_doi(val) or val.lower()) if key == 'doi' else val)
    return ''.join(clauses), values


def _doi(value):
    try:
        return normalize_doi(value)
    except CorpusError:
        return None


def _identifier(query):
    query = query.strip()
    if _DOCUMENT_REF.fullmatch(query):
        return ' AND m.document_ref=?', query
    if doi := _doi(query):
        return ' AND lower(v.doi)=?', doi
    return None


def _find_identifier(db, generations, clause, extra, limit):
    used, coverage, results = [], [], []
    for scope, g in generations:
        used.append({'scope_ref':scope, 'generation_ref':g['ref']})
        coverage.append({'scope_ref':scope, 'lexical':False, 'vector':False, 'documents':g['documents']})
        if len(results) < limit:
            rows = db.execute('SELECT ' + _COLUMNS + _BASE + ' WHERE m.gen=?' + clause +
                              ' ORDER BY m.document_ref,c.ordinal LIMIT ?',
                              [g['chunker'], g['ref'], *extra, limit - len(results)])
            results.extend({**evidence(row, scope, g['ref']), 'score':1.0} for row in rows)
    return {'status':'complete', 'index_snapshot':used, 'results':results, 'coverage':coverage, 'limitations':[]}


class Retrieval:
    def __init__(self, embedding, vector_index=None, reranker: Callable | None = None):
        self.embedding = embedding
        self.vector_index = vector_index or ExactVectorIndex()
        self.reranker = reranker

    def search(self, db, scopes, query, filters, limit):
        clause, extra = _filter(filters)
        generations = [(s, current(db, s)) for s in scopes]
        generations = [(s,g) for s,g in generations if g is not None]
        if identifier := _identifier(query):
            exact_clause, value = identifier
            return _find_identifier(db, generations, clause + exact_clause, [*extra, value], limit)
        relevance = Relevance(query, self.embedding.signature)
        used, coverage, limitations, hits = [], [], [], []
        qvector = None
        vector_error = False
        match = fts_query(query)
        cap = min(200, max(24, limit*4))
        for scope, g in generations:
            used.append({'scope_ref':scope, 'generation_ref':g['ref']})
            params = [g['chunker'], g['ref'], *extra]
            words = _lexical(db, clause, params, match, query, cap, relevance.lexical)
            supported = {row['chunk_ref'] for row in words}
            vectors = []
            vector_available = (g['embedding'], g['dimension']) == (self.embedding.signature, self.embedding.dimension)
            if vector_available and not vector_error:
                try:
                    if qvector is None:
                        qvector = encode_vector(self.embedding.embed([query], query=True)[0], g['dimension'])
                    rows = db.execute('SELECT ' + _COLUMNS + ',e.vector' + _BASE +
                        ' JOIN embeddings e ON e.chunk_id=c.id AND e.model=? AND e.dimension=? WHERE m.gen=?' + clause,
                        [params[0], g['embedding'], g['dimension'], *params[1:]])
                    vectors = self.vector_index.rank(
                        rows, qvector, g['dimension'], cap,
                        admit=lambda row, cosine: (
                            row['chunk_ref'] in supported or relevance.semantic(row['content'], cosine)
                        ),
                    )
                except (CorpusError, ValueError, RuntimeError, IndexError):
                    vector_error = True
            vector_available = vector_available and not vector_error
            if not vector_available:
                limitations.append({'scope_ref':scope, 'code':'vector_unavailable', 'message':'Only lexical retrieval was available.'})
            coverage.append({'scope_ref':scope,'lexical':bool(match),'vector':vector_available, 'documents':g['documents']})
            score, entries = defaultdict(float), {}
            for ranked in (words, vectors):
                for rank, row in enumerate(ranked, 1):
                    key = row['chunk_ref']
                    score[key] += 1 / (60 + rank)
                    entries[key] = row
            hits.extend((points, evidence(entries[key], scope, g['ref'])) for key, points in score.items())
        hits.sort(key=lambda h: (-h[0], h[1]['chunk_ref']))
        results = [{**item, 'score':score} for score,item in hits[:cap]]
        if results and self.reranker:
            try:
                ids = self.reranker(query, tuple((r['chunk_ref'],r['content']) for r in results))
                position = {key:i for i,key in enumerate(ids)}
                if position.keys() != {r['chunk_ref'] for r in results} or len(position) != len(ids):
                    raise ValueError('invalid reranking')
                results.sort(key=lambda r:position[r['chunk_ref']])
            except Exception:
                limitations.append({'code':'reranker_unavailable','message':'Kept hybrid ranking.'})
        return {'status':'partial' if limitations else 'complete', 'index_snapshot':used, 'results':results[:limit],
                'coverage':coverage, 'limitations':limitations}
