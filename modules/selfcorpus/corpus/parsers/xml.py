"""Bounded XML loading and JATS/TEI extraction. DTD resources are never loaded."""

import re
from xml.etree import ElementTree as ET
from xml.parsers import expat

from .model import Block, Parsed, build

MAX_XML_BYTES = 32 * 1024 * 1024
_IMAGE_ELEMENTS = frozenset({'graphic', 'inline-graphic', 'media', 'binary', 'svg'})
_XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'


def read_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise ValueError("XML byte limit exceeded")
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    depth = 0
    count = 0

    def qualify(name):
        return "{" + name if "}" in name else name

    def start(tag, attrs):
        nonlocal depth, count
        depth += 1
        count += 1
        if depth > 96 or count > 250_000:
            raise ValueError("XML structural limit exceeded")
        builder.start(qualify(tag), {qualify(k): v for k, v in attrs.items()})

    def end(tag):
        nonlocal depth
        builder.end(qualify(tag))
        depth -= 1

    def doctype(_name, _system, _public, internal_subset):
        # JATS often declares an external DTD. Ignore it entirely, but reject all internal subsets.
        if internal_subset:
            raise ValueError("XML internal DTD subsets are not supported")

    def skipped_entity(_name, _parameter):
        raise ValueError("XML references an unavailable entity")

    parser.SkippedEntityHandler = skipped_entity
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = doctype
    parser.ExternalEntityRefHandler = lambda *_args: 0
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, LookupError) as exc:
        raise ValueError("XML is invalid or references an unavailable entity") from exc
    return builder.close()


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text(node) -> str:
    def parts(element):
        if local(element.tag) in _IMAGE_ELEMENTS:
            return
        if element.text:
            yield element.text
        for child in element:
            yield from parts(child)
            if child.tail:
                yield child.tail

    return " ".join("".join(parts(node)).split())


def first(node, name):
    return next((child for child in node.iter() if local(child.tag) == name), None)


def parse_article(root: ET.Element, *, parser: str | None = None) -> Parsed:
    tag = local(root.tag)
    if tag in {"article", "pmc-articleset"}:
        if tag == "pmc-articleset":
            articles = [child for child in root if local(child.tag) == "article"]
            if len(articles) != 1:
                raise ValueError("JATS input must contain exactly one article")
            root = articles[0]
        version = "jats/1"
        header = first(root, "article-meta")
        title_tag = "article-title"
    elif tag == "TEI":
        version = "tei/1"
        header = first(root, "teiHeader")
        title_tag = "title"
    else:
        raise ValueError("XML is not a JATS article or TEI document")
    jats = version.startswith("jats/")
    title = ""
    metadata = {}
    language = root.get(_XML_LANG) or root.get('lang')
    if not language and not jats:
        body_language = first(root, 'text')
        if body_language is not None:
            language = body_language.get(_XML_LANG)
    if language:
        metadata['language'] = language[:64]
    blocks: list[Block] = []
    path: tuple[str, ...] = ()
    page: int | None = None
    if header is not None:
        title_node = first(header, title_tag)
        if title_node is not None and (title := text(title_node)):
            metadata["title"] = title
            path = (title,)
            blocks.append(Block(title, path, kind="heading", level=1))
        authors = []
        for node in header.iter():
            name = local(node.tag)
            if name in {"article-id", "idno"}:
                if (node.get("pub-id-type") or node.get("type") or "").lower() == "doi" and (value := text(node)):
                    metadata.setdefault("doi", value)
            if name == ("contrib" if jats else "author") and (not jats or node.get("contrib-type", "author") == "author"):
                display = first(node, "name" if jats else "persName")
                if display is None:
                    display = first(node, "collab" if jats else "orgName")
                if display is not None:
                    parts = [text(part) for part in display if text(part)]
                    if value := " ".join(parts) or text(display):
                        authors.append(value)
        if authors:
            metadata["authors"] = list(dict.fromkeys(authors))

    def pages(node):
        coords = node.get("coords", "")
        values = [int(match) for match in re.findall(r"(?:^|;)\s*([1-9][0-9]{0,6}),", coords)]
        return (min(values), max(values)) if values else (page, page)

    def walk(node, section):
        nonlocal page
        name = local(node.tag)
        if name == "pb":
            label = node.get("n", "")
            page = int(label) if re.fullmatch(r"[1-9][0-9]{0,6}", label) else None
            return
        if name in _IMAGE_ELEMENTS or name in {"title", "head", "teiHeader", "front-stub", "label", "sub-article", "response"}:
            return
        if name in {"sec", "div", "abstract", "ref-list", "listBibl"}:
            heading = next((child for child in node if local(child.tag) in {"head", "title"}), None)
            label = text(heading) if heading is not None else ""
            if not label:
                label = {"abstract": "Abstract", "ref-list": "References", "listBibl": "References"}.get(name, "")
            if label:
                section = (*section, label)
                start, end = pages(heading if heading is not None else node)
                blocks.append(Block(label, section, start, end, "heading", len(section)))
        if name in {"p", "item", "list-item", "ref", "bibl", "biblStruct", "caption", "figDesc", "formula", "disp-formula", "table"}:
            if name == "table":
                rows = []
                for row in node.iter():
                    if local(row.tag) not in {"tr", "row"}:
                        continue
                    cells = [text(cell).replace("|", "\\|") for cell in row if local(cell.tag) in {"th", "td", "cell"}]
                    if cells:
                        rows.append("| " + " | ".join(cells) + " |")
                value = "\n".join(rows) or text(node)
            else:
                value = text(node)
            if value:
                start, end = pages(node)
                for child in node.iter():
                    if local(child.tag) == "pb":
                        walk(child, section)
                        end = page
                blocks.append(Block(value, section, start, end, name))
            return
        if node.text and node.text.strip() and name == "abstract":
            blocks.append(Block(node.text.strip(), section, page, page))
        for child in node:
            walk(child, section)

    if header is not None:
        for node in header.iter():
            if local(node.tag) == "abstract":
                if abstract := text(node):
                    metadata.setdefault("abstract", abstract)
                    walk(node, path)
    content = next((child for child in root if local(child.tag) == "text"), None) if not jats else root
    body = next((child for child in content if local(child.tag) == "body"), None) if content is not None else None
    body_start = len(blocks)
    if body is not None:
        walk(body, path)
    warnings = [] if any(block.kind != "heading" for block in blocks[body_start:]) else ["full_text_body_not_found"]
    if content is not None:
        back = next((child for child in content if local(child.tag) == "back"), None)
        if back is not None:
            walk(back, path)
    return build(parser or version, blocks, metadata, warnings=warnings)
