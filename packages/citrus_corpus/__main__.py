"""Operator commands for an initialized private corpus root."""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from .acquisition import ExplicitAcquirer
from .catalog import Catalog
from .intake import Intake
from .models import CpuE5, CpuRerank, FusedRank
from .publication import Publisher
from .reader import Reader
from .store import Store


def _model_args(parser):
    parser.add_argument("--embedding-cache", required=True)
    parser.add_argument("--reranker-dir")


def _models(args):
    reranker = CpuRerank(args.reranker_dir) if args.reranker_dir else FusedRank()
    return {"embedder": CpuE5(args.embedding_cache), "reranker": reranker}


def parser():
    main = ArgumentParser(description=__doc__)
    main.add_argument("--root", required=True, help="absolute path to a private corpus root")
    commands = main.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    local = commands.add_parser("submit-saved")
    local.add_argument("path", help="path relative to the corpus inbox")
    local.add_argument("--actor", required=True)
    local.add_argument("--sha256", required=True)
    local.add_argument("--doi", default="")
    local.add_argument("--url", default="")
    local.add_argument("--title", default="")
    local.add_argument("--local-key", default="")
    explicit = commands.add_parser("submit-explicit")
    source = explicit.add_mutually_exclusive_group(required=True)
    source.add_argument("--doi", default="")
    source.add_argument("--url", default="")
    explicit.add_argument("--actor", required=True)
    explicit.add_argument("--sha256")
    explicit.add_argument("--title", default="")
    explicit.add_argument("--revision", default="")
    status = commands.add_parser("job")
    status.add_argument("id")
    commands.add_parser("versions")
    rights = commands.add_parser("rights")
    rights.add_argument("decision", choices=("granted", "revoked"))
    rights.add_argument("document")
    rights.add_argument("version")
    rights.add_argument("--actor", required=True)
    rights.add_argument("--reason", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--watch", action="store_true")
    worker.add_argument("--allow-network", action="store_true")
    worker.add_argument("--allow-redirect-host", action="append", default=[])
    commands.add_parser("publish").add_argument("--idle-marker")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("generation")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=6)
    for name in ("publish", "rollback", "search"):
        _model_args(commands.choices[name])
    reference = commands.add_parser("read")
    reference.add_argument("reference", help="JSON reference returned by search")
    return main


def _run(args):
    command = args.command
    if command == "init":
        Store.initialize(args.root)
        return {"state": "initialized"}
    store = Store(args.root)
    if command in {"submit-saved", "submit-explicit", "job", "worker"}:
        intake = Intake(store)
        if command == "submit-saved":
            return intake.submit_local(args.path, expected_sha256=args.sha256, actor=args.actor,
                                       doi=args.doi, url=args.url, title=args.title, local_key=args.local_key)
        if command == "submit-explicit":
            return intake.submit_explicit(actor=args.actor, doi=args.doi, url=args.url, title=args.title,
                                          expected_sha256=args.sha256, revision=args.revision)
        if command == "job":
            return intake.queue.status(args.id)
        os.nice(10)
        acquire = ExplicitAcquirer(args.allow_redirect_host) if args.allow_network else None
        while True:
            try:
                result = intake.process_one(acquire=acquire)
            except (sqlite3.Error, OSError, ValueError) as exc:
                if not args.watch:
                    raise
                result = {"state": "deferred", "error": type(exc).__name__}
            if not args.watch:
                return result or {"state": "idle"}
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            time.sleep(2 if result else 5)
    if command == "versions":
        return Catalog(store).versions()
    if command == "rights":
        Catalog(store).decide(args.document, args.version, decision=args.decision,
                              actor=args.actor, reason=args.reason)
        return {"state": args.decision}
    if command == "read":
        return Reader(store).reference(json.loads(args.reference))
    if command == "search":
        with Reader(store).pin(**_models(args)) as pinned:
            return pinned.search(args.query, args.limit)
    os.nice(10)
    idle_marker = getattr(args, "idle_marker", None)
    publisher = Publisher(store, idle=(lambda: idle_marker is None or Path(idle_marker).is_file()), **_models(args))
    if command == "rollback":
        return publisher.rollback(args.generation)
    return publisher.publish()


def main():
    os.environ.setdefault("RAYON_NUM_THREADS", "2")
    os.environ.setdefault("TOKIO_WORKER_THREADS", "2")
    args = parser().parse_args()
    try:
        result = _run(args)
    except (OSError, sqlite3.Error, ValueError, LookupError, RuntimeError) as exc:
        code = str(exc)
        if not code or len(code) > 80 or not all(char.isalnum() or char == "_" for char in code):
            code = type(exc).__name__
        print(json.dumps({"state": "failed", "error": code}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
