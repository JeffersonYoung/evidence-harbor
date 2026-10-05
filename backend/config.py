"""Environment configuration. Production has no implicit authentication bypass."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import URL


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes"}


def configured_database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    if any(os.getenv(key) for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")):
        # SQLAlchemy performs password escaping; never interpolate secrets into URLs.
        url = URL.create(
            "postgresql+psycopg",
            username=os.getenv("DB_USER", "research"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            database=os.getenv("DB_NAME", "research"),
        )
        return url.render_as_string(hide_password=False)
    return "sqlite:///./research.db"


@dataclass
class Settings:
    database_url: str = field(default_factory=configured_database_url)
    blob_dir: Path = field(
        default_factory=lambda: Path(os.getenv("BLOB_DIR", os.getenv("OBJECTS_DIR", "./data/blobs")))
    )
    demo_mode: bool = field(default_factory=lambda: env_bool("DEMO_MODE"))
    demo_workspace_id: str = field(
        default_factory=lambda: os.getenv("DEMO_WORKSPACE_ID", os.getenv("WORKSPACE_ID", "demo"))
    )
    api_tokens: dict[str, str | dict] = field(
        default_factory=lambda: json.loads(os.getenv("API_TOKENS_JSON") or "{}")
    )
    cors_origins: list[str] = field(
        default_factory=lambda: os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
        ).split(",")
    )
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    )
    inline_worker: bool = field(
        default_factory=lambda: env_bool("LOCAL_WORKER", env_bool("INLINE_WORKER", True))
    )
    search_limit: int = 100

    def __post_init__(self):
        from .recovery import check_declared_durability

        admission = check_declared_durability()
        if admission and (not self.database_url.startswith("postgresql") or self.inline_worker):
            raise ValueError("Declared durable runs require PostgreSQL and the Temporal worker mode")
        configured_token = os.getenv("API_TOKEN", "").strip()
        placeholders = {
            "change-me",
            "changeme",
            "replace-me",
            "replace_me",
            "your-api-token",
            "your-token-here",
            "dev-token",
            "development-token",
            "development-only-token",
            "local-dev-token",
            "secret",
            "password",
        }
        if configured_token.lower() in placeholders:
            raise ValueError(
                "API_TOKEN is a known placeholder; use a generated secret or leave blank for account mode"
            )
        if configured_token and not self.api_tokens:
            self.api_tokens = {configured_token: os.getenv("WORKSPACE_ID", "default")}
        if not isinstance(self.api_tokens, dict) or not all(
            isinstance(k, str)
            and k
            and k == k.strip()
            and k.lower() not in placeholders
            and (
                (isinstance(v, str) and v)
                or (
                    isinstance(v, dict)
                    and isinstance(v.get("workspace_id"), str)
                    and v.get("workspace_id")
                    and v.get("role", "admin") in {"reader", "researcher", "editor", "admin"}
                )
            )
            for k, v in self.api_tokens.items()
        ):
            raise ValueError("API_TOKENS_JSON must map bearer tokens to workspace identifiers")


settings = Settings()
