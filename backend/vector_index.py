"""Optional pgvector index generations; lexical search remains independently usable.

The provider is explicitly supplied. This module never starts paid calls on import.
Exact cosine search intentionally avoids an ANN index until embedding dimensions and
scale are known. Model/version/dimension are fixed per immutable generation.
"""

from __future__ import annotations

import math
from typing import Protocol

from sqlalchemy import text


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def validate_vector(vector: list[float], dimensions: int) -> list[float]:
    if len(vector) != dimensions or dimensions < 1 or dimensions > 4096:
        raise ValueError("Embedding dimension mismatch or unsupported dimensions")
    converted = [float(x) for x in vector]
    if any(not math.isfinite(x) for x in converted) or not any(converted):
        raise ValueError("Embedding must contain finite values and have nonzero norm")
    return converted


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


def create_schema(connection) -> None:
    """Called by PostgreSQL migration only, not automatically during a request."""
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    connection.execute(
        text("""CREATE TABLE IF NOT EXISTS block_embeddings (
        generation_id VARCHAR(36) NOT NULL REFERENCES index_generations(id),
        block_id VARCHAR(36) NOT NULL REFERENCES blocks(id),
        workspace_id VARCHAR(120) NOT NULL,
        project_id VARCHAR(36) NOT NULL REFERENCES projects(id),
        embedding vector NOT NULL,
        PRIMARY KEY (generation_id, block_id)
    )""")
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_embedding_scope ON block_embeddings (workspace_id, project_id, generation_id)"
        )
    )


def build_generation(
    session, workspace_id: str, project_id: str, representation_id: str, provider: EmbeddingProvider
):
    """One transaction publishes all vectors and their immutable generation together."""
    from sqlalchemy import select

    from .models import Block, IndexGeneration, Representation, new_id

    if session.bind.dialect.name != "postgresql":
        raise ValueError("Vector indexing requires PostgreSQL with the vector extension")
    representation = session.scalar(
        select(Representation).where(
            Representation.id == representation_id,
            Representation.workspace_id == workspace_id,
            Representation.project_id == project_id,
        )
    )
    if representation is None:
        raise ValueError("Representation not found in this project")
    blocks = list(
        session.scalars(
            select(Block)
            .where(
                Block.representation_id == representation_id,
                Block.workspace_id == workspace_id,
                Block.project_id == project_id,
            )
            .order_by(Block.ordinal)
        )
    )
    if not blocks:
        raise ValueError("Cannot index an empty representation")
    outputs = provider.embed([block.text for block in blocks])
    if len(outputs) != len(blocks):
        raise ValueError("Embedding provider returned an incomplete batch")
    vectors = [validate_vector(v, provider.dimensions) for v in outputs]
    generation = IndexGeneration(
        id=new_id(),
        workspace_id=workspace_id,
        project_id=project_id,
        representation_id=representation_id,
        index_kind="vector",
        version=provider.model,
        metadata_json={
            "model": provider.model,
            "dimensions": provider.dimensions,
            "status": "ready",
            "distance": "cosine",
            "implementation": "pgvector-exact-v1",
        },
    )
    session.add(generation)
    session.flush()
    for block, vector in zip(blocks, vectors, strict=True):
        session.execute(
            text("""INSERT INTO block_embeddings
            (generation_id, block_id, workspace_id, project_id, embedding)
            VALUES (:generation, :block, :workspace, :project, CAST(:vector AS vector))"""),
            {
                "generation": generation.id,
                "block": block.id,
                "workspace": workspace_id,
                "project": project_id,
                "vector": vector_literal(vector),
            },
        )
    session.flush()
    return generation


