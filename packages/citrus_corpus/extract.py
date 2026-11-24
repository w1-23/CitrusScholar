"""Convert one bounded supported original into offset-stable Markdown."""
from pathlib import Path

from . import prepare
from .html_text import html_to_markdown
from .pdf_text import pdf_text


FORMATS = {
    ".md": "markdown", ".markdown": "markdown", ".txt": "text",
    ".pdf": "pdf", ".html": "html", ".htm": "html", ".xml": "xml",
}
PARSER_VERSIONS = {"text": "utf8-v1", "markdown": "utf8-markdown-v1", "pdf": "pymupdf-text-v1",
                   "html": "lxml-markdown-v1", "xml": "lxml-jats-markdown-v1"}
MEDIA_TYPES = {
    "text/markdown": "markdown", "text/plain": "text", "application/pdf": "pdf",
    "text/html": "html", "application/xhtml+xml": "html", "application/xml": "xml", "text/xml": "xml",
}


def source_format(name, media_type=""):
    suffix = Path(name).suffix.lower()
    mime = media_type.split(";", 1)[0].strip().lower()
    if mime and mime != "application/octet-stream":
        result = MEDIA_TYPES.get(mime)
    else:
        result = FORMATS.get(suffix)
    if not result:
        raise prepare.PreparationError("unsupported_source_format")
    return result


def _pdf(source):
    import fitz

    try:
        if not source.startswith(b"%PDF-"):
            raise ValueError("pdf_header_missing")
        with fitz.open(stream=source, filetype="pdf") as document:
            if document.needs_pass:
                raise ValueError("pdf_encrypted")
            text, truncated = pdf_text(document, max_pages=200, max_chars=prepare.MAX_DOCUMENT_CHARS)
    except (RuntimeError, ValueError, OSError) as exc:
        raise prepare.PreparationError("invalid_pdf") from exc
    if truncated:
        raise prepare.PreparationError("document_too_large")
    return text


def _tree(source, fmt):
    from lxml import etree

    try:
        if fmt == "xml":
            parser = etree.XMLParser(load_dtd=False, resolve_entities=False, no_network=True, huge_tree=False)
        else:
            parser = etree.HTMLParser(no_network=True, huge_tree=False)
        root = etree.fromstring(source, parser=parser)
        if root is None:
            raise ValueError("document_tree_missing")
        if fmt == "xml":
            if root.getroottree().docinfo.doctype:
                raise ValueError("xml_doctype_disallowed")
            for node in root.iter():
                if isinstance(node.tag, str):
                    node.tag = etree.QName(node).localname
            # JATS sections and paragraphs use the same Markdown paths as HTML.
            for node in root.iter("article-title"):
                node.tag = "h1"
            for node in root.iter("title"):
                node.tag = "h2"
            for node in root.iter("sec"):
                node.tag = "section"
        for node in root.xpath("//head | //script | //style | //noscript | //svg | //nav | //footer | //form | //iframe"):
            parent = node.getparent()
            if parent is None:
                raise ValueError("empty_document")
            parent.remove(node)
        for node in root.xpath("//*[@href]"):
            if not node.get("href", "").lower().startswith(("https://", "http://")):
                node.attrib.pop("href", None)
        markup = etree.tostring(root, encoding="unicode", method="html")
    except (etree.Error, RuntimeError, ValueError) as exc:
        raise prepare.PreparationError("invalid_" + fmt) from exc
    return html_to_markdown(markup)


def extract_markdown(source, fmt):
    if not isinstance(source, bytes) or not source or len(source) > prepare.MAX_SOURCE_BYTES:
        raise prepare.PreparationError("source_size_invalid")
    if fmt == "pdf":
        text = _pdf(source)
    elif fmt in {"xml", "html"}:
        text = _tree(source, fmt)
    elif fmt in {"text", "markdown"}:
        try:
            text = source.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise prepare.PreparationError("invalid_text") from exc
    else:
        raise prepare.PreparationError("unsupported_source_format")
    return prepare.normalize_markdown(text)
