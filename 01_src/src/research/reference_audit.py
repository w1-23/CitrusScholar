"""Owner/session-bound, incremental ReferenceAudit service shared by chat/PDF.

The host owns operation authorization and job scheduling. This service never
spawns a worker or an LLM. Each HTTP dispatch is charged durably before sending;
each completed record is committed before progress is emitted. A crashed run's
finite lease can be reclaimed, but its request budget is never replenished.
"""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from copy import deepcopy
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid

from citrus_refcheck.compare import (
    FIELDS, _value, _author_comparison, _journal_comparison, compare_fields,
    identity_sufficient as _identity_sufficient,
)

from src.research import literature as lit
from src.research.reference_extraction import digest, extract_inventory

RULE_VERSION = 'reference-fields/4'
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 6 * 3600
_AUDIT_CONCURRENCY = 4
_BATCH_LOOKUPS: ContextVar[dict | None] = ContextVar("reference_batch_lookups", default=None)
_PROVIDER_DEBIT: ContextVar = ContextVar("reference_provider_debit", default=None)


class AuditStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _scope(owner, session_id):
    from src.session import manager
    if type(owner) is not int or owner <= 0 or not isinstance(session_id, str) or not session_id:
        raise PermissionError('Authenticated owner and session required')
    if manager.session_manager.get_session_owner(session_id) != owner:
        raise PermissionError('Session not found for this owner')


@contextmanager
def _lease(owner):
    from src.core.account_leases import current_account_lease, acquire_account_lease, bound_account_lease
    current = current_account_lease()
    lease = current.retain() if current is not None and current.owner == owner else acquire_account_lease(owner)
    try:
        with bound_account_lease(lease):
            yield
    finally:
        lease.release()


