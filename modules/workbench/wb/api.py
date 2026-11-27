"""Independent HTTP boundary. Launch and assets are delegated, never read from Host storage."""
import hmac
import os
import re
from html import escape
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParameter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import InvalidTicket, issue, parse_session, session_key, verify
from .bundle import InvalidInput, MAX_BYTES
from .figures import FigureError, Figures, saved_edits
from .import_inputs import ASSET_NS, filename, resolve_inputs
from .library import Library, LibraryMissing, LibraryUnavailable
from .models import (
    Edit, Export, FigureDocument, GroupMove, GroupScale, GroupSelection,
    ImportRequest, Launch, NativeEdit, Pick, PreviewCards, PreviewImport, PreviewOperation, PreviewRun, Ref, Restore,
    RevisionHistory, RevisionName, RevisionSummary,
)
from .previews import Previews
from .runtime_pin import identity as runtime_identity
from .sandbox import SandboxError, run as sandbox_run
from .store import Conflict, Missing, Store, digest

MIMES = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}
COOKIE = "workbench_session"
HOURS_8 = 8 * 60 * 60
WEB_DIR = Path(__file__).with_name("web")
WEB_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
           "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'"}


def _launch_key():
    raw = os.getenv("WORKBENCH_HOST_KEY", "").encode()
    if raw and len(raw) < 32:
        raise ValueError("WORKBENCH_HOST_KEY must be at least 32 bytes")
    return raw


def _source_offer(value):
    if value and not any(ord(c) < 33 or c == "\\" for c in value):
        try:
            url = urlsplit(value)
            local = value.startswith("/") and not value.startswith("//") and not url.netloc
            remote = url.scheme == "https" and url.hostname and url.username is None
            if local or remote:
                return f'<a href="{escape(value, quote=True)}" rel="noreferrer">下载对应源码</a>'
        except ValueError:
            pass
    return "当前实例未配置对应源码下载地址。"


