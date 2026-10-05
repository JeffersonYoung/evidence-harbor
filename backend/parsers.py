"""Offline parsers: all extraction consumes already-saved bytes, never a live URL.

Offsets use Python/Unicode code points in the saved canonical representation, not
HTML byte positions or PDF glyph coordinates. PDF locators identify exact pages.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import stat
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_TEXT_CHARS = 4_000_000
MAX_PDF_PAGES = 500
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_ENTRIES = 2048
MAX_DOCX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_DOCX_PART_BYTES = 16 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 200
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORD = "{" + _WORD_NS + "}"


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    blocks: list[dict]
    parser_version: str
    title: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def normalize_text(text: str) -> str:
    # Unicode NFC is a deliberate representation transformation, not a claim of raw offsets.
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _assemble(parts: list[tuple[str, dict]], parser: str, *, title: str = "", warnings=None, metadata=None) -> ParsedDocument:
    text_parts, blocks, offset = [], [], 0
    for content, locator in parts:
        content = normalize_text(content)
        if not content:
            continue
        if offset + (2 if text_parts else 0) + len(content) > MAX_TEXT_CHARS:
            raise ParseError("Extracted text exceeds the character limit")
        if text_parts:
            offset += 2
        start = offset
        text_parts.append(content)
        offset += len(content)
        quote_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        block = {"text": content, "start_offset": start, "end_offset": offset,
                 "quote_hash": quote_hash, "content_hash": quote_hash,
                 "locator_json": {"offset_unit": "unicode_codepoint", **locator}}
        if "page" in locator:
            block["page"] = locator["page"]
        blocks.append(block)
    return ParsedDocument("\n\n".join(text_parts), blocks, parser, title, list(warnings or []),
                          {"offset_unit": "unicode_codepoint", "normalized": "NFC", **(metadata or {})})


def _paragraphs(text: str, locator: dict | None = None) -> list[tuple[str, dict]]:
    return [(part, dict(locator or {})) for part in re.split(r"\n\s*\n", normalize_text(text)) if part.strip()]


class _SavedHTMLParser(HTMLParser):
    BLOCK_TAGS = frozenset({"p", "div", "article", "section", "main", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "pre"})
    HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "canvas", "nav", "footer", "header", "form"})
    VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden_stack = []
        self.parts = []
        self.title_parts = []
        self.in_title = False
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        hidden = tag in self.HIDDEN_TAGS or "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
        style = re.sub(r"\s+", "", (attrs.get("style") or "").lower())
        hidden = hidden or "display:none" in style or "visibility:hidden" in style
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, hidden))
        if tag in self.BLOCK_TAGS or tag == "br":
            self.parts.append("\n\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if any(hidden or tag in {"head", "title"} for tag, hidden in self.stack):
            return
        self.parts.append(data)


def _decode(data: bytes) -> tuple[str, list[str]]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), []
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        # Do not quietly claim a lossy decode is exact raw text.
        return data.decode("utf-8", errors="replace"), ["Input was not valid UTF-8; replacement characters mark undecodable bytes"]


def parse_html(data: bytes, *, parser: str = "auto", include_selector: str = "", exclude_selector: str = "") -> ParsedDocument:
    html, warnings = _decode(data)
    baseline = _SavedHTMLParser()
    try:
        baseline.feed(html)
    except Exception as exc:
        raise ParseError("Saved HTML could not be parsed") from exc
    title = normalize_text("".join(baseline.title_parts))
    selection_metadata = {"source_html_characters": len(html), "include_selector": include_selector, "exclude_selector": exclude_selector}
    if include_selector or exclude_selector:
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            raise ParseError("Install beautifulsoup4 and soupsieve for CSS include/exclude selectors") from exc
        try:
            soup = BeautifulSoup(html, "html.parser")
            if exclude_selector:
                for node in soup.select(exclude_selector):
                    node.decompose()
            selected = soup.select(include_selector) if include_selector else [soup]
            if not selected:
                raise ParseError("HTML include selector matched no elements")
            html = "\n".join(str(node) for node in selected)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError("Invalid or unsupported HTML selector") from exc
        baseline = _SavedHTMLParser()
        baseline.feed(html)
    selection_metadata["selected_html_characters"] = len(html)
    selection_metadata["selector_character_delta"] = selection_metadata["source_html_characters"] - len(html)
    if parser not in {"auto", "trafilatura", "builtin"}:
        raise ParseError("Unregistered HTML parser")
    if parser in {"auto", "trafilatura"}:
        try:
            import trafilatura
            # extract() is offline; fetch_url()/bare_extraction URL downloading are never used.
            extracted = trafilatura.extract(html, output_format="txt", include_tables=True,
                                             include_links=False, include_images=False,
                                             include_comments=False, favor_precision=True)
            if extracted and extracted.strip():
                return _assemble(_paragraphs(extracted, {"kind": "html_paragraph"}),
                                 f"trafilatura-{package_version('trafilatura')}/normalizer-v1", title=title, warnings=warnings,
                                 metadata={"extraction": "offline_saved_html", **selection_metadata})
            warnings.append("Trafilatura found no main text; used conservative saved-HTML fallback")
        except ImportError:
            warnings.append("Trafilatura is not installed; used built-in saved-HTML parser")
        except Exception as exc:  # noqa: BLE001 - optional parser failures trigger an explicit degraded fallback
            warnings.append(f"Trafilatura extraction failed ({type(exc).__name__}); used built-in saved-HTML parser")
    return _assemble(_paragraphs("".join(baseline.parts), {"kind": "html_paragraph"}),
                     "builtin-html-v1/normalizer-v1", title=title, warnings=warnings,
                     metadata={"extraction": "offline_saved_html", "fallback": True, "dom_byte_offsets": False, **selection_metadata})


class _DocxXMLBuilder(ET.TreeBuilder):
    """Reject DTD/entity declarations regardless of XML encoding, before expansion."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.elements = 0

    def doctype(self, name, pubid, system):
        raise ParseError("DOCX XML DTDs and entity declarations are unsupported")

    def start(self, tag, attrs):
        self.depth += 1
        self.elements += 1
        if self.depth > 128 or self.elements > 500_000:
            raise ParseError("DOCX XML structural limit exceeded")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def _docx_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        with archive.open(name) as part:
            data = part.read(MAX_DOCX_PART_BYTES + 1)
        if len(data) > MAX_DOCX_PART_BYTES:
            raise ParseError("DOCX expanded part size limit exceeded")
        return ET.fromstring(data, parser=ET.XMLParser(target=_DocxXMLBuilder()))
    except ParseError:
        raise
    except (KeyError, ET.ParseError, zipfile.BadZipFile, RuntimeError, EOFError, OSError, zlib.error) as exc:
        raise ParseError("DOCX contains a missing, malformed or unreadable XML part") from exc


