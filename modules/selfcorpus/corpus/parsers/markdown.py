"""Built-in Markdown and plain-text parsers."""

import re

from .model import Block, Parsed, build, heading_path

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
# Inline and reference-form image markup is not indexed as prose.
_IMAGE = re.compile(r'!\[(?:[^\]\\]|\\.)*\](?:\((?:[^()\r\n]|\([^()\r\n]*\))*\)|\[[^\]\r\n]*\])')
_IMAGE_DATA_REF = re.compile(r'^ {0,3}\[[^\]\r\n]+\]:[ \t]*<?data:image/[^\r\n]*$', re.I | re.M)


def _without_images(text: str) -> str:
    return _IMAGE_DATA_REF.sub('', _IMAGE.sub('', text))


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def parse_markdown(text: str, warnings: list[str] | None = None, *, version: str = "markdown/1") -> Parsed:
    blocks: list[Block] = []
    mutable_path: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    pending: list[str] = []
    fence = ""
    title = ""

    def flush(kind: str = "paragraph") -> None:
        if pending:
            blocks.append(Block("\n".join(pending).strip("\n"), path, kind=kind))
            pending.clear()

    for line in text.splitlines():
        marker = _FENCE.match(line)
        if fence:
            pending.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
                flush("code")
            continue
        if marker and (marker[1][0] == "~" or "`" not in marker[2]):
            flush()
            fence = marker[1]
            pending.append(line)
            continue
        line = _without_images(line)
        atx = _ATX.fullmatch(line)
        setext = _SETEXT.fullmatch(line)
        if atx and atx[2]:
            label = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", atx[2]).strip()
            level = len(atx[1])
            flush()
        elif setext and pending and not re.match(r"^\s*(?:[-+*>]|\d+[.)] )", pending[0]):
            label = " ".join(value.strip() for value in pending)
            level = 1 if setext[1][0] == "=" else 2
            pending.clear()
        else:
            if line.strip():
                pending.append(line)
            else:
                flush()
            continue
        if label:
            path = heading_path(mutable_path, level, label)
            blocks.append(Block(label, path, kind="heading", level=level))
            if level == 1 and not title:
                title = label
    flush("code" if fence else "paragraph")
    return build(version, blocks, {"title": title} if title else {}, warnings=warnings, markdown=_without_images(text))


def parse_text(text: str, warnings: list[str] | None = None) -> Parsed:
    blocks = [Block(p.strip()) for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    return build("plain-text/1", blocks, warnings=warnings, markdown=text)
