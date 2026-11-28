import argparse
import json
import logging
import os
from pathlib import Path

from . import __version__
from .embeddings import HashEmbedding, OnnxEmbedding
from .storage import Database


def _embedding(args):
    if args.device == 'test':
        return HashEmbedding()
    if not args.model_dir:
        raise SystemExit('Set SELFCORPUS_MODEL_DIR or --model-dir to a local E5 ONNX snapshot.')
    return OnnxEmbedding(args.model_dir, device=args.device)


def main():
    parser = argparse.ArgumentParser(prog='selfcorpus')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--db', default=os.getenv('SELFCORPUS_DB', '.runtime/selfcorpus.sqlite3'))
    parser.add_argument('--device', choices=('cuda','cpu','test'), default=os.getenv('SELFCORPUS_DEVICE','cuda'))
    parser.add_argument('--model-dir', default=os.getenv('SELFCORPUS_MODEL_DIR'))
    sub = parser.add_subparsers(dest='command', required=True)
    web = sub.add_parser('serve')
    web.add_argument('--bind', default='127.0.0.1')
    web.add_argument('--port', type=int, default=8061)
    web.add_argument('--secret-file', type=Path, default=os.getenv('SELFCORPUS_SECRET_FILE'))
    web.add_argument('--legacy-root', type=Path, default=os.getenv('SELFCORPUS_LEGACY_ROOT'))
    web.add_argument('--legacy-dense', action='store_true', default=os.getenv('SELFCORPUS_LEGACY_DENSE') == '1')
    worker = sub.add_parser('worker')
    worker.add_argument('--once', action='store_true')
    rebuild = sub.add_parser('reembed')
    rebuild.add_argument('scope_ref')
    sub.add_parser('init')
    sub.add_parser('check-model')
    sub.add_parser('stats')
    discovery = sub.add_parser('discover-links')
    discovery.add_argument('seed')
    discovery.add_argument('--max-pages', type=int, default=5)
    discovery.add_argument('--depth', type=int, default=1)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if args.command == 'init':
        Database(args.db)
        return
    if args.command == 'discover-links':
        from .discovery import discover
        print(json.dumps(discover(args.seed, max_pages=args.max_pages, depth=args.depth), indent=2, ensure_ascii=False))
        return
    if args.command == 'stats':
        database = Database(args.db)
        with database.transaction() as db:
            counts = dict(db.execute('SELECT status,count(*) FROM requests GROUP BY status').fetchall())
            active = db.execute('SELECT count(*) FROM active').fetchone()[0]
            print(json.dumps({'requests':counts,'active_scopes':active}))
        return
    embedding = _embedding(args)
    if args.command == 'check-model':
        from .vector_index import encode_vector
        encode_vector(embedding.embed(['citrus photosynthesis'], query=True)[0], embedding.dimension)
        print(f'{embedding.signature} {embedding.dimension} {args.device} OK')
        return
    if args.command in {'serve','worker'} and args.device != 'test':
        from .vector_index import encode_vector
        encode_vector(embedding.embed(['Selfcorpus startup probe'], query=True)[0], embedding.dimension)
    database = Database(args.db)
    if args.command == 'serve':
        from .api import serve
        from .service import App
        if args.bind not in {'127.0.0.1','localhost'}:
            raise SystemExit('Serve on IPv4 loopback behind the Host or a TLS reverse proxy.')
        if not args.secret_file or args.secret_file.stat().st_mode & 0o077:
            raise SystemExit('Provide a secret file with restricted permissions via --secret-file.')
        secret = args.secret_file.read_bytes().strip()
        if len(secret) < 32:
            raise SystemExit('The Host signing secret must contain at least 32 bytes.')
        legacy = None
        if args.legacy_dense and (not args.legacy_root or not args.model_dir or args.device == 'test'):
            raise SystemExit('Attested legacy dense recall needs a read-only legacy root and pinned local E5 model.')
        if args.legacy_root:
            from .legacy import LegacyCorpus
            if args.legacy_dense:
                from .legacy import PRODUCTION_DENSE_ATTESTATION
                from .legacy.legacy_e5 import LegacyE5
                dense_model = LegacyE5(args.model_dir, device='cpu')
                legacy = LegacyCorpus(args.legacy_root, embedding=dense_model, dense_attestation=PRODUCTION_DENSE_ATTESTATION)
                legacy.prepare_dense()
            else:
                legacy = LegacyCorpus(args.legacy_root)
        serve(App(database, embedding, legacy=legacy), secret, args.bind, args.port)
    elif args.command == 'reembed':
        from .maintenance import reembed_scope
        print(reembed_scope(database, embedding, args.scope_ref))
    else:
        from .parsers import ParserRouter
        from .sources import Sources
        from .worker import Worker
        from .translation_provider import TranslationProvider
        try:
            translator = TranslationProvider.from_environment()
        except ValueError:
            raise SystemExit('Invalid worker translation configuration.') from None
        worker = Worker(database, Sources(), ParserRouter(), embedding, translator=translator)
        worker.once() if args.once else worker.run()


if __name__ == '__main__':
    main()