def _docx_relationship_target(name: str, target: str) -> str:
    # Relationships are inspected only. This parser never follows a URL or extracts a file.
    target = unquote(target)
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise ParseError("DOCX external or invalid relationship targets are unsupported") from exc
    if not target or parsed.scheme or parsed.netloc or target.startswith("//") or "\\" in target:
        raise ParseError("DOCX external or invalid relationship targets are unsupported")
    if any(ord(char) < 32 for char in target):
        raise ParseError("DOCX relationship target contains control characters")
    source_directory = "" if name == "_rels/.rels" else posixpath.dirname(posixpath.dirname(name))
    if parsed.path.startswith("/"):
        source_directory = ""
    resolved = posixpath.normpath(posixpath.join(source_directory, parsed.path.lstrip("/")))
    if resolved == ".." or resolved.startswith("../"):
        raise ParseError("DOCX relationship path traversal is unsupported")
    return resolved.lstrip("/")


def parse_docx(data: bytes, *, parser: str = "auto") -> ParsedDocument:
    """Conservative, offline WordprocessingML body text with physical-cell provenance.

    Only standard, macro-free Transitional OOXML DOCX is supported. No rendering,
    field evaluation, embedded-object execution, network access or ZIP extraction.
    """
    if parser not in {"auto", "builtin"}:
        raise ParseError("Unregistered DOCX parser")
    if len(data) > 100 * 1024 * 1024:
        raise ParseError("Raw parser input exceeds 100 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise ParseError("Saved DOCX is not a valid ZIP package") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_DOCX_ENTRIES:
            raise ParseError("DOCX ZIP entry count limit exceeded")
        names, expanded_bytes = set(), 0
        for entry in entries:
            name = entry.filename
            normalized = name.rstrip("/")
            decoded_name = unquote(normalized)
            if (not normalized or name != entry.orig_filename or "\\" in name or ":" in name
                    or name.startswith("/") or any(part in {".", "..", ""} for part in normalized.split("/"))
                    or decoded_name.startswith("/") or "\\" in decoded_name or ":" in decoded_name
                    or any(part in {".", "..", ""} for part in decoded_name.split("/"))
                    or any(ord(char) < 32 for char in decoded_name)):
                raise ParseError("DOCX ZIP path traversal or invalid member name")
            if name in names:
                raise ParseError("DOCX ZIP contains duplicate member names")
            names.add(name)
            if entry.flag_bits & 1:
                raise ParseError("Encrypted DOCX ZIP members are unsupported")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ParseError("DOCX ZIP symbolic links are unsupported")
            if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ParseError("DOCX ZIP compression method is unsupported")
            expanded_bytes += entry.file_size
            if entry.file_size > MAX_DOCX_PART_BYTES or expanded_bytes > MAX_DOCX_EXPANDED_BYTES:
                raise ParseError("DOCX ZIP expanded size limit exceeded")
            if entry.file_size > max(1, entry.compress_size) * MAX_DOCX_COMPRESSION_RATIO:
                raise ParseError("DOCX ZIP compression ratio limit exceeded")

        content_types = _docx_xml(archive, "[Content_Types].xml")
        if content_types.tag != "{" + _CONTENT_NS + "}Types":
            raise ParseError("DOCX content types are invalid")
        main_types = [item.get("ContentType") for item in content_types
                      if item.get("PartName") == "/word/document.xml"]
        if main_types != ["application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"]:
            raise ParseError("DOCX requires a standard macro-free word/document.xml part")
        if any("macroenabled" in item.get("ContentType", "").lower()
               or "vbaproject" in item.get("ContentType", "").lower() for item in content_types):
            raise ParseError("Macro-enabled DOCX packages are unsupported")
        if any(name.lower().endswith("vbaproject.bin") for name in names):
            raise ParseError("Macro-enabled DOCX packages are unsupported")

        office_parts = []
        for name in sorted(names):
            if not name.lower().endswith(".rels"):
                continue
            relationships = _docx_xml(archive, name)
            if relationships.tag != "{" + _REL_NS + "}Relationships":
                raise ParseError("DOCX relationships are invalid")
            for relationship in relationships:
                mode = relationship.get("TargetMode", "Internal")
                if mode.lower() != "internal":
                    raise ParseError("DOCX external relationships are unsupported; no targets were fetched")
                target = _docx_relationship_target(name, relationship.get("Target", ""))
                if name == "_rels/.rels" and relationship.get("Type", "").endswith("/officeDocument"):
                    office_parts.append(target)
        if office_parts != ["word/document.xml"]:
            raise ParseError("DOCX requires one internal main-document relationship")

        document = _docx_xml(archive, "word/document.xml")
        if document.tag != _WORD + "document":
            raise ParseError("DOCX requires standard Transitional WordprocessingML")
        body = document.find(_WORD + "body")
        if body is None:
            raise ParseError("DOCX main document has no body")
        tracked = {"ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveFromRangeEnd",
                   "moveToRangeStart", "moveToRangeEnd", "cellIns", "cellDel", "cellMerge",
                   "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
                   "customXmlDelRangeEnd", "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
                   "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd", "numberingChange"}
        tags = {element.tag for element in document.iter()}
        if any(tag.startswith(_WORD) and (tag[len(_WORD):] in tracked or tag.endswith("Change")) for tag in tags):
            raise ParseError("DOCX tracked changes are unsupported; accept or reject revisions before import")
        if _WORD + "altChunk" in tags:
            raise ParseError("DOCX alternative-format content is unsupported")

        warnings = [("DOCX extracts main-body paragraph text and physical table cells only; pagination, "
                     "bounding boxes, numbering labels, style-based visibility and rendered layout are unavailable")]
        omitted_parts = sorted(name for name in names if re.fullmatch(
            r"word/(?:header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name))
        if omitted_parts:
            warnings.append("DOCX auxiliary text parts were not extracted: " + ", ".join(omitted_parts))
        excluded = {_WORD + name for name in ("drawing", "pict", "object", "txbxContent")}
        math_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
        excluded.update({math_ns + "oMath", math_ns + "oMathPara"})
        if tags & excluded:
            warnings.append("DOCX drawings, embedded objects, text boxes or equations were omitted")
        alternate_content = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"
        if alternate_content in tags:
            excluded.add(alternate_content)
            warnings.append("DOCX alternate compatibility representations were omitted")
        if _WORD + "sym" in tags:
            warnings.append("DOCX font-specific symbols were omitted; their Unicode representation is unavailable")
        if tags & {_WORD + "instrText", _WORD + "fldSimple", _WORD + "fldChar"}:
            warnings.append("DOCX fields use saved display text only; field instructions were not evaluated")
        paragraph_indices = {id(element): index for index, element in enumerate(document.iter(_WORD + "p"), 1)}
        parts, table_count = [], 0

        def paragraph_text(element):
            if element.tag in excluded:
                return ""
            if element.tag == _WORD + "r":
                properties = element.find(_WORD + "rPr")
                if properties is not None and any(
                    item.tag in {_WORD + "vanish", _WORD + "webHidden"}
                    and item.get(_WORD + "val", "true").lower() not in {"0", "false", "off"}
                    for item in properties
                ):
                    return ""
            if element.tag == _WORD + "t":
                return element.text or ""
            if element.tag == _WORD + "tab":
                return "\t"
            if element.tag in {_WORD + "br", _WORD + "cr"}:
                return "\n"
            if element.tag == _WORD + "noBreakHyphen":
                return "\u2011"
            if element.tag == _WORD + "softHyphen":
                return "\u00ad"
            return "".join(paragraph_text(child) for child in element)

        def visit(element, tables):
            nonlocal table_count
            if element.tag in excluded:
                return
            if element.tag == _WORD + "tbl":
                table_count += 1
                tables = [*tables, {"table_index": table_count, "row_index": 0, "cell_index": 0,
                                   "cell_paragraph_index": 0}]
            elif element.tag == _WORD + "tr" and tables:
                tables[-1].update(row_index=tables[-1]["row_index"] + 1, cell_index=0)
            elif element.tag == _WORD + "tc" and tables:
                tables[-1].update(cell_index=tables[-1]["cell_index"] + 1, cell_paragraph_index=0)
            elif element.tag == _WORD + "p":
                locator = {"kind": "docx_paragraph", "part": "word/document.xml",
                           "paragraph_index": paragraph_indices[id(element)]}
                if tables:
                    tables[-1]["cell_paragraph_index"] += 1
                    locator.update(kind="docx_table_cell_paragraph", **tables[-1])
                    if len(tables) > 1:
                        locator["parent_cells"] = [dict(table) for table in tables[:-1]]
                parts.append((paragraph_text(element), locator))
                return
            for child in element:
                visit(child, tables)

        visit(body, [])
        return _assemble(parts, "builtin-docx-v1/normalizer-v1", warnings=warnings,
                         metadata={"extraction": "offline_saved_docx", "scope": "main_document_body",
                                   "layout_fidelity": "logical_paragraphs_and_physical_cells", "degraded": True,
                                   "bbox_available": False, "page_numbers": "unavailable", "ocr": False,
                                   "table_count": table_count, "table_structure": "physical_cells_only",
                                   "locator_indices": "one_based", "omitted_parts": omitted_parts,
                                   "tracked_changes": "rejected", "external_relationships": "rejected"})


def _parse_docling(data: bytes, *, options: dict | None = None) -> ParsedDocument:
    options = options or {}
    artifacts = os.environ.get("DOCLING_ARTIFACTS_PATH", "")
    if not artifacts or not Path(artifacts).is_dir():
        raise ParseError("Docling requires a preinstalled local DOCLING_ARTIFACTS_PATH; model downloads are disabled")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    do_ocr, do_tables = options.get("docling_ocr", False), options.get("docling_tables", True)
    pipeline = PdfPipelineOptions(artifacts_path=Path(artifacts), do_ocr=do_ocr, do_table_structure=do_tables,
                                  enable_remote_services=False, allow_external_plugins=False,
                                  document_timeout=options.get("parser_timeout_seconds", 30))
    if do_ocr:
        import shutil
        languages = options.get("ocr_language", "eng").split("+")
        tessdata = os.environ.get("TESSDATA_PREFIX", "")
        if not shutil.which("tesseract") or not tessdata or not all(Path(tessdata, language + ".traineddata").is_file() for language in languages):
            raise ParseError("OCR requires local Tesseract and TESSDATA_PREFIX with every configured language; downloads are disabled")
        pipeline.ocr_options = TesseractCliOcrOptions(lang=languages)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)})
    result = converter.convert(DocumentStream(name="saved-source.pdf", stream=io.BytesIO(data)), max_num_pages=MAX_PDF_PAGES)
    status = getattr(result.status, "value", str(result.status)).lower()
    if status != "success":
        raise ParseError("Docling conversion was incomplete; partial results are not publishable")
    parts, table_count = [], 0
    for item, level in result.document.iterate_items():
        label = str(getattr(item, "label", "")).lower()
        is_table = "table" in label and hasattr(item, "export_to_markdown")
        content = item.export_to_markdown(doc=result.document) if is_table else getattr(item, "text", "")
        if not content:
            continue
        provenance = getattr(item, "prov", [])
        if not provenance:
            raise ParseError("Docling text lacks page provenance")
        pages = sorted({int(p.page_no) for p in provenance})
        if len(pages) != 1:
            raise ParseError("Docling produced a multi-page block; precise single-page mapping unavailable")
        boxes = []
        for item_provenance in provenance:
            box = getattr(item_provenance, "bbox", None)
            if box is not None:
                boxes.append(box.model_dump(mode="json") if hasattr(box, "model_dump") else {key: getattr(box, key) for key in ("l", "t", "r", "b")})
        locator = {"kind": "pdf_table" if is_table else "pdf_text", "page": pages[0], "bboxes": boxes,
                   "docling_item_ref": getattr(item, "self_ref", None), "reading_order_level": level}
        if is_table:
            table_count += 1
            table = item.data
            locator["table_structure"] = {"rows": table.num_rows, "columns": table.num_cols,
                "cells": [{"text": cell.text, "row_start": cell.start_row_offset_idx, "row_end": cell.end_row_offset_idx,
                           "column_start": cell.start_col_offset_idx, "column_end": cell.end_col_offset_idx,
                           "column_header": cell.column_header, "row_header": cell.row_header}
                          for cell in table.table_cells]}
        parts.append((content, locator))
    return _assemble(parts, f"docling-{package_version('docling')}/normalizer-v1",
                     warnings=[] if do_ocr else ["PDF OCR was explicitly disabled"],
                     metadata={"ocr": do_ocr, "table_structure": do_tables, "table_count": table_count,
                               "bbox_available": True, "degraded": False, "page_numbers": "one_based",
                               "layout_fidelity": "docling_layout", "table_representation": "markdown"})


