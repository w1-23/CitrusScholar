BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS documents (
 ref TEXT PRIMARY KEY, scope TEXT NOT NULL, identity TEXT NOT NULL, UNIQUE(scope, identity)
);
CREATE TABLE IF NOT EXISTS versions (
 ref TEXT PRIMARY KEY, document_ref TEXT NOT NULL REFERENCES documents(ref), doi TEXT,
 title TEXT, year INTEGER, source_uri TEXT NOT NULL, source_kind TEXT NOT NULL, source_hash TEXT NOT NULL,
 retrieved_at TEXT NOT NULL, http_status INTEGER, access_note TEXT NOT NULL, media_type TEXT NOT NULL,
 original BLOB NOT NULL, metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS version_document ON versions(document_ref);
CREATE TABLE IF NOT EXISTS parsed (
 ref TEXT PRIMARY KEY, version_ref TEXT NOT NULL REFERENCES versions(ref), parser TEXT NOT NULL,
 markdown TEXT NOT NULL, structured TEXT NOT NULL, quality TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY, ref TEXT NOT NULL UNIQUE, parsed_ref TEXT NOT NULL REFERENCES parsed(ref),
 chunker TEXT NOT NULL, ordinal INTEGER NOT NULL, heading_path TEXT NOT NULL,
 page_start INTEGER, page_end INTEGER, text TEXT NOT NULL, token_count INTEGER NOT NULL, content_hash TEXT NOT NULL,
 UNIQUE(parsed_ref,chunker,ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(terms);
CREATE TABLE IF NOT EXISTS embeddings (
 chunk_id INTEGER NOT NULL REFERENCES chunks(id), model TEXT NOT NULL, dimension INTEGER NOT NULL,
 vector BLOB NOT NULL CHECK(length(vector)=dimension*4), PRIMARY KEY(chunk_id,model)
);
CREATE TABLE IF NOT EXISTS generations (
 ref TEXT PRIMARY KEY, scope TEXT NOT NULL, embedding TEXT NOT NULL, dimension INTEGER NOT NULL,
 chunker TEXT NOT NULL, created_at TEXT NOT NULL, operation TEXT NOT NULL,
 documents INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0, validation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_members (
 gen TEXT NOT NULL REFERENCES generations(ref), document_ref TEXT NOT NULL REFERENCES documents(ref),
 version_ref TEXT NOT NULL REFERENCES versions(ref), parsed_ref TEXT NOT NULL REFERENCES parsed(ref),
 PRIMARY KEY(gen,document_ref)
);
CREATE TABLE IF NOT EXISTS active (
 scope TEXT PRIMARY KEY, gen TEXT NOT NULL REFERENCES generations(ref)
);
CREATE TABLE IF NOT EXISTS tombstones (
 scope TEXT NOT NULL, document_ref TEXT NOT NULL REFERENCES documents(ref), withdrawn_at TEXT NOT NULL,
 PRIMARY KEY(scope, document_ref)
);
CREATE TABLE IF NOT EXISTS requests (
 ref TEXT PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL, idem TEXT NOT NULL, spec_hash TEXT NOT NULL,
 spec TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL,
 not_before REAL NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}', error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner,scope,idem)
);
CREATE INDEX IF NOT EXISTS claimable ON requests(status,not_before,lease_until,created_at);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, request_ref TEXT REFERENCES requests(ref), scope TEXT NOT NULL,
 actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS request_events ON events(request_ref,id);
PRAGMA user_version=1;
COMMIT;
