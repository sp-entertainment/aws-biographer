"""Environment into a frozen dataclass. Read once, at import of `settings()`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    aws_region: str
    # Bedrock: a strong model for reasoning, a cheap one for merges and
    # durability checks, an embedding model for memory bodies and questions.
    model_strong: str
    model_cheap: str
    model_embed: str
    embed_dim: int
    # Set once the read-only role exists. Absent means "use ambient credentials",
    # which is fine for local development and never acceptable in deployment.
    role_arn: str | None
    external_id: str | None


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        database_url=_require("DATABASE_URL"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        model_strong=os.environ.get(
            "MODEL_STRONG", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        ),
        model_cheap=os.environ.get(
            "MODEL_CHEAP", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
        model_embed=os.environ.get("MODEL_EMBED", "amazon.titan-embed-text-v2:0"),
        # Measured from the model, not read from documentation. If MODEL_EMBED
        # changes, this must change with it or every vector index is wrong.
        embed_dim=int(os.environ.get("EMBED_DIM", "1024")),
        role_arn=os.environ.get("BIOGRAPHER_ROLE_ARN") or None,
        external_id=os.environ.get("BIOGRAPHER_EXTERNAL_ID") or None,
    )
