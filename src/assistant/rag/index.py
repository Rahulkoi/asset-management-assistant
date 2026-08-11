"""Policy index construction and persistence.

Embeddings are computed once at build time and cached to disk, so serving a
query costs a single embed call rather than re-embedding the corpus. If no API
key is available — or the embedding call fails — the index still builds and the
retriever falls back to lexical-only search rather than refusing to start.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from assistant.config import get_settings
from assistant.rag.chunker import Chunk, chunk_directory

logger = logging.getLogger(__name__)


@dataclass
class PolicyIndex:
    chunks: list[Chunk]
    embeddings: list[list[float]] | None = None
    model: str | None = None

    @property
    def has_embeddings(self) -> bool:
        return bool(self.embeddings) and len(self.embeddings or []) == len(self.chunks)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self.model,
            "chunks": [
                {
                    "doc_id": c.doc_id,
                    "document_title": c.document_title,
                    "heading": c.heading,
                    "text": c.text,
                }
                for c in self.chunks
            ],
            "embeddings": self.embeddings if self.has_embeddings else None,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PolicyIndex:
        payload = json.loads(path.read_text(encoding="utf-8"))
        chunks = [Chunk(**item) for item in payload["chunks"]]
        return cls(chunks=chunks, embeddings=payload.get("embeddings"), model=payload.get("model"))


def embed_texts(texts: list[str], *, task: str = "search result") -> list[list[float]] | None:
    """Embed with Gemini. Returns None if unavailable — never raises.

    A missing embedding backend degrades retrieval quality; it should not take
    the whole assistant down.
    """
    settings = get_settings()
    if not settings.rag_use_embeddings or not settings.resolved_gemini_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.resolved_gemini_key)
        vectors: list[list[float]] = []
        # Batched to stay well inside free-tier request limits.
        for start in range(0, len(texts), 32):
            batch = texts[start : start + 32]
            result = client.models.embed_content(
                model=settings.embedding_model,
                contents=batch,
                config=types.EmbedContentConfig(
                    output_dimensionality=settings.embedding_dimensions
                ),
            )
            for item in result.embeddings:
                values = getattr(item, "values", None) or getattr(item, "embedding", None)
                vectors.append(list(values))
        return vectors if len(vectors) == len(texts) else None
    except Exception:  # noqa: BLE001 - optional capability
        logger.warning("Embedding backend unavailable; using lexical search only", exc_info=True)
        return None


def build_index(policy_dir: Path | None = None, use_embeddings: bool | None = None) -> PolicyIndex:
    settings = get_settings()
    directory = policy_dir or settings.policy_dir
    chunks = chunk_directory(directory)
    if not chunks:
        raise ValueError(f"No policy documents found in {directory}")

    embeddings = None
    if use_embeddings if use_embeddings is not None else settings.rag_use_embeddings:
        embeddings = embed_texts([c.indexable_text for c in chunks])

    return PolicyIndex(
        chunks=chunks,
        embeddings=embeddings,
        model=settings.embedding_model if embeddings else None,
    )


def load_or_build(
    index_path: Path | None = None, policy_dir: Path | None = None
) -> PolicyIndex:
    settings = get_settings()
    path = index_path or settings.policy_index_path
    directory = policy_dir or settings.policy_dir

    if path.exists():
        try:
            index = PolicyIndex.load(path)
            # Rebuild if the corpus changed under a stale index.
            if len(index.chunks) == len(chunk_directory(directory)):
                return index
            logger.info("Policy corpus changed; rebuilding index")
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Policy index unreadable; rebuilding", exc_info=True)

    index = build_index(directory)
    index.save(path)
    return index
