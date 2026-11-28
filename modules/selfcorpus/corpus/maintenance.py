import json

from .errors import CorpusError
from .generations import current, switch, validate
from .storage import event, now, ref
from .vector_index import encode_vector


def reembed_scope(database, embedding, scope):
    """Prepare vectors outside the pointer transaction, fence on the original generation."""
    signature, dimension = embedding.signature, embedding.dimension
    with database.transaction() as db:
        old = current(db, scope)
        if not old:
            raise CorpusError('not_found','No active generation exists for this scope.', 404)
        if (old['embedding'], old['dimension']) == (signature,dimension):
            return old['ref']
        selection = db.execute('''SELECT c.id,COALESCE(t.text_en,c.text) AS text,v.title,c.heading_path FROM generation_members m
            JOIN versions v ON v.ref=m.version_ref JOIN chunks c ON c.parsed_ref=m.parsed_ref AND c.chunker=?
            LEFT JOIN chunk_translations t ON t.chunk_id=c.id
            LEFT JOIN embeddings e ON e.chunk_id=c.id AND e.model=?
            WHERE m.gen=? AND e.chunk_id IS NULL ORDER BY c.id''', (old['chunker'],signature,old['ref'])).fetchall()
    for start in range(0, len(selection), 32):
        batch = selection[start:start+32]
        vectors = embedding.embed(['\n'.join(x for x in [r['title'],*json.loads(r['heading_path']),r['text']] if x) for r in batch])
        payload = [(r['id'],signature,dimension,encode_vector(v,dimension)) for r,v in zip(batch,vectors,strict=True)]
        with database.transaction(write=True) as db:
            db.executemany('INSERT OR IGNORE INTO embeddings VALUES(?,?,?,?)', payload)
    with database.transaction(write=True) as db:
        if current(db, scope)['ref'] != old['ref']:
            raise CorpusError('generation_changed','Active generation changed during re-embedding. Retry against its current version.',409)
        generated = ref('gen')
        db.execute('''INSERT INTO generations(ref,scope,embedding,dimension,chunker,created_at,operation,validation)
            VALUES(?,?,?,?,?,?,'reembed','staging')''',(generated,scope,signature,dimension,old['chunker'],now()))
        db.execute('''INSERT INTO generation_members SELECT ?,document_ref,version_ref,parsed_ref FROM generation_members
            WHERE gen=?''',(generated,old['ref']))
        validate(db,generated,signature,dimension,old['chunker'],scope,None)
        switch(db,scope,generated)
        event(db,scope,'maintenance','reembedded',{'generation_ref':generated,'previous':old['ref']})
        return generated
