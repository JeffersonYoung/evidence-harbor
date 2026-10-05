"""Bounded public RSS/Atom/sitemap discovery; all candidates use normal ingestion.

XML is data only: DTD/entity declarations, UTF-16/NUL encoding, XInclude processing,
compressed sitemap expansion, and unbounded sitemap recursion are unsupported.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from .security import FetchError, is_public_address, safe_fetch

MAX_XML_BYTES = 2 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 8 * 1024 * 1024
MAX_XML_NODES = 50_000
MAX_XML_DEPTH = 32
MAX_SITEMAP_DEPTH = 2
MAX_FETCHES = 5
MAX_ITEMS = 100
MAX_CURSOR_HISTORY = 10_000


class ConnectorError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveredSource:
    url: str
    title: str
    feed_locator: str
    item_id: str
    updated: str | None
    cursor_id: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryBatch:
    candidates: list[DiscoveredSource]
    next_state: dict
    warnings: list[str]
    fetch_count: int
    truncated: bool


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _xml(data: bytes):
    if not data or len(data) > MAX_XML_BYTES:
        raise ConnectorError("Feed XML exceeds its byte limit or is empty")
    if b"\x00" in data or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.IGNORECASE):
        raise ConnectorError("XML DTD/entities and NUL/UTF-16 encodings are forbidden")
    parser = ET.XMLPullParser(events=("start", "end"))
    root, depth, count = None, 0, 0
    try:
        for position in range(0, len(data), 16384):
            parser.feed(data[position:position + 16384])
            for event, element in parser.read_events():
                if event == "start":
                    root = root if root is not None else element
                    depth += 1
                    count += 1
                    if depth > MAX_XML_DEPTH or count > MAX_XML_NODES:
                        raise ConnectorError("Feed XML exceeds depth or element limits")
                else:
                    depth -= 1
        parser.close()
    except ET.ParseError as exc:
        raise ConnectorError("Feed is not valid supported XML") from exc
    if root is None or depth != 0:
        raise ConnectorError("Feed XML is incomplete")
    return root


def _hostname(value: str) -> str:
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (AttributeError, UnicodeError) as exc:
        raise ConnectorError("Invalid allowed domain") from exc
    if not host or any(character in host for character in "/@?#*%\\") or any(ord(c) <= 32 for c in host):
        raise ConnectorError("Allowed domains must be exact hostnames, without wildcards or paths")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith((".local", ".internal", ".localhost", ".invalid", ".test")):
            raise ConnectorError("Local/reserved feed domains are forbidden")
    else:
        if not is_public_address(host):
            raise ConnectorError("Private/reserved feed addresses are forbidden")
    return host


def _candidate_url(value: str, base: str, allowed_hosts: set[str]) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        raise ConnectorError("Discovered URL is invalid")
    try:
        parsed = urlsplit(urljoin(base, value))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ConnectorError("Discovered URLs must be credential-free HTTP(S)")
        host = _hostname(parsed.hostname)
        if host not in allowed_hosts:
            raise ConnectorError("Discovered URL is outside the configured domain allowlist")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443}:
            raise ConnectorError("Discovered URL uses a forbidden web port")
        if urlsplit(base).scheme == "https" and parsed.scheme != "https":
            raise ConnectorError("Discovered URL downgrades HTTPS")
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        result = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        result.encode("ascii")
        return result
    except (ValueError, UnicodeError) as exc:
        raise ConnectorError(str(exc)) from exc


def _child_text(item, names: set[str]) -> str:
    for child in item:
        if _tag(child) in names:
            return "".join(child.itertext()).strip()
    return ""


def _item_url(item, atom: bool) -> str:
    if atom:
        for child in item:
            if _tag(child) == "link" and child.attrib.get("rel", "alternate") == "alternate" and child.attrib.get("href"):
                return child.attrib["href"]
        return ""
    return _child_text(item, {"link"})


def discover_feed(locator: str, *, source_type: str = "rss", allowed_domains: list[str] | None = None,
                  max_items: int = 50, state: dict | None = None) -> DiscoveryBatch:
    if source_type not in {"rss", "sitemap"}:
        raise ConnectorError("Unsupported feed connector type")
    if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
        raise ConnectorError("Feed max_items must be between 1 and 100")
    if allowed_domains is not None and (not isinstance(allowed_domains, list) or len(allowed_domains) > 50):
        raise ConnectorError("Feed domain allowlist must contain at most 50 exact hostnames")
    try:
        parsed_locator = urlsplit(locator)
        feed_host = _hostname(parsed_locator.hostname or "")
    except ValueError as exc:
        raise ConnectorError("Invalid feed locator") from exc
    allowed = {_hostname(host) for host in (allowed_domains or [feed_host])}
    # Feed itself is always permitted as a control document. Discovered source and
    # nested sitemap URLs must match the explicit allowlist/same-host default.
    canonical_locator = _candidate_url(locator, locator, allowed | {feed_host})
    state = state or {}
    if not isinstance(state, dict):
        raise ConnectorError("Invalid connector cursor state")
    historical = state.get("seen_cursor_ids", [])
    if not isinstance(historical, list) or len(historical) > MAX_CURSOR_HISTORY or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in historical):
        raise ConnectorError("Invalid or oversized connector cursor history")
    seen = set(historical)
    emitted = set()
    visited = set()
    candidates, warnings, fingerprints = [], [], []
    fetch_count, total_bytes, truncated = 0, 0, False

    def visit(url: str, depth: int):
        nonlocal fetch_count, total_bytes, truncated
        if url in visited:
            return
        if fetch_count >= MAX_FETCHES or depth > MAX_SITEMAP_DEPTH:
            truncated = True
            warnings.append("Sitemap traversal reached its bounded fetch/depth limit")
            return
        visited.add(url)
        result = safe_fetch(url, max_bytes=MAX_XML_BYTES, timeout=20)
        fetch_count += 1
        total_bytes += len(result.data)
        if total_bytes > MAX_TOTAL_XML_BYTES:
            raise ConnectorError("Combined feed XML exceeds the total byte limit")
        fingerprints.append(hashlib.sha256(result.data).hexdigest())
        root = _xml(result.data)
        root_tag = _tag(root)
        base = urljoin(result.url, root.attrib.get("{http://www.w3.org/XML/1998/namespace}base", ""))
        if source_type == "sitemap" and root_tag == "sitemapindex":
            for child in root:
                if fetch_count >= MAX_FETCHES:
                    truncated = True
                    break
                if _tag(child) != "sitemap":
                    continue
                raw_url = _child_text(child, {"loc"})
                try:
                    nested = _candidate_url(raw_url, base, allowed)
                    if urlsplit(nested).path.endswith(".gz"):
                        warnings.append("Compressed sitemap skipped; gzip expansion is disabled")
                        continue
                    if len(candidates) >= max_items:
                        truncated = True
                        break
                    visit(nested, depth + 1)
                except (ConnectorError, FetchError) as exc:
                    warnings.append(f"Nested sitemap skipped: {str(exc)[:160]}")
            return
        if source_type == "sitemap":
            if root_tag != "urlset":
                raise ConnectorError("Expected a sitemap urlset or sitemapindex")
            entries = [item for item in root if _tag(item) == "url"]
        else:
            if root_tag not in {"rss", "feed", "rdf"}:
                raise ConnectorError("Expected RSS or Atom feed XML")
            entries = [item for item in root.iter() if _tag(item) in {"item", "entry"}]
        for item in entries:
            if len(candidates) >= max_items:
                truncated = True
                break
            atom = _tag(item) == "entry"
            raw_url = _child_text(item, {"loc"}) if source_type == "sitemap" else _item_url(item, atom)
            if not raw_url:
                continue
            item_base = urljoin(base, item.attrib.get("{http://www.w3.org/XML/1998/namespace}base", ""))
            try:
                source_url = _candidate_url(raw_url, item_base, allowed)
            except ConnectorError as exc:
                warnings.append(f"Feed item skipped: {str(exc)[:160]}")
                continue
            item_id = (_child_text(item, {"id", "guid"}) or source_url)[:2000]
            updated = (_child_text(item, {"updated", "pubdate", "published", "lastmod"}) or None)
            if updated:
                updated = updated[:200]
            title = _child_text(item, {"title"})[:1000] or source_url
            version_hash = hashlib.sha256(ET.tostring(item, encoding="utf-8")).hexdigest()
            cursor = hashlib.sha256((canonical_locator + "\n" + item_id + "\n" + source_url + "\n" + version_hash).encode()).hexdigest()
            if cursor in seen or cursor in emitted:
                continue
            emitted.add(cursor)
            candidates.append(DiscoveredSource(source_url, title, canonical_locator, item_id, updated, cursor))

    visit(canonical_locator, 0)
    history = (historical + [item.cursor_id for item in candidates])[-MAX_CURSOR_HISTORY:]
    next_state = {"version": 1, "seen_cursor_ids": history,
                  "feed_fingerprint": hashlib.sha256("\n".join(fingerprints).encode()).hexdigest()}
    return DiscoveryBatch(candidates, next_state, warnings[:100], fetch_count, truncated)


def enqueue_connector_watch(session, watch, invocation_id: str) -> dict:
    """Fetch control XML and atomically queue ordinary URL ingestion operations.

    Cursor advances with queued operations in the caller's transaction. Snippets,
    summaries and feed XML are never accepted as source evidence. Item-version
    idempotency keys prevent replayed ticks from re-enqueueing the same item.
    """
    from . import domain
    if not watch.enabled:
        return {"status": "disabled", "watch_id": watch.id, "operation_ids": []}
    source_type = getattr(watch, "source_type", "http")
    if source_type not in {"rss", "sitemap"}:
        raise ConnectorError("This watch is not a feed/sitemap connector")
    batch = discover_feed(watch.locator, source_type=source_type,
                          allowed_domains=getattr(watch, "allowed_domains", []) or [],
                          max_items=getattr(watch, "max_items", 50), state=getattr(watch, "connector_state", {}) or {})
    operation_ids = []
    for item in batch.candidates:
        payload = {"type": "url", "url": item.url, "title": item.title,
                   "watch_id": watch.id, "pipeline_config": watch.pipeline_config or None,
                   "classification": getattr(watch, "classification", "internal"),
                   "discovery_provenance": {"connector_type": source_type, "feed_locator": item.feed_locator,
                       "item_id": item.item_id, "updated": item.updated, "cursor_id": item.cursor_id}}
        operation = domain.create_operation(session, watch.workspace_id, watch.project_id, payload,
                                            idempotency_key=f"feed:{watch.id}:{item.cursor_id}")
        operation_ids.append(operation.id)
    watch.connector_state = batch.next_state
    summary = {"watch_id": watch.id, "status": "queued", "operation_ids": operation_ids,
               "candidate_count": len(batch.candidates), "fetch_count": batch.fetch_count,
               "warnings": batch.warnings, "truncated": batch.truncated, "invocation_id": invocation_id}
    domain.emit_event(session, watch.workspace_id, watch.project_id, "source.discovery.completed", summary)
    session.flush()
    return summary
