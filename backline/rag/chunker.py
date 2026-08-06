"""Clause-aware contract chunking (§4.4): chunks *are* clauses, never token windows.

The PDF renderer emits a deterministic structure (title line, then §-numbered clause
headings, paragraphs, blank separators — see ``datagen/pdfrender.py``); its ``.txt``
sidecar is the canonical text this module parses. Clause numbers carry through to
retrieval results and citations, so the split must be exact: a heading only ever starts
a clause at line start (``§3 (Royalties)`` *inside* an amendment body stays body text).

Oversize clauses split into ``part``-numbered chunks on paragraph boundaries (hard
split only within a single oversize paragraph); parts reassemble the clause verbatim.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# A clause heading as rendered: '§' + optional amendment letter + number + '.', then the
# title. Anchored at line start; body references like '§3 (Royalties)' don't match.
_HEADING = re.compile(r"^(§[A-Z]?\d+)\.\s+\S")

DEFAULT_MAX_CHARS = 2000  # ~500 tokens: inside bge-small's window with headroom


@dataclass(frozen=True)
class ClauseChunk:
    clause_no: str  # 'title' | '§1'..'§8' | '§A1', '§A2', '§A9'
    part: int
    heading: str
    content: str
    content_hash: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_content(content: str, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in content.split("\n"):
        while len(paragraph) > max_chars:  # one paragraph alone exceeds the cap
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            parts.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        extra = len(paragraph) + (1 if current else 0)
        if size + extra > max_chars and current:
            parts.append("\n".join(current))
            current, size = [], 0
            extra = len(paragraph)
        current.append(paragraph)
        size += extra
    if current:
        parts.append("\n".join(current))
    return parts


def chunk_document(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[ClauseChunk]:
    """Split one contract document into clause chunks, in document order."""
    clauses: list[tuple[str, str, list[str]]] = []  # (clause_no, heading, body lines)
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _HEADING.match(line)
        if match:
            clauses.append((match.group(1), line, []))
        elif not clauses:
            if line:
                clauses.append(("title", line, []))
            # leading blank lines before any heading: ignore
        elif line or clauses[-1][2]:  # skip leading blanks inside a clause
            clauses[-1][2].append(line)

    chunks: list[ClauseChunk] = []
    for clause_no, heading, body in clauses:
        while body and not body[-1]:
            body.pop()
        content = "\n".join(line for line in body if line)
        for part, piece in enumerate(_split_content(content, max_chars)):
            chunks.append(
                ClauseChunk(
                    clause_no=clause_no,
                    part=part,
                    heading=heading,
                    content=piece,
                    content_hash=_hash(piece),
                )
            )
    return chunks
