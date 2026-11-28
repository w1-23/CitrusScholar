"""Published Evolution membership, recall, and stored passage readback."""
from collections import defaultdict

from .errors import CorpusError
from .generations import current
from .lexical import fts_query
from .relevance import Relevance
from .retrieval import _BASE, _COLUMNS, _filter, _identifier, _lexical
from .storage import digest
from .vector_index import encode_vector


def pin(db, scopes):
    return {scope: g for scope in scopes if (g := current(db, scope)) is not None}


class EvolutionReader:
    def __init__(self, retrieval):
        self.retrieval = retrieval
        self.embedding = retrieval.embedding
        self.query_vectors = {}
        self.vector_failed = set()

    def search(self, db, scope, g, query, filters, caps, check):
        clauses, args = _filter(filters)
        params = [g['chunker'], g['ref'], *args]
        base = 'SELECT ' + _COLUMNS + ',c.content_hash,v.year' + _BASE + ' WHERE m.gen=?' + clauses
        reasons = []
        if not sum(caps.values()):
            return [], ['candidate_budget_exhausted'], 0
        if ident := _identifier(query):
            where, value = ident
            rows = list(db.execute(base + where + ' ORDER BY m.document_ref,c.ordinal LIMIT ?',
                                   [*params, value, sum(caps.values())]))
            check()
            return [self._row(item, scope, g) for item in rows], reasons, len(rows)
        relevance = Relevance(query, self.embedding.signature)
        match = fts_query(query)
        if caps['lexical']:
            words = _lexical(db, clauses, params, match, query, caps['lexical'],
                             lambda text: (check(), relevance.lexical(text))[1], extra_columns=',c.content_hash,v.year')
        else:
            words = []
            reasons.append('candidate_budget_exhausted')
        supported = {row['chunk_ref'] for row in words}
        vectors = []
        if not caps['dense']:
            reasons.append('candidate_budget_exhausted')
        elif query in self.vector_failed or (g['embedding'], g['dimension']) != (self.embedding.signature, self.embedding.dimension):
            reasons.append('vector_unavailable')
        else:
            try:
                check()
                if query not in self.query_vectors:
                    self.query_vectors[query] = encode_vector(self.embedding.embed([query], query=True)[0], g['dimension'])
                rows = db.execute('SELECT ' + _COLUMNS + ',c.content_hash,v.year,e.vector' + _BASE +
                    ' JOIN embeddings e ON e.chunk_id=c.id AND e.model=? AND e.dimension=? WHERE m.gen=?' + clauses,
                    [params[0], g['embedding'], g['dimension'], *params[1:]])
                def checked_rows():
                    for row in rows:
                        check()
                        yield row
                vectors = self.retrieval.vector_index.rank(checked_rows(), self.query_vectors[query], g['dimension'], caps['dense'],
                            admit=lambda row, cosine: row['chunk_ref'] in supported or relevance.semantic(row['translation_en'] or row['content'], cosine))
            except (CorpusError, ValueError, RuntimeError, IndexError):
                self.vector_failed.add(query)
                reasons.append('vector_unavailable')
        check()
        score, rows = defaultdict(float), {}
        for stream in (words, vectors):
            for rank, row in enumerate(stream, 1):
                key = row['chunk_ref']
                rows[key] = row
                score[key] += 1 / (60 + rank)
        ordered = sorted(rows, key=lambda k: (-score[k], k))
        return [self._row(rows[key], scope, g) for key in ordered], sorted(set(reasons)), len(words) + len(vectors)

    @staticmethod
    def _row(row, scope, g):
        return {'source_id': 'evolution', 'scope_ref': scope, 'generation_ref': g['ref'],
                'document_ref': row['document_ref'], 'version_ref': row['document_version_ref'],
                'passage_ref': row['chunk_ref'], 'title': row['title'] or '', 'doi': row['doi'],
                'year': row['year'], 'source_hash': row['source_hash'], 'passage_hash': row['content_hash'],
                'text': row['content'], 'locator': {'chunk_ref': row['chunk_ref']}}


def read_passage(db, actor, identity):
    scope = identity['scope_ref']
    if not actor.can_read(scope):
        return None
    row = db.execute('''SELECT c.text,c.content_hash FROM generations g
        JOIN generation_members m ON m.gen=g.ref
        JOIN documents d ON d.ref=m.document_ref AND d.scope=g.scope
        JOIN versions v ON v.ref=m.version_ref AND v.document_ref=d.ref
        JOIN parsed p ON p.ref=m.parsed_ref AND p.version_ref=v.ref
        JOIN chunks c ON c.parsed_ref=p.ref AND c.chunker=g.chunker
        WHERE g.ref=? AND g.scope=? AND g.validation='valid' AND m.document_ref=?
          AND m.version_ref=? AND c.ref=? AND NOT EXISTS (
            SELECT 1 FROM tombstones t WHERE t.scope=g.scope AND t.document_ref=m.document_ref)
        ''', (identity['generation_ref'], scope, identity['document_ref'], identity['version_ref'],
              identity['passage_ref'])).fetchone()
    if not row or row['content_hash'] != identity['passage_hash'] or digest(row['text']) != row['content_hash']:
        return None
    return {'text': row['text'], 'span': {'start': 0, 'end': len(row['text'])}}
