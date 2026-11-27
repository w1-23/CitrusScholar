"""Owner-scoped durable figures with atomic compare-and-swap revisions."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .bundle import InputAsset, InvalidInput, digest, validate_bundle


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    sha TEXT NOT NULL, body BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS figures (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    asset TEXT REFERENCES assets(id) NOT NULL, head INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS figure_inputs (
                    figure TEXT PRIMARY KEY REFERENCES figures(id) NOT NULL,
                    manifest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    figure TEXT REFERENCES figures(id) NOT NULL, rev INTEGER NOT NULL,
                    doc TEXT NOT NULL, overrides TEXT NOT NULL, preview BLOB NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(figure,rev)
                );
                CREATE TABLE IF NOT EXISTS revision_names (
                    figure TEXT NOT NULL, rev INTEGER NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY(figure,rev),
                    FOREIGN KEY (figure,rev) REFERENCES revisions(figure,rev)
                );
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, figure TEXT NOT NULL, rev INTEGER NOT NULL,
                    format TEXT NOT NULL, params TEXT NOT NULL, hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (figure,rev) REFERENCES revisions(figure,rev)
                );
                CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS owner_figures ON figures(owner,id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_asset(self, owner, name, body):
        ref = f"asset_{uuid4().hex}"
        with self.connect() as db:
            db.execute("INSERT INTO assets VALUES (?,?,?,?,?)", (ref, owner, name, digest(body), body))
        return ref

    def asset(self, owner, ref):
        with self.connect() as db:
            row = db.execute("SELECT id,name,sha,body FROM assets WHERE owner=? AND id=?", (owner, ref)).fetchone()
        if not row:
            raise Missing
        return row

    def input_asset(self, owner, ref):
        row = self.asset(owner, ref)
        return InputAsset(row["id"], row["name"], row["sha"], row["body"])

    @staticmethod
    def pin_assets(db, owner, assets):
        for asset in assets:
            if asset.external:
                db.execute("INSERT INTO assets VALUES (?,?,?,?,?)",
                           (asset.ref, owner, asset.name, asset.sha, asset.body))
            else:
                row = db.execute("SELECT name,sha,length(body) FROM assets WHERE id=? AND owner=?",
                                 (asset.ref, owner)).fetchone()
                if row is None or tuple(row) != (asset.name, asset.sha, len(asset.body)):
                    raise Missing

    def create(self, owner, doc, preview, source, dependencies=()):
        validate_bundle(source, dependencies)
        inputs = (source, *dependencies)
        with self.connect() as db:
            self.pin_assets(db, owner, inputs)
            db.execute("INSERT INTO figures VALUES (?,?,?,?,1)",
                       (doc.figure_id, owner, doc.name, source.ref))
            manifest = json.dumps([asset.pin() for asset in inputs])
            db.execute("INSERT INTO figure_inputs VALUES (?,?)", (doc.figure_id, manifest))
            self._insert(db, doc, {"inputs_sha256": digest(manifest.encode())}, preview)

    def figure_inputs(self, owner, doc, row):
        if row["asset"] != doc.source.source_ref:
            raise InvalidInput("已保存的脚本引用与图件不符。")
        try:
            source = self.input_asset(owner, row["asset"])
            source.check(doc.source.source_hash)
            manifest = row["manifest"]
            pin = json.loads(row["overrides"]).get("inputs_sha256")
            # Legacy figures always had a single source and no dependencies.
            if manifest is None and pin is None:
                return source, ()
            if not isinstance(manifest, str) or pin != digest(manifest.encode()):
                raise InvalidInput("已保存的输入清单缺失或发生变更。")
            pins = json.loads(manifest)
            if (not isinstance(pins, list) or not 1 <= len(pins) <= 33 or pins[0] != source.pin()
                    or any(not isinstance(pin, dict) or not isinstance(pin.get("ref"), str) for pin in pins)):
                raise InvalidInput("已保存的输入清单无效。")
            dependencies = tuple(self.input_asset(owner, pin["ref"]) for pin in pins[1:])
            if [asset.pin() for asset in dependencies] != pins[1:]:
                raise InvalidInput("已保存的依赖与输入清单不符。")
            validate_bundle(source, dependencies)
            return source, dependencies
        except (Missing, json.JSONDecodeError) as exc:
            raise InvalidInput("已保存的脚本或依赖缺失，无法重新渲染。") from exc

    @staticmethod
    def _insert(db, doc, overrides, preview):
        # Old sealed readers reject unknown keys even when their values are null.
        # Omit only the introduced optional fields when absent; retain real provenance.
        snapshot = doc.model_dump(mode="json")
        if snapshot["capture"] is None:
            snapshot.pop("capture")
        if snapshot["source"]["receipt"] is None:
            snapshot["source"].pop("receipt")
        db.execute("INSERT INTO revisions(figure,rev,doc,overrides,preview) VALUES (?,?,?,?,?)",
                   (doc.figure_id, doc.revision, json.dumps(snapshot, ensure_ascii=False), json.dumps(overrides), preview))

    def save(self, owner, doc, overrides, preview, expected):
        with self.connect() as db:
            found = db.execute("UPDATE figures SET head=? WHERE id=? AND owner=? AND head=?",
                               (doc.revision, doc.figure_id, owner, expected))
            if found.rowcount != 1:
                raise Conflict
            prior = db.execute("SELECT overrides FROM revisions WHERE figure=? AND rev=?",
                               (doc.figure_id, expected)).fetchone()
            pin = json.loads(prior["overrides"]).get("inputs_sha256")
            if pin is not None:
                overrides = {**overrides, "inputs_sha256": pin}
            self._insert(db, doc, overrides, preview)

    def revision(self, owner, ref, revision=None):
        with self.connect() as db:
            row = db.execute("""SELECT r.doc,r.overrides,r.preview,r.rev,f.asset,i.manifest
                FROM figures f JOIN revisions r ON r.figure=f.id AND r.rev=COALESCE(?,f.head)
                LEFT JOIN figure_inputs i ON i.figure=f.id
                WHERE f.owner=? AND f.id=?""", (revision, owner, ref)).fetchone()
        if not row:
            raise Missing
        return row

    def figures(self, owner):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id AS figure_id,name,head AS revision FROM figures WHERE owner=? ORDER BY rowid DESC LIMIT 200", (owner,))]

    def history(self, owner, ref):
        with self.connect() as db:
            rows = list(db.execute("""SELECT r.rev AS revision,r.created_at,n.name FROM figures f
                JOIN revisions r ON r.figure=f.id
                LEFT JOIN revision_names n ON n.figure=r.figure AND n.rev=r.rev
                WHERE f.owner=? AND f.id=? ORDER BY r.rev DESC LIMIT 200""", (owner, ref)))
        if not rows:
            raise Missing
        return [dict(r) for r in rows]

    def name_revision(self, owner, ref, revision, name):
        with self.connect() as db:
            row = db.execute("""SELECT r.rev AS revision,r.created_at FROM figures f
                JOIN revisions r ON r.figure=f.id AND r.rev=? WHERE f.owner=? AND f.id=?""",
                (revision, owner, ref)).fetchone()
            if row is None:
                raise Missing
            if name:
                db.execute("""INSERT INTO revision_names(figure,rev,name) VALUES (?,?,?)
                    ON CONFLICT(figure,rev) DO UPDATE SET name=excluded.name""", (ref, revision, name))
            else:
                db.execute("DELETE FROM revision_names WHERE figure=? AND rev=?", (ref, revision))
            return {**dict(row), "name": name or None}

    def export_record(self, ref, rev, fmt, params, output):
        export_id = f"export_{uuid4().hex}"
        sha = digest(output)
        with self.connect() as db:
            db.execute("INSERT INTO exports(id,figure,rev,format,params,hash) VALUES (?,?,?,?,?,?)",
                       (export_id, ref, rev, fmt, json.dumps(params), sha))
        return export_id, sha

    def consume_ticket(self, ticket_id, current, expires):
        try:
            with self.connect() as db:
                db.execute("DELETE FROM tickets WHERE expires < ?", (current,))
                db.execute("INSERT INTO tickets VALUES (?,?)", (ticket_id, expires))
        except sqlite3.IntegrityError:
            raise Conflict from None
