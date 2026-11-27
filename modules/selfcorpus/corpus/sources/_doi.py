"""DOI metadata registries and verified retrieval of open-access candidates."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, unquote, urlencode, urlsplit
from xml.parsers import expat

from ..errors import CorpusError
from ._documents import MAX_BYTES, Source, _clean_string, _now, _received_media, _validate_content
from ._http import FETCH_TIMEOUT, HTTPResponse, PublicHTTP, _DEFAULT_HTTP


MAX_METADATA_BYTES = 2 * 1024 * 1024


_DOI = re.compile(r'10\.\d{4,9}/[^\s\x00-\x1f\x7f-\x9f]+', re.ASCII)


def normalize_doi(value: str) -> str:
    if not isinstance(value, str):
        raise CorpusError('invalid_doi', 'A DOI string is required.')
    doi = value.strip()
    if doi.lower().startswith('doi:'):
        doi = doi[4:].strip()
    elif doi.lower().startswith(('https://', 'http://')):
        try:
            if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in doi):
                raise ValueError
            parts = urlsplit(doi)
            if parts.netloc.lower() not in {'doi.org', 'dx.doi.org'}:
                raise ValueError
            doi = unquote(parts.path[1:], errors='strict')
        except (ValueError, UnicodeError):
            raise CorpusError('invalid_doi', 'Use a DOI or a doi.org resolver URL.') from None
    if len(doi) > 512 or not _DOI.fullmatch(doi) or any(
        c.isspace() or 0xD800 <= ord(c) <= 0xDFFF for c in doi
    ):
        raise CorpusError('invalid_doi', 'The DOI must start with a valid 10.xxxx/ prefix.')
    return doi.lower()


def _strings(value) -> list[str]:
    return [s for item in value if (s := _clean_string(item))] if isinstance(value, list) else []


def _records(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _crossref(record: dict) -> dict:
    dates = _obj(record.get('issued')).get('date-parts')
    year = dates[0][0] if isinstance(dates, list) and dates and isinstance(dates[0], list) and dates[0] else None
    titles = _strings(record.get('title'))
    journals = _strings(record.get('container-title'))
    authors = [' '.join(filter(None, (_clean_string(a.get('given')), _clean_string(a.get('family')))))
               or _clean_string(a.get('name')) for a in _records(record.get('author'))]
    return {'title': titles[0] if titles else '', 'authors': list(filter(None, authors)),
            'journal': journals[0] if journals else '', 'year': year if type(year) is int else None,
            'abstract': _clean_string(record.get('abstract'))}


def _datacite(record: dict) -> dict:
    titles = [t.get('title') for t in _records(record.get('titles'))]
    authors = [a.get('name') for a in _records(record.get('creators'))]
    abstracts = [d.get('description') for d in _records(record.get('descriptions'))
                 if d.get('descriptionType') == 'Abstract']
    year = record.get('publicationYear')
    return {'title': next(iter(_strings(titles)), ''), 'authors': _strings(authors),
            'year': year if type(year) is int else None,
            'abstract': next(iter(_strings(abstracts)), '')}


def _europmc(record: dict) -> dict:
    authors = _records(_obj(record.get('authorList')).get('author'))
    year = _clean_string(record.get('pubYear'))
    return {'title': _clean_string(record.get('title')), 'authors': _strings([a.get('fullName') for a in authors]),
            'journal': _clean_string(record.get('journalTitle')) or _clean_string(
                _obj(_obj(record.get('journalInfo')).get('journal')).get('title')),
            'year': int(year) if re.fullmatch(r'[0-9]{4}', year) else None,
            'abstract': _clean_string(record.get('abstractText'))}


def _openalex(record: dict) -> dict:
    authors = [_obj(a.get('author')).get('display_name') for a in _records(record.get('authorships'))]
    year = record.get('publication_year')
    journal = _obj(_obj(record.get('primary_location')).get('source')).get('display_name')
    return {'title': _clean_string(record.get('title')), 'authors': _strings(authors),
            'year': year if type(year) is int else None, 'journal': _clean_string(journal)}


def _normalize_response_doi(value) -> str | None:
    try:
        return normalize_doi(value)
    except CorpusError:
        return None


def _json_response(response: HTTPResponse) -> dict:
    try:
        result = json.loads(response.content)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, RecursionError):
        raise CorpusError('source_metadata_invalid', 'A scholarly provider returned invalid metadata.', 502) from None


class _DOILookup:
    def __init__(self, doi: str, http: PublicHTTP, deadline: float):
        self.doi = doi
        self.http = http
        self.deadline = deadline
        self.metadata = {'doi': doi}
        self.diagnostics = []
        self.providers = []
        self.works: list[tuple[str, str]] = []

    def json(self, provider: str, url: str) -> dict:
        try:
            return _json_response(self.http.get(url, MAX_METADATA_BYTES, self.deadline))
        except CorpusError as exc:
            self.diagnostics.append({'provider': provider, 'code': exc.code, 'retryable': exc.retryable})
            return {}

    def merge(self, provider: str, fields: dict) -> None:
        if provider not in self.providers:
            self.providers.append(provider)
        for key, value in fields.items():
            if value and not self.metadata.get(key):
                self.metadata[key] = value

    def registry(self, doi_path: str) -> None:
        rec = _obj(self.json('crossref', 'https://api.crossref.org/works/' + doi_path).get('message'))
        if _normalize_response_doi(rec.get('DOI')) == self.doi:
            self.merge('crossref', _crossref(rec))
        else:
            data = _obj(self.json('datacite', 'https://api.datacite.org/dois/' + doi_path).get('data'))
            rec = _obj(data.get('attributes'))
            if _normalize_response_doi(rec.get('doi')) == self.doi:
                self.merge('datacite', _datacite(rec))

    def europe_pmc(self) -> None:
        # Quote the provider's search expression and verify an exact DOI match.
        escaped = self.doi.replace('\\', '\\\\').replace('"', '\\"')
        query = urlencode({'query': f'DOI:"{escaped}"', 'format': 'json', 'resultType': 'core', 'pageSize': 10})
        api = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'
        result = self.json('europe_pmc', api + 'search?' + query)
        for record in _records(_obj(result.get('resultList')).get('result')):
            if _normalize_response_doi(record.get('doi')) != self.doi:
                continue
            self.merge('europe_pmc', _europmc(record))
            pmcid = _clean_string(record.get('pmcid')).upper()
            if record.get('isOpenAccess') == 'Y' and re.fullmatch(r'PMC[0-9]{1,12}', pmcid):
                self.works.append(('europe_pmc', f'{api}{pmcid}/fullTextXML'))
                break

    def openalex(self, doi_path: str) -> None:
        record = self.json('openalex', 'https://api.openalex.org/works/https://doi.org/' + doi_path)
        if _normalize_response_doi(record.get('doi')) != self.doi:
            return
        self.merge('openalex', _openalex(record))
        seen = set()
        for location in [_obj(record.get('best_oa_location'))] + _records(record.get('locations')):
            url = _clean_string(location.get('pdf_url'))
            if location.get('is_oa') is True and url.startswith('https://') and url not in seen:
                seen.add(url)
                self.works.append(('openalex', url))
                if len(seen) >= 2:
                    break

    def fulltext(self, uri: str) -> Source | None:
        for provider, locator in self.works:
            try:
                response = self.http.get(locator, MAX_BYTES, self.deadline)
                media = _received_media(response)
                _validate_content(response.content, media)
                if provider == 'openalex' and media != 'application/pdf':
                    raise CorpusError('source_not_fulltext', 'The OA location did not return a PDF.', 422)
                if provider == 'europe_pmc' and not _has_jats_body(media, response.content):
                    raise CorpusError('source_not_fulltext', 'Europe PMC did not return a JATS article body.', 422)
                metadata = self.provenance() | {'coverage': 'fulltext', 'fulltext': True,
                                                'fulltext_provider': provider, 'fulltext_url': response.url}
                return Source(response.content, media, uri, metadata,
                              f'Public open-access full text via {provider}.', _now(), response.status, 'doi')
            except CorpusError as exc:
                self.diagnostics.append({'provider': provider, 'stage': 'fulltext',
                                         'code': exc.code, 'retryable': exc.retryable})
        return None

    def provenance(self) -> dict:
        return self.metadata | {'providers': self.providers, 'source_diagnostics': self.diagnostics}


def _has_jats_body(media: str, content: bytes) -> bool:
    if media not in {'application/xml', 'text/xml', 'application/jats+xml'}:
        return False
    stack: list[str] = []
    depth = 0
    found = False
    nodes = 0
    parser = expat.ParserCreate(namespace_separator=' ')
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def start(name, _attrs):
        nonlocal depth, nodes
        nodes += 1
        if len(stack) >= 96 or nodes > 250_000:
            raise ValueError('XML structural limit exceeded')
        stack.append(name.rsplit(' ', 1)[-1])
        if stack[-2:] == ['article', 'body'] and not depth:
            depth = len(stack)

    def end(_name):
        nonlocal depth
        if len(stack) == depth:
            depth = 0
        stack.pop()

    def text(data):
        nonlocal found
        if depth and data.strip():
            found = True

    def reject_entity(*_):
        raise ValueError('XML entity declarations are not supported')

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler, parser.EntityDeclHandler = text, reject_entity
    try:
        parser.Parse(content, True)
    except (expat.ExpatError, ValueError):
        return False
    return found


def resolve_doi(value: str, *, http: PublicHTTP | None = None, deadline: float | None = None) -> Source:
    doi = normalize_doi(value)
    doi_path = quote(doi, safe='/')
    uri = 'https://doi.org/' + doi_path
    lookup = _DOILookup(doi, http or _DEFAULT_HTTP,
                        time.monotonic() + FETCH_TIMEOUT if deadline is None else deadline)
    lookup.registry(doi_path)
    lookup.europe_pmc()
    fulltext = lookup.fulltext(uri)
    if fulltext is not None:
        return fulltext
    lookup.works.clear()
    lookup.openalex(doi_path)
    fulltext = lookup.fulltext(uri)
    if fulltext is not None:
        return fulltext
    if not lookup.providers:
        retryable = any(item['retryable'] for item in lookup.diagnostics)
        codes = sorted({item['code'] for item in lookup.diagnostics})
        unavailable = any(code != 'source_not_found' for code in codes)
        suffix = f" Provider outcomes were {', '.join(codes)}." if codes else ''
        raise CorpusError('doi_lookup_failed' if unavailable else 'doi_not_found',
                          'No scholarly provider returned a matching DOI.' + suffix,
                          502 if unavailable else 404, retryable)
    metadata = lookup.provenance() | {'coverage': 'metadata_only', 'fulltext': False}
    content = json.dumps(metadata, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf-8')
    return Source(content, 'application/json', uri, metadata,
                  'Bibliographic metadata only. No open-access article body was retrieved.', _now(), 200, 'doi')
