"""One EvidenceSet query across the approved base and scoped Evolution snapshots."""
import sqlite3
from collections import Counter
from time import monotonic

from . import evidence_refs
from .evolution_read import EvolutionReader, pin
from .storage import digest, dumps
from .unified_request import EVOLUTION, requests


class Deadline(Exception):
    pass


class Unified:
    def __init__(self, database, retrieval, legacy=None):
        self.database, self.retrieval, self.legacy = database, retrieval, legacy

    def search(self, actor, payload):
        query, choices, scopes, filters, budget = requests(actor, payload)
        due = monotonic() + budget.timeout_ms / 1000

        def check():
            if monotonic() >= due:
                raise Deadline

        sources = sorted(choices)
        outcomes = {s: {'source_id': s, 'candidate_count': 0, 'returned_count': 0,
                        'reason_codes': []} for s in sources}
        generations = {s: [] for s in sources}
        base = None
        fingerprint = None
        if any(s != EVOLUTION for s in sources):
            try:
                if self.legacy is not None:
                    base = self.legacy.pin()
                    fingerprint = base.fingerprint
            except (ValueError, OSError, RuntimeError):
                base = None
        ready, candidates = Counter(), []
        base_lanes = [(s, None) for s in sources if s != EVOLUTION and base is not None]
        for s in sources:
            if s != EVOLUTION:
                if base is not None:
                    generations[s] = [{'generation_ref': base.generation}]
                else:
                    outcomes[s]['reason_codes'].append('legacy_unavailable')
        evolution = EvolutionReader(self.retrieval)
        with self.database.transaction(timeout=min(.1, budget.timeout_ms / 1000)) as db:
            pinned = pin(db, scopes)
            if EVOLUTION in choices:
                generations[EVOLUTION] = [{'scope_ref': s, 'generation_ref': g['ref']} for s, g in pinned.items()]
            caps = budget.caps(base_lanes + [(EVOLUTION, scope) for scope in pinned])
            db.set_progress_handler(lambda: monotonic() >= due, 1000)
            for (source, scope), quota in caps.items():
                maximum = sum(quota.values())
                outcome = outcomes[source]
                if not maximum:
                    outcome['reason_codes'].append('candidate_budget_exhausted')
                    continue
                try:
                    check()
                except Deadline:
                    outcome['reason_codes'].append('timeout')
                    continue
                try:
                    if scope is None:
                        data = base.search(source, query, choices[source], filters, quota, check)
                    else:
                        data = evolution.search(db, scope, pinned[scope], query, filters, quota, check)
                    rows, reasons, count = data
                    if (type(count) is not int or not 0 <= len(rows) <= count <= maximum or
                            any(row.get('source_id') != source or row.get('scope_ref') != scope or
                                row.get('generation_ref') != (base.generation if scope is None else pinned[scope]['ref'])
                                for row in rows)):
                        raise ValueError('candidate budget or generation invariant')
                    check()
                except Deadline:
                    outcome['candidate_count'] += maximum
                    outcome['reason_codes'].append('timeout')
                    continue
                except (ValueError, LookupError, OSError, RuntimeError, sqlite3.Error):
                    outcome['candidate_count'] += maximum
                    outcome['reason_codes'].append('timeout' if monotonic() >= due else 'retrieval_unavailable')
                    continue
                ready[source] += 1
                outcome['candidate_count'] += count
                outcome['reason_codes'].extend(reasons)
                for rank, row in enumerate(rows):
                    candidates.append((rank, source, scope or '', row))
            db.set_progress_handler(None, 0)
        if EVOLUTION in choices and not pinned:
            # An authorized Evolution scope with no published generation is a known empty read.
            ready[EVOLUTION] += 1
        if base is not None:
            try:
                base.check_current()
            except (ValueError, OSError, RuntimeError):
                candidates = [item for item in candidates if item[1] == EVOLUTION]
                for s, _ in base_lanes:
                    ready[s] = 0
                    generations[s] = []
                    outcomes[s]['reason_codes'].append('legacy_generation_changed')
                fingerprint = None
        results, used_chars = self._select(actor, candidates, outcomes, budget, due)
        hits = {source for _, source, _, row in candidates if str(row.get('text') or '').strip()}
        for s, out in outcomes.items():
            out['reason_codes'] = sorted(set(out['reason_codes']))
            out['status'] = ('unavailable' if not ready[s] else 'partial' if out['reason_codes'] else
                             'hit' if s in hits else 'empty')
        statuses = {out['status'] for out in outcomes.values()}
        status = ('unavailable' if statuses == {'unavailable'} else
                  'partial' if statuses & {'unavailable', 'partial'} else 'hit' if results else 'empty')
        summary = [{'source_id': s, 'generations': generations[s]} for s in sources]
        return {'schema_version': 1, 'status': status,
                'index_snapshot': {'snapshot_ref': 'snapshot_' + digest(dumps([summary, fingerprint])), 'sources': summary},
                'source_outcomes': list(outcomes.values()),
                'budget_used': {'candidates': sum(out['candidate_count'] for out in outcomes.values()),
                                'results': len(results), 'chars': used_chars},
                'results': results,
                'limitations': [{'source_id': s, 'code': code} for s, out in outcomes.items() for code in out['reason_codes']],
                'coverage': [{'source_id': s, 'generation_count': len(generations[s])} for s in sources]}

    def _select(self, actor, candidates, outcomes, budget, due):
        results, refs, seen = [], [], set()
        counts = Counter()
        used_chars = 0
        for _, source, scope, row in sorted(candidates, key=lambda item: (*item[:3], item[3]['passage_ref'])):
            if len(results) >= budget.max_results or used_chars >= budget.max_chars:
                break
            key = (source, scope, row['document_ref'], row['version_ref'], row['passage_ref'])
            if key in seen or counts[key[:4]] >= 2 or not (text := row.get('text')):
                continue
            excerpt = text[:min(2400, budget.max_chars - used_chars)]
            if not excerpt.strip():
                continue
            seen.add(key)
            counts[key[:4]] += 1
            exact = row.get('locator') is not None and row.get('passage_hash') == digest(text)
            mapping = evidence_refs.build(actor.subject, row) if exact else None
            used_chars += len(excerpt)
            results.append({**{k: row[k] for k in ('source_id', 'scope_ref', 'generation_ref', 'document_ref', 'version_ref',
                            'passage_ref', 'passage_hash', 'source_hash', 'doi', 'year') if row.get(k) is not None},
                            'title': str(row.get('title') or '')[:2048], 'excerpt': excerpt, 'readback': 'exact' if exact else 'excerpt_only',
                            'evidence_ref': mapping[0] if exact else 'excerpt_' + digest(dumps(key))})
            if exact:
                refs.append((results[-1], mapping))
            outcomes[source]['returned_count'] += 1
        if refs:
            try:
                with self.database.transaction(write=True, timeout=max(0, min(.1, due - monotonic()))) as db:
                    evidence_refs.save(db, [mapping for _, mapping in refs])
            except sqlite3.Error:
                for row, _ in refs:
                    row['readback'] = 'excerpt_only'
                    row['evidence_ref'] = 'excerpt_' + digest(row['evidence_ref'])
                    outcomes[row['source_id']]['reason_codes'].append('exact_read_unavailable')
        return results, used_chars

    def read(self, actor, payload):
        return evidence_refs.read(self.database, self.legacy, actor, payload)
