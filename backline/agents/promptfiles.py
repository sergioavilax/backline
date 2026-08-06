"""Versioned agent prompts (Phase 4): files under ``prompts/``, content-hashed.

The prompt *is* the file — no runtime templating — so one sha256 of the bytes pins
exactly what the agent saw. The hash rides into run meta via ``AgentSpec.trace_attrs``
(``prompt_sha256``), which is how Phase 5 eval results pin to prompt versions: change
a prompt file and every subsequent run/result records the new hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Trace attrs carry the short prefix; the full hex lives here for exact pinning.
_SHORT_HASH_CHARS = 12


@dataclass(frozen=True)
class AgentPrompt:
    name: str
    text: str
    sha256: str

    @property
    def short_hash(self) -> str:
        return self.sha256[:_SHORT_HASH_CHARS]


@cache
def load_prompt(name: str) -> AgentPrompt:
    """Load ``prompts/{name}.md`` and pin its content hash. Unknown names list what exists."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))) or "(none)"
        raise FileNotFoundError(f"no prompt file {path.name!r} — available: {available}")
    raw = path.read_bytes()
    text = raw.decode("utf-8").strip()
    if not text:
        raise ValueError(f"prompt file {path.name!r} is empty")
    return AgentPrompt(name=name, text=text, sha256=hashlib.sha256(raw).hexdigest())
