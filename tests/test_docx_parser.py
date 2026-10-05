"""Original synthetic OOXML fixtures (CC0-1.0); no downloaded document bytes."""
import io
import struct
import zipfile

import pytest

from backend import parsers
from backend.parsers import DOCX_MEDIA_TYPE, ParseError, parse_document, parse_document_isolated
from backend.processing import STAGE_ORDER, process_document, validate_source_map
from backend.storage import LocalContentStore
from tests.helpers import digest
from tests.test_api_workflow import checked, project
from tests.test_parsers import assert_exact_blocks

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def docx_bytes(body=None, *, extra=None, compression=zipfile.ZIP_DEFLATED):
    """Create a real minimal macro-free DOCX archive, including package relationships."""
    if body is None:
        body = ("<w:p><w:r><w:t>Cafe\u0301 evidence 🧭.</w:t></w:r></w:p>"
                "<w:p/>"
                "<w:tbl><w:tr><w:tc>"
                "<w:p><w:r><w:t>Policy evidence</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Second cell paragraph.</w:t></w:r></w:p>"
                "</w:tc><w:tc><w:p><w:r><w:t>政策：20%。</w:t></w:r></w:p></w:tc></w:tr>"
                "<w:tr><w:tc><w:p><w:r><w:t>Limitations remain.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
                "<w:p><w:r><w:t>Closing paragraph.</w:t></w:r></w:p><w:sectPr/>")
    members = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{REL_NS}"><Relationship Id="rId1" '
            f'Type="{OFFICE_REL}officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>',
    }
    members.update(extra or {})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def benign_docx(tmp_path):
    path = tmp_path / "synthetic-evidence.docx"
    path.write_bytes(docx_bytes())
    return path


def test_real_docx_fixture_preserves_unicode_and_paragraph_cell_locators(benign_docx):
    parsed = parse_document(benign_docx.read_bytes(), DOCX_MEDIA_TYPE)
    assert parsed.text == ("Café evidence 🧭.\n\nPolicy evidence\n\nSecond cell paragraph.\n\n"
                           "政策：20%。\n\nLimitations remain.\n\nClosing paragraph.")
    assert parsed.parser_version == "builtin-docx-v1/normalizer-v1"
    assert [block["locator_json"]["paragraph_index"] for block in parsed.blocks] == [1, 3, 4, 5, 6, 7]
    locations = [block["locator_json"] for block in parsed.blocks]
    assert locations[0]["kind"] == "docx_paragraph"
    assert locations[1] == {
        "offset_unit": "unicode_codepoint", "kind": "docx_table_cell_paragraph", "part": "word/document.xml",
        "paragraph_index": 3, "table_index": 1, "row_index": 1, "cell_index": 1, "cell_paragraph_index": 1,
    }
    assert locations[2]["cell_paragraph_index"] == 2
    assert locations[3]["cell_index"] == 2
    assert locations[4]["row_index"] == 2
    assert all("page" not in block and "page" not in block["locator_json"]
               and "bbox" not in block["locator_json"] for block in parsed.blocks)
    assert parsed.metadata["bbox_available"] is False
    assert parsed.metadata["page_numbers"] == "unavailable"
    assert parsed.metadata["table_count"] == 1
    assert parsed.metadata["degraded"] is True
    assert_exact_blocks(parsed)


def test_docx_runs_breaks_hidden_text_and_saved_field_display():
    body = ('<w:p><w:r><w:t>Visible</w:t><w:tab/><w:t>evidence</w:t><w:br/><w:t>Next line</w:t></w:r>'
            '<w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDEN</w:t></w:r>'
            '<w:r><w:rPr><w:vanish w:val="false"/></w:rPr><w:t> shown</w:t></w:r>'
            '<w:r><w:instrText>INCLUDETEXT https://invalid.example/secret</w:instrText></w:r>'
            '<w:fldSimple w:instr="DATE"><w:r><w:t> saved display</w:t></w:r></w:fldSimple></w:p>')
    parsed = parse_document(docx_bytes(body), DOCX_MEDIA_TYPE)
    assert parsed.text == "Visible evidence\nNext line shown saved display"
    assert any("field instructions were not evaluated" in warning for warning in parsed.warnings)
    assert_exact_blocks(parsed)


