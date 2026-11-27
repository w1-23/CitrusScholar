"""Bounded discovery for explicitly authorized public seed URLs."""
from collections import deque
from html.parser import HTMLParser
import time
from urllib.parse import urldefrag, urljoin, urlsplit

from .errors import CorpusError
from .sources import PublicHTTP


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a' and len(self.links) < 2000:
            href = dict(attrs).get('href')
            if href and len(href) <= 2048:
                self.links.append(href)


def _origin(url):
    try:
        p = urlsplit(url)
        port = p.port or (443 if p.scheme == 'https' else 80)
        if p.scheme not in ('http','https') or not p.hostname or '@' in p.netloc:
            return None
        return p.scheme, p.hostname.casefold(), port
    except ValueError:
        return None


def discover(seed, *, max_pages=5, depth=1, http=None):
    """Traverse same-origin HTML only. Report links, never enqueue or publish them."""
    origin = _origin(seed)
    if origin is None or not 1 <= max_pages <= 20 or not 0 <= depth <= 2:
        raise CorpusError('invalid_discovery', 'Provide a public HTTP(S) seed and a bounded discovery budget.')
    transport = http or PublicHTTP()
    deadline = time.monotonic() + 30
    seed = urldefrag(seed).url
    queue = deque([(seed,0)])
    seen = {seed}
    candidates, failures = {}, []
    fetched = 0
    while queue and fetched < max_pages and time.monotonic() < deadline:
        url, level = queue.popleft()
        fetched += 1
        try:
            result = transport.get(url, 1024*1024, deadline)
        except CorpusError as exc:
            failures.append({'url':url,'code':exc.code})
            continue
        # Even if the seed redirects, do not traverse a new origin.
        if _origin(result.url) != origin or 'html' not in result.headers.get('content-type','').lower():
            continue
        parser = _Links()
        parser.feed(result.content.decode('utf-8','replace'))
        for raw in parser.links:
            try:
                target = urldefrag(urljoin(result.url,raw)).url
            except ValueError:
                continue
            if _origin(target) != origin or target in seen:
                continue
            seen.add(target)
            candidates[target] = {'url':target, 'discovered_from':result.url}
            if level < depth and len(queue) < max_pages:
                queue.append((target,level+1))
            if len(candidates) >= 200:
                return {'attempted_pages':fetched,'candidates':list(candidates.values()),'failures':failures,'limited':True}
    return {'attempted_pages':fetched,'candidates':list(candidates.values()),'failures':failures,'limited':bool(queue)}
