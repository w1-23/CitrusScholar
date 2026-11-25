"""Operator commands for an initialized private corpus root."""
from argparse import ArgumentParser
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from .acquisition import ExplicitAcquirer
from .catalog import Catalog
from .intake import Intake
from .models import CpuE5, CpuRerank, FusedRank
from .publication import Publisher, resume_publication
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
    rights.add_argument("--evidence", default="", help="operator-approved evidence URI or record identifier")
    rights.add_argument("--license", default="")
    worker = commands.add_parser("worker")
    worker.add_argument("--watch", action="store_true")
    worker.add_argument("--allow-network", action="store_true")
    worker.add_argument("--allow-redirect-host", action="append", default=[])
    worker.add_argument("--network-marker", help="optional file whose presence authorizes network dispatch")
    publish = commands.add_parser("publish")
    publish.add_argument("--idle-marker")
    publish.add_argument("--watch", action="store_true")
    publish.add_argument("--poll-seconds", type=int, default=30)
    publish.add_argument("--max-load-per-cpu", type=float, default=0.75)
    publish.add_argument("--min-free-mib", type=int, default=2048)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("generation")
    commands.add_parser("resume-publication", help="release a completed rollback hold")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=6)
    for name in ("publish", "rollback", "search"):
        _model_args(commands.choices[name])
    reference = commands.add_parser("read")
    reference.add_argument("reference", help="JSON reference returned by search")
    return main


def _publication_window(root, marker, max_load_per_cpu, min_free_mib):
    if (not math.isfinite(max_load_per_cpu) or max_load_per_cpu <= 0
            or not 0 <= min_free_mib <= 1024 * 1024):
        raise ValueError("corpus_publication_budget_invalid")

    def idle():
        if marker is not None and not Path(marker).is_file():
            return False
        load = os.getloadavg()[0]
        return (load <= max_load_per_cpu * (os.cpu_count() or 1)
                and shutil.disk_usage(root).free >= min_free_mib * 1024 ** 2)

    return idle


def _publish(args, store):
    if not 5 <= args.poll_seconds <= 3600:
        raise ValueError("corpus_publication_interval_invalid")
    idle = _publication_window(store.root, args.idle_marker, args.max_load_per_cpu, args.min_free_mib)
    publisher = Publisher(store, idle=idle, **_models(args))
    if not args.watch:
        return publisher.publish()
    previous = None
    while True:
        try:
            state = publisher.publish()
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            state = {"state": "deferred", "error": _error_code(exc)}
        if state != previous:
            print(json.dumps(state, ensure_ascii=False), flush=True)
            previous = state
        time.sleep(args.poll_seconds)


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
        def network_allowed():
            return args.allow_network and (args.network_marker is None or Path(args.network_marker).is_file())

        acquire = ExplicitAcquirer(args.allow_redirect_host, allowed=network_allowed)
        while True:
            try:
                result = intake.process_one(acquire=acquire if network_allowed() else None)
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
                              actor=args.actor, reason=args.reason, evidence=args.evidence, license_id=args.license)
        return {"state": args.decision}
    if command == "read":
        return Reader(store).reference(json.loads(args.reference))
    if command == "search":
        with Reader(store).pin(**_models(args)) as pinned:
            return pinned.search(args.query, args.limit)
    if command == "resume-publication":
        return resume_publication(store)
    os.nice(10)
    if command == "rollback":
        return Publisher(store, **_models(args)).rollback(args.generation)
    return _publish(args, store)


def _error_code(exc):
    code = str(exc)
    return code if code and len(code) <= 80 and all(c.isalnum() or c == "_" for c in code) else type(exc).__name__


def main():
    os.environ.setdefault("RAYON_NUM_THREADS", "2")
    os.environ.setdefault("TOKIO_WORKER_THREADS", "2")
    args = parser().parse_args()
    try:
        result = _run(args)
    except (OSError, sqlite3.Error, ValueError, LookupError, RuntimeError) as exc:
        print(json.dumps({"state": "failed", "error": _error_code(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