def test_docx_omitted_parts_and_drawings_are_disclosed():
    body = ('<w:p><w:r><w:t>Main body</w:t><w:drawing><w:txbxContent>'
            '<w:p><w:r><w:t>Text box omitted</w:t></w:r></w:p>'
            '</w:txbxContent></w:drawing></w:r></w:p>')
    data = docx_bytes(body, extra={"word/header1.xml": f'<w:hdr xmlns:w="{WORD_NS}"/>'})
    parsed = parse_document(data, DOCX_MEDIA_TYPE)
    assert parsed.text == "Main body"
    assert parsed.metadata["omitted_parts"] == ["word/header1.xml"]
    assert any("text boxes" in warning for warning in parsed.warnings)
    assert any("word/header1.xml" in warning for warning in parsed.warnings)


def test_docx_nested_tables_and_content_controls_retain_locations():
    body = ('<w:sdt><w:sdtContent><w:tbl><w:tr><w:tc>'
            '<w:p><w:r><w:t>Outer</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Nested</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:r><w:t>Outer after</w:t></w:r></w:p>'
            '</w:tc></w:tr></w:tbl></w:sdtContent></w:sdt>')
    parsed = parse_document(docx_bytes(body), DOCX_MEDIA_TYPE)
    assert parsed.text == "Outer\n\nNested\n\nOuter after"
    locations = [block["locator_json"] for block in parsed.blocks]
    assert [location["table_index"] for location in locations] == [1, 2, 1]
    assert locations[1]["parent_cells"][0]["table_index"] == 1
    assert locations[2]["cell_paragraph_index"] == 2
    assert_exact_blocks(parsed)


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04truncated", docx_bytes()[:-25]])
def test_docx_rejects_malformed_zip(data):
    with pytest.raises(ParseError, match="valid ZIP"):
        parse_document(data, DOCX_MEDIA_TYPE)


def test_docx_rejects_generic_zip_and_wrong_xml():
    with pytest.raises(ParseError):
        parse_document(docx_bytes(extra={"word/document.xml": "<broken>"}), DOCX_MEDIA_TYPE)
    with pytest.raises(ParseError, match="content types"):
        parse_document(docx_bytes(extra={"[Content_Types].xml": "<anything/>"}), DOCX_MEDIA_TYPE)


@pytest.mark.parametrize("name", ["../escape.xml", "/absolute.xml", "word/../escape.xml", "word\\escape.xml",
                                 "C:/escape.xml", "word//empty.xml", "%2e%2e/escape.xml", "word/%2e%2e/escape.xml"])
def test_docx_rejects_archive_path_traversal(name):
    with pytest.raises(ParseError, match="path traversal"):
        parse_document(docx_bytes(extra={name: "not extracted"}), DOCX_MEDIA_TYPE)


def test_docx_rejects_duplicate_members():
    stream = io.BytesIO(docx_bytes())
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(stream, "a") as archive:
        archive.writestr("word/document.xml", "<different/>")
    with pytest.raises(ParseError, match="duplicate"):
        parse_document(stream.getvalue(), DOCX_MEDIA_TYPE)


def test_docx_rejects_encrypted_member_flags():
    data = bytearray(docx_bytes())
    # Toggle encryption in actual central/local ZIP headers, without needing a password library.
    central = data.index(b"PK\x01\x02")
    local = data.index(b"PK\x03\x04")
    for position in (central + 8, local + 6):
        flags = struct.unpack_from("<H", data, position)[0]
        struct.pack_into("<H", data, position, flags | 1)
    with pytest.raises(ParseError, match="Encrypted"):
        parse_document(bytes(data), DOCX_MEDIA_TYPE)


def test_docx_rejects_real_high_ratio_zip_bomb():
    data = docx_bytes(extra={"word/highly-compressed.xml": b"0" * 2_000_000})
    assert len(data) < 10_000
    with pytest.raises(ParseError, match="compression ratio"):
        parse_document(data, DOCX_MEDIA_TYPE)


