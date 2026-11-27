"""Exercise a real local E5 snapshot through HTTP, queue, Worker and publication."""
import argparse
import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from corpus.api import CorpusHTTPServer
from corpus.auth import Actor
from corpus.embeddings import OnnxEmbedding
from corpus.host_client import HostClient
from corpus.parsers import ParserRouter
from corpus.service import App
from corpus.sources import Sources
from corpus.storage import Database
from corpus.worker import Worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('model_dir')
    parser.add_argument('--device', choices=('cpu','cuda'), default='cuda')
    args = parser.parse_args()
    model = OnnxEmbedding(args.model_dir, device=args.device)
    secret = b'ephemeral-loopback-selfcorpus-smoke-test-not-a-service-secret'
    docs = {
        'next': '# Orchard roots\n\n## Results\n\n柑橘根系在缺铁土壤中上调质子泵并分泌柠檬酸，显著改变了根际铁的溶解度。研究在标准温室环境下比较不同柑橘根砧，使用重复样本和显微成像确认了根尖的变化。',
        'control': '# Planetary photography\n\n## Results\n\nA camera array recorded cloud cover above Mars. A set of thirty photographs captured high altitude ice clouds at dusk. The observations used a consistent shutter speed and compared images taken on different orbits.',
    }
    with TemporaryDirectory(dir='.') as work:
        db = Database(Path(work)/'state.sqlite3')
        app = App(db, model)
        worker = Worker(db, Sources(), ParserRouter(), model)
        with CorpusHTTPServer(app, secret, '127.0.0.1', 0) as httpd:
            thread = Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            client = HostClient(secret, f'http://127.0.0.1:{httpd.server_address[1]}', timeout=30)
            alice, bob = Actor('alice',{'research':'write'}), Actor('bob',{'research':'read'})
            query = 'Why do citrus roots improve iron solubility in soil?'
            try:
                for key, text in docs.items():
                    spec = {'kind':'upload','filename':f'{key}.md','media_type':'text/markdown',
                            'content_b64':base64.b64encode(text.encode()).decode()}
                    queued = client.submit(alice,spec,'personal:alice',key)
                    assert worker.once()
                    receipt = client.request(alice, queued['request_ref'])
                    assert receipt['status']=='published', receipt
                personal = client.search(alice,query,{'personal':True})
                assert personal['status']=='complete' and personal['coverage'][0]['vector'], personal
                assert personal['results'][0]['source_uri']=='upload:next.md', personal
                assert personal['index_snapshot'][0]['generation_ref']==receipt['output']['generation_ref']
                assert client.search(bob,query,{'personal':True})['results']==[]
                spec['content_b64'] = base64.b64encode(docs['next'].encode()).decode()
                spec['filename'] = 'project.md'
                queued = client.submit(alice,spec,'project:research','project')
                assert worker.once()
                assert client.request(alice,queued['request_ref'])['status']=='published'
                assert client.search(bob,query,{'project_refs':['research']})['results'][0]['source_uri']=='upload:project.md'
                print(f'{args.device}: HTTP retrieval, cross-language ranking, publication, and isolation OK ({model.dimension} dimensions)')
            finally:
                httpd.shutdown()
                thread.join()


if __name__ == '__main__':
    main()
