"""Bounded lexical matching over approved immutable legacy chunks.

The term-frequency factor adapts Citrus QA Agent BM25Plus in bm25.py (MIT),
Copyright (c) 2026 Citrus QA Agent contributors. It uses native chunk character
length for multilingual normalization and requires all lexical terms in text.
"""
import heapq
import json
from unicodedata import normalize

from ..lexical import is_han
from ..relevance import Relevance
from .chunks import _MAX_LINE, clean, document_ref
from .inventory import LegacyUnavailable


def _eligible(row, source, index, topics, filters):
    if topics and row.get('l1_category', '').strip().casefold() not in topics:
        return False
    doc = row['paper_id']
    doi, year, _ = index.doc_metadata[doc]
    if filters.get('doi') and filters['doi'] != doi:
        return False
    if filters.get('year') and filters['year'] != year:
        return False
    return not filters.get('document_ref') or filters['document_ref'] == document_ref(source, doc)


def _word_part(char):
    # Same word boundaries as corpus.lexical: underscores and Han runs
    # separate non-Han words, as do non-alphanumeric characters.
    return char.isalnum() and not is_han(char)


def _frequency(text, term, han):
    if han:
        return text.count(term)
    count = pos = 0
    total = len(text)
    while (start := text.find(term, pos)) >= 0:
        end = start + len(term)
        if ((start == 0 or not _word_part(text[start - 1]))
                and (end == total or not _word_part(text[end]))):
            count += 1
            pos = end
        else:
            pos = start + 1
    return count


def _score(text, matchers, avg):
    normalized = normalize('NFKC', text).casefold()
    frequencies = []
    for term, han in matchers:
        # Locate literal forms with native substring search and inspect word
        # boundaries only near matches, not every character in the corpus.
        frequency = _frequency(normalized, term, han)
        if not frequency:
            return 0.0
        frequencies.append(frequency)
    norm = 1.5 * (0.25 + 0.75 * len(text) / max(1, avg))
    return sum(2 * numerator / (numerator + norm) for numerator in frequencies)


def rank(stream, source, index, query, topics, filters, cap, check):
    terms = sorted(Relevance(query, None).terms)
    if not terms or cap <= 0:
        return []
    # Each shortlist window is capped by the caller's lexical allocation.
    # The byte guard only optimizes literal forms supported by the legacy
    # analyzer; JSON-escaped records also fall through to the Unicode analyzer.
    han = next((term for term in terms if is_han(term)), None)
    guard = han.encode() if han else next((term.encode() for term in terms if term.isascii()), None)
    selected = []
    matchers = [(term, is_han(term)) for term in terms]
    for n, raw in enumerate(iter(lambda: stream.readline(_MAX_LINE + 1), b'')):
        if n % 128 == 0:
            check()
        if guard and guard not in (raw if han else raw.lower()) and b'\\u' not in raw:
            continue
        # ChunkIndex already validated the bytes of this exact open file.
        try:
            row = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError) as exc:
            raise LegacyUnavailable('legacy_chunk_unstable') from exc
        if not _eligible(row, source, index, topics, filters):
            continue
        text = clean(row['text'])
        score = _score(text, matchers, index.avg_chars)
        if score > 0:
            item = (score, -n, row)
            if len(selected) < cap:
                heapq.heappush(selected, item)
            elif item[:2] > selected[0][:2]:
                heapq.heapreplace(selected, item)
    return [r for _, _, r in sorted(selected, reverse=True)]
