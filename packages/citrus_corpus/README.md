# Independent literature corpus

This Linux package owns one private data root with `inbox/`, content-addressed `originals/`, prepared `documents/`, a SQLite queue/catalog/publication journal, a writer lock and immutable LanceDB generations. Run it with the corpus root and corpus-specific OS account. The operator uses filesystem access as the administration boundary. Search and reference consumers require read access to the same root.

```sh
python -m citrus_corpus --root /path/to/sixth init
cp paper.md /path/to/sixth/inbox/paper.md
python -m citrus_corpus --root /path/to/sixth submit-saved paper.md --sha256 SHA256 --actor operator --doi 10.1234/example
python -m citrus_corpus --root /path/to/sixth worker
python -m citrus_corpus --root /path/to/sixth versions
python -m citrus_corpus --root /path/to/sixth rights granted DOCUMENT VERSION --actor reviewer --reason 'Verified reuse permission for these bytes'
python -m citrus_corpus --root /path/to/sixth publish --embedding-cache /path/to/fastembed
python -m citrus_corpus --root /path/to/sixth search 'nutrient uptake' --embedding-cache /path/to/fastembed
```

`submit-explicit` accepts one DOI or URL. Acquisition runs only when the operator supplies `worker --allow-network`. HTTPS redirects require explicit destination hosts supplied via `--allow-redirect-host`. The worker retrieves a single bounded response, preserves its bytes and ignores references and links found in the body. Use `--revision` when explicitly requesting newer bytes without a known checksum. A saved input must match its declared SHA-256. Prepared content starts in rights review and only a recorded grant makes it searchable. Revocation also applies to historical references and readers pinned before the revocation.

The CPU indexer and reader use the same locally cached E5 model. Optional `--reranker-dir` uses a locally cached CPU model. `publish --idle-marker /path/to/ready` checks the supplied marker independently of any chat service. A publication verifies the immutable artifact, records it in the journal, and switches the reader pointer atomically. `rollback GENERATION` permits selecting a known published generation with current rights and keeps the newer artifacts available for historical references. `read` takes the exact JSON reference emitted by `search`.
