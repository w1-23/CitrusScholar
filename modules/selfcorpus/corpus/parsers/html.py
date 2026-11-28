"""HTML text and heading extraction with no fetching or active content."""

from dataclasses import dataclass, field
from html.parser import HTMLParser

from .model import Block, Parsed, build, heading_path

_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_SKIP = set("script style noscript template svg nav footer aside form button select dialog iframe object picture canvas".split())
_HEADING = {f"h{i}" for i in range(1, 7)}
_CONTAINER = set("document html body main article div section header figure fieldset dl ol ul".split())
_LEAF = set("p li dt dd blockquote pre figcaption table address".split())
_BLOCK = _HEADING | _CONTAINER | _LEAF


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list["_Node | str"] = field(default_factory=list)


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        self.add(tag, attrs, tag not in _VOID)

    def handle_startendtag(self, tag, attrs):
        self.add(tag, attrs, False)

    def add(self, tag, attrs, nest):
        if tag in _BLOCK and self.stack[-1].tag == "p":
            self.stack.pop()
        if tag == self.stack[-1].tag and tag in {"li", "dt", "dd", "tr", "td", "th"}:
            self.stack.pop()
        if len(self.stack) > 96:
            raise ValueError("HTML nesting depth exceeds 96")
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if nest:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _excluded(node):
    return node.tag in _SKIP or "hidden" in node.attrs


def _walk(node):
    yield node
    for child in node.children:
        if isinstance(child, _Node) and not _excluded(child):
            yield from _walk(child)


def _text(node):
    if isinstance(node, str):
        return node
    if _excluded(node):
        return ""
    if node.tag in {"br", "hr"}:
        return "\n"
    if node.tag == "img":
        return ""
    result = "".join(_text(child) for child in node.children)
    return f"\n{result}\n" if node.tag in _BLOCK else result


def _clean(text):
    return " ".join(text.split())


def _table(node):
    rows = []
    caption = ""
    for current in _walk(node):
        if current.tag == "caption" and not caption:
            caption = _clean(_text(current))
        if current.tag == "tr":
            cells = [_clean(_text(c)).replace("|", "\\|") for c in current.children
                     if isinstance(c, _Node) and c.tag in {"th", "td"}]
            if cells:
                rows.append(cells)
    if not rows:
        return _clean(_text(node))
    width = len(rows[0])
    lines = ["| " + " | ".join(cells) + " |" for cells in rows]
    if all(len(cells) == width for cells in rows):
        lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n\n".join(filter(None, (caption, "\n".join(lines))))


def parse_html(text: str, warnings: list[str] | None = None) -> Parsed:
    tree = _Tree()
    tree.feed(text)
    tree.close()
    containers = {}
    metadata: dict = {}
    notes = list(warnings or [])
    for node in _walk(tree.root):
        if node.tag in {"body", "main", "article"}:
            containers.setdefault(node.tag, node)
        elif node.tag == "html" and (language := node.attrs.get("lang") or node.attrs.get("xml:lang")):
            metadata.setdefault("language", language[:64])
        elif node.tag == "title" and (value := _clean(_text(node))):
            metadata.setdefault("title", value)
        elif node.tag == "meta":
            name = (node.attrs.get("name") or node.attrs.get("property") or "").lower()
            value = _clean(node.attrs.get("content") or "")
            if not value:
                continue
            if name in {"citation_title", "dc.title"}:
                metadata["title"] = value
            elif name in {"citation_doi", "dc.identifier.doi"}:
                metadata.setdefault("doi", value)
            elif name in {"citation_author", "dc.creator"}:
                metadata.setdefault("authors", []).append(value)
            elif name in {"citation_date", "citation_publication_date", "dc.date"}:
                metadata.setdefault("date", value)
            elif name in {"citation_language", "dc.language", "content-language"}:
                metadata.setdefault("language", value[:64])
            elif name == "isaccessibleforfree" and value.lower() in {"false", "0"}:
                notes.append("html_declares_restricted_access")
    blocks: list[Block] = []
    levels: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()

    def emit(node):
        nonlocal path
        if _excluded(node) or node.tag == "head":
            return
        if node.tag in _HEADING:
            if title := _clean(_text(node)):
                level = int(node.tag[1])
                path = heading_path(levels, level, title)
                blocks.append(Block(title, path, kind="heading", level=level))
                if level == 1:
                    metadata.setdefault("title", title)
            return
        if node.tag in _LEAF:
            value = _table(node) if node.tag == "table" else _clean(_text(node))
            if node.tag == "pre":
                value = _text(node).strip("\n")
            if value:
                kinds = {"li": "list_item", "table": "table", "pre": "code", "figcaption": "caption"}
                blocks.append(Block(value, path, kind=kinds.get(node.tag, "paragraph")))
            return
        inline = []

        def flush_inline():
            if value := _clean("".join(inline)):
                blocks.append(Block(value, path))
            inline.clear()

        for child in node.children:
            if isinstance(child, _Node) and child.tag in (_BLOCK | {"head"}):
                flush_inline()
                emit(child)
            elif not (isinstance(child, _Node) and child.tag in {"meta", "title"}):
                inline.append(_text(child))
        flush_inline()

    emit(containers.get("main") or containers.get("article") or containers.get("body") or tree.root)
    content = [block for block in blocks if block.kind != "heading"]
    if metadata.get("doi") and content and all(
        any(label.casefold() in {"abstract", "summary"} for label in block.heading_path) for block in content
    ):
        notes.append("html_full_text_body_not_found")
        metadata["abstract"] = "\n\n".join(block.text for block in content)
    return build("html/1", blocks, metadata, warnings=notes)
