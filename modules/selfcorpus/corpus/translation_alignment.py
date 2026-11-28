"""Validate per-original-passage English variants before any index write."""
import re
import unicodedata

from .errors import CorpusError
from .storage import digest

_ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
_STRONG_IDENT = re.compile(r"10\.\d{4,9}/[^\s<>]+|\b(?:PMC\d+|[A-Z]{1,6}_\d+(?:\.\d+)?)\b")
_MAX_TRANSLATED_CHARS = 16000


def _language(value):
    text = str(value or "").strip().lower().replace("_", "-")
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", text) and text not in {"und", "mul", "zxx"}:
        return text.split("-", 1)[0]
    return None


def _has_native_script(text):
    # Avoid triggering for an English passage containing a few Greek letters.
    return sum(c.isalpha() and unicodedata.name(c, "").startswith((
        "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "CYRILLIC", "ARABIC", "DEVANAGARI", "HEBREW",
        "GREEK", "THAI", "LAO", "BENGALI", "GUJARATI", "GURMUKHI", "TAMIL", "TELUGU", "KANNADA",
        "MALAYALAM", "SINHALA", "MYANMAR", "KHMER", "GEORGIAN", "ARMENIAN", "ETHIOPIC", "TIBETAN"))
        for c in text) >= 4


def align_for_index(chunks, language, translator):
    """Return independent derived rows and immutable alignment identity.

    ``translator`` returns one English string for each supplied original in
    order. Workers do not write any generation until validation succeeds.
    """
    declared = _language(language)
    selected = [ch for ch in chunks if (declared not in {None, "en", "eng"}
                                       or _has_native_script(ch['text']))]
    if not selected:
        return chunks, None
    # The translator adapter is responsible for enforcing transport and data
    # scope. Do not expose input or provider payload in the failure receipt.
    try:
        values = translator(tuple(ch['text'] for ch in selected), source_language=declared)
    except CorpusError:
        raise
    except Exception:
        raise CorpusError('translation_unavailable', 'English indexing is temporarily unavailable.', 503, True) from None
    if not isinstance(values, (list, tuple)) or len(values) != len(selected):
        raise CorpusError('translation_alignment_invalid', 'English passages do not align with the original.', 422)
    aligned = {}
    for ch, value in zip(selected, values):
        if (not isinstance(value, str) or not 0 < len(value.strip()) <= _MAX_TRANSLATED_CHARS
                or not _ASCII_WORD.search(value) or value.strip() == ch['text'].strip()
                or any(identifier.rstrip(".,;:。；，：、！？）】」』") not in value
                       for identifier in _STRONG_IDENT.findall(ch['text']))):
            raise CorpusError('translation_alignment_invalid', 'English passages do not align with the original.', 422)
        aligned[ch['ordinal']] = value.strip()
    result = [({**ch, 'text_en': aligned[ch['ordinal']]} if ch['ordinal'] in aligned else ch) for ch in chunks]
    identity = [[ch['ordinal'], ch['content_hash'], digest(ch['text_en'])] for ch in result if ch.get('text_en')]
    return result, identity
