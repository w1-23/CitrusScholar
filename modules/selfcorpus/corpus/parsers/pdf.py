"""Opt-in PDF adapters. No parser receives a PDF unless explicitly enabled."""

from dataclasses import replace
from http.client import HTTPException
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from .model import Block, Parsed, build, heading_path
from .xml import MAX_XML_BYTES, local, parse_article, read_xml


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def grobid_endpoint(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc
            or parts.query or parts.fragment or any(c.isspace() for c in url)):
        raise ValueError("GROBID requires an explicit HTTP(S) URL without credentials, query, or fragment")
    _ = parts.port
    clean = url.rstrip("/")
    if clean.endswith("/api/processFulltextDocument"):
        return clean
    if clean.endswith("/api"):
        return clean + "/processFulltextDocument"
    return clean + "/api/processFulltextDocument"


def _grobid(data: bytes, endpoint: str, timeout: float) -> Parsed:
    boundary = uuid4().hex
    separator = f"--{boundary}\r\n".encode()
    body = (separator + b'Content-Disposition: form-data; name="input"; filename="document.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n" + data + b"\r\n"
            + b"".join(separator + b'Content-Disposition: form-data; name="teiCoordinates"\r\n\r\n'
                       + name + b"\r\n" for name in (b"p", b"head", b"figure", b"formula"))
            + f"--{boundary}--\r\n".encode())
    request = Request(endpoint, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/xml",
    })
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
        xml = response.read(MAX_XML_BYTES + 1)
    root = read_xml(xml)
    if local(root.tag) != "TEI":
        raise ValueError("GROBID did not return TEI")
    parsed = parse_article(root, parser="grobid-tei/1")
    return parsed


def _docling(data: bytes, artifacts_path: str) -> Parsed:
    from docling.datamodel.base_models import ConversionStatus, DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    path = Path(artifacts_path)
    if not path.is_dir():
        raise ValueError("Docling requires a local artifacts directory")
    # An explicit model directory and disabled remote services keep documents in this process.
    options = PdfPipelineOptions(artifacts_path=path, enable_remote_services=False, do_ocr=False,
                                 generate_picture_images=False, generate_page_images=False)
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
    })
    result = converter.convert(DocumentStream(name="document.pdf", stream=BytesIO(data)), raises_on_error=False)
    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        raise ValueError("Docling could not convert the document")
    warnings = ([] if result.status == ConversionStatus.SUCCESS else ["docling_partial_conversion"])
    blocks: list[Block] = []
    levels: list[tuple[int, str]] = []
    section: tuple[str, ...] = ()
    seen_pages = set()
    for item, _level in result.document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "")
        if label in {'picture', 'image', 'figure'}:
            continue
        text = (getattr(item, "text", "") or "").strip()
        if label == "table" and hasattr(item, "export_to_markdown"):
            text = item.export_to_markdown(doc=result.document).strip()
        if not text:
            continue
        pages = [entry.page_no for entry in getattr(item, "prov", ()) if entry.page_no > 0]
        seen_pages.update(pages)
        start, end = (min(pages), max(pages)) if pages else (None, None)
        if label in {"title", "section_header"}:
            level = 1 if label == "title" else max(1, int(getattr(item, "level", 1))) + 1
            section = heading_path(levels, level, text)
            blocks.append(Block(text, section, start, end, "heading", level))
        elif label not in {"page_header", "page_footer"}:
            blocks.append(Block(text, section, start, end, label or "paragraph"))
    expected = set(result.document.pages)
    if expected - seen_pages:
        warnings.append("docling_pages_without_extractable_text")
    try:
        adapter_version = version("docling")
    except PackageNotFoundError:
        adapter_version = "unknown"
    return build(f"docling-pdf/2:{adapter_version}", blocks, warnings=warnings)


def parse_pdf(data: bytes, endpoint: str | None, artifacts_path: str | None, timeout: float) -> Parsed:
    warnings = []
    partial = None
    if endpoint:
        try:
            parsed = _grobid(data, endpoint, timeout)
            if parsed.quality["status"] == "complete":
                return parsed
            partial = parsed
            warnings.append("grobid_partial_conversion")
        except (OSError, ValueError, HTTPException):
            warnings.append("grobid_unavailable_or_invalid_response")
    if artifacts_path:
        try:
            parsed = _docling(data, artifacts_path)
            if parsed.quality["status"] == "complete":
                return parsed
            if partial is None or len(parsed.markdown) > len(partial.markdown):
                partial = parsed
            warnings.append("docling_partial_conversion")
        except Exception:
            # Optional libraries can fail from missing package, model, or runtime dependencies.
            warnings.append("docling_unavailable_or_conversion_failed")
    if partial:
        combined = list(dict.fromkeys([*warnings, *partial.quality["warnings"]]))
        return replace(partial, quality={**partial.quality, "warnings": combined})
    return build("pdf-unparsed/1", [], warnings=warnings or ["pdf_parser_not_configured"])
