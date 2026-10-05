# Saved-byte processing pipelines

Pipelines are data, never executable code. `load_pipeline()` uses a safe YAML loader,
rejects duplicate keys, aliases, unknown tags, names and options, checks types and
bounds, and hashes the normalized complete configuration. The fixed registered
publication sequence is:

1. `raw_verify`: persist the original bytes atomically under SHA-256; verify a read
2. `extract`: select a registered saved-HTML, text or PDF parser by media type
3. `normalize_privacy`: NFC-normalized text, optional contact redaction
4. `source_map`: stable content-addressed block identities and exact Unicode offsets
5. `quality`: text/token/replacement-character thresholds
6. `lexical_index`: a nonempty Unicode-token postings index

Mandatory gates cannot be omitted, reordered, or disabled. Optional behavior is
expressed with typed registered options rather than arbitrary callbacks or scripts.
The `extract` stage has format-specific branches; this is not a general DAG engine.
Configuration changes and parser versions are recorded with each representation.
Reprocessing consumes a verified existing capture without another network fetch.

## Options

- raw_verify: `max_bytes` (1–104857600; default 26214400)
- extract:
  - `html_parser`: `auto`, `trafilatura`, `builtin`
  - `include_selector`, `exclude_selector`: bounded CSS selectors; requires
    BeautifulSoup/soupsieve. Exclusions apply first. An unmatched include fails
  - `pdf_parser`: `auto`, `docling`, `pypdf`
  - `pdf_fallback`: `error` (default), `pypdf`
  - `docling_ocr`: boolean, default false
  - `docling_tables`: boolean, default true
  - `ocr_language`: local Tesseract language code(s), e.g. `eng` or `eng+deu`
  - `parser_timeout_seconds`: 1–180, default 30
  - `parser_memory_mb`: 128–8192, default 1024
- normalize_privacy: `redact_emails`, `redact_phones`, both false by default
- source_map: `max_block_chars`: 100–100000, default 3000
- quality: `min_characters`, `min_word_tokens`, `max_replacement_ratio`
- lexical_index: `minimum_token_length`: 1–10, default 1

The service parser runs in a separate process with CPU/address-space/file-size and
wall-time limits. A timeout terminates its process group. Parser subprocesses do
not inherit provider credentials; Hugging Face/Transformers are offline. These
limits mitigate decompression/resource exhaustion; they are not a full OS sandbox.
Production should additionally isolate parser workers in restricted containers.

## Parser fidelity and source-map meaning

Trafilatura only receives saved HTML. No parser refetches the source URL. Its
fallback is labelled as built-in extraction; CSS selection changes record selector
and character-count metadata. PDF `auto` uses local Docling when configured,
otherwise a clearly degraded pypdf fallback. Explicit `docling` is strict unless
`pdf_fallback: pypdf` was requested. High-quality configuration requires local
Docling artifacts and, if OCR is enabled, Tesseract plus `TESSDATA_PREFIX` with the
specified `.traineddata` files. No model download is attempted.

Docling preserves text/table reading order, single-page provenance, bounding boxes,
and table row/column cell structure. Multi-page blocks without precise per-page
mapping and incomplete conversions fail rather than inventing provenance. Optional
OCR and table processing require deployment validation with representative files.
The dependency-heavy Docling runtime is not part of default offline acceptance.
pypdf preserves page numbers, but cannot promise table structure, OCR, or layout
fidelity; metadata explicitly states those limitations.

All offsets are Unicode code points in the stored canonical text representation,
not byte offsets into HTML or glyph positions in PDF. Contact redaction produces a
new representation and records transformation ranges against the pre-redaction
extraction. Unredacted raw bytes remain in restricted immutable storage. Regex
contact redaction is not a complete health/financial/identity-data classifier.

## External services

No provider calls occur without explicit configuration. Models additionally require
`EXTERNAL_EVIDENCE_POLICY=allow-reviewed` or `redact-contact-data`; the default is
`deny`. `EXTERNAL_ALLOWED_CLASSIFICATIONS` defaults to `public`; including
`internal` or `sensitive` requires explicit administrator configuration. Unlabelled
evidence defaults to internal. Declared sensitive evidence needs a separate
`ALLOW_SENSITIVE_EXTERNAL_DATA=true` opt-in. This policy setting does not replace
an organization's legal review or consent requirements. Model output is staged,
known evidence IDs are checked, and semantic support still requires human review.

The model protocol requests a JSON `title` and `claims` array only. Each claim has
`text` and nonempty `evidence_ids` containing exact saved IDs from the request.
The adapter rejects unknown IDs and constructs proposal `content` from those exact
claim text strings itself. Thus every claim is an exact substring of proposal
content by construction; freeform model-supplied content is never trusted to match.
The domain still independently verifies this invariant before staging a proposal.

`render_with_playwright()` is a client for an independently audited HTTPS renderer,
not a browser running inside the backend. It requires `ISOLATED_RENDERER_URL`,
`ISOLATED_RENDERER_API_KEY`, and `ISOLATED_RENDERER_EGRESS_ATTESTED=true`. The service
must acknowledge `X-Egress-Policy: public-dns-pinned-v1` and enforce public-only,
DNS-pinned egress for every request/redirect/subresource, block WebSockets and
service workers, contain no ambient credentials, and bound runtime/output. An
environment assertion and response header are not independent proof of isolation.
The adapter stays disabled unless an operator provides that audited service.
