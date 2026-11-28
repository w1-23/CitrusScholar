BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS chunk_translations (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    text_en TEXT NOT NULL, translation_sha256 TEXT NOT NULL
);
INSERT OR IGNORE INTO corpus_extensions VALUES('chunk_translations',1);
COMMIT;
