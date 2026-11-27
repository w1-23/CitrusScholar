import hashlib
import io
import json
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from corpus.chunking import chunk
from corpus.errors import CorpusError
from corpus.parsers import ParserRouter
from corpus.sources import Source


def source(data, media_type, *, uri="upload://document", metadata=None):
    return Source(
        content=data if isinstance(data, bytes) else data.encode(), media_type=media_type, uri=uri,
        metadata=metadata or {}, access_note="fixture", retrieved_at="2026-01-01T00:00:00Z",
        http_status=None, kind="upload",
    )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.router = ParserRouter(grobid_url="", docling=False)

    def test_markdown_heading_and_deterministic_chunks(self):
        parsed = self.router.parse(source("# Paper\n\nIntro.\n\n## Methods\n\nExample method.\n\n```txt\n# not a section\n```\n\n## Results\n\nExample result.", "text/markdown"))
        self.assertEqual(parsed.quality["status"], "complete")
        self.assertEqual(parsed.metadata["title"], "Paper")
        values = chunk(parsed)
        self.assertEqual(values, chunk(parsed))
        self.assertEqual([item["heading_path"] for item in values], [["Paper"], ["Paper", "Methods"], ["Paper", "Methods"], ["Paper", "Results"]])
        self.assertIn("# not a section", values[2]["text"])
        self.assertEqual([item["ordinal"] for item in values], list(range(len(values))))
        self.assertEqual(values[0]["content_hash"], hashlib.sha256(b"Intro.").hexdigest())
        json.dumps(parsed.structured)

    def test_chunking_limits_cjk_and_long_terms(self):
        text = "你好世界甲乙丙丁" + "x" * 31
        parsed = self.router.parse(source(text, "text/plain"))
        values = chunk(parsed, max_tokens=3)
        self.assertGreater(len(values), 3)
        self.assertTrue(all(0 < item["token_count"] <= 3 for item in values))
        self.assertEqual("".join(item["text"] for item in values), text)

    def test_html_excludes_navigation_and_extracts_tables(self):
        html = '''<html><head><title>Website</title><meta name="citation_title" content="Article">
        <meta name="citation_author" content="A. Author"></head><body><nav>outside</nav>
        <main><h1>Heading</h1><p>Real <em>content</em>.</p><p hidden>hidden</p><script>secret</script>
        <table><tr><th>A</th><th>B</th></tr><tr><td>x</td><td>y</td></tr></table></main></body></html>'''
        parsed = self.router.parse(source(html, "text/html"))
        self.assertEqual(parsed.metadata["title"], "Article")
        self.assertEqual(parsed.metadata["authors"], ["A. Author"])
        self.assertIn("Real content.", parsed.markdown)
        self.assertIn("| x | y |", parsed.markdown)
        self.assertNotRegex(parsed.markdown, "outside|secret|hidden")

    def test_abstract_only_html_with_citation_is_partial(self):
        html = '<meta name="citation_doi" content="10.1234/test"><main><h1>Article</h1><h2>Abstract</h2><p>Summary.</p></main>'
        parsed = self.router.parse(source(html, "text/html"))
        self.assertEqual(parsed.quality["status"], "partial")
        self.assertEqual(parsed.metadata["abstract"], "Summary.")

    def test_jats_dtd_reference_not_loaded_and_abstract_preserved(self):
        jats = '''<?xml version="1.0"?><!DOCTYPE article SYSTEM "file:///nonexistent.dtd">
        <article><front><article-meta><title-group><article-title>A title</article-title></title-group>
        <article-id pub-id-type="doi">10.1234/t</article-id><abstract><p>Summary.</p></abstract>
        </article-meta></front><body><sec><title>Results</title><p>Experiment body.</p></sec></body></article>'''
        parsed = self.router.parse(source(jats, "application/jats+xml"))
        self.assertEqual(parsed.quality["status"], "complete")
        self.assertEqual(parsed.metadata["abstract"], "Summary.")
        self.assertEqual(parsed.metadata["doi"], "10.1234/t")
        self.assertEqual(chunk(parsed)[-1]["heading_path"], ["A title", "Results"])
        parsed = self.router.parse(source(jats.split("<body>")[0] + "</article>", "application/jats+xml"))
        self.assertEqual(parsed.quality["status"], "partial")

    def test_unsafe_or_invalid_xml_rejected(self):
        fixtures = [
            b'<!DOCTYPE article [<!ENTITY test SYSTEM "file:///etc/passwd">]><article><body><p>&test;</p></body></article>',
            '<!DOCTYPE article [<!ENTITY test "unsafe">]><article>&test;</article>'.encode("utf-16"),
            b"<article><body></article>",
            b"<article>" + b"<node>" * 100 + b"</node>" * 100 + b"</article>",
        ]
        for invalid in fixtures:
            with self.subTest(length=len(invalid)), self.assertRaises(CorpusError) as error:
                self.router.parse(source(invalid, "application/xml"))
            self.assertEqual(error.exception.code, "invalid_document")

    def test_tei_ranges_and_null_do_not_merge(self):
        tei = '''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><head>Methods</head>
        <p>Unlocated.</p><p coords="2,1,2,3,4;3,1,2,3,4">Range text.</p>
        <pb n="4"/><p>Page four.</p></div></body></text></TEI>'''
        parsed = self.router.parse(source(tei, "application/xml"))
        self.assertEqual(parsed.parser, "tei/1")
        self.assertEqual([(value["page_start"], value["page_end"]) for value in chunk(parsed)], [(None, None), (2, 3), (4, 4)])

    def test_docx_extracts_without_unpacking_entries(self):
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("../../not-extracted", b"unused")
            archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Survey</w:t></w:r></w:p>
              <w:p><w:r><w:t>Survey text.</w:t></w:r></w:p></w:body></w:document>''')
        parsed = self.router.parse(source(buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
        self.assertEqual(parsed.quality["status"], "complete")
        self.assertEqual(chunk(parsed)[0]["heading_path"], ["Survey"])

    def test_metadata_only_never_chunks(self):
        parsed = self.router.parse(source(
            '{"abstract":"Summary only."}', "application/json", metadata={"fulltext": False, "title": "Paper"},
        ))
        self.assertEqual(parsed.quality["status"], "partial")
        self.assertEqual(parsed.structured["abstract"], "Summary only.")
        self.assertEqual(chunk(parsed), [])

    def test_grobid_adapter_uses_tei_page_evidence(self):
        tei = b'<TEI><text><body><p coords="3,1,2,3,4">A GROBID fixture.</p></body></text></TEI>'
        with patch("corpus.parsers.pdf.build_opener") as mock:
            opened = mock.return_value.open
            opened.return_value.__enter__.return_value.read.return_value = tei
            parsed = ParserRouter(grobid_url="https://parser.test/api", docling=False).parse(
                source(b"%PDF-1.7\n", "application/pdf"))
        self.assertEqual(parsed.parser, "grobid-tei/1")
        self.assertEqual(chunk(parsed)[0]["page_start"], 3)
        self.assertEqual(opened.call_args.args[0].get_method(), "POST")
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)
        self.assertIn(b"%PDF-1.7", opened.call_args.args[0].data)

    def test_pdf_requires_explicit_adapter(self):
        with patch("corpus.parsers.pdf._grobid") as adapter:
            parsed = self.router.parse(source(b"%PDF-1.7\n", "application/pdf"))
            adapter.assert_not_called()
        self.assertEqual(chunk(parsed), [])
        self.assertEqual(parsed.quality["status"], "partial")
        with patch("corpus.parsers.pdf._grobid", side_effect=OSError("fixture")) as adapter:
            parsed = ParserRouter(grobid_url="https://parser.test/api", docling=False).parse(
                source(b"%PDF-1.7\n", "application/pdf"))
            adapter.assert_called_once_with(b"%PDF-1.7\n", "https://parser.test/api/processFulltextDocument", 30)
        self.assertIn("grobid_unavailable_or_invalid_response", parsed.quality["warnings"])



class BibliographyChunkTests(unittest.TestCase):
    def test_references_preserved_but_not_embedded(self):
        from corpus.parsers.markdown import parse_markdown
        parsed = parse_markdown('# Study\n\n## Results\n\nMeasured significant root changes.\n\n## References\n\nA uniquely marked external citation.')
        self.assertIn('external citation', parsed.markdown)
        self.assertEqual([item['text'] for item in chunk(parsed)], ['Measured significant root changes.'])
        self.assertGreater(parsed.quality['text_characters'], 0)

if __name__ == "__main__":
    unittest.main()
