"""Bounded text extraction from an already opened PyMuPDF document."""


def pdf_text(document, *, max_pages, max_chars):
    page_count = len(document)
    chunks = []
    remaining = max_chars
    truncated = page_count > max_pages
    for page_no in range(min(page_count, max_pages)):
        text = document.load_page(page_no).get_text("text") or ""
        if len(text) > remaining:
            chunks.append(text[:remaining])
            truncated = True
            break
        chunks.append(text)
        remaining -= len(text)
    return "\n".join(chunks), truncated