@pytest.mark.parametrize("limit,value,match", [
    ("MAX_DOCX_ENTRIES", 2, "entry count"),
    ("MAX_DOCX_PART_BYTES", 100, "expanded size"),
    ("MAX_DOCX_EXPANDED_BYTES", 100, "expanded size"),
])
def test_docx_enforces_independent_zip_limits(monkeypatch, limit, value, match):
    monkeypatch.setattr(parsers, limit, value)
    with pytest.raises(ParseError, match=match):
        parse_document(docx_bytes(compression=zipfile.ZIP_STORED), DOCX_MEDIA_TYPE)


@pytest.mark.parametrize("target,mode", [
    ("https://invalid.example/never-fetch", ' TargetMode="External"'),
    ("file:///etc/passwd", ' TargetMode="External"'),
    ("https://invalid.example/implicit-external", ""),
    ("//invalid.example/implicit-external", ""),
    ("http://[invalid-ipv6", ""),
    ("../../outside.xml", ""),
    ("/../../outside.xml", ""),
])
def test_docx_rejects_external_relationships_without_network(monkeypatch, target, mode):
    import socket

    def network_forbidden(*args, **kwargs):
        pytest.fail("DOCX parser attempted network access")

    monkeypatch.setattr(socket, "socket", network_forbidden)
    relationships = (f'<Relationships xmlns="{REL_NS}"><Relationship Id="rId9" '
                     f'Type="{OFFICE_REL}hyperlink" Target="{target}"{mode}/></Relationships>')
    # Check every .rels part, even parts unrelated to the main document.
    data = docx_bytes(extra={"word/_rels/header1.xml.rels": relationships})
    with pytest.raises(ParseError, match="external|path traversal"):
        parse_document(data, DOCX_MEDIA_TYPE)


def test_docx_allows_safe_internal_parent_and_package_root_relationships():
    relationships = (f'<Relationships xmlns="{REL_NS}"><Relationship Id="rId2" '
                     f'Type="{OFFICE_REL}customXml" Target="../customXml/item1.xml"/>'
                     f'<Relationship Id="rId3" Type="{OFFICE_REL}styles" Target="/word/styles.xml"/>'
                     '</Relationships>')
    parsed = parse_document(docx_bytes(extra={"word/_rels/document.xml.rels": relationships}), DOCX_MEDIA_TYPE)
    assert parsed.text.startswith("Café evidence")


@pytest.mark.parametrize("revision", ["ins", "del", "moveFrom", "moveTo", "pPrChange", "rPrChange", "cellDel"])
def test_docx_rejects_tracked_changes(revision):
    body = f'<w:p><w:{revision}><w:r><w:t>Ambiguous revision</w:t></w:r></w:{revision}></w:p>'
    with pytest.raises(ParseError, match="tracked changes"):
        parse_document(docx_bytes(body), DOCX_MEDIA_TYPE)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_docx_rejects_dtd_and_entities_for_all_supported_xml_encodings(encoding):
    xml = (f'<?xml version="1.0" encoding="{encoding}"?>'
           '<!DOCTYPE document [<!ENTITY x "expanded text">]>'
           f'<w:document xmlns:w="{WORD_NS}"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p>'
           '</w:body></w:document>')
    with pytest.raises(ParseError, match="DTD"):
        parse_document(docx_bytes(extra={"word/document.xml": xml.encode(encoding)}), DOCX_MEDIA_TYPE)


def test_docx_rejects_unsupported_xml_depth_and_altchunk():
    body = "<w:sdt>" * 130 + "</w:sdt>" * 130
    with pytest.raises(ParseError, match="structural limit"):
        parse_document(docx_bytes(body), DOCX_MEDIA_TYPE)
    with pytest.raises(ParseError, match="alternative-format"):
        parse_document(docx_bytes("<w:altChunk/>"), DOCX_MEDIA_TYPE)


def test_docx_typed_mime_routing_does_not_guess_legacy_word_or_generic_zip():
    data = docx_bytes()
    parsed = parse_document(data, DOCX_MEDIA_TYPE.upper() + "; charset=binary")
    assert parsed.metadata["extraction"] == "offline_saved_docx"
    for media_type in ("application/zip", "application/msword", "application/octet-stream"):
        with pytest.raises(ParseError, match="Unsupported media type"):
            parse_document(data, media_type)
    with pytest.raises(ParseError, match="Unregistered DOCX parser"):
        parse_document(data, DOCX_MEDIA_TYPE, {"docx_parser": "unsafe"})


