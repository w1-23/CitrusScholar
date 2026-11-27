"""Extract DOCX body from bounded ZIP entries without writing files or resolving links."""

import io
import re
from zipfile import BadZipFile, ZipFile

from .model import Block, Parsed, build, heading_path
from .xml import first, local, read_xml, text

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_STRICT = "{http://purl.oclc.org/ooxml/wordprocessingml/main}"
_MAX_ENTRY = 16 * 1024 * 1024


def _attribute(node, name):
    return node.get(_WORD + name) or node.get(_STRICT + name) or ""


def _read(archive, name, *, optional=False):
    try:
        info = archive.getinfo(name)
    except KeyError:
        if optional:
            return None
        raise ValueError("DOCX main document is missing") from None
    if info.file_size > _MAX_ENTRY:
        raise ValueError("DOCX XML entry byte limit exceeded")
    with archive.open(info) as member:
        data = member.read(_MAX_ENTRY + 1)
    if len(data) > _MAX_ENTRY:
        raise ValueError("DOCX XML entry byte limit exceeded")
    return read_xml(data)


def _paragraph(node):
    parts = []
    for element in node.iter():
        tag = local(element.tag)
        if tag == "t" and element.text:
            parts.append(element.text)
        elif tag in {"br", "cr", "tab"}:
            parts.append("\n" if tag != "tab" else "\t")
    return "".join(parts).strip()


def parse_docx(data: bytes) -> Parsed:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            entries = archive.namelist()
            if len(entries) > 4096 or len(set(entries)) != len(entries):
                raise ValueError("DOCX archive has too many or ambiguous entries")
            document = _read(archive, "word/document.xml")
            styles = _read(archive, "word/styles.xml", optional=True)
            core = _read(archive, "docProps/core.xml", optional=True)
    except (BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("DOCX archive could not be read") from exc
    if document.tag not in {_WORD + "document", _STRICT + "document"}:
        raise ValueError("DOCX main document is invalid")
    body = first(document, "body")
    if body is None:
        raise ValueError("DOCX body is missing")
    style_levels = {}
    if styles is not None:
        for style in styles:
            if local(style.tag) != "style":
                continue
            outline = first(style, "outlineLvl")
            if outline is not None and re.fullmatch(r"[0-8]", _attribute(outline, "val")):
                style_levels[_attribute(style, "styleId")] = int(_attribute(outline, "val")) + 1
    metadata = {}
    if core is not None:
        for candidate, key in (("title", "title"), ("creator", "authors"), ("created", "date")):
            node = first(core, candidate)
            if node is not None and (value := text(node)):
                metadata[key] = [value] if key == "authors" else value
    blocks: list[Block] = []
    levels: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    notes = []
    if any(local(node.tag) in {"drawing", "pict", "object", "altChunk"} for node in body.iter()):
        notes.append("docx_embedded_content_not_extracted")
    if any(name in entries for name in ("word/footnotes.xml", "word/endnotes.xml")):
        if any(local(node.tag) in {"footnoteReference", "endnoteReference"} for node in body.iter()):
            notes.append("docx_notes_not_extracted")
    for node in body:
        if local(node.tag) == "p" and (value := _paragraph(node)):
            style = first(node, "pStyle")
            name = _attribute(style, "val") if style is not None else ""
            outline = first(node, "outlineLvl")
            match = re.fullmatch(r"heading\s*([1-9])", name, re.I)
            level = style_levels.get(name) or (int(match[1]) if match else None)
            if outline is not None and re.fullmatch(r"[0-8]", _attribute(outline, "val")):
                level = int(_attribute(outline, "val")) + 1
            if name.lower() == "title":
                level = 1
                metadata.setdefault("title", value)
            if level:
                path = heading_path(levels, level, value)
                blocks.append(Block(value, path, kind="heading", level=level))
            else:
                blocks.append(Block(value, path))
        elif local(node.tag) == "tbl":
            rows = []
            for row in node:
                if local(row.tag) == "tr":
                    values = [" ".join(_paragraph(cell).split()).replace("|", "\\|") for cell in row if local(cell.tag) == "tc"]
                    if values:
                        rows.append("| " + " | ".join(values) + " |")
            if rows:
                blocks.append(Block("\n".join(rows), path, kind="table"))
        elif local(node.tag) != "sectPr" and any(local(child.tag) == "t" and child.text for child in node.iter()):
            notes.append("docx_unsupported_block_not_extracted")
    return build("docx/1", blocks, metadata, warnings=notes)
