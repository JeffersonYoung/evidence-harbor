from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def declared_original_url(value: str | None) -> str | None:
    """Syntax-only provenance, deliberately not a fetch or DNS lookup."""
    if value is None:
        return None
    if len(value) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Original URL contains invalid characters")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Original URL must be HTTP(S), without embedded credentials")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        if port and not (
            (parsed.scheme.lower() == "http" and port == 80)
            or (parsed.scheme.lower() == "https" and port == 443)
        ):
            host += ":" + str(port)
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Original URL is malformed or embeds credentials") from exc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=50000)


class ProjectUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=50000)
    archived: bool | None = None


class IngestionCreate(StrictModel):
    project_id: str
    type: Literal["url", "text"] = "text"
    url: str | None = Field(default=None, max_length=8192)
    text: str | None = Field(default=None, max_length=5_000_000)
    title: str | None = Field(default=None, max_length=1000)
    original_url: str | None = Field(default=None, max_length=8192)
    pipeline_config: dict | str | None = None
    classification: Literal["public", "internal", "sensitive"] = "internal"
    access_scope: str = Field(default="workspace", min_length=1, max_length=120)
    representation_variant: str = Field(default="default", min_length=1, max_length=120)

    @field_validator("original_url")
    @classmethod
    def original_provenance(cls, value):
        return declared_original_url(value)

    @model_validator(mode="after")
    def input_required(self):
        if self.type == "url" and not self.url:
            raise ValueError("url is required for URL ingestion")
        if self.type == "text" and not self.text:
            raise ValueError("text is required for text ingestion")
        return self


class ReprocessRequest(StrictModel):
    pipeline_config: dict | str | None = None
    pipeline_id: str | None = None
    title: str | None = Field(default=None, max_length=1000)


class SearchRequest(StrictModel):
    project_id: str
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=20, ge=1, le=100)


class EvidenceCreate(StrictModel):
    project_id: str
    block_id: str
    quote: str = Field(min_length=1, max_length=100000)
    start_offset: int | None = Field(default=None, ge=0)


class QuestionCreate(StrictModel):
    project_id: str
    text: str = Field(min_length=1, max_length=50000)
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    evidence_gaps: list[str] = Field(default_factory=list)
    recheck_condition: str = ""


class QuestionUpdate(StrictModel):
    priority: Literal["low", "normal", "high", "critical"] | None = None
    evidence_gaps: list[str] | None = None
    recheck_condition: str | None = None
    status: Literal["open", "answered", "dismissed"] | None = None
    answer: str | None = None
    evidence_ids: list[str] | None = None


class ResearchRunCreate(StrictModel):
    project_id: str
    question: str = Field(min_length=1, max_length=50000)
    provider: Literal["local", "openai", "openai-compatible"] = "local"
    model: str | None = Field(default=None, max_length=120)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    max_searches: int = Field(default=3, ge=1, le=10)
    max_documents: int = Field(default=8, ge=1, le=30)
    max_tool_calls: int = Field(default=16, ge=3, le=100)
    max_duration_seconds: int = Field(default=180, ge=5, le=900)
    max_cost_usd: float = Field(default=0.0, ge=0, le=100)
    max_output_tokens: int = Field(default=2000, ge=128, le=16000)
    web_discovery: bool = False
    search_provider: Literal["brave", "searxng"] | None = None
    discovery_queries: list[str] = Field(default_factory=list, max_length=3)
    discovery_classification: Literal["public", "internal", "sensitive"] = "internal"

    @model_validator(mode="after")
    def discovery_budget(self):
        if any(not query.strip() or len(query) > 2000 for query in self.discovery_queries):
            raise ValueError("Discovery queries must contain 1-2000 characters")
        if self.web_discovery:
            if not self.search_provider:
                raise ValueError("Explicit search_provider is required for web discovery")
            if self.max_searches < 2 or self.max_tool_calls < 5:
                raise ValueError("Web discovery requires at least 2 searches and 5 tool calls")
            if not self.discovery_queries and len(self.question) > 2000:
                raise ValueError("Provide a discovery query of at most 2000 characters")
        return self


class EvidenceLink(StrictModel):
    evidence_id: str
    relation: Literal["supporting", "contradicting", "limiting"] = "supporting"
    rationale: str = ""


class Claim(StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    scope: str = ""
    as_of: str | None = None
    status: Literal["unreviewed", "supported", "contested", "limited"] = "unreviewed"
    limitations: list[str] = Field(default_factory=list)
    evidence_links: list[EvidenceLink] = Field(default_factory=list)


class ProposalCreate(StrictModel):
    project_id: str
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=2_000_000)
    claims: list[Claim] = Field(min_length=1)
    target_document_id: str | None = None
    base_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def target_version(self):
        if (self.target_document_id is None) != (self.base_version is None):
            raise ValueError("target_document_id and base_version must be supplied together")
        return self


class ProposalPublish(StrictModel):
    expected_version: int | None = Field(default=None, ge=1)
    accepted_claim_indices: list[int] | None = Field(default=None, min_length=1)


class DocumentLocks(StrictModel):
    sections: list[str] = Field(max_length=100)


class SubscriptionCreate(StrictModel):
    project_id: str
    event_types: list[str] = Field(default_factory=lambda: ["*"], min_length=1)
    target_url: str | None = None
