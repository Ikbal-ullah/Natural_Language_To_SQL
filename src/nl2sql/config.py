"""Configuration.

Every threshold that governs safety lives here rather than being hardcoded inside the
component that enforces it, so that the evaluation harness can sweep them and so that
tightening a limit is a config change rather than a code change.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- connections -------------------------------------------------------
    admin_dsn: str = Field(
        default="postgresql://localhost:5432/nl2sql",
        alias="NL2SQL_ADMIN_DSN",
        description="Owner connection. Used for migrations, seeding and index builds only.",
    )
    readonly_dsn: str = Field(
        default="postgresql://nl2sql_ro@localhost:5432/nl2sql",
        alias="NL2SQL_READONLY_DSN",
        description="Connection used to execute every generated query. Never the owner.",
    )

    # --- defence in depth --------------------------------------------------
    statement_timeout_ms: int = Field(default=3_000, alias="NL2SQL_STATEMENT_TIMEOUT_MS")
    max_plan_cost: float = Field(
        default=5_000_000.0,
        alias="NL2SQL_MAX_PLAN_COST",
        description="Reject a query whose estimated total cost exceeds this.",
    )
    large_table_rows: int = Field(
        default=100_000,
        alias="NL2SQL_LARGE_TABLE_ROWS",
        description="A sequential scan over a relation with at least this many live rows "
        "is treated as a full scan of a large table by the plan gate.",
    )
    max_limit: int = Field(
        default=1_000,
        alias="NL2SQL_MAX_LIMIT",
        description="Largest LIMIT a generated query is allowed to carry.",
    )
    default_limit: int = Field(default=100, alias="NL2SQL_DEFAULT_LIMIT")

    # --- domain ------------------------------------------------------------
    domain: str = Field(
        default="learning",
        alias="NL2SQL_DOMAIN",
        description="Which prompt domain to use: 'learning' (original 5M-row DB) or 'ecommerce'.",
    )
    schema_linking: bool = Field(
        default=True,
        alias="NL2SQL_SCHEMA_LINKING",
        description="Send only the question-relevant tables (BM25 + 1-hop FK) to the model.",
    )
    link_top_k: int = Field(default=2, alias="NL2SQL_LINK_TOP_K")
    link_max_tables: int = Field(default=5, alias="NL2SQL_LINK_MAX_TABLES")

    # --- pipeline ----------------------------------------------------------
    max_retries: int = Field(
        default=2,
        alias="NL2SQL_MAX_RETRIES",
        description="Self-correction attempts after the first. Bounded on purpose: an "
        "unbounded loop is a way to spend money without converging.",
    )
    few_shot_count: int = Field(default=4, alias="NL2SQL_FEW_SHOT_COUNT")
    schema_format: str = Field(default="ddl", alias="NL2SQL_SCHEMA_FORMAT")
    prompt_strategy: str = Field(default="structured", alias="NL2SQL_PROMPT_STRATEGY")

    # --- llm ---------------------------------------------------------------
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    model: str = Field(default="claude-opus-5", alias="NL2SQL_MODEL")
    max_tokens: int = Field(
        default=8_000,
        alias="NL2SQL_MAX_TOKENS",
        description="Adaptive thinking tokens count against this, so it must be well "
        "above the size of the SQL we expect back.",
    )
    effort: str = Field(
        default="medium",
        alias="NL2SQL_EFFORT",
        description="low | medium | high | xhigh | max. Text-to-SQL over a known schema "
        "does not need the top of the range; the sweep measures the tradeoff.",
    )
    llm_provider: str = Field(
        default="anthropic",
        alias="NL2SQL_LLM_PROVIDER",
        description="'anthropic' for the real model; 'retrieval' for the offline "
        "deterministic baseline used when no API key is present.",
    )

    # --- pool --------------------------------------------------------------
    pool_min_size: int = Field(default=1, alias="NL2SQL_POOL_MIN")
    pool_max_size: int = Field(default=8, alias="NL2SQL_POOL_MAX")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