@contextmanager
def _connect():
    from src import config
    path = config.PROJECT_ROOT / 'state' / 'reference_audits.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=3, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        if os.name != 'nt':
            os.chmod(path, 0o600)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''CREATE TABLE IF NOT EXISTS reference_audits (
            id TEXT PRIMARY KEY, owner INTEGER NOT NULL, session TEXT NOT NULL,
            inventory TEXT NOT NULL, status TEXT NOT NULL, request_limit INTEGER NOT NULL,
            requests_used INTEGER NOT NULL DEFAULT 0,
            queries_used INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL)''')
        conn.execute('CREATE INDEX IF NOT EXISTS reference_scope ON reference_audits(owner,session,created)')
        if 'queries_used' not in {row[1] for row in conn.execute('PRAGMA table_info(reference_audits)')}:
            conn.execute('ALTER TABLE reference_audits ADD COLUMN queries_used INTEGER NOT NULL DEFAULT 0')
        conn.execute('''CREATE TABLE IF NOT EXISTS reference_records (
            audit_id TEXT NOT NULL REFERENCES reference_audits(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, entry TEXT NOT NULL, result TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(audit_id,ordinal))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS reference_attempts (
            audit_id TEXT NOT NULL REFERENCES reference_audits(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, attempt INTEGER NOT NULL, result TEXT NOT NULL,
            created REAL NOT NULL, PRIMARY KEY(audit_id,ordinal,attempt))''')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load(conn, owner, session_id, audit_id):
    if not isinstance(audit_id, str) or not re.fullmatch(r'[a-f0-9]{32}', audit_id):
        raise ValueError('Explicit valid audit_id required')
    row = conn.execute('SELECT * FROM reference_audits WHERE id=? AND owner=? AND session=?',
                       (audit_id, owner, session_id)).fetchone()
    if row is None:
        raise LookupError('Reference audit not found')
    return row


def create_audit(owner, session_id, *, name='references.txt', content=None, text='', references=None,
                 request_budget=1200, source_ref=None, column_map=None, encoding=""):
    _scope(owner, session_id)
    inventory = extract_inventory(name=name, content=content, text=text,
                                  references=references, source_ref=source_ref,
                                  column_map=column_map, encoding=encoding)
    return _create_inventory(owner, session_id, inventory, request_budget=request_budget)


def _create_inventory(owner, session_id, inventory, *, request_budget=1200):
    _scope(owner, session_id)
    if type(request_budget) is not int or not 1 <= request_budget <= 50000:
        raise ValueError('request_budget must be 1..50000')
    if len(_json(inventory)) > 24 * 1024 * 1024:
        raise ValueError('Reference inventory exceeds storage limit')
    entries = inventory['entries']
    audit_id, now = uuid.uuid4().hex, time.time()
    with _lease(owner), _connect() as conn:
        conn.execute('''INSERT INTO reference_audits
            (id,owner,session,inventory,status,request_limit,created,updated) VALUES (?,?,?,?,?,?,?,?)''',
            (audit_id, owner, session_id, _json({k: v for k, v in inventory.items() if k != 'entries'}),
             'pending' if entries else 'partial', request_budget, now, now))
        conn.executemany('INSERT INTO reference_records(audit_id,ordinal,entry) VALUES (?,?,?)',
                         [(audit_id, i, _json(entry)) for i, entry in enumerate(entries)])
    return get_audit(owner, session_id, audit_id)


def _row(item, entry, result, verdict):
    # Keep the immutable source intact; flattened fields are compatibility only.
    return {**(result or {}), **entry, 'result': result, 'attempts': item['attempts'],
            'status': result['status'] if result else 'not_checked', 'verdict': verdict}


def _view(conn, audit, *, offset=0, limit=100):
    inventory = json.loads(audit['inventory'])
    raw = conn.execute('SELECT * FROM reference_records WHERE audit_id=? ORDER BY ordinal', (audit['id'],)).fetchall()
    from citrus_refcheck.verdict import project_verdict
    counts = {}
    verdict_counts = dict.fromkeys(('verified', 'correction_required', 'not_confirmed', 'benign_variant', 'coverage_unavailable'), 0)
    row_data = []
    processed = retryable = stale = 0
    for item in raw:
        result = json.loads(item['result']) if item['result'] else None
        status = result['status'] if result else 'not_checked'
        entry = json.loads(item['entry'])
        stored = result.get('verdict') if isinstance(result, dict) else None
        verdict = (stored if isinstance(stored, dict)
                   and stored.get('decision') in {'verified', 'correction_required', 'not_confirmed'}
                   else project_verdict(entry, result, audit_id=audit['id']) if result else None)
        row_data.append((item, entry, result, verdict))
        if verdict:
            verdict_counts[verdict['decision']] += 1
            verdict_counts['benign_variant'] += bool(verdict['benign_variants'])
            source_coverage = verdict['coverage']
            verdict_counts['coverage_unavailable'] += bool(source_coverage['failed_sources'] or source_coverage['omitted_sources']
                                                           or not source_coverage['queried_sources'])
        counts[status] = counts.get(status, 0) + 1
        if result:
            processed += 1
            outdated = (result.get('rule_version') != RULE_VERSION
                        or result.get('claims_sha256') != json.loads(item['entry'])['claims_sha256'])
            stale += int(outdated)
            retryable += int(bool(result.get('retryable')) or outdated)
    total = len(raw)
    pending = total - processed
    complete = bool(total and not pending and not retryable and not counts.get('not_checked')
                    and not counts.get('network_error') and inventory['extraction']['status'] == 'complete')
    processing_complete = bool(total and not pending)
    review_required = bool(inventory['extraction']['status'] != 'complete'
                           or verdict_counts['correction_required'] or verdict_counts['not_confirmed'])
    terminal = processing_complete and not audit['cancelled']
    status = ('completed_with_review' if (review_required or retryable) else 'complete') if terminal else audit['status']
    has_budget = audit['requests_used'] < audit['request_limit']
    retry_optional = bool(total and not pending and retryable and not stale)
    next_action = ('export_and_review' if terminal and (review_required or retryable) else 'export' if terminal
                   else 'budget_exhausted_export_partial' if not has_budget
                   else 'export_and_review' if retry_optional
                   else 'resume' if audit['cancelled'] or (retryable and not pending)
                   else 'run' if pending else 'review_source')
    history = {}
    for attempt in conn.execute('SELECT ordinal,attempt,result,created FROM reference_attempts WHERE audit_id=? AND ordinal>=? AND ordinal<? ORDER BY ordinal,attempt',
                                (audit['id'], offset, offset + limit)):
        history.setdefault(attempt['ordinal'], []).append({'attempt': attempt['attempt'],
            'created_at': attempt['created'], 'result': json.loads(attempt['result'])})
    return {'schema': 'reference_audit/2', 'audit_id': audit['id'],
            'status': status, 'source': inventory['source'],
            'terminal': terminal, 'review_required': review_required,
            'next_action': next_action, 'can_resume': bool(has_budget and (pending or retryable)),
            'export_ready': True, 'retry_optional': retry_optional,
            'parser_version': inventory['parser_version'], 'rule_version': RULE_VERSION,
            'extraction': inventory['extraction'], 'counts': counts, 'verdict_counts': verdict_counts,
            'coverage': {'total': total, 'submitted': total, 'processed': processed,
                         'pending': pending, 'retryable': retryable, 'complete': complete,
                         'processing_complete': processing_complete,
                         'extraction_complete': inventory['extraction']['status'] == 'complete',
                         'source_sha256': inventory['source']['sha256']},
            'budget': {'request_limit': audit['request_limit'], 'requests_used': audit['requests_used'],
                       'queries_used': audit['queries_used'],
                       'requests_remaining': max(0, audit['request_limit'] - audit['requests_used'])},
            'rows': [{**_row(item, entry, result, verdict), 'attempt_history': history.get(item['ordinal'], [])}
                     for item, entry, result, verdict in row_data[offset:offset + limit]],
            'offset': offset, 'next_offset': offset + limit if offset + limit < total else None,
            'created_at': audit['created'], 'updated_at': audit['updated'],
            'summary': ('库存条目均已尝试；来源错误或未确认结论保留在报告中。如需重试，显式 resume=True，仍服从剩余预算；无需为交付持续续跑。'
                        if terminal and retry_optional else
                        '库存条目均已尝试，待人工复核解析或字段结果；请导出此 audit_id 的全量证据，无需反复 resume。'
                        if terminal and review_required else
                        '登记记录与字段准确性分开呈现；未查到、解析遗漏或联网失败不能证明文献虚构。'),
            '_tool_outcome': {'status': 'complete' if terminal else 'partial',
                              'reason_code': 'audit_completed_with_review' if terminal and (review_required or retryable)
                              else 'audit_complete' if terminal
                              else 'reference_extraction_incomplete' if processing_complete
                              else 'reference_coverage_incomplete',
                              'artifacts': [], 'result_refs': [{'kind': 'reference_audit', 'audit_id': audit['id']}]}}


def get_audit(owner, session_id, audit_id, *, offset=0, limit=100):
    _scope(owner, session_id)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError('Invalid audit page')
    with _lease(owner), _connect() as conn:
        return _view(conn, _load(conn, owner, session_id, audit_id), offset=offset, limit=limit)


def list_audits(owner, session_id, *, limit=20):
    _scope(owner, session_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid audit list limit')
    with _lease(owner), _connect() as conn:
        rows = conn.execute('SELECT * FROM reference_audits WHERE owner=? AND session=? ORDER BY created DESC LIMIT ?',
                            (owner, session_id, limit)).fetchall()
        return [_view(conn, row, limit=0) for row in rows]


def cancel_audit(owner, session_id, audit_id):
    _scope(owner, session_id)
    with _lease(owner), _connect() as conn:
        _load(conn, owner, session_id, audit_id)
        # Fence this runner immediately; late results cannot overwrite cancelled state.
        conn.execute("UPDATE reference_audits SET cancelled=1,status='cancelled',lease_token='',lease_until=0,updated=? WHERE id=?",
                     (time.time(), audit_id))
    return get_audit(owner, session_id, audit_id)


def _lookup_cached(owner, session_id, key, call, context):
    from src.core.capability_policy import allows, current_policy
    capability = 'web_search' if key['provider'] == 'DeepSeek Search' else 'bibliographic_lookup'
    if not allows(capability):
        return 0, [], capability + '_disabled'
    cache_key = (owner, session_id, current_policy().history_key, digest(key))
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL:
            evidence = deepcopy(cached[2])
            for item in evidence:
                item.update(cache_hit=True, cache_age_seconds=round(now - cached[0], 3))
            context.evidence.extend(evidence)
            return deepcopy(cached[1])
    lookups = _BATCH_LOOKUPS.get()
    future = None
    if lookups is not None:
        with _CACHE_LOCK:
            previous = lookups.get(cache_key)
            if previous is None:
                future = lookups[cache_key] = Future()
        if previous is not None:
            try:
                result, evidence = previous.result(timeout=max(0, context.deadline - time.monotonic()))
            except TimeoutError as exc:
                raise AuditStopped('deadline') from exc
            context.evidence.extend({**item, 'batch_reuse': True} for item in deepcopy(evidence))
            return deepcopy(result)
    start = len(context.evidence)
    try:
        result = call()
        evidence = deepcopy(context.evidence[start:])
        if (result[0] == 200 and isinstance(result[1], list) and result[1]
                and all(isinstance(item, dict) for item in result[1]) and not result[2]):
            with _CACHE_LOCK:
                _CACHE[cache_key] = (now, deepcopy(result), evidence)
                if len(_CACHE) > 2000:
                    for stale in sorted(_CACHE, key=lambda item: _CACHE[item][0])[:500]:
                        del _CACHE[stale]
        if future is not None:
            future.set_result((deepcopy(result), evidence))
    except BaseException as exc:
        if future is not None:
            future.set_exception(exc)
        raise
    return result


def verify_record(claims, *, owner=0, session_id='', uncertain_fields=(), deadline=None, check=None, debit=None):
    """Direct adapter for one entry; callers persist it through run_audit.

    Uses existing registry adapters, original language queries and no LLM/web
    fallback. Returns every provider candidate and actual HTTP receipt.
    """
    from src.core.capability_policy import allows
    if not (allows('bibliographic_lookup') or allows('web_search')):
        return {'status': 'not_checked', 'reason': 'network_disabled', 'retryable': True,
                'existence': 'not_checked', 'identity': 'not_checked', 'rule_version': RULE_VERSION,
                'fields': compare_fields(claims, None), 'evidence': [], 'candidates': [],
                'facts': {'records': [], 'providers': {}, 'errors': {}, 'http_evidence': []}}
    deadline = min(deadline or time.monotonic() + 25, time.monotonic() + 25)
    doi, title = lit.normalize_doi(claims.get('doi')), _value(claims, 'title')
    arxiv_id = _value(claims, 'arxiv_id')
    records, errors, providers = [], {}, {}
    planned_sources = set()
    query_time = time.time()
    stopped = ''
    with lit.bounded_lookup(deadline=deadline, check=check, debit=debit) as context:
        def plan(kind, source_names):
            planned_sources.update(name + ':' + kind for name in source_names)

        def query(provider, kind, value, function, *, collect=True):
            nonlocal stopped
            if check:
                check()
            if time.monotonic() >= deadline:
                stopped = 'deadline'
                return
            try:
                status, found, error = _lookup_cached(owner, session_id,
                    {'provider': provider, 'kind': kind, 'value': value}, function, context)
            except AuditStopped as exc:
                stopped = exc.reason
                raise
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                status, found, error = 0, [], 'invalid_provider_payload:' + type(exc).__name__
            key = provider + ':' + kind
            if status not in (200, 404) and not error:
                error = f'http_{status}' if isinstance(status, int) and 100 <= status <= 599 else 'invalid_response'
            if status == 200 and (not isinstance(found, list) or any(not isinstance(item, dict) for item in found)):
                error = error or 'invalid_provider_payload'
            providers[key] = 'error' if error else 'not_found' if status == 404 else 'ok'
            if error:
                errors[key] = error
            if status == 200 and not error:
                if collect:
                    records.extend({**record, 'lookup_kind': kind, 'record_sha256': digest(record)}
                                   for record in found if record.get('title') or record.get('doi'))
                if context.evidence:
                    context.evidence[-1]['candidate_count'] = len(found)
                return found
            return []
        def has_identity(identifier, field):
            return bool(identifier and any(
                item.get('source') in {'Crossref', 'DataCite', 'arXiv'}
                and (lit.normalize_doi(item.get(field)) if field == 'doi' else item.get(field)) == identifier
                and (not title or (lit.norm_title(item.get('title')) == lit.norm_title(title)
                                    and 'title' not in uncertain_fields))
                for item in records
            ))

        try:
            if doi:
                encoded = lit.quote(doi, safe='/')
                for provider, function in (('Crossref', lit._crossref_work), ('DataCite', lit._datacite_work)):
                    plan('doi', (provider,))
                    query(provider, 'doi', doi, lambda fn=function: fn(encoded))
                    if has_identity(doi, 'doi'):
                        break
            elif arxiv_id:
                plan('arxiv_id', ('arXiv',))
                query('arXiv', 'arxiv_id', arxiv_id, lambda: lit._arxiv_by_id(arxiv_id))
                if not has_identity(arxiv_id, 'arxiv_id'):
                    plan('arxiv_id', ('DataCite',))
                    query('DataCite', 'arxiv_id', arxiv_id, lambda: lit._datacite_arxiv(arxiv_id))
            if title and not (has_identity(doi, 'doi') or has_identity(arxiv_id, 'arxiv_id')):
                matched = False
                if not (doi or arxiv_id):
                    plan('title', ('DeepSeek Search',))
                    candidates = query('DeepSeek Search', 'title', lit.norm_title(title),
                                       lambda: lit._deepseek_reference_candidates(title), collect=False) or []
                    identifiers = {item['doi'] for item in candidates
                                   if lit.norm_title(item['title']) == lit.norm_title(title)}
                    if len(identifiers) == 1:
                        suggested_doi = next(iter(identifiers))
                        plan('title_registry', ('Crossref',))
                        query('Crossref', 'title_registry', suggested_doi,
                              lambda: lit._crossref_work(lit.quote(suggested_doi, safe='/')))
                        for item in records:
                            if lit.normalize_doi(item.get('doi')) != suggested_doi:
                                continue
                            checks = compare_fields(claims, item, uncertain_fields=uncertain_fields)
                            matched = all(checks[field]['status'] == 'matched' for field in ('title', 'authors', 'year'))
                            if matched:
                                break
                if not matched:
                    plan('title', ('Crossref', 'OpenAlex'))
                    query('Crossref', 'title', title, lambda: lit._crossref_search(title))
                    query('OpenAlex', 'title', title, lambda: lit._openalex_search(title))
        except AuditStopped as exc:
            stopped = exc.reason
        evidence = deepcopy(context.evidence)
    facts = {'records': records, 'providers': providers, 'errors': errors,
             'http_evidence': evidence, 'planned_sources': sorted(planned_sources), 'query_time': query_time}
    return _project_verification(claims, facts, uncertain_fields=uncertain_fields, stopped=stopped)


def _project_verification(claims, facts, *, uncertain_fields=(), stopped=''):
    """Project provider facts into the host's historical verification fields.

    Legacy statuses describe submitted-field observations, never a truth verdict.
    Keep the underlying facts separate from candidate ranking and field hints.
    """
    doi, title = lit.normalize_doi(claims.get('doi')), _value(claims, 'title')
    arxiv_id = _value(claims, 'arxiv_id')
    records = deepcopy(facts['records'])
    errors, evidence = facts['errors'], facts['http_evidence']

    def identifier_matches(item):
        return bool(lit.normalize_doi(item.get('doi')) == doi if doi
                    else arxiv_id and item.get('arxiv_id') == arxiv_id)

    # Identity is the primary selection axis. Title search can suggest an
    # alternative citation but must never replace the record named by the DOI.
    candidates = sorted(records, key=lambda item: (
        0 if identifier_matches(item) else 1,
        -lit.title_similarity(title, item.get('title')) if title else 0))
    direct = [item for item in candidates if identifier_matches(item)]
    relevant = [item for item in candidates if item in direct
                or (title and lit.title_similarity(title, item.get('title')) >= lit.CANDIDATE_FLOOR)]
    best = relevant[0] if relevant else None
    for item in candidates:
        item['fields'] = compare_fields(claims, item, uncertain_fields=uncertain_fields)
        item['identifier_match'] = item in direct
        item['title_similarity'] = round(lit.title_similarity(title, item.get('title')), 3)
    fields = compare_fields(claims, best, uncertain_fields=uncertain_fields)
    corroborated = bool(best and fields['title']['status'] == 'matched'
                        and fields['authors']['status'] == 'matched' and fields['year']['status'] == 'matched')
    identity = 'identifier_match' if direct and best in direct else 'metadata_corroborated' if corroborated else 'candidate' if best else 'unresolved'
    if doi and best and lit.normalize_doi(best.get('doi')) != doi:
        identity = 'identifier_conflict_candidate'
    # A returned record exists, but may be unrelated to the submitted citation.
    existence = 'record_found' if direct or corroborated else 'candidate_records_found' if best else 'not_established'
    provider_conflicts = []
    if best:
        for item in candidates:
            if item is best or not best.get('doi') or lit.normalize_doi(item.get('doi')) != lit.normalize_doi(best['doi']):
                continue
            comparison = compare_fields(best, item)
            different = [key for key, value in comparison.items() if value['status'] == 'conflicting']
            if different:
                provider_conflicts.append({'provider': item.get('source'), 'fields': different,
                                           'record_sha256': item['record_sha256']})
    # Assess disagreement against the submitted claim, keeping the independent
    # provider-to-provider differences in the evidence above.
    same_record = [item for item in relevant if best and item is not best
                   and (item in direct if best in direct else best.get('doi')
                        and lit.normalize_doi(item.get('doi')) == lit.normalize_doi(best['doi']))]
    if fields['year']['reason'] in {'possible_online_print_difference_not_documented', 'provider_field_missing'}:
        corroborating_year = next((item for item in same_record
                                   if item['fields']['year']['status'] == 'matched'), None)
        if corroborating_year:
            fields['year'] = {**corroborating_year['fields']['year'],
                              'record_sha256': corroborating_year['record_sha256']}
    for field, observation in fields.items():
        if observation['status'] != 'matched':
            continue
        if any(item['fields'][field]['status'] == 'conflicting' for item in same_record):
            observation.update(status='uncertain', reason='providers_disagree_on_submitted_field')
    conflicts = [key for key, value in fields.items() if value['status'] == 'conflicting']
    uncertain = any(value['status'] in {'uncertain', 'missing'} for value in fields.values())
    if not best:
        status = 'not_checked' if not (doi or arxiv_id or title) else 'network_error' if errors or stopped else 'not_found_in_checked_registries'
    elif conflicts:
        status = 'metadata_mismatch'
    elif identity in {'identifier_match', 'metadata_corroborated'} and not uncertain and not (errors or stopped):
        status = 'metadata_matched'
    else:
        status = 'title_candidate' if identity in {'candidate', 'identifier_conflict_candidate'} else 'metadata_uncertain'
    identities = {lit.normalize_doi(item.get('doi')) or item.get('arxiv_id') or item.get('url')
                  for item in relevant if lit.norm_title(item.get('title')) == lit.norm_title(title)} - {None, ''}
    if len(identities) > 1 and identity == 'metadata_corroborated':
        identity, status = 'ambiguous_candidates', 'metadata_uncertain'
    return {'status': status, 'existence': existence, 'identity': identity, 'fields': fields,
            'metadata_scope': 'submitted_fields_only',
            'rule_version': RULE_VERSION, 'claims_sha256': digest(claims),
            'retryable': bool(errors or stopped), 'stop_reason': stopped,
            'facts': facts,
            'evidence': evidence, 'errors': errors, 'candidates': candidates,
            'selected_record': best, 'identifier_records': direct,
            'title_candidates': [item for item in candidates if item.get('lookup_kind') == 'title'
                                 and not identifier_matches(item)],
            'selection_basis': 'exact_identifier' if direct else 'title_candidate' if best else 'unresolved',
            'provider_conflicts': provider_conflicts,
            'limits': {'max_seconds': 25, 'title_candidates_per_provider': 8,
                       'web_search': 'DeepSeek Search:title' in facts['providers'], 'translation': 'not_performed',
                       'response_limit_bytes': 4 * 1024 * 1024, 'retries': 0},
            'doi': doi, 'input_title': title, 'input_authors': _value(claims, 'authors'),
            'input_year': _value(claims, 'year'),
            'metadata_title': _value(best or {}, 'title'), 'metadata_authors': _value(best or {}, 'authors'),
            'metadata_year': _value(best or {}, 'year'), 'metadata_journal': _value(best or {}, 'journal'),
            'provider': (best or {}).get('source', ''), 'source_url': (best or {}).get('url', ''),
            'evidence_url': (best or {}).get('url', ''), 'field_conflicts': '; '.join(conflicts),
            'detail': '登记记录存在与引用身份、字段准确性分别报告；未查到不等于不存在。'}


# ---------------------------------------------------------------- identity kernel
from citrus_refcheck.verification import (
    BatchLimits as _KernelLimits, Capabilities as _KernelCapabilities,
    Observation as _KernelObservation, verify_batch as _verify_batch,
)
from citrus_refcheck.lookup import AuditStopped as _KernelStopped
from citrus_refcheck.providers import normalize_doi as _kernel_doi, norm_title as _kernel_title
from citrus_refcheck.compare import _value as _kernel_value

_KERNEL_VERSION = 'citrus_refcheck/reference-verification/1'
_KERNEL_DOI = re.compile(r'10\.\d{4,9}/\S+')
_KERNEL_STOPS = frozenset({'cancelled', 'canceled', 'deadline', 'request_budget_exhausted',
                           'network_disabled', 'operation_stopped', 'lease_lost',
                           'item_deadline', 'rate_limit_cooldown'})


def _kernel_reason(error):
    reason = str(getattr(error, 'reason', error) or 'operation_stopped')
    if reason == 'budget_exhausted':
        reason = 'request_budget_exhausted'
    return reason if reason in _KERNEL_STOPS else 'operation_stopped'


def _legacy_reason(reason):
    return {'canceled': 'cancelled', 'item_deadline': 'deadline',
            'request_budget_exhausted': 'budget_exhausted',
            'rate_limit_cooldown': 'operation_stopped'}.get(reason, reason or '')


def _kernel_check(check):
    def guarded():
        try:
            check()
        except AuditStopped as exc:
            raise _KernelStopped(_kernel_reason(exc)) from exc
    return guarded


def _kernel_debit(debit):
    def guarded(query):
        try:
            debit()
        except AuditStopped as exc:
            raise _KernelStopped(_kernel_reason(exc)) from exc
    return guarded


class _KernelRecorder:
    """Thread-safe side channel for exact receipts and candidates per query."""

    def __init__(self):
        self.lock = threading.Lock()
        self.receipts = {}
        self.records = {}
        self.evidence = {}

    def add(self, key, source, status, error, evidence):
        refs = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            ref = str(item.get('response_sha256') or '')
            if len(ref) == 64:
                refs.append('http:' + ref)
            elif item.get('url'):
                refs.append('http:' + digest([item.get('url'), item.get('status'),
                                              item.get('http_status')]))
        receipt = {'source': source, 'http_status': status if type(status) is int else 0,
                   'error': error or '', 'evidence_refs': list(dict.fromkeys(refs))[:20]}
        with self.lock:
            self.receipts.setdefault(key, []).append(receipt)
            self.evidence.setdefault(key, []).extend(
                item for item in evidence[:20] if isinstance(item, dict))

    def add_records(self, key, source, records, lookup_kind):
        with self.lock:
            bucket = self.records.setdefault(key, [])
            seen = {(item.get('source'), item.get('doi') or item.get('arxiv_id')
                     or _kernel_title(item.get('title'))) for item in bucket}
            for record in records[:20]:
                if not isinstance(record, dict):
                    continue
                item = {**record, 'source': record.get('source') or source,
                        'lookup_kind': lookup_kind}
                identity = (item.get('source'), item.get('doi') or item.get('arxiv_id')
                            or _kernel_title(item.get('title')))
                if identity in seen:
                    continue
                seen.add(identity)
                bucket.append(item)
            del bucket[20:]


def _entry_query_key(entry):
    if entry.get('parse_status') == 'invalid_row':
        return None
    claims = entry.get('claims') or {}
    doi = _kernel_doi(claims.get('doi'))
    if doi:
        return ('doi', doi) if _KERNEL_DOI.fullmatch(doi) else None
    arxiv = _kernel_value(claims, 'arxiv_id')
    if arxiv:
        return ('arxiv', arxiv)
    title = _kernel_value(claims, 'title')
    return ('search', title) if title else None


def _call_provider(function, args, source, deadline, check, recorder, key, kind,
                   *, collect_records=True):
    try:
        debit = _PROVIDER_DEBIT.get()
        if debit is not None:
            debit()
        with lit.bounded_lookup(deadline=deadline, check=check, debit=lambda: None) as context:
            status, records, error = function(*args)
        evidence = deepcopy(context.evidence)
    except _KernelStopped:
        raise
    except Exception as exc:
        status, records, error, evidence = 0, [], type(exc).__name__, []
    if type(status) is not int:
        error = error or str(status) or 'invalid_response'
        status = 0
    if status not in (200, 404) and not error:
        error = f'http_{status}' if 100 <= status <= 599 else 'invalid_response'
    if not isinstance(records, (list, tuple)):
        records, error = [], error or 'invalid_provider_payload'
    records = [record for record in records if isinstance(record, dict)]
    usable = status == 200 and not error
    recorder.add(key, source, status, error, evidence)
    if collect_records:
        recorder.add_records(key, source, records if usable else [], kind)
    return {'status': status, 'records': records if usable else [],
            'error': error or '', 'evidence': evidence}


def _observation(recorder, key, source):
    receipts = list(recorder.receipts.get(key, []))
    records = [dict(item) for item in recorder.records.get(key, [])][:20]
    successes = [item for item in receipts
                 if item.get('http_status') == 200 and not item.get('error')]
    statuses = [item.get('http_status') for item in receipts
                if type(item.get('http_status')) is int]
    if successes:
        status, error = 200, ''
    elif 429 in statuses:
        status, error = 429, ''
    elif statuses and all(value == 404 for value in statuses):
        status, error = 404, ''
    else:
        status = statuses[-1] if statuses else 0
        error = next((item.get('error') for item in receipts if item.get('error')), '')
        if not error:
            error = f'http_{status}' if 100 <= status <= 599 else 'provider_error'
    refs = ['record:' + digest(record) for record in records]
    for receipt in receipts:
        refs.extend(receipt.get('evidence_refs') or [])
    return _KernelObservation(source=source, http_status=status, records=tuple(records),
                              evidence_refs=tuple(list(dict.fromkeys(refs))[:20]), error=error)


def _make_lookups(recorder, check, web_allowed, debit=None):
    def with_debit(lookup):
        def guarded(query, deadline):
            token = _PROVIDER_DEBIT.set(debit)
            try:
                return lookup(query, deadline)
            finally:
                _PROVIDER_DEBIT.reset(token)
        return guarded

    def doi_lookup(query, deadline):
        key, base = ('doi', query.value), query.value
        first = _call_provider(lit._crossref_work, (lit.quote(base, safe='/'),),
                               'Crossref', deadline, check, recorder, key, 'doi')
        direct = any(_kernel_doi(record.get('doi')) == base for record in first['records'])
        if first['status'] == 404 or (first['status'] == 200 and not direct):
            _call_provider(lit._datacite_work, (lit.quote(base, safe='/'),),
                           'DataCite', deadline, check, recorder, key, 'doi')
        return _observation(recorder, key, 'Crossref+DataCite')

    def arxiv_lookup(query, deadline):
        key, base = ('arxiv', query.value), query.value
        first = _call_provider(lit._arxiv_by_id, (base,), 'arXiv', deadline, check,
                               recorder, key, 'arxiv_id')
        found = any(record.get('arxiv_id') == base for record in first['records'])
        if first['status'] == 404 or (first['status'] == 200 and not found):
            _call_provider(lit._datacite_arxiv, (base,), 'DataCite', deadline, check,
                           recorder, key, 'arxiv_id')
        return _observation(recorder, key, 'arXiv+DataCite')

    def title_lookup(query, deadline):
        key, title = ('search', query.value), query.value
        if web_allowed:
            suggested = _call_provider(lit._deepseek_reference_candidates, (title,),
                                       'DeepSeek Search', deadline, check, recorder, key,
                                       'title', collect_records=False)
            dois = []
            for record in suggested['records']:
                doi = _kernel_doi(record.get('doi'))
                if doi and _KERNEL_DOI.fullmatch(doi) and doi not in dois:
                    dois.append(doi)
                if len(dois) == 2:
                    break
            claims = {'title': title, 'authors': getattr(query, 'authors', '')}
            for doi in dois:
                confirmed = _call_provider(lit._crossref_work,
                                           (lit.quote(doi, safe='/'),), 'Crossref',
                                           deadline, check, recorder, key, 'doi')
                match = next((record for record in confirmed['records']
                              if _kernel_doi(record.get('doi')) == doi), None)
                if match is not None and _identity_sufficient(claims, match):
                    return _observation(recorder, key, 'Crossref')
        first = _call_provider(lit._crossref_search, (title,), 'Crossref', deadline,
                               check, recorder, key, 'title')
        exact = any(_kernel_title(record.get('title')) == _kernel_title(title)
                    for record in first['records'])
        if first['status'] == 404 or (first['status'] == 200 and not exact):
            _call_provider(lit._openalex_search, (title,), 'OpenAlex', deadline, check,
                           recorder, key, 'title')
        return _observation(recorder, key, 'DeepSeek Search+Crossref+OpenAlex')

    return (with_debit(doi_lookup), with_debit(arxiv_lookup), with_debit(title_lookup))


def _kernel_verdict(entry, result, audit_id):
    from citrus_refcheck.verdict import project_verdict
    verdict = project_verdict(entry, result, audit_id=audit_id)
    status = result.get('match_status')
    if status in {'unconfirmed', 'unprocessed'}:
        verdict['identity'] = 'unresolved'
        verdict['decision'] = 'not_confirmed'
        if not any(item.get('field') == 'identity' for item in verdict['critical_unknowns']):
            verdict['critical_unknowns'].insert(0, {
                'field': 'identity', 'missing_evidence': 'unique_registry_identity',
                'reason': result.get('reason') or status})
    elif status == 'conflict':
        verdict['identity'] = 'conflict'
        if verdict['material_differences']:
            verdict['decision'] = 'correction_required'
        else:
            verdict['decision'] = 'not_confirmed'
            if not any(item.get('field') == 'identity' for item in verdict['critical_unknowns']):
                verdict['critical_unknowns'].insert(0, {
                    'field': 'identity', 'missing_evidence': 'consistent_registry_identity',
                    'reason': result.get('reason') or 'identity_conflict'})
    elif status == 'matching':
        if verdict['material_differences']:
            verdict['decision'] = 'correction_required'
        elif verdict['decision'] == 'not_confirmed':
            remaining = [item for item in verdict['critical_unknowns']
                         if not (item.get('field') == 'identity'
                                 and item.get('reason') == 'unresolved')]
            if not remaining:
                verdict['identity'] = 'confirmed'
                verdict['decision'] = 'verified'
                verdict['critical_unknowns'] = remaining
    verdict['note'] = {
        'verified': '已确认引用身份，未发现有证据支持的关键差错。',
        'correction_required': '登记证据支持更正至少一项已提交的书目信息。',
        'not_confirmed': '现有证据不足以确认引用；未核实不等于不存在。',
    }[verdict['decision']]
    verdict['kernel'] = _KERNEL_VERSION
    return verdict


def _kernel_result(entry, evidence, recorder, audit_id):
    claims = entry.get('claims') or {}
    uncertain = entry.get('uncertain_fields') or []
    key = _entry_query_key(entry)
    raw_match = evidence.get('match') if isinstance(evidence, dict) else None
    match = dict(raw_match) if isinstance(raw_match, dict) else None
    receipts = list(recorder.receipts.get(key, [])) if key else []
    candidates = [dict(item) for item in recorder.records.get(key, [])] if key else []
    lookup_kind = key[0] if key else ''
    kind = {'doi': 'doi', 'arxiv': 'arxiv_id'}.get(lookup_kind, 'title')
    if match is not None:
        match.setdefault('source', next((item.get('source') for item in candidates
                                         if match.get('doi')
                                         and _kernel_doi(item.get('doi')) == _kernel_doi(match.get('doi'))), ''))
        match.setdefault('lookup_kind', kind)
        match['record_sha256'] = match.get('record_sha256') or digest(match)
        duplicate = any(
            item.get('source') == match.get('source')
            and (item.get('doi') or item.get('title')) == (match.get('doi') or match.get('title'))
            for item in candidates)
        if not duplicate:
            candidates.insert(0, match)
    for item in candidates:
        item.setdefault('lookup_kind', kind)
        item['record_sha256'] = item.get('record_sha256') or digest(item)
    providers, errors, http_evidence = {}, {}, []
    for receipt in receipts:
        source = str(receipt.get('source') or 'adapter')
        status = receipt.get('http_status')
        error = str(receipt.get('error') or '')
        if status == 200 and not error:
            providers[source] = 'ok'
        elif status == 404 and not error:
            providers[source] = 'not_found'
        else:
            providers[source] = 'error'
            errors[source] = error or (f'http_{status}'
                                       if type(status) is int and 100 <= status <= 599
                                       else 'invalid_response')
        http_evidence.append({'source': source, 'http_status': status,
                              'error': errors.get(source, ''),
                              'evidence_refs': list(receipt.get('evidence_refs') or [])})
    provider_conflicts = []
    by_doi = {}
    for item in candidates:
        doi = _kernel_doi(item.get('doi'))
        if doi:
            by_doi.setdefault(doi, []).append(item)
    for group in by_doi.values():
        if len({item.get('source') for item in group}) < 2:
            continue
        base = group[0]
        for item in group[1:]:
            fields = [field for field, value in compare_fields(base, item).items()
                      if value['status'] == 'conflicting']
            if fields:
                provider_conflicts.append({'provider': item.get('source'), 'fields': fields,
                                           'record_sha256': item.get('record_sha256')})
    fields = compare_fields(claims, match, uncertain_fields=uncertain)
    processing = str(evidence.get('processing_status') or 'unprocessed')
    match_status = str(evidence.get('match_status') or 'unprocessed')
    reason = str(evidence.get('reason') or '')
    if processing != 'processed':
        status, existence, identity = 'not_checked', 'not_checked', 'not_checked'
    elif match_status == 'matching':
        differences = evidence.get('differences') or []
        status = ('metadata_mismatch'
                  if any(item.get('status') == 'conflicting' for item in differences)
                  else 'metadata_matched')
        existence = 'record_found'
        identity = ('identifier_match' if reason == 'identifier_match'
                    else 'metadata_corroborated')
    elif match_status == 'conflict':
        status, existence, identity = ('metadata_mismatch', 'record_found',
                                       'identifier_conflict_candidate')
    elif match_status == 'unconfirmed':
        existence, identity = 'not_established', 'unresolved'
        if reason in {'rate_limited', 'provider_error'}:
            status = 'network_error'
        elif reason == 'not_found':
            status = 'not_found_in_checked_registries'
        elif reason == 'capability_unavailable':
            status = 'not_checked'
        else:
            status = 'metadata_uncertain'
    else:
        status, existence, identity = 'metadata_uncertain', 'not_established', 'unresolved'
    retryable = bool(processing != 'processed' or errors
                     or reason in {'rate_limited', 'provider_error', 'deadline',
                                   'request_budget_exhausted', 'item_deadline'})
    if reason in {'not_found', 'invalid_input', 'capability_unavailable'}:
        retryable = False
    result = {
        'status': status, 'existence': existence, 'identity': identity, 'fields': fields,
        'metadata_scope': 'minimal_identity', 'rule_version': RULE_VERSION,
        'claims_sha256': entry.get('claims_sha256') or digest(claims),
        'retryable': retryable,
        'stop_reason': reason if reason in _KERNEL_STOPS else '',
        'facts': {'records': candidates, 'providers': providers, 'errors': errors,
                  'http_evidence': http_evidence, 'planned_sources': sorted(providers),
                  'query_time': time.time()},
        'evidence': http_evidence, 'errors': errors, 'candidates': candidates,
        'selected_record': match,
        'identifier_records': [item for item in candidates
                               if claims.get('doi')
                               and _kernel_doi(item.get('doi')) == _kernel_doi(claims.get('doi'))],
        'title_candidates': [item for item in candidates
                             if item is not match and item.get('lookup_kind') == 'title'],
        'selection_basis': ('exact_identifier' if reason == 'identifier_match'
                            else 'title_match' if reason == 'title_match' else 'unresolved'),
        'provider_conflicts': provider_conflicts,
        'limits': {'max_seconds': 25, 'kernel': _KERNEL_VERSION,
                   'response_limit_bytes': 4 * 1024 * 1024, 'retries': 0},
        'doi': _kernel_doi(claims.get('doi')), 'input_title': _kernel_value(claims, 'title'),
        'input_authors': _kernel_value(claims, 'authors'), 'input_year': _kernel_value(claims, 'year'),
        'metadata_title': _kernel_value(match or {}, 'title'),
        'metadata_authors': _kernel_value(match or {}, 'authors'),
        'metadata_year': _kernel_value(match or {}, 'year'),
        'metadata_journal': _kernel_value(match or {}, 'journal'),
        'provider': (match or {}).get('source', ''), 'source_url': (match or {}).get('url', ''),
        'evidence_url': (match or {}).get('url', ''),
        'field_conflicts': '; '.join(field for field, value in fields.items()
                                     if value['status'] == 'conflicting'),
        'match_status': match_status, 'processing_status': processing, 'reason': reason,
        'identity_differences': evidence.get('differences') or [],
        'detail': '身份内核仅核对存在性与最小身份；已处理但未确认不等于文献不存在。',
    }
    result['verdict'] = _kernel_verdict(entry, result, audit_id)
    return result


def run_audit(owner, session_id, audit_id, *, max_rows=20, time_budget=45, resume=False, progress=None,
              require_started=False):
    from src.core.cancel import is_cancelled
    from src.core.capability_policy import allows
    _scope(owner, session_id)
    if type(max_rows) is not int or not 1 <= max_rows <= 100:
        raise ValueError('max_rows must be 1..100')
    if not isinstance(time_budget, (int, float)) or not math.isfinite(time_budget) or not 0 < time_budget <= 120:
        raise ValueError('time_budget must be >0 and <=120 seconds')
    if require_started:
        with _lease(owner), _connect() as conn:
            audit = _load(conn, owner, session_id, audit_id)
            # A stored inventory alone was never admitted to verification. Require
            # a durable run lease, lookup debit, or exact persisted attempt.
            started = (audit['lease_token'] or audit['requests_used'] or audit['queries_used'] or
                       conn.execute('SELECT 1 FROM reference_attempts WHERE audit_id=? LIMIT 1',
                                    (audit_id,)).fetchone())
            if not started:
                return {'run_reason': 'legacy_audit_not_started'}
    if not (allows('bibliographic_lookup') or allows('web_search')):
        result = get_audit(owner, session_id, audit_id)
        result['run_reason'] = 'network_disabled'
        return result
    from src.core.tracing import get_job_id
    from src.core.task_events import remaining_seconds
    inherited_remaining = remaining_seconds(get_job_id())
    if inherited_remaining <= 0:
        result = get_audit(owner, session_id, audit_id)
        result['run_reason'] = 'deadline'
        return result
    time_budget = min(time_budget, inherited_remaining)
    token, deadline = uuid.uuid4().hex, time.monotonic() + time_budget
    completed, reason = [], ''
    with _lease(owner):
        with _connect() as conn:
            audit = _load(conn, owner, session_id, audit_id)
            if audit['lease_token'] and audit['lease_until'] > time.time():
                result = _view(conn, audit)
                result['run_reason'] = 'already_running'
                return result
            if audit['cancelled'] and not resume:
                result = _view(conn, audit)
                result['run_reason'] = 'cancelled_use_resume'
                return result
            view = _view(conn, audit)
            if view['terminal'] and not (resume and view['coverage']['retryable']):
                view['run_reason'] = 'already_completed'
                view['processed_indices'] = []
                return view
            if view['retry_optional'] and not resume:
                view['run_reason'] = 'all_entries_attempted_use_explicit_resume'
                view['processed_indices'] = []
                return view
            conn.execute("UPDATE reference_audits SET status='running',cancelled=0,lease_token=?,lease_until=?,updated=? WHERE id=?",
                         (token, time.time() + time_budget + 10, time.time(), audit_id))
            raw = conn.execute('SELECT * FROM reference_records WHERE audit_id=? ORDER BY ordinal', (audit_id,)).fetchall()
            pending, retries = [], []
            for item in raw:
                entry = json.loads(item['entry'])
                prior = json.loads(item['result']) if item['result'] else None
                pair = (item['ordinal'], entry, prior)
                if (prior is None or prior.get('rule_version') != RULE_VERSION
                        or prior.get('claims_sha256') != entry['claims_sha256']):
                    pending.append(pair)
                elif resume and prior.get('retryable'):
                    retries.append(pair)
            selected = (pending + retries)[:max_rows]
            remaining_budget = max(0, int(audit['request_limit'] - audit['requests_used']))

        def check():
            if is_cancelled():
                raise AuditStopped('cancelled')
            if time.monotonic() >= deadline:
                raise AuditStopped('deadline')
            if not (allows('bibliographic_lookup') or allows('web_search')):
                raise AuditStopped('network_disabled')
            with _connect() as conn:
                current = _load(conn, owner, session_id, audit_id)
                if current['cancelled'] or current['lease_token'] != token:
                    raise AuditStopped('cancelled' if current['cancelled'] else 'lease_lost')

        def _lease_checked():
            check()
            with _connect() as conn:
                current = _load(conn, owner, session_id, audit_id)
                if current['lease_token'] != token or current['cancelled']:
                    raise AuditStopped('lease_lost')
                return current

        def debit_request():
            current = _lease_checked()
            if current['requests_used'] >= current['request_limit']:
                raise AuditStopped('budget_exhausted')
            with _connect() as conn:
                conn.execute('UPDATE reference_audits SET requests_used=requests_used+1 WHERE id=?', (audit_id,))

        def debit_query():
            _lease_checked()
            with _connect() as conn:
                conn.execute('UPDATE reference_audits SET queries_used=queries_used+1 WHERE id=?', (audit_id,))

        batch = None
        recorder = _KernelRecorder()
        try:
            guarded_check = _kernel_check(check)
            lookups = _make_lookups(recorder, guarded_check, allows('web_search'), debit_request)
            capabilities = _KernelCapabilities(check=guarded_check, debit=_kernel_debit(debit_query),
                                               lookup_doi=lookups[0], lookup_arxiv=lookups[1],
                                               search=lookups[2])

            def persist_row(row):
                evidence = row.get('evidence') or {}
                stop = str(evidence.get('reason') or evidence.get('stop_reason') or '')
                if (evidence.get('processing_status') != 'processed'
                        and stop not in {'invalid_input', 'capability_unavailable'}):
                    return
                result = _kernel_result(row, evidence, recorder, audit_id)
                with _connect() as conn:
                    current = _load(conn, owner, session_id, audit_id)
                    if current['lease_token'] != token or current['cancelled']:
                        raise AuditStopped('cancelled' if current['cancelled'] else 'lease_lost')
                    conn.execute('UPDATE reference_records SET result=?,attempts=attempts+1 WHERE audit_id=? AND ordinal=?',
                                 (_json(result), audit_id, row['record_index']))
                    conn.execute('''INSERT INTO reference_attempts(audit_id,ordinal,attempt,result,created)
                        SELECT audit_id,ordinal,attempts,result,? FROM reference_records WHERE audit_id=? AND ordinal=?''',
                                 (time.time(), audit_id, row['record_index']))
                    conn.execute('UPDATE reference_audits SET updated=? WHERE id=?', (time.time(), audit_id))
                completed.append(row['record_index'])
                if progress:
                    try:
                        progress({'audit_id': audit_id, 'record_id': row.get('record_id', ''),
                                  'record_index': row['record_index'],
                                  'status': result['status'], 'durable': True})
                    except Exception:
                        pass
            limits = _KernelLimits(concurrency=_AUDIT_CONCURRENCY,
                                   request_budget=remaining_budget,
                                   timeout=float(time_budget),
                                   item_timeout=float(max(0.1, min(8.0, time_budget))),
                                   rate_limit_retries=0)
            entries = [entry for _, entry, _ in selected]
            batch = _verify_batch({'entries': entries}, capabilities=capabilities, limits=limits,
                                  on_row=persist_row)
        except (_KernelStopped, AuditStopped) as exc:
            batch, reason = None, _legacy_reason(_kernel_reason(exc))
        except Exception:
            batch, reason = None, 'operation_stopped'
        if batch is not None:
            reason = reason or _legacy_reason(str(batch.get('stop_reason') or ''))
        with _connect() as conn:
            current = _load(conn, owner, session_id, audit_id)
            if current['lease_token'] == token:
                final = _view(conn, current, limit=0)
                status = ('cancelled' if reason == 'cancelled'
                          else 'budget_exhausted' if reason == 'budget_exhausted'
                          else final['status'] if final['terminal'] else 'partial')
                conn.execute('UPDATE reference_audits SET status=?,cancelled=?,lease_token=?,lease_until=0,updated=? WHERE id=?',
                             (status, int(reason == 'cancelled'), '', time.time(), audit_id))
    result = get_audit(owner, session_id, audit_id)
    result.update(run_reason=reason or 'batch_finished', processed_indices=sorted(completed))
    return result


def verification_rows(owner, session_id, audit_id):
    """All original entries, including unprocessed/failed; never read 'latest'."""
    return get_audit(owner, session_id, audit_id, limit=5000)['rows']


_EXPORT_VERDICT_LABELS = {
    'verified': '已核实',
    'correction_required': '建议更正',
    'not_confirmed': '尚未确认',
}


def _export_decision(verdict):
    decision = verdict.get('decision')
    return decision if decision in _EXPORT_VERDICT_LABELS else 'not_confirmed'


def export_audit(owner, session_id, audit_id, *, format='json'):
    payload = get_audit(owner, session_id, audit_id, limit=5000)
    format = str(format).lower()
    name = 'reference_audit_' + audit_id
    if format == 'json':
        return _json(payload).encode('utf-8'), 'application/json', name + '.json'
    if format == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        entries = wb.active
        entries.title = 'References'
        entries.append(['record_index', 'record_id', 'citation', 'status', 'existence', 'identity', 'claims', 'positions',
                        'verdict', 'verdict_code', 'verdict_identity', 'benign_variants', 'material_differences',
                        'critical_unknowns', 'source_coverage'])
        fields = wb.create_sheet('Fields')
        fields.append(['record_id', 'field', 'status', 'submitted', 'registered', 'reason',
                       'input_certainty', 'comparison_status'])
        evidence = wb.create_sheet('Evidence')
        evidence.append(['record_id', 'kind', 'sequence', 'json_part', 'json'])

        def safe(value):
            text = str(value if value is not None else '')
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            return "'" + text if text.startswith(('=', '+', '-', '@')) else text

        def evidence_json(record_id, kind, value, sequence=0):
            text = _json(value)
            # Excel's cell limit must not silently truncate original evidence.
            for part, start in enumerate(range(0, max(1, len(text)), 30000)):
                evidence.append([record_id, kind, sequence, part, text[start:start + 30000]])
                evidence.cell(evidence.max_row, 5).data_type = 's'

        for row in payload['rows']:
            result = row.get('result') or {}
            verdict = row.get('verdict') or {}
            decision = _export_decision(verdict)
            details = [safe(_json(verdict.get(key, []))[:30000]) for key in
                       ('benign_variants', 'material_differences', 'critical_unknowns')]
            coverage = verdict.get('coverage') or {'status': 'not_checked'}
            entries.append([row['record_index'], row['record_id'], safe(row['citation'][:30000]), row['status'],
                            result.get('existence', 'not_checked'), result.get('identity', 'not_checked'),
                            safe(_json(row['claims'])[:30000]), safe(_json(row['positions'])[:30000]),
                            _EXPORT_VERDICT_LABELS[decision], decision, verdict.get('identity', 'unresolved'),
                            *details, safe(_json(coverage)[:30000])])
            evidence_json(row['record_id'], 'original_entry', {k: v for k, v in row.items() if k != 'result'})
            evidence_json(row['record_id'], 'full_result', result)
            for field, check in result.get('fields', {}).items():
                fields.append([row['record_id'], field, check['status'], safe(check['submitted'][:30000]),
                               safe(check['registered'][:30000]), check['reason'],
                               check.get('input_certainty', ''), check.get('comparison_status', '')])
        evidence_json('', 'audit_metadata', {k: v for k, v in payload.items() if k != 'rows'})
        for sheet in wb:
            sheet.freeze_panes = 'A2'
            sheet.auto_filter.ref = sheet.dimensions
        output = io.BytesIO()
        wb.save(output)
        return output.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', name + '.xlsx'
    if format not in {'ris', 'bibtex'}:
        raise ValueError('Supported exports: json/xlsx/ris/bibtex')
    lines = []
    for row in payload['rows']:
        claims = row['claims']
        # Bibliographic projection of submitted claims, never silently replace
        # an incorrect DOI with a candidate's DOI. JSON/XLSX is the full audit.
        decision = _export_decision(row.get('verdict') or {})
        note = (f"verdict={decision} ({_EXPORT_VERDICT_LABELS[decision]}); audit_id={audit_id}; "
                f"record_id={row['record_id']}; status={row['status']}; full evidence in JSON/XLSX")
        clean = lambda value: re.sub(r'[\r\n\x00-\x1f]+', ' ', str(value or ''))
        if format == 'ris':
            lines.append('TY  - GEN')
            for tag, key in (('TI', 'title'), ('AU', 'authors'), ('PY', 'year'), ('JO', 'journal'),
                             ('VL', 'volume'), ('IS', 'issue'), ('SP', 'pages'), ('DO', 'doi')):
                value = _value(claims, key)
                if value:
                    lines.append(tag + '  - ' + clean(value))
            lines.extend(['N1  - ' + note, 'ER  - ', ''])
        else:
            def bib(value):
                return clean(value).replace('\\', '\\textbackslash{}').replace('{', '\\{').replace('}', '\\}')
            values = []
            for tag, key in (('title', 'title'), ('author', 'authors'), ('year', 'year'), ('journal', 'journal'),
                             ('volume', 'volume'), ('number', 'issue'), ('pages', 'pages'), ('doi', 'doi')):
                value = _value(claims, key)
                if value:
                    values.append('  ' + tag + ' = {' + bib(value) + '}')
            values.append('  note = {' + bib(note) + '}')
            lines.append('@misc{ref_' + row['record_id'][:16] + ',\n' + ',\n'.join(values) + '\n}\n')
    extension = 'bib' if format == 'bibtex' else 'ris'
    mime = 'application/x-bibtex' if format == 'bibtex' else 'application/x-research-info-systems'
    return '\n'.join(lines).encode('utf-8'), mime, name + '.' + extension


def purge_owner(owner):
    """Account deletion hook; caller must close admission and drain leases first."""
    with _connect() as conn:
        count = conn.execute('DELETE FROM reference_audits WHERE owner=?', (owner,)).rowcount
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if key[0] == owner:
                del _CACHE[key]
    return count
