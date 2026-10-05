"""Real offline text/HTML/PDF extraction, exact Unicode offsets and page provenance."""
import pytest

from backend.parsers import ParseError, parse_document
from tests.helpers import digest, text_pdf


def assert_exact_blocks(parsed):
    assert parsed.blocks
    previous_end = -1
    for block in parsed.blocks:
        start, end = block['start_offset'], block['end_offset']
        assert 0 <= start < end <= len(parsed.text)
        assert start > previous_end
        assert parsed.text[start:end] == block['text']
        assert block['content_hash'] == digest(block['text'])
        assert block['locator_json']['offset_unit'] == 'unicode_codepoint'
        previous_end = end


def test_text_normalization_has_honest_unicode_offsets():
    raw = '  Cafe\u0301  evidence.\r\n\r\n政策证据：风险降低 20%。\r\n\r\nFinal paragraph.\x00'
    parsed = parse_document(raw.encode(), 'text/plain')
    assert parsed.text == 'Café evidence.\n\n政策证据：风险降低 20%。\n\nFinal paragraph.'
    assert parsed.metadata['normalized'] == 'NFC'
    assert len(parsed.blocks) == 3
    assert_exact_blocks(parsed)


def test_invalid_utf8_is_disclosed():
    parsed = parse_document(b'Research \xff source', 'text/plain')
    assert '\ufffd' in parsed.text
    assert any('UTF-8' in warning for warning in parsed.warnings)
    assert_exact_blocks(parsed)


def test_saved_html_builtin_hides_scripts_navigation_and_invisible_text():
    html = b'''<!doctype html><html><head><title>Saved research</title></head><body>
    <nav>NavigationSecret</nav><script>ScriptSecret</script><style>StyleSecret</style>
    <p hidden>HiddenSecret</p><div aria-hidden="true">AriaSecret</div>
    <div style="display: none">CSSSecret</div><article><h1>Evidence report</h1>
    <p>Reliable source evidence &amp; exact quotations.</p><p>Second research paragraph.</p>
    </article><footer>FooterSecret</footer></body></html>'''
    parsed = parse_document(html, 'text/html', {'html_parser': 'builtin'})
    assert parsed.title == 'Saved research'
    assert 'Reliable source evidence & exact quotations.' in parsed.text
    assert 'Second research paragraph.' in parsed.text
    assert 'Secret' not in parsed.text
    assert parsed.metadata['extraction'] == 'offline_saved_html'
    assert parsed.metadata['dom_byte_offsets'] is False
    assert_exact_blocks(parsed)


def test_real_trafilatura_extracts_saved_article():
    content = ('Research evidence demonstrates that urban tree cover reduces local heat. '
               'The study compares measurements from several neighborhoods over two years. '
               'Methods and findings are described in this saved source for accurate quotation.')
    html = f'<html><head><title>Urban evidence</title></head><body><article><h1>Urban evidence</h1><p>{content}</p></article></body></html>'
    parsed = parse_document(html.encode(), 'text/html', {'html_parser': 'trafilatura'})
    assert 'urban tree cover reduces local heat' in parsed.text
    assert parsed.parser_version.startswith('trafilatura-')
    assert_exact_blocks(parsed)


def test_pdf_extracts_actual_page_text_and_precise_page_locators(monkeypatch):
    monkeypatch.delenv('DOCLING_ARTIFACTS_PATH', raising=False)
    pages = ('Page one reports a 20 percent reduction.', 'Page two records limitations and uncertainty.')
    parsed = parse_document(text_pdf(*pages), 'application/pdf', {'pdf_parser': 'pypdf'})
    assert parsed.text == '\n\n'.join(pages)
    assert [block['locator_json']['page'] for block in parsed.blocks] == [1, 2]
    assert parsed.metadata['pages'] == 2
    assert parsed.metadata['ocr'] is False
    assert parsed.metadata['page_numbers'] == 'one_based'
    assert_exact_blocks(parsed)


def test_pdf_fallback_is_explicit_when_docling_unavailable(monkeypatch):
    monkeypatch.delenv('DOCLING_ARTIFACTS_PATH', raising=False)
    parsed = parse_document(text_pdf('Saved PDF evidence.'), 'application/pdf', {'pdf_parser': 'docling', 'pdf_fallback': 'pypdf'})
    assert parsed.text == 'Saved PDF evidence.'
    assert parsed.parser_version.startswith('pypdf-')
    assert any('Docling' in warning and 'fallback' in warning for warning in parsed.warnings)


def test_pdf_empty_page_does_not_invent_text():
    parsed = parse_document(text_pdf(''), 'application/pdf', {'pdf_parser': 'pypdf'})
    assert parsed.text == ''
    assert parsed.blocks == []
    assert parsed.metadata['empty_pages'] == [1]
    assert any('OCR is not configured' in warning for warning in parsed.warnings)


@pytest.mark.parametrize('data,media_type,options', [
    (b'Not a PDF', 'application/pdf', {}),
    (b'%PDF-not-valid', 'application/pdf', {}),
    (b'hello', 'application/x-unknown', {}),
    (b'<html>text</html>', 'text/html', {'html_parser': 'os.system'}),
    (text_pdf('Hello'), 'application/pdf', {'pdf_parser': 'python.eval'}),
])
def test_invalid_media_or_unregistered_parser_fails(data, media_type, options):
    with pytest.raises(ParseError):
        parse_document(data, media_type, options)


def test_explicit_docling_request_does_not_silently_downgrade(monkeypatch):
    monkeypatch.delenv('DOCLING_ARTIFACTS_PATH', raising=False)
    with pytest.raises(ParseError, match='Requested Docling extraction failed'):
        parse_document(text_pdf('Saved PDF evidence.'), 'application/pdf', {'pdf_parser': 'docling'})
