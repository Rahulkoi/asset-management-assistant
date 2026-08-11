"""Hybrid retrieval: BM25 + dense embeddings, fused with reciprocal rank fusion.

Why hybrid. Policy questions arrive in two shapes. "What is the AMC exclusion
age" is lexical — the exact words are in the document, and BM25 nails it while
embeddings can drift to a topically-similar but wrong section. "Can I take my
screen home" is semantic — no word matches "monitor", and only embeddings find
it. Running both and fusing ranks covers both shapes; running either alone has a
visible failure mode.

Why RRF rather than a weighted score blend. BM25 scores are unbounded and
corpus-dependent, cosine sits in [-1, 1]; normalising them onto a shared scale
means picking constants that are really just tuned to this corpus. RRF only
consumes the *ordering* from each retriever, so there is no scale to tune.

The relevance floor is separate from the fusion, and deliberately so: RRF ranks
everything, including a corpus of nothing but wrong answers. The floor is what
lets `search_policy` return no results for a question the policy does not cover,
instead of confidently citing the least-wrong paragraph.

The floor is measured as IDF-weighted term coverage, not as a fraction of the
best BM25 score. Normalising against the best hit is self-defeating — the best
hit is 1.0 by construction, so a relative floor accepts everything, including
"what is the capital of France". Weighting by IDF also makes the floor
discriminating rather than merely strict: matching a rare word like "stolen"
counts for far more than matching "days", and a query term that appears nowhere
in the corpus counts fully against the score.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from assistant.config import get_settings
from assistant.rag.chunker import Chunk
from assistant.rag.index import PolicyIndex, embed_texts

logger = logging.getLogger(__name__)

RRF_K = 60  # standard damping constant; larger = flatter rank weighting

_TOKEN = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    """a an and are as at be been being by can could do does for from get had has have how i if
    in is it its may me much must my no not of often on or our please should so some that the
    their then there these they this to want was we what when where which who why will with
    would you your""".split()
)


def _stem(word: str) -> str:
    """Light suffix stripping, applied to corpus and query alike.

    Not linguistically correct, and it does not need to be — it only has to be
    *consistent*, so that "laptops" and "laptop", or "refreshed" and "refresh",
    land on the same key. Without it, the plural in a user's question simply
    fails to match the singular in the policy, which is a silent recall hole.
    """
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith(("ches", "shes", "xes", "zes", "ses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    return word


def tokenize(text: str) -> list[str]:
    return [
        _stem(token)
        for token in _TOKEN.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


@dataclass(frozen=True)
class Hit:
    citation: str
    document_title: str
    heading: str
    text: str
    score: float
    coverage: float = 0.0
    semantic_score: float = 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class HybridRetriever:
    def __init__(self, index: PolicyIndex) -> None:
        from rank_bm25 import BM25Okapi

        self.index = index
        self.chunks: list[Chunk] = index.chunks
        tokenized = [tokenize(c.indexable_text) for c in self.chunks]
        self._bm25 = BM25Okapi(tokenized)
        self._chunk_terms = [set(tokens) for tokens in tokenized]
        self._idf: dict[str, float] = dict(self._bm25.idf)
        # Weight for a query term that appears nowhere in the corpus. Mean IDF,
        # not max: "this word is absent" is real evidence the question is not
        # covered, but weighting it at the ceiling lets one ordinary unmatched
        # word ("often") veto an otherwise perfect match.
        positive = [value for value in self._idf.values() if value > 0]
        self._unseen_idf = (sum(positive) / len(positive)) if positive else 1.0
        self.settings = get_settings()

    @property
    def uses_embeddings(self) -> bool:
        return self.index.has_embeddings

    def _coverage(self, query_terms: set[str], chunk_index: int) -> float:
        """Share of the query's information content present in this chunk."""
        weights = {term: max(self._idf.get(term, self._unseen_idf), 0.0) for term in query_terms}
        total = sum(weights.values())
        if total <= 0:
            return 0.0
        present = self._chunk_terms[chunk_index]
        matched = sum(weight for term, weight in weights.items() if term in present)
        return matched / total

    def search(self, query: str, k: int | None = None) -> list[Hit]:
        k = k or self.settings.rag_top_k
        tokens = tokenize(query)
        if not tokens:
            return []
        query_terms = set(tokens)

        lexical = list(self._bm25.get_scores(tokens))
        semantic: list[float] = [0.0] * len(self.chunks)
        if self.uses_embeddings:
            query_vector = embed_texts([query], task="question answering")
            if query_vector:
                semantic = [
                    _cosine(query_vector[0], vector) for vector in self.index.embeddings or []
                ]

        # --- fuse orderings -------------------------------------------
        scores: dict[int, float] = {}
        for rank, idx in enumerate(sorted(range(len(lexical)), key=lambda i: -lexical[i])):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        if any(semantic):
            for rank, idx in enumerate(sorted(range(len(semantic)), key=lambda i: -semantic[i])):
                scores[idx] = scores.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)

        ordered = sorted(scores, key=lambda i: -scores[i])

        # --- relevance floor ------------------------------------------
        hits: list[Hit] = []
        for idx in ordered:
            coverage = self._coverage(query_terms, idx)
            passes_lexical = coverage >= self.settings.rag_coverage_floor
            passes_semantic = semantic[idx] >= self.settings.rag_cosine_floor
            if not (passes_lexical or passes_semantic):
                continue
            chunk = self.chunks[idx]
            hits.append(
                Hit(
                    citation=chunk.citation,
                    document_title=chunk.document_title,
                    heading=chunk.heading,
                    text=chunk.text,
                    score=scores[idx],
                    coverage=round(coverage, 4),
                    semantic_score=round(semantic[idx], 4),
                )
            )
            if len(hits) >= k:
                break
        return hits


def build_retriever(index: PolicyIndex | None = None) -> HybridRetriever:
    from assistant.rag.index import load_or_build

    return HybridRetriever(index or load_or_build())
