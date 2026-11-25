# Independent literature corpus

This Linux package owns one private data root with `inbox/`, content-addressed `originals/`, prepared `documents/`, a SQLite queue/catalog/publication journal, a writer lock and immutable LanceDB generations. Run it with the corpus root and corpus-specific OS account. The operator uses filesystem access as the administration boundary. Search and reference consumers require read access to the same root.

```sh
python -m citrus_corpus --root /path/to/sixth init
cp paper.md /path/to/sixth/inbox/paper.md
python -m citrus_corpus --root /path/to/sixth submit-saved paper.md --sha256 SHA256 --actor operator --doi 10.1234/example
python -m citrus_corpus --root /path/to/sixth worker
python -m citrus_corpus --root /path/to/sixth versions
python -m citrus_corpus --root /path/to/sixth rights granted DOCUMENT VERSION --actor reviewer --reason 'Verified reuse permission for these bytes' --evidence 'rights-record:123'
python -m citrus_corpus --root /path/to/sixth publish --embedding-cache /path/to/fastembed
python -m citrus_corpus --root /path/to/sixth search 'nutrient uptake' --embedding-cache /path/to/fastembed
```

`submit-explicit` accepts one DOI or URL. Acquisition runs only when the operator supplies `worker --allow-network`. The optional `--network-marker` is rechecked during acquisition. HTTPS redirects require explicit destination hosts supplied via `--allow-redirect-host`. The worker retrieves a single bounded response, preserves its bytes and ignores references and links found in the body. Use `--revision` when explicitly requesting newer bytes without a known checksum. A saved input must match its declared SHA-256. Prepared content starts in rights review and only a recorded grant makes it searchable. Grants require a reviewed evidence locator and may include an admissible open-license identifier. Each decision records the scope and the original SHA-256. Revocation also applies to historical references and readers pinned before the revocation.

The CPU indexer and reader use the same locally cached E5 model. Optional `--reranker-dir` uses a locally cached CPU model. `publish --idle-marker /path/to/ready` checks the supplied marker independently of any chat service. A publication verifies the immutable artifact, records it in the journal, and switches the reader pointer atomically. `rollback GENERATION` permits selecting a known published generation with current rights and keeps the newer artifacts available for historical references. `read` takes the exact JSON reference emitted by `search`.

Run `publish --watch --embedding-cache ...` as a separate process for automatic build and publication. By default it rechecks every 30 seconds and publishes only below 0.75 one-minute system load per CPU with at least 2 GiB free on the corpus filesystem. `--poll-seconds`, `--max-load-per-cpu`, `--min-free-mib` and `--idle-marker` tune this independently of active chat sessions. The corpus writer lock also prevents overlapping intake and publication.

`AutoIntake(Store(root)).register(...)` is a host-only adapter for a reviewed
work with a DOI, PMID or PMCID. `raw=None` stores bibliography and alias links
without creating a version, original, or publication grant. `lookup(...)`
reads that bibliography using a known identifier. DOI promotion does not
rewrite historical document IDs or references.

For a text observation the host must authenticate the official PMC Cloud
responses, verify rights for that exact object revision, and pass the
unmodified text bytes together with `text_url`, `evidence_url`, `pmcid` and a
supported `license_id`. `source_url` remains the separate public bibliography
link. The adapter performs no network requests. The corpus worker prepares
the queued observation and journals the grant with the exact original digest
in one SQLite transaction. The publisher independently builds and activates
an index. An operator's exact-version revocation is never overridden by an
automatic observation. For rollback, legacy workers can still prepare a
queued observation, but it needs operator rights review on the legacy code.
