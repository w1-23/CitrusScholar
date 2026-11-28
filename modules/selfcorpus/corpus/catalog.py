import re

from .errors import CorpusError
from .lexical import tokens
from .storage import digest, dumps, ref
from .sources import normalize_doi
from .vector_index import encode_vector


def active_doi(db, scope, doi):
    return db.execute('''SELECT m.document_ref, m.gen AS generation_ref FROM documents d
                         JOIN active a ON a.scope=d.scope JOIN generation_members m
                         ON m.gen=a.gen AND m.document_ref=d.ref
                         WHERE d.scope=? AND d.identity=?''', (scope, 'doi:' + doi)).fetchone()


def document_identity(source, parsed):
    metadata = {**source.metadata, **{k:v for k,v in parsed.metadata.items() if v not in (None, '', [])}}
    raw_doi = metadata.get('doi')
    doi = normalize_doi(raw_doi) if raw_doi else None
    expected = source.metadata.get('doi')
    if doi and expected and doi != normalize_doi(expected):
        raise CorpusError('identity_mismatch', 'Resolved DOI and parsed DOI do not match.', 422)
    if doi:
        metadata['doi'] = doi
    source_hash = digest(source.content)
    identity = ('doi:' + str(doi).casefold()) if doi else ('uri:' + source.uri if source.kind != 'upload' else 'sha256:' + source_hash)
    return identity, source_hash, metadata


def active_copy(db, scope, identity, source_hash, model, dimension, chunker):
    return db.execute('''SELECT m.document_ref, m.gen AS generation_ref
        FROM active a JOIN generations g ON g.ref=a.gen
        JOIN generation_members m ON m.gen=g.ref
        JOIN documents d ON d.ref=m.document_ref AND d.scope=a.scope
        JOIN versions v ON v.ref=m.version_ref AND v.document_ref=d.ref
        WHERE a.scope=? AND d.identity=? AND v.source_hash=? AND g.embedding=? AND g.dimension=? AND g.chunker=?
        ''', (scope, identity, source_hash, model, dimension, chunker)).fetchone()


def document_rows(db, scope, source, parsed, *, alignment=None):
    identity, source_hash, metadata = document_identity(source, parsed)
    doi = metadata.get('doi')
    title = metadata.get('title') or None
    year = metadata.get('year')
    year = int(year) if re.fullmatch(r'[12][0-9]{3}', str(year)) else None
    doc = ref('doc', scope, identity)
    uri = metadata.get('fulltext_url') or source.uri
    version = ref('ver', doc, uri, source_hash, metadata, source.access_note)
    alignment_identity = ('aligned-english-v1', alignment) if alignment else ()
    parsed_ref = ref('parse', version, parsed.parser, digest(parsed.markdown), digest(dumps(parsed.structured)),
                     *alignment_identity)
    db.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?)', (doc, scope, identity))
    db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (version, doc, doi, title, year, uri, source.kind, source_hash, source.retrieved_at,
                source.http_status, source.access_note, source.media_type, source.content, dumps(metadata)))
    db.execute('INSERT OR IGNORE INTO parsed VALUES(?,?,?,?,?,?)',
               (parsed_ref, version, parsed.parser, parsed.markdown, dumps(parsed.structured), dumps(parsed.quality)))
    return {'document_ref':doc, 'document_version_ref':version, 'parsed_document_ref':parsed_ref,
            'source_hash':source_hash, 'parser':parsed.parser, 'quality':parsed.quality}


def save_chunks(db, result, chunks, vectors, model, dimension, chunker, title):
    for ch, vector in zip(chunks, vectors, strict=True):
        chunk_ref = ref('chunk', result['parsed_document_ref'], chunker, ch['ordinal'], ch['content_hash'])
        cursor = db.execute('''INSERT OR IGNORE INTO chunks(ref,parsed_ref,chunker,ordinal,heading_path,
            page_start,page_end,text,token_count,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (chunk_ref, result['parsed_document_ref'], chunker, ch['ordinal'], dumps(ch['heading_path']),
             ch.get('page_start'), ch.get('page_end'), ch['text'], ch['token_count'], ch['content_hash']))
        if cursor.rowcount:
            id_ = cursor.lastrowid
            translation = ch.get('text_en', '')
            if translation:
                db.execute('INSERT INTO chunk_translations VALUES(?,?,?)', (id_, translation, digest(translation)))
            terms = ' '.join(tokens(' '.join([title or '', *ch['heading_path'], ch['text'], translation])))
            db.execute('INSERT INTO chunk_fts(rowid,terms) VALUES(?,?)', (id_, terms))
        else:
            id_ = db.execute('SELECT id FROM chunks WHERE ref=?', (chunk_ref,)).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO embeddings VALUES(?,?,?,?)',
                   (id_, model, dimension, encode_vector(vector, dimension)))
