"""Retrieval tests.

Run lexical-only (no embedding API key needed), which keeps them deterministic
and free. The dense half improves ranking; the behaviour asserted here —
correct section retrieved, and *nothing* returned for uncovered questions — must
hold either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import get_settings
from assistant.rag.chunker import chunk_directory, chunk_markdown
from assistant.rag.index import PolicyIndex, build_index
from assistant.rag.retriever import HybridRetriever, tokenize


@pytest.fixture(scope="session")
def policy_dir() -> Path:
    return get_settings().policy_dir


@pytest.fixture(scope="session")
def retriever(policy_dir: Path) -> HybridRetriever:
    index = build_index(policy_dir, use_embeddings=False)
    return HybridRetriever(index)


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_corpus_chunks_on_headings(policy_dir: Path) -> None:
    chunks = chunk_directory(policy_dir)
    assert len(chunks) > 30
    assert all(chunk.text.strip() for chunk in chunks)


def test_every_chunk_has_a_citation(policy_dir: Path) -> None:
    for chunk in chunk_directory(policy_dir):
        assert "#" in chunk.citation
        assert chunk.citation == chunk.citation.lower()


def test_citations_are_unique_enough_to_be_useful(policy_dir: Path) -> None:
    citations = [c.citation for c in chunk_directory(policy_dir)]
    duplicates = {c for c in citations if citations.count(c) > 1}
    assert not duplicates, f"ambiguous citations: {duplicates}"


def test_chunk_carries_document_title(policy_dir: Path) -> None:
    chunks = chunk_markdown(policy_dir / "02-asset-transfer.md")
    assert all(c.document_title == "Asset Transfer and Reassignment Procedure" for c in chunks)
    assert chunks[0].doc_id == "asset-transfer"


def test_tokenizer_drops_stopwords() -> None:
    assert tokenize("What is the refresh cycle for a laptop") == ["refresh", "cycle", "laptop"]


def test_tokenizer_stems_so_plurals_match_singulars() -> None:
    """Without this, a user asking about 'laptops' misses every 'laptop' rule."""
    assert tokenize("laptops refreshed approves policies") == [
        "laptop",
        "refresh",
        "approve",
        "policy",
    ]


# --------------------------------------------------------------------------
# Retrieval quality
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_doc"),
    [
        ("how often are laptops refreshed", "refresh-cycle"),
        ("who approves a transfer between departments", "asset-transfer"),
        ("my laptop was stolen what do I do", "loss-and-damage"),
        ("am I allowed a second laptop", "asset-eligibility"),
        ("what does the AMC cover for printers", "warranty-and-amc"),
        ("do I have to return my monitor when I leave", "offboarding"),
        ("can I take equipment home", "work-from-home-equipment"),
        ("can I transfer an asset that is in repair", "asset-transfer"),
    ],
)
def test_retrieves_the_right_document(retriever: HybridRetriever, query: str, expected_doc: str) -> None:
    hits = retriever.search(query, k=3)
    assert hits, f"no hits for {query!r}"
    assert any(hit.citation.startswith(expected_doc) for hit in hits), (
        f"{query!r} returned {[h.citation for h in hits]}"
    )


def test_top_hit_is_the_right_section(retriever: HybridRetriever) -> None:
    hits = retriever.search("what is the deadline for reporting a stolen laptop", k=3)
    assert hits[0].citation.startswith("loss-and-damage")


def test_results_are_capped_at_k(retriever: HybridRetriever) -> None:
    assert len(retriever.search("laptop", k=2)) <= 2


# --------------------------------------------------------------------------
# The floor: knowing when the corpus does not answer the question.
# --------------------------------------------------------------------------


def test_unrelated_question_returns_nothing(retriever: HybridRetriever) -> None:
    """The guardrail that stops confident wrong policy answers."""
    assert retriever.search("what is the capital of France") == []


def test_plausible_but_uncovered_topic_returns_nothing(retriever: HybridRetriever) -> None:
    """Sounds like HR policy, is not in the corpus — must not be answered from it."""
    assert retriever.search("how many annual leave days do I accrue") == []


@pytest.mark.parametrize(
    "query",
    [
        "who won the cricket match",
        "what is my salary band",
        "how do I claim travel expenses",
    ],
)
def test_out_of_scope_questions_return_nothing(retriever: HybridRetriever, query: str) -> None:
    assert retriever.search(query) == []


def test_empty_query_returns_nothing(retriever: HybridRetriever) -> None:
    assert retriever.search("") == []
    assert retriever.search("the and of") == []


def test_known_limitation_lexical_only_false_positive(retriever: HybridRetriever) -> None:
    """Documents a real weakness rather than hiding it.

    "revenue this quarter" shares the discriminative token 'quarter' with the
    refresh-prioritisation section, so lexical-only retrieval lets it through
    the floor. Raising the floor enough to reject it also rejects legitimate
    questions like "am I allowed a second laptop", which scores similarly — a
    single lexical threshold cannot separate them.

    Two things contain it in practice: the dense half of the hybrid scores the
    pair far apart semantically, and the agent still has to read the passage and
    decide it does not answer the question. Asserted here so the behaviour is
    tracked, not forgotten.
    """
    hits = retriever.search("what is our revenue this quarter")
    assert all(hit.coverage < 0.7 for hit in hits), (
        "a weak lexical match must at least score weakly"
    )


# --------------------------------------------------------------------------
# Index persistence
# --------------------------------------------------------------------------


def test_index_round_trips_through_disk(policy_dir: Path, tmp_path: Path) -> None:
    index = build_index(policy_dir, use_embeddings=False)
    path = tmp_path / "index.json"
    index.save(path)

    reloaded = PolicyIndex.load(path)
    assert len(reloaded.chunks) == len(index.chunks)
    assert reloaded.chunks[0].citation == index.chunks[0].citation


def test_index_without_embeddings_still_serves(policy_dir: Path) -> None:
    index = build_index(policy_dir, use_embeddings=False)
    assert not index.has_embeddings
    assert HybridRetriever(index).search("refresh cycle for monitors")
