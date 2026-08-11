"""Central configuration.

Everything tunable lives here so the runtime never reads os.environ directly and
tests can override a single object.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Provider -------------------------------------------------------
    # The agent runtime talks to LLMClient, never to a vendor SDK. Switching
    # provider is a config change, not a refactor.
    llm_provider: str = Field(default="gemini", description="gemini | openai_compat")
    llm_model: str = Field(default="gemini-3.6-flash")
    embedding_model: str = Field(default="gemini-embedding-2")
    embedding_dimensions: int = Field(default=768)

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")

    # OpenAI-compatible fallback (Groq / OpenRouter / any compatible gateway)
    openai_compat_api_key: str | None = Field(default=None)
    openai_compat_base_url: str = Field(default="https://api.groq.com/openai/v1")
    openai_compat_model: str = Field(default="llama-3.3-70b-versatile")

    # --- Paths ----------------------------------------------------------
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    db_path: Path = Field(default=PROJECT_ROOT / "data" / "assets.db")
    source_xlsx: Path = Field(default=PROJECT_ROOT / "data" / "Sample_Asset_Master.xlsx")
    policy_dir: Path = Field(default=PROJECT_ROOT / "docs" / "policies")
    policy_index_path: Path = Field(default=PROJECT_ROOT / "data" / "policy_index.json")
    trace_path: Path = Field(default=PROJECT_ROOT / "data" / "traces.jsonl")

    # --- Agent loop budgets (guardrail: bounded work per turn) ----------
    max_tool_calls_per_turn: int = Field(default=8)
    max_loop_iterations: int = Field(default=6)
    turn_timeout_seconds: float = Field(default=90.0)
    max_user_message_chars: int = Field(default=2000)
    max_rows_per_query: int = Field(default=50)

    # --- Rate limiting (guardrail: per-session token bucket) ------------
    rate_limit_requests: int = Field(default=20)
    rate_limit_window_seconds: float = Field(default=60.0)

    # --- Writes ---------------------------------------------------------
    confirmation_ttl_seconds: float = Field(default=300.0)

    # --- Retrieval ------------------------------------------------------
    # Two floors rather than one: a chunk is worth returning if it either shares
    # real vocabulary with the query (lexical) or is semantically close
    # (dense). Below both, `search_policy` returns nothing, which is what stops
    # the agent answering policy questions the corpus does not cover.
    rag_top_k: int = Field(default=4)
    rag_coverage_floor: float = Field(
        default=0.45, description="Share of the query's IDF-weighted terms the chunk must contain."
    )
    # Measured against gemini-embedding-2 at 768 dimensions on this corpus:
    # in-scope questions bottom out at 0.698, out-of-scope questions top out at
    # 0.658 ("how many annual leave days do I get?"). 0.68 sits in that gap.
    #
    # The margin is 0.04, which is thin, and worth stating rather than hiding:
    # the floor is tuned to this corpus and this embedding model, and both
    # numbers should be re-measured if either changes. The lexical coverage
    # floor is the more robust of the two guards; this one exists to catch the
    # semantically-phrased questions coverage cannot see, and the two are
    # combined with OR so a question only has to convince one of them.
    rag_cosine_floor: float = Field(default=0.68)
    rag_use_embeddings: bool = Field(default=True)

    # --- Conversation memory --------------------------------------------
    max_history_turns: int = Field(default=12)

    @property
    def resolved_gemini_key(self) -> str | None:
        return self.gemini_api_key or self.google_api_key

    @property
    def active_model(self) -> str:
        """The model actually being called, which depends on the provider.

        `llm_model` only ever describes the Gemini path. Reporting it while
        running on an OpenAI-compatible endpoint names a model that was never
        called — misleading in /healthz and in the UI, where it is the one
        place a reviewer looks to see what answered them.
        """
        if self.llm_provider.lower() in {"openai_compat", "groq", "openrouter"}:
            return self.openai_compat_model
        return self.llm_model

    @property
    def embeddings_available(self) -> bool:
        """Dense retrieval needs a Gemini key regardless of the chat provider.

        Embeddings are a Gemini-only capability here, so running the agent on
        Groq leaves retrieval lexical-only unless a Gemini key is also present.
        """
        return bool(self.rag_use_embeddings and self.resolved_gemini_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
