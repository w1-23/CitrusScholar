import json
import re

from .catalog import active_doi
from .errors import CorpusError
from .generations import current, rollback, withdraw
from .queue import VERIFIED_SHARED_JOB, enqueue, pending_intake_doi, receipt
from .retrieval import Retrieval, document_chunks
from .sources import VERIFIED_TEXT, validate_verified_spec
from .storage import dumps, event, now
from .unified import Unified

_SCOPE_ID = re.compile(r'[A-Za-z0-9._@-]{1,128}')


def forbidden():
    return CorpusError('forbidden', 'Requested scope is not authorized.', 403)


def not_found():
    return CorpusError('not_found', 'No matching accessible resource.', 404)


def _valid_scope(scope):
    if scope != 'shared':
        if not isinstance(scope, str) or ':' not in scope:
            raise CorpusError('invalid_scope', 'Expected personal:<subject>, project:<ref> or shared.')
        family, ident = scope.split(':', 1)
        if family not in {'personal','project'} or not _SCOPE_ID.fullmatch(ident):
            raise CorpusError('invalid_scope', 'Expected personal:<subject>, project:<ref> or shared.')


def _search_scopes(actor, selection):
    if not isinstance(selection, dict) or selection.keys() - {'personal','project_refs','shared'}:
        raise CorpusError('invalid_scope', 'Invalid retrieval scope.')
    personal, shared, projects = selection.get('personal', False), selection.get('shared', False), selection.get('project_refs', [])
    if type(personal) is not bool or type(shared) is not bool or not isinstance(projects, list) or len(projects)>32:
        raise CorpusError('invalid_scope', 'Invalid retrieval scope.')
    if any(not isinstance(p, str) or not _SCOPE_ID.fullmatch(p) for p in projects):
        raise CorpusError('invalid_scope', 'Invalid project reference.')
    scopes = ([f'personal:{actor.subject}'] if personal else []) + ['project:'+p for p in projects] + (['shared'] if shared else [])
    if not all(actor.can_read(s) for s in scopes):
        raise forbidden()
    return list(dict.fromkeys(scopes))


def _valid_idempotency_key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 160 or not key.strip():
        raise CorpusError('invalid_idempotency_key', 'A nonempty idempotency key is required.')