def create_app(*, db_path=None, run=None, library=None, secret=None, host_key=None, standalone_token=None, runtime=None):
    db_path = db_path or Path(os.getenv("WORKBENCH_DB", Path.home() / ".local/share/citrus-workbench/state.sqlite3"))
    store = Store(Path(db_path))
    secret = secret or session_key()
    host_key = _launch_key() if host_key is None else host_key
    library = library or Library(os.getenv("WORKBENCH_HOST_ORIGIN", ""), host_key)
    standalone_token = (standalone_token if standalone_token is not None
                        else os.getenv("WORKBENCH_STANDALONE_TOKEN", ""))
    secure = os.getenv("WORKBENCH_SECURE_COOKIE", "1") == "1"
    audience = os.getenv("WORKBENCH_AUDIENCE", "citrus-workbench")
    origin = os.getenv("WORKBENCH_PUBLIC_ORIGIN", "").rstrip("/")
    permits = BoundedSemaphore(2)

    def limited_run(*args, **kwargs):
        if not permits.acquire(blocking=False):
            raise HTTPException(503, "当前渲染队列已满，请稍后再试。", headers={"Retry-After": "2"})
        try:
            return (run or sandbox_run)(*args, **kwargs)
        finally:
            permits.release()

    runtime = runtime or runtime_identity
    figures = Figures(store, limited_run, runtime)
    previews = Previews(store, figures, runtime)
    app = FastAPI(title="Citrus Figure Workbench API", version="0.1.0")

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            sent_origin = request.headers.get("origin")
            expected = origin or str(request.base_url).rstrip("/")
            if sent_origin and sent_origin != expected:
                return JSONResponse({"detail": "Origin not permitted"}, 403, headers=PRIVATE)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    def handle(code, detail):
        async def handler(_request, _error):
            return JSONResponse({"detail": detail if detail is not None else str(_error)}, code, headers=PRIVATE)
        return handler

    for error, status, detail in (
        (Missing, 404, "图件或资产不存在。"), (LibraryMissing, 404, "图件资源不可用。"),
        (Conflict, 409, "版本已改变或票据已使用。"), (InvalidTicket, 401, "授权无效或已失效。"),
        (LibraryUnavailable, 502, "资料库接口暂不可用。"), (FigureError, 422, None), (InvalidInput, 422, None),
        (SandboxError, 422, "隔离运行或渲染未完成。请检查脚本、依赖或编辑属性。"),
    ):
        app.add_exception_handler(error, handle(status, detail))

    def login_claims(request: Request):
        return verify(secret, "workbench-session", audience, request.cookies.get(COOKIE), max_lifetime=HOURS_8)

    def context(claims=Depends(login_claims)):
        return parse_session(claims)

    def establish(request, claims, session_ref):
        stable = {**claims, "session_ref": session_ref}
        token = issue(secret, "workbench-session", audience, stable, lifetime=HOURS_8)
        payload = {"workbench_session_ref": session_ref, "status": "open",
                   "workbench_url": request.scope.get("root_path", "").rstrip("/") + "/workbench/",
                   "opened_assets": stable.get("source_assets", []), "return_to": stable.get("return_to", "")}
        response = JSONResponse(payload, headers=PRIVATE)
        response.set_cookie(COOKIE, token, max_age=HOURS_8, secure=secure, httponly=True, samesite="strict")
        return response

    app.mount("/workbench/static", StaticFiles(directory=WEB_DIR / "static"), name="workbench-static")

    @app.get("/", include_in_schema=False)
    def workbench_entry():
        return RedirectResponse("workbench/", status_code=307)

    @app.get("/workbench/", response_class=HTMLResponse, include_in_schema=False)
    def workbench_page():
        offer = _source_offer(os.getenv("WORKBENCH_SOURCE_URL", ""))
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("<!-- source-offer -->", offer)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": WEB_CSP,
                                           "Referrer-Policy": "no-referrer"})

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": app.version}

    @app.post("/api/v1/launch")
    def launch(body: Launch, request: Request):
        claims = verify(host_key, "citrus-workbench-launch", audience, body.ticket, max_lifetime=120)
        session = parse_session(claims, from_host=True)
        nonce = claims.get("jti")
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", nonce):
            raise InvalidTicket
        import time
        store.consume_ticket(nonce, int(time.time()), claims["exp"])
        return establish(request, {"user_ref": session.user, "project_ref": session.project,
                          "return_to": session.return_to, "source_assets": list(session.source_assets)}, uuid4().hex)

    @app.post("/api/v1/standalone")
    async def standalone(request: Request):
        raw = await _limited_body(request, 8192)
        try:
            from json import loads
            body = loads(raw)
            token = body.get("token", "") if isinstance(body, dict) else ""
        except (ValueError, RecursionError):
            raise InvalidTicket from None
        if not standalone_token or not isinstance(token, str) or not hmac.compare_digest(token.encode(), standalone_token.encode()):
            raise InvalidTicket
        return establish(request, {"user_ref": "standalone", "project_ref": "default", "return_to": ""}, uuid4().hex)

    @app.get("/api/v1/session")
    def get_session(claims=Depends(login_claims), session=Depends(context)):
        return {"workbench_session_ref": claims["session_ref"], "return_to": session.return_to,
                "source_assets": list(session.source_assets)}

    @app.post("/api/v1/assets", status_code=201)
    async def upload(request: Request, x_filename: str = Header(), session=Depends(context)):
        try:
            name = filename(x_filename)
        except ValueError:
            raise HTTPException(422, "文件名无效。") from None
        body = await _limited_body(request, MAX_BYTES)
        if not body:
            raise HTTPException(422, "文件为空。")
        return {"asset_ref": store.put_asset(session.owner, name, body), "source_hash": digest(body)}

    @app.get("/api/v1/library")
    def library_list(format: str = Query(default="", pattern=r"^(?:png|svg|pdf|jpeg|jpg|py|r|csv|tsv|json|txt|xlsx|parquet|npy)?$"),
                     type: str = Query(default="", pattern=r"^(?:script|static|dependency)?$"), session=Depends(context)):
        items = library.list(session, format, kind=type) if type else library.list(session, format)
        return {"assets": [{**item.model_dump(), "asset_ref": ASSET_NS + item.asset_ref,
                           **({} if type == "dependency" else {"preview_url": f"/api/v1/library/{item.asset_ref}/preview"})}
                           for item in items]}

    @app.get("/api/v1/library/{ref}/preview")
    def library_preview(ref: Ref, session=Depends(context)):
        artifact, headers = library.asset(session, ref, "preview")
        mime = headers.get("content-type", "").split(";", 1)[0].lower()
        if mime not in {"image/png", "image/svg+xml", "image/jpeg", "application/pdf"}:
            raise LibraryUnavailable
        return Response(artifact, media_type=mime, headers=PRIVATE)

    @app.get("/api/v1/figures")
    def list_figures(session=Depends(context)):
        return {"figures": store.figures(session.owner)}

    @app.get("/api/v1/preview-cards", response_model=PreviewCards, response_model_exclude_none=True)
    def preview_cards(session=Depends(context)):
        return {"cards": previews.list(session.owner)}

    @app.get("/api/v1/preview-cards/{ref}/thumbnail")
    def preview_thumbnail(ref: Ref, version: int = Query(ge=1), session=Depends(context)):
        data, sha = previews.thumbnail(session.owner, ref, version)
        return Response(data, media_type="image/png", headers={**PRIVATE, "X-Content-SHA256": sha})

    @app.post("/api/v1/preview-runs", response_model=PreviewOperation, response_model_exclude_none=True)
    def prepare_preview(body: PreviewRun, idempotency_key: str = Header(pattern=r"^[A-Za-z0-9_.:-]{8,128}$"),
                        session=Depends(context)):
        if body.source.kind != "script":
            raise InvalidInput("只有明确选取的绘图脚本可以准备来源预览。")
        ref, started = previews.begin(session.owner, idempotency_key, body.source)
        if started:
            try:
                resolved = resolve_inputs(session, store, library, body.source)
                previews.prepare(session.owner, ref, body.source, resolved)
            except (Missing, LibraryMissing):
                previews.fail(session.owner, ref, "source_missing")
            except LibraryUnavailable:
                previews.fail(session.owner, ref, "library_unavailable")
            except (InvalidInput, FigureError):
                previews.fail(session.owner, ref, "invalid_source_or_capture")
            except SandboxError:
                previews.fail(session.owner, ref, "isolated_render_failed")
            except HTTPException as exc:
                previews.fail(session.owner, ref, "renderer_unavailable")
                if exc.status_code != 503:
                    raise
            except Exception:
                previews.fail(session.owner, ref, "internal_error")
                raise
        return previews.operation(session.owner, ref)

    @app.get("/api/v1/preview-runs/{ref}", response_model=PreviewOperation, response_model_exclude_none=True)
    def preview_operation(ref: Ref, session=Depends(context)):
        return previews.operation(session.owner, ref)

    @app.post("/api/v1/figures/import", status_code=201)
    def import_figure(body: ImportRequest | PreviewImport, session=Depends(context)):
        if isinstance(body, PreviewImport):
            doc = previews.import_card(session.owner, body)
        else:
            asset, dependencies, library_ref, library_version = resolve_inputs(session, store, library, body.source)
            doc = figures.import_figure(session.owner, asset, body, dependencies=dependencies,
                                        library_ref=library_ref, library_version=library_version)
        return {"import_id": uuid4().hex, "figure_id": doc.figure_id, "detected_language": doc.source.language,
                "renderer_id": doc.renderer.id, "editability": doc.editability, "status": "ready",
                "limitations": doc.limitations}

    @app.get("/api/v1/figures/{ref}", response_model=FigureDocument)
    def get_figure(ref: Ref, revision: int | None = Query(default=None, ge=1), session=Depends(context)):
        return figures.get(session.owner, ref, revision)[0]

    @app.get("/api/v1/figures/{ref}/objects")
    def get_objects(ref: Ref, revision: int | None = Query(default=None, ge=1), session=Depends(context)):
        doc, _ = figures.get(session.owner, ref, revision)
        return {"revision": doc.revision, "panels": doc.panels, "objects": doc.objects,
                "native_manifest": doc.native_manifest, "native_editability": doc.native_editability}

    @app.post("/api/v1/figures/{ref}/pick")
    def hit_test(ref: Ref, body: Pick, session=Depends(context)):
        from .picking import pick
        doc, _ = figures.get(session.owner, ref, body.revision)
        if doc.native_manifest is None:
            raise FigureError("此图件不具备 Tavotto 原生拾取数据。")
        return {"revision": doc.revision, **pick(doc.native_manifest, body.x, body.y, body.locked_gids, body.current_gid)}

    @app.get("/api/v1/figures/{ref}/groups/{gid}")
    def get_group(ref: Ref, gid: Ref, revision: int | None = Query(default=None, ge=1), session=Depends(context)):
        from .native_groups import group_info
        doc, _ = figures.get(session.owner, ref, revision)
        return {"revision": doc.revision, "group": group_info(doc, gid)}

    @app.post("/api/v1/figures/{ref}/groups/resolve")
    def group_selection(ref: Ref, body: GroupSelection, session=Depends(context)):
        from .native_groups import resolve_groups
        doc, row = figures.get(session.owner, ref, body.revision)
        return resolve_groups(doc, saved_edits(row)[1], body)

    @app.post("/api/v1/figures/{ref}/groups/{gid}/move", response_model=FigureDocument)
    def group_movement(ref: Ref, gid: Ref, body: GroupMove, session=Depends(context)):
        from .native_groups import move_group
        doc, row = figures.get(session.owner, ref)
        batch = move_group(doc, saved_edits(row)[1], gid, body)
        return figures.edit_native(session.owner, ref, batch)

    @app.post("/api/v1/figures/{ref}/groups/{gid}/scale", response_model=FigureDocument)
    def group_scaling(ref: Ref, gid: Ref, body: GroupScale, session=Depends(context)):
        from .native_groups import scale_group
        doc, row = figures.get(session.owner, ref)
        batch = scale_group(doc, saved_edits(row)[1], gid, body)
        return figures.edit_native(session.owner, ref, batch)

    @app.get("/api/v1/figures/{ref}/preview")
    def preview(ref: Ref, revision: int | None = Query(default=None, ge=1), session=Depends(context)):
        _, row = figures.get(session.owner, ref, revision)
        return Response(row["preview"], media_type="image/png", headers=PRIVATE)

    @app.patch("/api/v1/figures/{ref}/overrides", response_model=FigureDocument)
    def edit(ref: Ref, body: Edit, session=Depends(context)):
        return figures.edit(session.owner, ref, body)

    @app.patch("/api/v1/figures/{ref}/native-overrides", response_model=FigureDocument)
    def native_edit(ref: Ref, body: NativeEdit, session=Depends(context)):
        return figures.edit_native(session.owner, ref, body)

    @app.get("/api/v1/figures/{ref}/history", response_model=RevisionHistory, response_model_exclude_none=True)
    def history(ref: Ref, session=Depends(context)):
        return {"versions": store.history(session.owner, ref)}

    @app.put("/api/v1/figures/{ref}/history/{revision}/name", response_model=RevisionSummary,
             response_model_exclude_none=True)
    def name_revision(ref: Ref, body: RevisionName, revision: int = PathParameter(ge=1, le=2**63-1),
                      session=Depends(context)):
        return store.name_revision(session.owner, ref, revision, body.name)

    @app.post("/api/v1/figures/{ref}/restore", response_model=FigureDocument)
    def restore(ref: Ref, body: Restore, session=Depends(context)):
        return figures.restore(session.owner, ref, body)

    @app.post("/api/v1/figures/{ref}/export")
    def export(ref: Ref, body: Export, session=Depends(context)):
        data, export_id, sha, revision = figures.export(session.owner, ref, body)
        headers = {**PRIVATE, "Content-Disposition": f'attachment; filename="{ref}.{body.format}"',
                   "X-Export-Id": export_id, "X-Content-SHA256": sha, "X-Figure-Revision": str(revision)}
        return Response(data, media_type=MIMES[body.format], headers=headers)

    return app


async def _limited_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "请求体过大。")
        body.extend(chunk)
    return bytes(body)