def test_docx_uses_existing_resource_bounded_subprocess(benign_docx):
    parsed = parse_document_isolated(benign_docx.read_bytes(), DOCX_MEDIA_TYPE,
                                     {"parser_timeout_seconds": 3, "parser_memory_mb": 256})
    assert parsed.metadata["parser_isolation"] == {
        "subprocess": True, "cpu_seconds": 3, "memory_mb": 256, "wall_timeout_seconds": 6,
    }
    assert_exact_blocks(parsed)


def test_canonical_character_limit_includes_block_separators(monkeypatch):
    monkeypatch.setattr(parsers, "MAX_TEXT_CHARS", 4)
    with pytest.raises(ParseError, match="character limit"):
        parse_document(b"ab\n\nc", "text/plain")


def test_docx_pipeline_preserves_cell_locators_after_redaction_and_block_splitting(tmp_path):
    body = ('<w:p><w:r><w:t>Cafe\u0301 evidence.</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Contact analyst@example.org for details. '
            + 'Synthetic policy evidence has measurable limitations. ' * 10
            + '</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    raw = docx_bytes(body)
    store = LocalContentStore(tmp_path)
    pipeline = {"version": 1, "stages": [
        {"name": name, "options": ({"redact_emails": True} if name == "normalize_privacy" else
                                  {"max_block_chars": 100} if name == "source_map" else {})}
        for name in STAGE_ORDER
    ]}
    result = process_document(raw, DOCX_MEDIA_TYPE, store=store, pipeline_config=pipeline)
    assert result.ready_for_publication is True
    assert result.raw_hash == digest(raw)
    assert store.get(result.raw_hash) == raw
    assert result.extracted_text.startswith("Café evidence.")
    assert "[EMAIL_REDACTED]" in result.extracted_text
    assert "analyst@example.org" not in result.extracted_text
    assert validate_source_map(result.extracted_text, result.blocks)
    assert result.metadata["parser_isolation"]["subprocess"] is True
    assert len(result.blocks) > 3
    for block in result.blocks[1:]:
        assert block["locator_json"] == {
            "offset_unit": "unicode_codepoint", "kind": "docx_table_cell_paragraph",
            "part": "word/document.xml", "paragraph_index": 2, "table_index": 1, "row_index": 1,
            "cell_index": 1, "cell_paragraph_index": 1,
        }


@pytest.mark.parametrize("media_type", [DOCX_MEDIA_TYPE, "application/octet-stream", "application/zip", "application/msword"])
def test_docx_upload_api_preserves_saved_bytes_and_cell_source_maps(client, benign_docx, media_type):
    raw = benign_docx.read_bytes()
    proj = project(client, "Synthetic DOCX evidence")
    submitted = checked(client.post("/v1/ingestions/upload", data={"project_id": proj["id"]},
                        files={"file": ("synthetic-evidence.docx", raw, media_type)}), 202)
    operation = checked(client.get(f'/v1/operations/{submitted["id"]}'))
    assert operation["status"] == "succeeded", operation
    result = operation["result_json"]
    capture = checked(client.get(f'/v1/captures/{result["capture_id"]}'))
    assert capture["content_hash"] == digest(raw)
    assert capture["media_type"] == DOCX_MEDIA_TYPE
    representation = checked(client.get(f'/v1/representations/{result["representation_id"]}'))
    assert representation["text"].startswith("Café evidence 🧭.")
    assert validate_source_map(representation["text"], representation["blocks"])
    assert representation["metadata_json"]["parser_isolation"]["subprocess"] is True
    cells = [block for block in representation["blocks"]
             if block["locator_json"]["kind"] == "docx_table_cell_paragraph"]
    assert len(cells) == 4
    assert cells[0]["locator_json"]["cell_paragraph_index"] == 1
    assert cells[1]["locator_json"]["cell_paragraph_index"] == 2
    assert cells[2]["locator_json"]["cell_index"] == 2
    assert cells[3]["locator_json"]["row_index"] == 2
    for block in representation["blocks"]:
        assert block["locator_json"]["part"] == "word/document.xml"
        assert block.get("page") is None