class App:
    def __init__(self, database, embedding, *, vector_index=None, reranker=None, legacy=None):
        self.database = database
        self.retrieval = Retrieval(embedding, vector_index, reranker)
        self.unified = Unified(database, self.retrieval, legacy)

    def health(self):
        with self.database.connection() as db:
            db.execute('SELECT count(*) FROM active').fetchone()
        return {'status':'ok','service':'selfcorpus','schema':1}

    def submit(self, actor, spec, scope_ref, idempotency_key):
        _valid_scope(scope_ref)
        if scope_ref == 'shared' or not actor.can_write(scope_ref):
            raise forbidden()
        _valid_idempotency_key(idempotency_key)
        if not isinstance(spec, dict) or spec.get('kind') not in ('upload', 'url', 'doi', VERIFIED_TEXT):
            raise CorpusError('invalid_source', 'Unsupported source kind.')
        if spec['kind'] == VERIFIED_TEXT:
            if scope_ref != f'personal:{actor.subject}':
                raise forbidden()
            return self._submit_verified(actor, spec, scope_ref, idempotency_key)
        with self.database.transaction(write=True) as db:
            return enqueue(db, actor.subject, scope_ref, idempotency_key, spec)

    def submit_verified_shared(self, actor, spec, idempotency_key):
        if not actor.can_read('shared'):
            raise forbidden()
        _valid_idempotency_key(idempotency_key)
        if not isinstance(spec, dict) or spec.get('kind') != VERIFIED_TEXT:
            raise CorpusError('invalid_source', 'Shared automatic intake requires verified PMC Cloud text.')
        return self._submit_verified(actor, spec, 'shared', idempotency_key)

    def _submit_verified(self, actor, spec, scope, key):
        validate_verified_spec(spec)
        doi = spec['doi']
        job_spec = {'kind': VERIFIED_SHARED_JOB, 'source': spec} if scope == 'shared' else spec
        with self.database.transaction(write=True) as db:
            def reject_duplicate():
                if active_doi(db, scope, doi):
                    raise CorpusError('already_present', 'The exact DOI is already active in this scope.', 409)
                if pending_intake_doi(db, scope, doi):
                    raise CorpusError('already_pending', 'This scope already has pending verified text for the DOI.', 409)

            return enqueue(db, actor.subject, scope, key, job_spec, check=reject_duplicate,
                           detail={'verified_doi': doi})

    def _request_row(self, db, actor, request_ref):
        row = db.execute('SELECT * FROM requests WHERE ref=?', (request_ref,)).fetchone()
        if not row or not actor.can_read(row['scope']) or (row['owner'] != actor.subject and not (row['scope'] == 'shared' and actor.governor)):
            raise not_found()
        return row

    def request(self, actor, request_ref):
        with self.database.transaction() as db:
            return receipt(db, self._request_row(db, actor, request_ref))

    def search(self, actor, query, scope, filters, limit):
        scopes = _search_scopes(actor, scope)
        if not isinstance(query, str) or not query.strip() or len(query)>4096:
            raise CorpusError('invalid_query', 'Query must contain 1 to 4096 characters.')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise CorpusError('invalid_limit', 'Limit must be between 1 and 50.')
        with self.database.transaction() as db:
            return self.retrieval.search(db, scopes, query, filters, limit)

    def document(self, actor, ref, chunks=False):
        with self.database.transaction() as db:
            doc = db.execute('SELECT * FROM documents WHERE ref=?', (ref,)).fetchone()
            if not doc or not actor.can_read(doc['scope']):
                raise not_found()
            g = current(db, doc['scope'])
            if not g:
                raise not_found()
            row = db.execute('''SELECT m.version_ref,m.parsed_ref,v.metadata,v.source_uri,v.source_hash,v.retrieved_at,
                      v.source_kind,v.access_note,v.http_status,p.parser,p.quality FROM generation_members m
                      JOIN versions v ON v.ref=m.version_ref JOIN parsed p ON p.ref=m.parsed_ref
                      WHERE m.gen=? AND m.document_ref=?''', (g['ref'], ref)).fetchone()
            if row is None:
                raise not_found()
            identity = {'document_ref':ref, 'scope_ref':doc['scope'],'generation_ref':g['ref'],
                        'document_version_ref':row['version_ref'],'parsed_document_ref':row['parsed_ref']}
            if chunks:
                return {**identity, 'chunks':document_chunks(db, g, ref)}
            fields = {key:row[key] for key in ('source_uri','source_hash','retrieved_at','source_kind','access_note','http_status','parser')}
            return {**identity, **fields, 'metadata':json.loads(row['metadata']), 'quality':json.loads(row['quality'])}

    def generation(self, actor, ref):
        with self.database.transaction() as db:
            g = db.execute('SELECT * FROM generations WHERE ref=? AND validation=\'valid\'', (ref,)).fetchone()
            if not g or not actor.can_read(g['scope']):
                raise not_found()
            active = current(db, g['scope'])
            return {'generation_ref':ref, 'scope_ref':g['scope'], 'embedding':g['embedding'], 'chunker':g['chunker'],
                    'dimension':g['dimension'], 'created_at':g['created_at'], 'documents':g['documents'], 'chunks':g['chunks'],
                    'validation':g['validation'], 'index_config':{'lexical':'fts5','vector':'exact-cosine'},
                    'active':bool(active and active['ref']==ref)}

    def proposal(self, actor, request_ref, approve=None):
        if approve is not None and (type(approve) is not bool or not actor.can_write('shared')):
            raise forbidden()
        with self.database.transaction(write=approve is not None) as db:
            row = self._request_row(db, actor, request_ref)
            if row['scope'] != 'shared':
                raise not_found()
            if approve is None:
                reviewed = receipt(db, row)
                version = reviewed['output'].get('document_version_ref')
                source = db.execute('SELECT source_uri,access_note,source_hash,media_type,metadata FROM versions WHERE ref=?', (version,)).fetchone()
                if source:
                    reviewed['source'] = {**dict(source), 'metadata':json.loads(source['metadata'])}
                return reviewed
            if approve:
                raise forbidden()
            target = 'rejected'
            if row['status'] == target:
                return receipt(db, row)
            if row['status'] != 'pending_review':
                raise CorpusError('proposal_not_pending', 'Proposal is not waiting for a decision.', 409)
            result = json.loads(row['result'])
            db.execute('UPDATE requests SET status=?,result=?,updated_at=? WHERE ref=?', (target,dumps(result),now(),request_ref))
            event(db, 'shared', actor.subject, target, {'proposal_decision':approve}, request_ref)
            return receipt(db, self._request_row(db, actor, request_ref))

    def withdraw(self, actor, scope_ref, document_ref):
        _valid_scope(scope_ref)
        if not actor.can_write(scope_ref):
            raise forbidden()
        with self.database.transaction(write=True) as db:
            return withdraw(db, scope_ref, document_ref, actor.subject)

    def rollback(self, actor, scope_ref, generation_ref):
        _valid_scope(scope_ref)
        if not actor.can_write(scope_ref):
            raise forbidden()
        with self.database.transaction(write=True) as db:
            return rollback(db, scope_ref, generation_ref, actor.subject)
