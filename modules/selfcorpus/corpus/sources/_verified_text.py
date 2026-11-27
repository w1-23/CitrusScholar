"""Validate the trusted Host's exact PMC Cloud text and evidence objects.

The signed API authenticates their origin assertion. This adapter performs no
network fetch and checks that the signed evidence binds the text and identifiers.
"""
import base64
import html
import json
import re
from hashlib import md5, sha256

from ..errors import CorpusError
from ._documents import Source, _now

KIND = 'verified_text'
_CLOUD = 'https://pmc-oa-opendata.s3.amazonaws.com'
_EVIDENCE_URL = re.compile(re.escape(_CLOUD) + r'/metadata/(PMC[1-9][0-9]{0,11})\.([1-9][0-9]{0,8})\.json')
_DOI = re.compile(r'10\.[0-9]{4,9}/[a-z0-9._;:()/+\-]+', re.ASCII)
_PMID = re.compile(r'[1-9][0-9]{0,11}')
_SHA = re.compile(r'[0-9a-f]{64}')
_NOTICE_TITLE = re.compile(
    r'^(?:retract(?:ion|ed)|withdraw(?:al|n)|expression of concern|editorial|corrigendum|erratum|'
    r'correction|author correction|publisher correction)(?=\s*[:\-]|\s+(?:to|of|for)\b|$)', re.I)
_BODY_HEADING = re.compile(
    r'^[ \t]*(?:(?:[0-9]+\.?[ \t]+)[|.–—-]?[ \t]*)?'
    r'(?:introduction|background|methods|materials and methods|patients and methods|'
    r'results|discussion|case (?:report|presentation))[ \t]*[:.]?[ \t]*\r?$', re.I | re.M)
_MAX_TEXT = 8 * 1024 * 1024
_MAX_EVIDENCE = 256 * 1024
_LICENSE_CODES = {
    'cc by': 'cc-by', 'cc-by': 'cc-by', 'cc by-sa': 'cc-by-sa', 'cc-by-sa': 'cc-by-sa',
    'cc0': 'cc0', 'cc zero': 'cc0', 'cc0-1.0': 'cc0',
    'public domain': 'public-domain', 'public-domain': 'public-domain',
}
_FIELDS = frozenset({'kind', 'doi', 'pmid', 'pmcid', 'title', 'license_id',
                     'evidence_url', 'text_url', 'sha256', 'content_b64', 'metadata_b64'})


def _invalid():
    return CorpusError('invalid_verified_text', 'PMC Cloud text or its evidence could not be verified.', 422)


def _valid_doi(doi):
    return 0 < len(doi) <= 500 and _DOI.fullmatch(doi) and not {'', '.', '..'}.intersection(doi.split('/'))


def _title(value):
    if not isinstance(value, str) or len(value) > 1024:
        return ''
    title = ' '.join(html.unescape(re.sub(r'<[^>]*>', '', value)).split())
    if len(title) > 512 or any(not ch.isprintable() or ch in '<>' for ch in title):
        return ''
    return '' if _NOTICE_TITLE.search(title) else title


