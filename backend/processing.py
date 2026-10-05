"""Registered, typed YAML processing stages and publication eligibility gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .parsers import _assemble, parse_document_isolated
from .security import safe_fetch
from .storage import ContentStore, LocalContentStore, get_store


class PipelineError(ValueError):
    pass


@dataclass(frozen=True)
class StageConfig:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineConfig:
    version: int
    stages: tuple[StageConfig, ...]

    @property
    def config_hash(self) -> str:
        payload = {
            "version": self.version,
            "stages": [{"name": stage.name, "options": stage.options} for stage in self.stages],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


STAGE_ORDER = ("raw_verify", "extract", "normalize_privacy", "source_map", "quality", "lexical_index")
# name: option -> (required Python type, default, lower/upper bound or allowed values)
STAGE_OPTIONS = {
    "raw_verify": {"max_bytes": (int, 25 * 1024 * 1024, (1, 100 * 1024 * 1024))},
    "extract": {
        "html_parser": (str, "auto", {"auto", "trafilatura", "builtin"}),
        "pdf_parser": (str, "auto", {"auto", "docling", "pypdf"}),
        "include_selector": (str, "", (0, 1024)),
        "exclude_selector": (str, "", (0, 1024)),
        "pdf_fallback": (str, "error", {"error", "pypdf"}),
        "docling_ocr": (bool, False, {False, True}),
        "docling_tables": (bool, True, {False, True}),
        "ocr_language": (str, "eng", (3, 64)),
        "parser_timeout_seconds": (int, 30, (1, 180)),
        "parser_memory_mb": (int, 1024, (128, 8192)),
    },
    "normalize_privacy": {
        "redact_emails": (bool, False, {False, True}),
        "redact_phones": (bool, False, {False, True}),
    },
    "source_map": {"max_block_chars": (int, 3000, (100, 100_000))},
    "quality": {
        "min_characters": (int, 1, (1, 100_000)),
        "min_word_tokens": (int, 1, (1, 10_000)),
        "max_replacement_ratio": (float, 0.05, (0.0, 0.25)),
    },
    "lexical_index": {"minimum_token_length": (int, 1, (1, 10))},
}


class _StrictLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in mapping:
            raise PipelineError("Pipeline YAML keys must be unique strings")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_pipeline(config: str | dict | PipelineConfig | Path | None = None) -> PipelineConfig:
    if isinstance(config, PipelineConfig):
        # Revalidate to prevent constructed dataclass options from bypassing the registry.
        config = {
            "version": config.version,
            "stages": [{"name": item.name, "options": item.options} for item in config.stages],
        }
    if config is None:
        config = {"version": 1, "stages": [{"name": name} for name in STAGE_ORDER]}
    if isinstance(config, Path):
        config = config.read_text(encoding="utf-8")
    if isinstance(config, str):
        if len(config.encode("utf-8")) > 32_768:
            raise PipelineError("Pipeline YAML exceeds 32 KiB")
        try:
            events = list(yaml.parse(config))
            depth = 0
            for event in events:
                if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                    depth += 1
                    if depth > 20:
                        raise PipelineError("Pipeline YAML nesting exceeds the safe limit")
                elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                    depth -= 1
            if len(events) > 512 or any(isinstance(event, yaml.AliasEvent) for event in events):
                raise PipelineError("Pipeline YAML aliases or excessive nodes are forbidden")
            config = yaml.load(config, Loader=_StrictLoader)
        except yaml.YAMLError as exc:
            raise PipelineError("Pipeline must be safe, valid YAML") from exc
    if not isinstance(config, dict) or set(config) != {"version", "stages"}:
        raise PipelineError("Pipeline requires exactly version and stages")
    if type(config["version"]) is not int or config["version"] != 1:
        raise PipelineError("Unsupported pipeline version")
    if not isinstance(config["stages"], list) or len(config["stages"]) != len(STAGE_ORDER):
        raise PipelineError("Pipeline must include every publication gate exactly once")
    stages = []
    for expected, item in zip(STAGE_ORDER, config["stages"]):
        if not isinstance(item, dict) or set(item) - {"name", "options"} or item.get("name") != expected:
            raise PipelineError(f"Expected registered stage {expected}; stage order and names are fixed")
        options = item.get("options", {})
        if not isinstance(options, dict) or set(options) - set(STAGE_OPTIONS[expected]):
            raise PipelineError(f"Unknown options for stage {expected}")
        normalized = {}
        for key, (kind, default, constraint) in STAGE_OPTIONS[expected].items():
            value = options.get(key, default)
            if kind is float and type(value) in {int, float}:
                value = float(value)
            if type(value) is not kind:
                raise PipelineError(f"Option {expected}.{key} must be {kind.__name__}")
            if isinstance(constraint, set):
                valid = value in constraint
            else:
                valid = constraint[0] <= (len(value) if kind is str else value) <= constraint[1]
            if not valid:
                raise PipelineError(f"Option {expected}.{key} is outside allowed bounds")
            if (
                expected == "extract"
                and key == "ocr_language"
                and not re.fullmatch(r"[a-z]{3}(?:\+[a-z]{3})*", value)
            ):
                raise PipelineError("OCR languages must be registered three-letter Tesseract codes")
            normalized[key] = value
        stages.append(StageConfig(expected, normalized))
    return PipelineConfig(1, tuple(stages))


@dataclass(frozen=True)
class ProcessingResult:
    raw_hash: str
    raw_relative_path: str
    extracted_text: str
    blocks: list[dict]
    parser_version: str
    config_hash: str
    title: str
    warnings: list[str]
    quality: dict
    lexical_index: dict[str, list[str]]
    gates: dict[str, bool]
    metadata: dict

    @property
    def ready_for_publication(self) -> bool:
        return all(self.gates.values()) and bool(self.blocks) and bool(self.lexical_index)


def validate_source_map(text: str, blocks: list[dict]) -> bool:
    if not isinstance(text, str) or not blocks:
        return False
    end_previous = 0
    for block in blocks:
        start, end = block.get("start_offset"), block.get("end_offset")
        if type(start) is not int or type(end) is not int or not (end_previous <= start < end <= len(text)):
            return False
        quote = block.get("text")
        if not isinstance(quote, str) or text[start:end] != quote:
            return False
        expected_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        if block.get("quote_hash", block.get("content_hash")) != expected_hash:
            return False
        if block.get("page") is not None and (type(block["page"]) is not int or block["page"] < 1):
            return False
        end_previous = end
    return True


def _raw_verify(state: dict, options: dict):
    raw = state["raw"]
    if not isinstance(raw, bytes) or not raw:
        raise PipelineError("A nonempty saved raw byte sequence is required")
    if len(raw) > options["max_bytes"]:
        raise PipelineError("Raw input exceeds the pipeline size limit")
    stored = state["store"].put(raw, state["media_type"])
    verified = state["store"].verify(stored.sha256)
    if not verified or hashlib.sha256(raw).hexdigest() != stored.sha256:
        raise PipelineError("Raw object verification failed")
    state["stored"] = stored
    state["gates"]["raw_verified"] = True


def _extract(state: dict, options: dict):
    # Extraction reads the verified immutable object, rather than re-fetching source URLs.
    raw = state["store"].get(state["stored"].sha256)
    state["parsed"] = parse_document_isolated(raw, state["media_type"], options)


# Deliberately limited contact-data detectors, not a complete sensitive-data classifier.
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)")


def _normalize_privacy(state: dict, options: dict):
    parsed = state["parsed"]
    parts, transformations = [], []
    for block in parsed.blocks:
        original = block["text"]
        matches = []
        if options["redact_emails"]:
            matches.extend(
                (match.start(), match.end(), "email", "[EMAIL_REDACTED]")
                for match in EMAIL_PATTERN.finditer(original)
            )
        if options["redact_phones"]:
            matches.extend(
                (match.start(), match.end(), "phone", "[PHONE_REDACTED]")
                for match in PHONE_PATTERN.finditer(original)
                if 9 <= sum(char.isdigit() for char in match.group()) <= 15
            )
        cursor, fragments = 0, []
        for start, end, kind, replacement in sorted(matches):
            if start < cursor:
                continue
            fragments.extend((original[cursor:start], replacement))
            transformations.append(
                {
                    "kind": kind,
                    "start_offset": block["start_offset"] + start,
                    "end_offset": block["start_offset"] + end,
                    "replacement": replacement,
                }
            )
            cursor = end
        fragments.append(original[cursor:])
        parts.append(("".join(fragments), block["locator_json"]))
    metadata = {
        **parsed.metadata,
        "privacy": {
            "redact_emails": options["redact_emails"],
            "redact_phones": options["redact_phones"],
            "transformations": transformations,
            "transformation_offset_basis": "pre_redaction_extracted_text_unicode_codepoints",
            "source_map_basis": "stored_transformed_representation",
            "complete_dlp": False,
        },
    }
    warnings = parsed.warnings[:]
    if transformations:
        warnings.append(
            "Contact redaction changed extracted text; quote offsets refer to the saved transformed representation"
        )
    state["parsed"] = _assemble(
        parts,
        parsed.parser_version + ("/contact-redaction-v1" if transformations else ""),
        title=parsed.title,
        warnings=warnings,
        metadata=metadata,
    )


def _source_map(state: dict, options: dict):
    parsed = state["parsed"]
    blocks = []
    for original in parsed.blocks:
        text = original["text"]
        cursor = 0
        while cursor < len(text):
            until = min(cursor + options["max_block_chars"], len(text))
            if until < len(text):
                break_at = text.rfind(" ", cursor + options["max_block_chars"] // 2, until)
                if break_at > cursor:
                    until = break_at
            untrimmed = text[cursor:until]
            content = untrimmed.strip()
            if content:
                leading = len(untrimmed) - len(untrimmed.lstrip())
                start = original["start_offset"] + cursor + leading
                end = start + len(content)
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                block_id = hashlib.sha256(
                    f"{state['stored'].sha256}:{parsed.parser_version}:{state['config_hash']}:{start}:{end}:{digest}".encode()
                ).hexdigest()
                blocks.append(
                    {
                        **original,
                        "id": block_id,
                        "text": content,
                        "start_offset": start,
                        "end_offset": end,
                        "quote_hash": digest,
                        "content_hash": digest,
                    }
                )
            cursor = until
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
    if not validate_source_map(parsed.text, blocks):
        raise PipelineError("Extracted document has no valid, exact source map")
    state["blocks"] = blocks
    state["gates"]["source_map_valid"] = True


def _quality(state: dict, options: dict):
    text = state["parsed"].text
    characters = len(text.strip())
    words = len(re.findall(r"\w+", text, flags=re.UNICODE))
    ratio = text.count("\ufffd") / max(1, characters)
    passed = (
        characters >= options["min_characters"]
        and words >= options["min_word_tokens"]
        and ratio <= options["max_replacement_ratio"]
    )
    state["quality"] = {
        "passed": passed,
        "characters": characters,
        "word_tokens": words,
        "replacement_ratio": ratio,
        "thresholds": options,
        "limitations": state["parsed"].warnings,
    }
    if not passed:
        raise PipelineError("Extracted text failed the configured quality gate")
    state["gates"]["quality_passed"] = True


def _lexical_index(state: dict, options: dict):
    postings = {}
    for block in state["blocks"]:
        for token in set(re.findall(r"\w+", block["text"].casefold(), flags=re.UNICODE)):
            if len(token) >= options["minimum_token_length"]:
                postings.setdefault(token, []).append(block["id"])
    if not postings:
        raise PipelineError("Lexical index is empty; capture cannot be published")
    state["lexical_index"] = dict(sorted(postings.items()))
    state["gates"]["lexical_index_ready"] = True


STAGE_REGISTRY = {
    "raw_verify": _raw_verify,
    "extract": _extract,
    "normalize_privacy": _normalize_privacy,
    "source_map": _source_map,
    "quality": _quality,
    "lexical_index": _lexical_index,
}


def process_document(
    raw_bytes: bytes,
    media_type: str | None = None,
    *,
    mime_type: str | None = None,
    source_url: str | None = None,
    pipeline_config=None,
    store: ContentStore | None = None,
) -> ProcessingResult:
    config = load_pipeline(pipeline_config)
    media_type = media_type or mime_type or "text/plain"
    state = {
        "raw": raw_bytes,
        "media_type": media_type,
        "store": store or get_store(),
        "gates": {},
        "config_hash": config.config_hash,
    }
    for stage in config.stages:
        STAGE_REGISTRY[stage.name](state, stage.options)
    parsed, stored = state["parsed"], state["stored"]
    metadata = {
        **parsed.metadata,
        "config_hash": config.config_hash,
        "parser_version": parsed.parser_version,
        "pipeline_version": config.version,
        "stage_names": list(STAGE_ORDER),
        "source_url": source_url,
        "raw_hash": stored.sha256,
        "raw_object_key": stored.key,
        "storage_backend": stored.backend,
        "quality": state["quality"],
        "publication_gates": state["gates"],
        "warnings": parsed.warnings,
        "lexical_index_version": "unicode-postings-v1",
    }
    return ProcessingResult(
        stored.sha256,
        stored.key,
        parsed.text,
        state["blocks"],
        parsed.parser_version,
        config.config_hash,
        parsed.title,
        parsed.warnings,
        state["quality"],
        state["lexical_index"],
        state["gates"],
        metadata,
    )


def _read_upload(path: str, limit: int, settings=None) -> bytes:
    if settings is None:
        from .config import settings
    if path.startswith("s3://"):
        from urllib.parse import urlsplit

        from .storage import S3ContentStore

        parsed = urlsplit(path)
        digest = parsed.path.rsplit("/", 1)[-1]
        store = get_store()
        if (
            not isinstance(store, S3ContentStore)
            or parsed.netloc != store.bucket
            or path != f"s3://{store.bucket}/{store._key(digest)}"
        ):
            raise PipelineError(
                "Upload S3 object must belong to the configured bucket and content-addressed prefix"
            )
        data = store.get(digest)
        if len(data) > limit:
            raise PipelineError("Upload exceeds the size limit")
        return data
    roots = [settings.blob_dir, Path(os.environ.get("RAW_STORAGE_DIR", "./data/objects"))]
    candidate = Path(path).expanduser().resolve(strict=True)
    if not any(candidate.is_relative_to(root.expanduser().resolve()) for root in roots):
        raise PipelineError("Upload object must be inside configured raw storage")
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PipelineError("Upload exceeds the size limit")
    return data


def ingest_operation(session, operation) -> dict:
    """Worker entry point. Domain owns transaction/status; this never commits."""
    from . import domain
    from .config import settings as default_settings

    settings = session.info.get("settings", default_settings)
    inputs = dict(operation.input_json or {})
    kind = inputs.get("type", "text")
    configuration = inputs.get("pipeline_config", inputs.get("pipeline"))
    config = load_pipeline(None if configuration == {} else configuration)
    max_bytes = config.stages[0].options["max_bytes"]
    metadata = {}
    provenance = inputs.get("discovery_provenance")
    if isinstance(provenance, dict):
        metadata["discovery_provenance"] = {
            key: str(value)[:8192]
            for key, value in provenance.items()
            if key
            in {"feed_locator", "item_id", "updated", "cursor_id", "source_type", "search_provider", "query"}
        }
    if kind == "url":
        response = safe_fetch(inputs["url"], max_bytes=max_bytes)
        raw, media_type, source_uri = response.data, response.media_type, response.url
        metadata.update(
            {
                "requested_url": inputs["url"],
                "final_url": response.url,
                "fetched_by_application": True,
                "http_status": response.status,
                "response_headers": {
                    name: response.headers[name]
                    for name in ("content-type", "etag", "last-modified")
                    if name in response.headers
                },
            }
        )
    elif kind == "text":
        raw = inputs.get("text", "").encode("utf-8")
        media_type = inputs.get("media_type", "text/plain")
        source_uri = (
            inputs.get("source_uri")
            or inputs.get("original_url")
            or "text:" + hashlib.sha256(raw).hexdigest()
        )
        if inputs.get("original_url"):
            metadata.update(
                {
                    "declared_original_url": source_uri,
                    "provenance": "user_supplied_text",
                    "fetched_by_application": False,
                }
            )
    elif kind == "upload":
        raw = _read_upload(inputs["blob_path"], max_bytes, settings)
        media_type = inputs.get("media_type", "application/octet-stream")
        if media_type == "application/octet-stream":
            filename = inputs.get("filename", "").lower()
            if raw.lstrip().startswith(b"%PDF-"):
                media_type = "application/pdf"
            elif filename.endswith((".html", ".htm")):
                media_type = "text/html"
            elif filename.endswith((".txt", ".md")):
                media_type = "text/plain"
        source_uri = inputs.get("source_uri") or "upload:" + hashlib.sha256(raw).hexdigest()
        metadata["filename"] = inputs.get("filename", "uploaded-source")
        if inputs.get("original_url"):
            source_uri = inputs["original_url"]
            metadata.update(
                {
                    "declared_original_url": source_uri,
                    "provenance": "user_uploaded_saved_source",
                    "fetched_by_application": False,
                }
            )
    else:
        raise PipelineError("Unsupported ingestion input type")
    content_store = (
        get_store()
        if os.environ.get("STORAGE_BACKEND", "local") == "s3"
        else LocalContentStore(settings.blob_dir)
    )
    result = process_document(
        raw, media_type, source_url=source_uri, pipeline_config=config, store=content_store
    )
    if not result.ready_for_publication:
        raise PipelineError("Publication gates are incomplete")
    return domain.store_capture(
        session,
        workspace_id=operation.workspace_id,
        project_id=operation.project_id,
        source_uri=source_uri,
        kind=kind,
        title=inputs.get("title") or result.title or inputs.get("filename") or source_uri,
        raw_bytes=raw,
        media_type=media_type,
        extracted_text=result.extracted_text,
        blocks=result.blocks,
        parser=result.parser_version,
        metadata={**result.metadata, **metadata},
        access_scope=inputs.get("access_scope", "workspace"),
        representation_variant=inputs.get("representation_variant", "default"),
        classification=inputs.get("classification", "internal"),
        blob_dir=settings.blob_dir,
    )


def reprocess_operation(session, operation) -> dict:
    """Create a new representation from an existing capture without any network fetch."""
    from . import domain, models
    from .config import settings as default_settings

    settings = session.info.get("settings", default_settings)
    inputs = dict(operation.input_json or {})
    capture = domain.scoped(
        session, models.Capture, inputs["capture_id"], operation.workspace_id, operation.project_id
    )
    source = domain.scoped(
        session, models.Source, capture.source_id, operation.workspace_id, operation.project_id
    )
    raw = domain.read_blob(capture, settings)
    if hashlib.sha256(raw).hexdigest() != capture.content_hash:
        raise PipelineError("Saved capture integrity verification failed")
    content_store = (
        get_store()
        if os.environ.get("STORAGE_BACKEND", "local") == "s3"
        else LocalContentStore(settings.blob_dir)
    )
    result = process_document(
        raw,
        capture.media_type,
        source_url=source.canonical_uri,
        pipeline_config=(inputs.get("pipeline_config") or inputs.get("pipeline")),
        store=content_store,
    )
    if not result.ready_for_publication:
        raise PipelineError("Reprocessed representation failed publication gates")
    saved = domain.store_representation(
        session,
        operation.workspace_id,
        operation.project_id,
        capture,
        result.extracted_text,
        result.blocks,
        result.parser_version,
        {**result.metadata, "reprocessed_from_capture_id": capture.id, "network_fetch": False},
        inputs.get("title") or source.title,
    )
    saved.update({"source_id": source.id, "capture_id": capture.id})
    return saved
