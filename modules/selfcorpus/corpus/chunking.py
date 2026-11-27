"""Deterministic, section-aware chunking for parsed corpus documents."""

import hashlib
import re
from collections.abc import Iterator

from .parsers.markdown import parse_markdown
from .parsers.model import Parsed

CHUNKER_VERSION = "section-chunker/2"
_REFERENCE_KINDS = {"ref", "bibl", "biblStruct", "reference"}
_REFERENCE_HEADINGS = {"references", "bibliography", "literature cited", "works cited", "参考文献", "引用文献"}
DEFAULT_MAX_TOKENS = 350

# Local estimate without a model tokenizer. Count CJK/non-ASCII characters individually,
# punctuation separately, long Latin runs per six characters and long whitespace per 16.
_TOKENS = re.compile(r"[A-Za-z0-9_]{1,6}|\s{16}|\S")


def _pieces(text: str, limit: int) -> Iterator[tuple[str, int]]:
    start = count = previous_end = 0
    for match in _TOKENS.finditer(text):
        if count == limit:
            # Preserve whitespace between pieces when represented as Markdown boundaries.
            piece = text[start:previous_end].strip()
            if piece:
                yield piece, sum(1 for _ in _TOKENS.finditer(piece))
            start = match.start()
            count = 0
        count += 1
        previous_end = match.end()
    piece = text[start:].strip()
    if piece:
        yield piece, sum(1 for _ in _TOKENS.finditer(piece))


def chunk(parsed: Parsed, *, max_tokens: int = DEFAULT_MAX_TOKENS) -> list[dict]:
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    blocks = parsed.blocks or (parse_markdown(parsed.markdown).blocks if parsed.markdown.strip() else ())
    result: list[dict] = []
    pending: list[str] = []
    seen: set[tuple] = set()
    tokens = 0
    path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None

    def flush() -> None:
        nonlocal tokens
        if not pending:
            return
        text = "\n\n".join(pending)
        value = {
            "text": text,
            "heading_path": list(path),
            "page_start": page_start,
            "page_end": page_end,
            "ordinal": len(result),
            "token_count": tokens,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if path:
            value["section"] = path[-1]
        key = (path, page_start, page_end, value["content_hash"])
        if key not in seen:
            seen.add(key)
            result.append(value)
        pending.clear()
        tokens = 0

    for block in blocks:
        if block.kind == "heading":
            flush()
            continue
        if (not block.text.strip() or block.kind in _REFERENCE_KINDS or
                any(h.casefold().strip().rstrip(":：") in _REFERENCE_HEADINGS for h in block.heading_path)):
            continue
        # Keep unavailable page bounds distinct from known bounds, and do not combine
        # across pages. A range for a single block remains a range after a text split.
        if (block.heading_path, block.page_start, block.page_end) != (path, page_start, page_end):
            flush()
            path, page_start, page_end = block.heading_path, block.page_start, block.page_end
        if block.kind in {"code", "table"}:
            flush()
        for text, count in _pieces(block.text, max_tokens):
            if pending and tokens + count > max_tokens:
                flush()
            pending.append(text)
            tokens += count
        if block.kind in {"code", "table"}:
            flush()
    flush()
    return result
