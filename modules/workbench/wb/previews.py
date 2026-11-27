# SPDX-License-Identifier: AGPL-3.0-only
"""Scope-bound preparation receipts and thumbnail cards, never source paths."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from .bundle import InputAsset, InvalidInput, bundle_digest, digest, validate_bundle
from .figures import FigureError, identify
from .models import EngineDocument, SourceReceipt
from .store import Conflict, Missing


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def capability(engine):
    if engine.native_manifest and engine.native_editability in {"semantic", "partial"}:
        return "native"
    return "semantic" if any(o.editable for o in engine.objects) else "preview_only"


def card_label(engine, ordinal):
    title = next((obj.properties.get("text") for obj in engine.objects if obj.kind in {"title", "plot_title"}
                  and obj.properties.get("text")), None)
    return str(title).strip()[:120] if title and str(title).strip() else f"图件 {ordinal + 1}"


class Previews:
    def __init__(self, store, figures, runtime):
        self.store, self.figures, self.runtime = store, figures, runtime
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS preview_runs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
                request_hash TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, source TEXT, UNIQUE(owner, request_key)
            );
            CREATE TABLE IF NOT EXISTS preview_cards (
                id TEXT PRIMARY KEY, operation TEXT NOT NULL REFERENCES preview_runs(id),
                version INTEGER NOT NULL, ordinal INTEGER NOT NULL, label TEXT NOT NULL,
                capability TEXT NOT NULL, preview_hash TEXT NOT NULL, engine TEXT NOT NULL,
                preview BLOB NOT NULL, UNIQUE(operation, ordinal)
            );
            """)

    def begin(self, owner, key, source):
        request_hash = digest(canonical(source.model_dump()).encode())
        ref = "preview_run_" + uuid4().hex
        with self.store.connect() as db:
            count = db.execute("""INSERT INTO preview_runs(id,owner,request_key,request_hash,created_at,status)
                VALUES (?,?,?,?,?,'running') ON CONFLICT(owner,request_key) DO NOTHING""",
                               (ref, owner, key, request_hash, utc_now())).rowcount
            op = db.execute("SELECT id,request_hash FROM preview_runs WHERE owner=? AND request_key=?",
                            (owner, key)).fetchone()
            if op["request_hash"] != request_hash:
                raise Conflict
        return op["id"], bool(count)

    def fail(self, owner, ref, reason):
        with self.store.connect() as db:
            db.execute("UPDATE preview_runs SET status='failed', error=? WHERE id=? AND owner=? AND status='running'",
                       (reason, ref, owner))

    def prepare(self, owner, ref, request, resolved):
        asset, dependencies, library_ref, library_version = resolved
        validate_bundle(asset, dependencies)
        language = identify(asset.name, asset.body, request.kind, request.language)
        runtime = self.runtime(language)
        rendered = []
        count = 1
        ordinal = 0
        while ordinal < count:
            engine, png = self.figures.render(language, asset, {"format": "png", "dpi": 120,
                "overrides": {}, "figure_ordinal": ordinal}, dependencies)
            capture = engine.capture
            if not capture or capture.ordinal != ordinal or capture.count <= ordinal or (ordinal and capture.count != count):
                raise FigureError("隔离运行未返回一致的图件捕获序号。")
            if not ordinal:
                count = capture.count
            rendered.append((engine, png))
            if len(png) > 12 * 1024 * 1024 or sum(len(item[1]) for item in rendered) > 36 * 1024 * 1024:
                raise FigureError("预览图件总大小超限。")
            ordinal += 1
        if runtime != self.runtime(language):
            raise FigureError("渲染运行环境已变化，请重新选择及准备图件。")
        assets = (asset, *dependencies)
        witness = {"language": language, "pins": [a.pin() for a in assets], "runtime": runtime,
                   "bundle": bundle_digest(assets, language, runtime), "library_ref": library_ref,
                   "library_version": library_version}
        with self.store.connect() as db:
            if db.execute("""UPDATE preview_runs SET status='ready',source=?
                WHERE id=? AND owner=? AND status='running'""", (canonical(witness), ref, owner)).rowcount != 1:
                raise Conflict
            self.store.pin_assets(db, owner, assets)
            db.executemany("INSERT INTO preview_cards VALUES (?,?,?,?,?,?,?,?,?)", [
                ("preview_" + uuid4().hex, ref, 1, i, card_label(engine, i), capability(engine),
                 digest(png), engine.model_dump_json(), png) for i, (engine, png) in enumerate(rendered)])

    @staticmethod
    def _card(row):
        missing = bool(row["missing_inputs"])
        result = {"preview_ref": row["id"], "preview_version": row["version"],
                  "figure_ordinal": row["ordinal"], "label": row["label"],
                  "thumbnail_url": f'preview-cards/{row["id"]}/thumbnail?version={row["version"]}',
                  "preview_sha256": row["preview_hash"],
                  "capability": "preview_only" if missing else row["capability"],
                  "source_state": "unavailable" if missing else "verified"}
        if not missing and row["capability"] != "preview_only":
            source = json.loads(row["source"])
            result.update(bundle_sha256=source["bundle"], runtime_ref=source["runtime"])
        return result

    def _cards(self, db, owner, operation=None):
        sql = """SELECT c.id,c.version,c.ordinal,c.label,c.capability,c.preview_hash,r.source,
            EXISTS(SELECT 1 FROM json_each(r.source, '$.pins') AS p LEFT JOIN assets a
            ON a.id=json_extract(p.value,'$.ref') AND a.owner=r.owner
            AND a.name=json_extract(p.value,'$.name') AND a.sha=json_extract(p.value,'$.sha')
            AND length(a.body)=json_extract(p.value,'$.size') WHERE a.id IS NULL) AS missing_inputs
            FROM preview_cards c JOIN preview_runs r ON c.operation=r.id
            WHERE r.owner=? AND r.status='ready'"""
        params = (owner,)
        if operation:
            sql += " AND r.id=?"
            params += (operation,)
        return [self._card(row) for row in db.execute(sql + " ORDER BY r.created_at DESC,c.ordinal LIMIT 200", params)]

    def list(self, owner):
        with self.store.connect() as db:
            return self._cards(db, owner)

    def operation(self, owner, ref):
        with self.store.connect() as db:
            row = db.execute("SELECT id,created_at,status,error FROM preview_runs WHERE owner=? AND id=?",
                             (owner, ref)).fetchone()
            if not row:
                raise Missing
            result = {"operation_id": row["id"], "created_at": row["created_at"], "status": row["status"],
                      "cards": self._cards(db, owner, ref) if row["status"] == "ready" else []}
            if row["status"] == "running" and datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"]) > timedelta(minutes=10):
                expired = db.execute("UPDATE preview_runs SET status='failed',error='interrupted' WHERE id=? AND status='running'", (ref,))
                if expired.rowcount:
                    result.update(status="failed", error="interrupted")
                else:
                    recent = db.execute("SELECT status,error FROM preview_runs WHERE id=?", (ref,)).fetchone()
                    result.update(status=recent["status"], cards=self._cards(db, owner, ref))
                    if recent["error"]:
                        result["error"] = recent["error"]
            if row["error"]:
                result["error"] = row["error"]
            return result

    def _read(self, owner, ref, version):
        with self.store.connect() as db:
            row = db.execute("""SELECT c.*,r.source FROM preview_cards c JOIN preview_runs r ON c.operation=r.id
                WHERE r.owner=? AND c.id=? AND c.version=? AND r.status='ready'""", (owner, ref, version)).fetchone()
        if row is None:
            raise Missing
        if digest(row["preview"]) != row["preview_hash"]:
            raise InvalidInput("预览图件的内容校验不符。")
        return row

    def thumbnail(self, owner, ref, version):
        row = self._read(owner, ref, version)
        return row["preview"], row["preview_hash"]

    def import_card(self, owner, request):
        row = self._read(owner, request.preview_ref, request.preview_version)
        source = json.loads(row["source"])
        engine = EngineDocument.model_validate_json(row["engine"])
        receipt = SourceReceipt(preview_ref=row["id"], preview_version=row["version"],
            bundle_sha256=source["bundle"], runtime_ref=source["runtime"], capture=engine.capture)
        mode = request.options.parse_mode
        if mode == "preview_only":
            asset = InputAsset.snapshot("preview.png", row["preview_hash"], row["preview"])
            return self.figures.save_import(owner, asset, "raster", engine, row["preview"],
                mode=mode, receipt=receipt, name=row["label"])
        if capability(engine) == "preview_only":
            raise FigureError("此预览没有可验证的语义编辑对象，请使用仅预览。")
        if self.runtime(source["language"]) != source["runtime"]:
            raise FigureError("原渲染运行环境不可用，请重新准备图件。")
        try:
            assets = tuple(self.store.input_asset(owner, item["ref"]) for item in source["pins"])
        except Missing as exc:
            raise FigureError("此预览的原始来源不可用，请使用仅预览。") from exc
        if [asset.pin() for asset in assets] != source["pins"] or bundle_digest(assets, source["language"], source["runtime"]) != source["bundle"]:
            raise FigureError("此预览的绑定来源已变化，请重新准备图件。")
        validate_bundle(assets[0], assets[1:])
        return self.figures.save_import(owner, assets[0], source["language"], engine, row["preview"],
            mode="editable", dependencies=assets[1:], receipt=receipt, name=row["label"],
            library_ref=source["library_ref"], library_version=source["library_version"])
