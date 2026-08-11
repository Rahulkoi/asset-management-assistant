"""Markdown chunking.

Chunks on `##` headings rather than a fixed token window. The policy corpus is
already written in self-contained sections ("Approval requirements", "Return
deadline"), so heading boundaries are semantic boundaries — a fixed window would
cut mid-rule and retrieve half a policy.

The heading also gives every chunk a natural citation, which is what makes
"cite your source" enforceable downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADING_1 = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.M)
HEADING_2 = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.M)

MAX_CHUNK_CHARS = 1800


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    document_title: str
    heading: str
    text: str

    @property
    def citation(self) -> str:
        return f"{self.doc_id}#{_slug(self.heading)}"

    @property
    def indexable_text(self) -> str:
        # Title and heading carry real signal — "refresh cycle", "offboarding" —
        # and are often the only place a query's keyword appears.
        return f"{self.document_title}\n{self.heading}\n{self.text}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def chunk_markdown(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    doc_id = _slug(re.sub(r"^\d+-", "", path.stem))

    title_match = HEADING_1.search(raw)
    document_title = title_match.group("title").strip() if title_match else path.stem

    body_start = title_match.end() if title_match else 0
    body = raw[body_start:]

    matches = list(HEADING_2.finditer(body))
    if not matches:
        text = body.strip()
        return (
            [Chunk(doc_id=doc_id, document_title=document_title, heading="Overview", text=text)]
            if text
            else []
        )

    chunks: list[Chunk] = []

    preamble = body[: matches[0].start()].strip()
    if preamble:
        chunks.append(
            Chunk(
                doc_id=doc_id,
                document_title=document_title,
                heading="Overview",
                text=preamble,
            )
        )

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section = body[match.end() : end].strip()
        if not section:
            continue
        heading = match.group("heading").strip()
        for part in _split_long(section):
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    document_title=document_title,
                    heading=heading,
                    text=part,
                )
            )
    return chunks


def _split_long(text: str) -> list[str]:
    """Split an oversized section on paragraph boundaries, never mid-sentence."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if current and len(current) + len(paragraph) + 2 > MAX_CHUNK_CHARS:
            parts.append(current.strip())
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        parts.append(current.strip())
    return parts


def chunk_directory(directory: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("*.md")):
        chunks.extend(chunk_markdown(path))
    return chunks
