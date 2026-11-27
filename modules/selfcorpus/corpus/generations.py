from .errors import CorpusError
from .lexical import fts_query
from .storage import event, now, ref


def current(db, scope):
    return db.execute('SELECT g.* FROM active a JOIN generations g ON g.ref=a.gen WHERE a.scope=?', (scope,)).fetchone()


def switch(db, scope, generation):
    db.execute('INSERT INTO active VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET gen=excluded.gen', (scope, generation))


def build(db, scope, model, dimension, chunker, operation, *, replacement=None, removal=None):
    previous = current(db, scope)
    if previous and (previous['embedding'], previous['dimension'], previous['chunker']) != (model, dimension, chunker):
        raise CorpusError('rebuild_required', 'This scope uses a different embedding or chunker version.', 409)
    if replacement:
        doc = replacement['document_ref']
        if db.execute('SELECT 1 FROM tombstones WHERE scope=? AND document_ref=?', (scope, doc)).fetchone():
            raise CorpusError('source_withdrawn', 'This document has been withdrawn from the scope.', 409)
        if previous and db.execute('SELECT 1 FROM generation_members WHERE gen=? AND document_ref=? AND parsed_ref=?',
                                   (previous['ref'], doc, replacement['parsed_document_ref'])).fetchone():
            return previous['ref']
    generation = ref('gen')
    db.execute('''INSERT INTO generations(ref,scope,embedding,dimension,chunker,created_at,operation,validation)
                  VALUES(?,?,?,?,?,?,?,'staging')''', (generation, scope, model, dimension, chunker, now(), operation))
    if previous:
        excluded = replacement['document_ref'] if replacement else removal
        db.execute('''INSERT INTO generation_members SELECT ?,document_ref,version_ref,parsed_ref FROM generation_members
                  WHERE gen=? AND document_ref<>?''', (generation, previous['ref'], excluded or ''))
    if replacement:
        db.execute('INSERT INTO generation_members VALUES(?,?,?,?)', (generation, replacement['document_ref'],
                   replacement['document_version_ref'], replacement['parsed_document_ref']))
    validate(db, generation, model, dimension, chunker, scope, replacement)
    switch(db, scope, generation)
    return generation


def validate(db, generation, model, dimension, chunker, scope, replacement):
    counts = db.execute('''SELECT count(DISTINCT m.document_ref) docs, count(c.id) chunks,
        count(e.chunk_id) vectors, count(f.rowid) lexical,
        count(DISTINCT CASE WHEN c.id IS NOT NULL THEN m.document_ref END) indexed_docs
        FROM generation_members m
        INNER JOIN documents d ON d.ref=m.document_ref AND d.scope=?
        INNER JOIN versions v ON v.ref=m.version_ref AND v.document_ref=d.ref
        INNER JOIN parsed p ON p.ref=m.parsed_ref AND p.version_ref=v.ref
        LEFT JOIN chunks c ON c.parsed_ref=p.ref AND c.chunker=?
        LEFT JOIN embeddings e ON e.chunk_id=c.id AND e.model=? AND e.dimension=?
        LEFT JOIN chunk_fts f ON f.rowid=c.id WHERE m.gen=?''', (scope, chunker, model, dimension, generation)).fetchone()
    members = db.execute('SELECT count(*) FROM generation_members WHERE gen=?', (generation,)).fetchone()[0]
    if members != counts['docs'] or counts['docs'] != counts['indexed_docs'] or not counts['chunks'] == counts['vectors'] == counts['lexical']:
        raise CorpusError('generation_invalid', 'Source, chunk and index references do not validate.', 422)
    if replacement:
        criteria = (replacement['parsed_document_ref'], chunker)
        sample = db.execute('SELECT id,text FROM chunks WHERE parsed_ref=? AND chunker=? ORDER BY ordinal LIMIT 1', criteria).fetchone()
        if not sample or not (match := fts_query(sample['text'])) or not db.execute(
                'SELECT 1 FROM chunk_fts WHERE rowid=? AND chunk_fts MATCH ?', (sample['id'], match)).fetchone():
            raise CorpusError('generation_probe_failed', 'The staged generation failed its lexical retrieval probe.', 422)
    db.execute("UPDATE generations SET documents=?,chunks=?,validation='valid' WHERE ref=?",
               (members, counts['chunks'], generation))


def withdraw(db, scope, document, subject):
    old = current(db, scope)
    if not old or not db.execute('SELECT 1 FROM generation_members WHERE gen=? AND document_ref=?',
                                 (old['ref'], document)).fetchone():
        raise CorpusError('not_found', 'No matching active document.', 404)
    db.execute('INSERT INTO tombstones VALUES(?,?,?)', (scope, document, now()))
    generation = build(db, scope, old['embedding'], old['dimension'], old['chunker'], 'withdraw', removal=document)
    event(db, scope, subject, 'withdrawn', {'document_ref':document, 'generation_ref':generation})
    return {'generation_ref':generation, 'document_ref':document, 'status':'withdrawn'}


def rollback(db, scope, generation, subject):
    row = db.execute("SELECT * FROM generations WHERE scope=? AND ref=? AND validation='valid'", (scope, generation)).fetchone()
    if not row:
        raise CorpusError('not_found', 'No matching validated generation.', 404)
    if db.execute('''SELECT 1 FROM generation_members m JOIN tombstones t ON t.document_ref=m.document_ref
                      WHERE m.gen=? AND t.scope=? LIMIT 1''', (generation, scope)).fetchone():
        raise CorpusError('rollback_withdrawn', 'That generation contains a withdrawn document.', 409)
    switch(db, scope, generation)
    event(db, scope, subject, 'rollback', {'generation_ref':generation})
    return {'generation_ref':generation, 'status':'active'}
