"""Additive bibliography and identity aliases for the existing corpus journal."""


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS corpus_bibliography (
        id INTEGER PRIMARY KEY, canonical_identity TEXT NOT NULL UNIQUE,
        storage_identity TEXT NOT NULL UNIQUE, title TEXT NOT NULL, source_url TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS corpus_bibliography_aliases (
        identity TEXT PRIMARY KEY NOT NULL,
        bibliography_id INTEGER NOT NULL REFERENCES corpus_bibliography(id)
    )""",
    """CREATE INDEX IF NOT EXISTS corpus_bibliography_aliases_work
        ON corpus_bibliography_aliases(bibliography_id)""",
)
_KINDS = ("doi", "pmid", "pmcid")


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_bibliography'").fetchone() is not None


def _rows(conn, identities):
    marks = ",".join("?" for _ in identities)
    return conn.execute(f"""SELECT DISTINCT b.* FROM corpus_bibliography AS b
                        JOIN corpus_bibliography_aliases AS a ON a.bibliography_id=b.id
                        WHERE a.identity IN ({marks}) ORDER BY b.id""", tuple(identities)).fetchall()


def _aliases(conn, row_ids):
    marks = ",".join("?" for _ in row_ids)
    return {row[0] for row in conn.execute(f"""SELECT identity FROM corpus_bibliography_aliases
                                         WHERE bibliography_id IN ({marks})""", row_ids)}


def _identifiers(aliases):
    result = {kind: "" for kind in _KINDS}
    for alias in aliases:
        kind, value = alias.split(":", 1)
        if kind in result:
            if result[kind] and result[kind] != value:
                raise ValueError("bibliography_identity_conflict")
            result[kind] = value
    return result


def _view(row, identifiers):
    return {"identity": row["canonical_identity"], "title": row["title"],
            "source_url": row["source_url"], **identifiers}


def register(conn, *, identifiers, legacy_aliases, title, source_url):
    # SQLite DDL is transactional here. Do not use executescript (implicit commit).
    for ddl in _SCHEMA:
        conn.execute(ddl)
    aliases = set(identifiers) | set(legacy_aliases)
    matches = _rows(conn, aliases)
    row_ids = tuple(row["id"] for row in matches)
    if matches:
        aliases.update(_aliases(conn, row_ids))
    merged = _identifiers(aliases)
    canonical = next(f"{kind}:{merged[kind]}" for kind in _KINDS if merged[kind])
    marks = ",".join("?" for _ in aliases)
    existing_version = conn.execute(f"""SELECT identity FROM versions WHERE identity IN ({marks})
                                       ORDER BY seq LIMIT 1""", tuple(aliases)).fetchone()
    storage = existing_version[0] if existing_version else (matches[0]["storage_identity"] if matches else canonical)
    if matches:
        master, *rest = matches
        # A weaker observation must not replace stronger canonical metadata.
        if canonical not in identifiers:
            preferred = next(row for row in matches if row["canonical_identity"] == canonical)
            title, source_url = preferred["title"], preferred["source_url"]
        for row in rest:
            conn.execute("UPDATE corpus_bibliography_aliases SET bibliography_id=? WHERE bibliography_id=?",
                         (master["id"], row["id"]))
            conn.execute("DELETE FROM corpus_bibliography WHERE id=?", (row["id"],))
        work_id = master["id"]
        conn.execute("""UPDATE corpus_bibliography SET canonical_identity=?,storage_identity=?,title=?,source_url=?
                     WHERE id=?""", (canonical, storage, title, source_url, work_id))
    else:
        work_id = conn.execute("""INSERT INTO corpus_bibliography(canonical_identity,storage_identity,title,source_url)
                               VALUES(?,?,?,?)""", (canonical, storage, title, source_url)).lastrowid
    conn.executemany("INSERT OR IGNORE INTO corpus_bibliography_aliases VALUES(?,?)",
                     ((alias, work_id) for alias in aliases))
    result = {"identity": canonical, "title": title, "source_url": source_url, **merged}
    return result, storage


def lookup(conn, identities):
    if not available(conn):
        return None
    matches = _rows(conn, identities)
    if len(matches) > 1:
        raise ValueError("bibliography_identity_conflict")
    if not matches:
        return None
    row = matches[0]
    aliases = _aliases(conn, (row["id"],))
    _identifiers(aliases | set(identities))
    return _view(row, _identifiers(aliases))


def version_group(conn, identity):
    """Resolve a prepared/queued identity without changing any physical IDs."""
    if available(conn):
        matches = _rows(conn, (identity,))
        if matches:
            row = matches[0]
            return (row["storage_identity"], row["canonical_identity"],
                    tuple(_aliases(conn, (row["id"],))))
    return identity, identity, (identity,)