def search_generation(
    session,
    workspace_id: str,
    project_id: str,
    generation_id: str,
    query: str,
    provider: EmbeddingProvider,
    limit: int = 20,
    query_vector: list[float] | None = None,
) -> list[dict]:
    from sqlalchemy import select

    from .models import IndexGeneration

    generation = session.scalar(
        select(IndexGeneration).where(
            IndexGeneration.id == generation_id,
            IndexGeneration.workspace_id == workspace_id,
            IndexGeneration.project_id == project_id,
            IndexGeneration.index_kind == "vector",
        )
    )
    if generation is None:
        raise ValueError("Vector generation not found")
    if (
        generation.metadata_json.get("model") != provider.model
        or generation.metadata_json.get("dimensions") != provider.dimensions
    ):
        raise ValueError("Query embedding model must match the immutable index generation")
    vector = validate_vector(
        query_vector if query_vector is not None else provider.embed([query])[0], provider.dimensions
    )
    rows = session.execute(
        text("""SELECT block_id, embedding <=> CAST(:vector AS vector) AS distance
        FROM block_embeddings WHERE workspace_id=:workspace AND project_id=:project
        AND generation_id=:generation ORDER BY distance, block_id LIMIT :limit"""),
        {
            "vector": vector_literal(vector),
            "workspace": workspace_id,
            "project": project_id,
            "generation": generation_id,
            "limit": max(1, min(limit, 100)),
        },
    )
    return [{"block_id": row.block_id, "distance": float(row.distance)} for row in rows]


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Fuse ordered block IDs without comparing lexical and vector score scales."""
    if k < 1:
        raise ValueError("k must be positive")
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for rank, item in enumerate(ranking, 1):
            if item not in seen:
                scores[item] = scores.get(item, 0) + 1 / (k + rank)
                seen.add(item)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class LocalHashEmbeddingProvider:
    """Offline feature-hashing baseline, NOT a learned semantic embedding model."""

    model = "local-feature-hash-v1"
    dimensions = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        from .domain import tokenize

        vectors = []
        for content in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(content):
                hashed = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(hashed[:4], "big") % self.dimensions
                vector[index] += 1 if hashed[4] & 1 else -1
            if not any(vector):
                vector[0] = 1.0
            norm = math.sqrt(sum(x * x for x in vector))
            vectors.append([x / norm for x in vector])
        return vectors


class OpenAIEmbeddingProvider:
    """Opt-in OpenAI-compatible embeddings; batch cost reservation is conservative."""

    def __init__(self):
        import os

        from .providers import ProviderNotConfigured, _credential, _external_enabled

        _external_enabled()
        self.key = _credential("OPENAI_API_KEY")
        self.model = os.getenv("EMBEDDING_MODEL", "")
        self.dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
        self.endpoint = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/embeddings"
        self.price = float(os.getenv("EMBEDDING_PRICE_PER_MILLION", "0"))
        self.cap = float(os.getenv("EMBEDDING_MAX_BATCH_USD", "0"))
        if not self.model or self.price <= 0 or self.cap <= 0:
            raise ProviderNotConfigured(
                "Configure EMBEDDING_MODEL, DIMENSIONS, PRICE_PER_MILLION and MAX_BATCH_USD explicitly"
            )
        self.usage = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        import json

        from .providers import ProviderError
        from .security import safe_fetch

        # Bytes overestimate token count, intentionally conservative across languages.
        reservation = sum(len(t.encode("utf-8")) for t in texts) / 1_000_000 * self.price
        if reservation > self.cap:
            raise ProviderError("Embedding batch exceeds the configured conservative cost reservation")
        response = safe_fetch(
            self.endpoint,
            method="POST",
            max_redirects=0,
            timeout=120,
            max_bytes=25 * 1024 * 1024,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            body=json.dumps({"model": self.model, "input": texts, "dimensions": self.dimensions}).encode(),
        )
        payload = json.loads(response.data)
        if not isinstance(payload.get("data"), list) or len(payload["data"]) != len(texts):
            raise ProviderError("Embedding response has an incomplete data array")
        ordered = sorted(payload["data"], key=lambda row: row["index"])
        if [row["index"] for row in ordered] != list(range(len(texts))):
            raise ProviderError("Embedding response contains invalid indexes")
        self.usage = payload.get("usage")
        return [validate_vector(row["embedding"], self.dimensions) for row in ordered]


def embedding_provider(name: str):
    if name == "local_hash":
        return LocalHashEmbeddingProvider()
    if name == "openai":
        return OpenAIEmbeddingProvider()
    raise ValueError("Unknown embedding provider")


def index_operation(session, operation):
    from . import domain, models

    representation = domain.scoped(
        session,
        models.Representation,
        operation.input_json["representation_id"],
        operation.workspace_id,
        operation.project_id,
    )
    name = operation.input_json.get("provider", "local_hash")
    provider = embedding_provider(name)
    if name == "openai":
        capture = domain.scoped(
            session, models.Capture, representation.capture_id, operation.workspace_id, operation.project_id
        )
        source = domain.scoped(
            session, models.Source, capture.source_id, operation.workspace_id, operation.project_id
        )
        # Validate policy before any embedding request. Redaction would create a different
        # semantic representation; until indexed separately, only reviewed unredacted mode.
        import os

        from .providers import _prepare_external_evidence

        if os.getenv("EXTERNAL_EVIDENCE_POLICY") != "allow-reviewed":
            raise ValueError("External embedding requires allow-reviewed evidence policy")
        _prepare_external_evidence(
            [
                {
                    "id": representation.id,
                    "quote": representation.text,
                    "classification": source.classification,
                    "sensitivity": "private"
                    if source.classification == "internal"
                    else source.classification,
                }
            ]
        )
    generation = build_generation(
        session, operation.workspace_id, operation.project_id, representation.id, provider
    )
    return {
        "index_generation_id": generation.id,
        "representation_id": representation.id,
        "provider": name,
        "model": provider.model,
        "dimensions": provider.dimensions,
        "usage": getattr(provider, "usage", None),
        "semantic_model": name != "local_hash",
    }


def hybrid_search(
    session,
    workspace_id: str,
    project_id: str,
    query: str,
    generation_ids: list[str],
    provider_name: str = "local_hash",
    limit: int = 20,
):
    from sqlalchemy import select

    from . import domain, models
    from .scholarly import content_scope

    lexical = domain.search(session, workspace_id, project_id, query, limit=100)
    provider = embedding_provider(provider_name)
    generation_ids = list(dict.fromkeys(generation_ids))
    for generation_id in generation_ids:
        generation = domain.scoped(session, models.IndexGeneration, generation_id, workspace_id, project_id)
        if (
            generation.index_kind != "vector"
            or generation.metadata_json.get("model") != provider.model
            or generation.metadata_json.get("dimensions") != provider.dimensions
        ):
            raise ValueError("Query embedding model must match every requested index generation")
    if provider_name == "openai":
        from .providers import _prepare_external_evidence

        _prepare_external_evidence([{"id": "query", "quote": query, "classification": "internal"}])
    query_vector = provider.embed([query])[0] if generation_ids else None
    hits = {hit["block_id"]: hit for hit in lexical["results"]}
    rankings = [[hit["block_id"] for hit in lexical["results"]]]
    for generation in generation_ids:
        vector_hits = search_generation(
            session, workspace_id, project_id, generation, query, provider, 100, query_vector=query_vector
        )
        rankings.append([row["block_id"] for row in vector_hits])
        for row in vector_hits:
            if row["block_id"] in hits:
                continue
            block = domain.scoped(session, models.Block, row["block_id"], workspace_id, project_id)
            rep = domain.scoped(
                session, models.Representation, block.representation_id, workspace_id, project_id
            )
            cap = domain.scoped(session, models.Capture, rep.capture_id, workspace_id, project_id)
            source = domain.scoped(session, models.Source, cap.source_id, workspace_id, project_id)
            doc = session.scalar(
                select(models.Document).where(
                    models.Document.representation_id == rep.id, models.Document.workspace_id == workspace_id
                )
            )
            hits[block.id] = {
                "block_id": block.id,
                "document_id": doc.id,
                "representation_id": rep.id,
                "capture_id": cap.id,
                "source_id": source.id,
                "source_uri": source.canonical_uri,
                "title": doc.title,
                "text": block.text,
                "locator_json": block.locator_json,
                "classification": source.classification,
                "content_scope": content_scope(session, rep, cap),
            }
    ranked = reciprocal_rank_fusion(rankings)
    return {
        "results": [{**hits[key], "score": score} for key, score in ranked[:limit]],
        "mode": "hybrid_rrf",
        "vector_available": bool(generation_ids),
        "embedding_model": provider.model,
        "semantic_model": provider_name != "local_hash",
    }


def main():
    """Operator-only schema initialization, also usable after an earlier migration."""
    import argparse

    from .db import engine

    parser = argparse.ArgumentParser(description="Initialize optional pgvector tables")
    parser.add_argument("--initialize-schema", action="store_true", required=True)
    parser.parse_args()
    if engine.dialect.name != "postgresql":
        raise SystemExit("PostgreSQL is required")
    with engine.begin() as connection:
        create_schema(connection)
    print("pgvector schema initialized; no embedding service called")


if __name__ == "__main__":
    main()