def parse_pdf(data: bytes, *, parser: str = "auto", options: dict | None = None) -> ParsedDocument:
    options = options or {}
    if not data.lstrip().startswith(b"%PDF-"):
        raise ParseError("PDF media type does not match the saved bytes")
    if parser not in {"auto", "docling", "pypdf"}:
        raise ParseError("Unregistered PDF parser")
    warnings = []
    # auto favors reliable offline page extraction, unless local Docling assets were explicitly configured.
    if parser == "docling" or (parser == "auto" and os.environ.get("DOCLING_ARTIFACTS_PATH")):
        try:
            return _parse_docling(data, options=options)
        except Exception as exc:
            if parser == "docling" and options.get("pdf_fallback", "error") != "pypdf":
                raise ParseError(f"Requested Docling extraction failed: {exc}") from exc
            warnings.append(f"Docling unavailable or unsuitable ({type(exc).__name__}); explicitly permitted pypdf page-text fallback used")
    elif parser == "auto":
        warnings.append("Docling local assets are not configured; using degraded pypdf page-text extraction without layout or OCR")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ParseError("Encrypted PDFs are unsupported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ParseError("PDF page limit exceeded")
        parts, empty_pages = [], []
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                empty_pages.append(page_number)
            parts.extend(_paragraphs(text, {"kind": "pdf_page_text", "page": page_number}))
        if empty_pages:
            warnings.append("No extractable text on PDF pages " + ", ".join(map(str, empty_pages[:25])) + "; OCR is not configured")
        return _assemble(parts, f"pypdf-{package_version('pypdf')}/normalizer-v1", warnings=warnings,
                         metadata={"pages": len(reader.pages), "empty_pages": empty_pages, "ocr": False,
                                   "layout_fidelity": "page_text_only", "page_numbers": "one_based", "degraded": True,
                                   "table_structure": False, "bbox_available": False})
    except ParseError:
        raise
    except ImportError as exc:
        raise ParseError("Install pypdf to extract saved PDF files") from exc
    except Exception as exc:
        raise ParseError("Saved PDF could not be parsed") from exc


def parse_document(data: bytes, media_type: str, options: dict | None = None) -> ParsedDocument:
    options = options or {}
    media_type = media_type.lower().split(";", 1)[0].strip()
    if media_type == DOCX_MEDIA_TYPE:
        return parse_docx(data, parser=options.get("docx_parser", "auto"))
    if media_type == "application/pdf" or data.lstrip().startswith(b"%PDF-"):
        return parse_pdf(data, parser=options.get("pdf_parser", "auto"), options=options)
    if media_type in {"text/html", "application/xhtml+xml"}:
        return parse_html(data, parser=options.get("html_parser", "auto"),
                          include_selector=options.get("include_selector", ""), exclude_selector=options.get("exclude_selector", ""))
    if media_type in {"text/plain", "text/markdown", "text/csv", "application/json"}:
        text, warnings = _decode(data)
        return _assemble(_paragraphs(text, {"kind": "text_paragraph"}), "plain-text-v1/normalizer-v1", warnings=warnings)
    raise ParseError(f"Unsupported media type: {media_type}")


def parse_document_isolated(data: bytes, media_type: str, options: dict | None = None) -> ParsedDocument:
    """Bound untrusted parsing in a separate CPU/memory/wall-time-limited process."""
    import json
    import subprocess
    import sys
    options = dict(options or {})
    timeout = options.get("parser_timeout_seconds", 30)
    memory_mb = options.get("parser_memory_mb", 1024)
    if type(timeout) is not int or not 1 <= timeout <= 180 or type(memory_mb) is not int or not 128 <= memory_mb <= 8192:
        raise ParseError("Invalid parser resource limits")
    if len(data) > 100 * 1024 * 1024:
        raise ParseError("Raw parser input exceeds 100 MiB")
    header = json.dumps({"media_type": media_type, "options": options}).encode("utf-8") + b"\n"
    # Do not expose provider credentials to a parser subprocess. No model download fallback.
    allowed_env = {"PATH", "LANG", "LC_ALL", "HOME", "VIRTUAL_ENV", "SYSTEMROOT", "DOCLING_ARTIFACTS_PATH", "TESSDATA_PREFIX"}
    environment = {name: value for name, value in os.environ.items() if name in allowed_env}
    project_root = str(Path(__file__).resolve().parent.parent)
    environment.update({"PYTHONPATH": project_root, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false"})
    try:
        process = subprocess.Popen([sys.executable, "-m", "backend.parsers", "--worker"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                   cwd=project_root, start_new_session=True)
        try:
            output, errors = process.communicate(header + data, timeout=timeout + 3)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        completed = subprocess.CompletedProcess(process.args, process.returncode, output, errors)
    except subprocess.TimeoutExpired as exc:
        raise ParseError("Parser exceeded the wall-time limit") from exc
    if len(completed.stdout) > 40 * 1024 * 1024:
        raise ParseError("Parser output exceeded the size limit")
    if completed.returncode != 0:
        try:
            error = json.loads(completed.stdout).get("error", "Parser process failed")
        except (ValueError, AttributeError):
            error = "Parser process failed or exceeded CPU/memory limits"
        raise ParseError(str(error)[:500])
    try:
        output = json.loads(completed.stdout)
        return ParsedDocument(**output)
    except (ValueError, TypeError) as exc:
        raise ParseError("Parser process returned an invalid result") from exc


def _worker_main():
    import contextlib
    import json
    import resource
    import sys
    from dataclasses import asdict
    try:
        header = sys.stdin.buffer.readline(16_385)
        if len(header) > 16_384 or not header.endswith(b"\n"):
            raise ParseError("Invalid parser request header")
        request = json.loads(header)
        options = request.get("options", {})
        timeout = options.get("parser_timeout_seconds", 30)
        memory = options.get("parser_memory_mb", 1024) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (40 * 1024 * 1024, 40 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        data = sys.stdin.buffer.read(100 * 1024 * 1024 + 1)
        if len(data) > 100 * 1024 * 1024:
            raise ParseError("Parser input exceeds the size limit")
        with contextlib.redirect_stdout(sys.stderr):
            parsed = parse_document(data, request["media_type"], options)
        metadata = {**parsed.metadata, "parser_isolation": {"subprocess": True, "memory_mb": memory // (1024 * 1024),
                                                         "cpu_seconds": timeout, "wall_timeout_seconds": timeout + 3}}
        output = asdict(parsed)
        output["metadata"] = metadata
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - serialize bounded worker failure across process boundary
        sys.stdout.write(json.dumps({"error": str(exc) if isinstance(exc, ParseError) else f"Parser failed ({type(exc).__name__})"}))
        raise SystemExit(1)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This module is an internal bounded parser worker")
    _worker_main()