def validate_spec(spec):
    """Check the signed envelope before persisting a background job."""
    if (spec.keys() != _FIELDS or any(not isinstance(value, str) for value in spec.values())
            or spec['kind'] != KIND or not _valid_doi(spec['doi'])
            or (spec['pmid'] and not _PMID.fullmatch(spec['pmid']))
            or not 0 < len(spec['title']) <= 512
            or spec['license_id'] not in _LICENSE_CODES.values() or not _SHA.fullmatch(spec['sha256'])):
        raise _invalid()
    match = _EVIDENCE_URL.fullmatch(spec['evidence_url'])
    if not match or match[1] != spec['pmcid']:
        raise _invalid()
    key = f'{match[1]}.{match[2]}'
    path = f'/{key}/{key}.txt'
    if spec['text_url'] != _CLOUD + path:
        raise _invalid()
    for field, maximum in (('content_b64', _MAX_TEXT), ('metadata_b64', _MAX_EVIDENCE)):
        if not spec[field]:
            raise _invalid()
        if len(spec[field]) > 4 * ((maximum + 2) // 3):
            raise CorpusError('source_too_large', 'The PMC Cloud source exceeds the allowed size.', 413)
    return int(match[2]), path


def _decode(value, limit):
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raise _invalid() from None
    if len(raw) > limit:
        raise CorpusError('source_too_large', 'The PMC Cloud source exceeds the allowed size.', 413)
    return raw


def _unique_object(items):
    record = dict(items)
    if len(items) != len(record):
        raise ValueError('duplicate metadata field')
    return record


def _invalid_constant(_):
    raise ValueError('invalid metadata number')


def _evidence(raw, spec, version, path):
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(data, dict):
        raise _invalid()
    code = data.get('license_code')
    doi, pmid = data.get('doi'), data.get('pmid')
    pmid = '' if pmid is None else str(pmid) if type(pmid) in (int, str) else None
    object_uri = data.get('text_url')
    object_match = re.fullmatch(re.escape('s3://pmc-oa-opendata' + path) + r'\?md5=([0-9a-f]{32})',
                                object_uri) if isinstance(object_uri, str) else None
    if (data.get('is_pmc_openaccess') is not True or data.get('is_retracted') is not False
            or not object_match or not isinstance(code, str)
            or _LICENSE_CODES.get(code.strip().casefold()) != spec['license_id']
            or data.get('pmcid') != spec['pmcid'] or type(data.get('version')) is not int or data['version'] != version
            or not isinstance(doi, str) or doi.strip().lower() != spec['doi']
            or pmid != spec['pmid']
            or _title(data.get('title')) != spec['title']):
        raise _invalid()
    return object_match[1]


def read_verified_text(spec):
    version, path = validate_spec(spec)
    evidence = _decode(spec['metadata_b64'], _MAX_EVIDENCE)
    expected_md5 = _evidence(evidence, spec, version, path)
    raw = _decode(spec['content_b64'], _MAX_TEXT)
    if (sha256(raw).hexdigest() != spec['sha256'] or b'\x00' in raw
            or md5(raw, usedforsecurity=False).hexdigest() != expected_md5):
        raise _invalid()
    try:
        text = raw.decode('utf-8-sig').lstrip(' \t\r\n')
    except UnicodeDecodeError:
        raise _invalid() from None
    _validate_text(text, spec, version)
    metadata = {key: spec[key] for key in ('doi', 'pmid', 'pmcid', 'title', 'license_id',
                                           'evidence_url', 'text_url', 'sha256')}
    metadata.update(fulltext=True, coverage='verified_open_access_fulltext', pmc_version=version,
                    evidence_sha256=sha256(evidence).hexdigest(), text_md5=expected_md5)
    return Source(raw, 'text/plain', spec['text_url'], metadata,
                  'Verified PMC Cloud open-access original.', _now(), None, KIND)


def _validate_text(text, spec, version):
    # PMC Cloud TXT embeds an article identity header before the paper text.
    header_start = re.search(r'(?m)^ARTICLE INFORMATION\r?\n=+\r?\n', text[:8192])
    if not header_start or not text.startswith('JOURNAL INFORMATION') or len(text.rstrip()) < 1500:
        raise _invalid()
    header = re.split(r'\r?\n[ \t]*\r?\n', text[header_start.end():header_start.end() + 4096], 1)[0]
    for label, expected in (('PMCID', spec['pmcid']), ('PMID', spec['pmid']), ('DOI', spec['doi']),
                            ('Article version', str(version))):
        values = re.findall(rf'(?m)^{label}:[ \t]*([^\r\n]*)', header)
        if [value.strip().casefold() for value in values] != ([expected.casefold()] if expected else []):
            raise _invalid()
    # The PMC formatter separates its front matter from article prose with this delimiter.
    _, separator, prose = text.partition('\x9f==============================\x9f')
    heading = _BODY_HEADING.search(prose)
    if not separator or not heading or len(prose[heading.end():].strip()) < 500:
        raise _invalid()
