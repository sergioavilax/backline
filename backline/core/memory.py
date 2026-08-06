"""Memory model, Phase 2 scopes (BUILD_PLAN §4.5).

Two of the three scopes ship here, deliberately boring and legible:

- ``SessionMemory`` — rolling conversation window; overflow folds into a running
  summary via a pluggable summarizer (the utility model plugs in when Phase 6's API
  constructs sessions — D-014; without one, overflow elides with a deterministic
  note so keyless paths stay honest).
- ``WorkingMemory`` — per-run scratchpad of tool results, deduplicated by content hash
  (the Prometheus lesson): an identical result re-fetched mid-run enters the context
  once; repeats become a short pointer.

Long-term notes (``save_note``/``recall_notes``) are Phase 3 tools.
"""

import hashlib
from collections.abc import Awaitable, Callable, Sequence

from pydantic import BaseModel, ConfigDict

from backline.providers.base import Message

Summarizer = Callable[[Sequence[Message]], Awaitable[str]]


class SessionMemory:
    """Rolling window over a conversation, with a summarization hook past ``window``."""

    def __init__(self, *, window: int = 20, summarizer: Summarizer | None = None) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        self._window = window
        self._summarizer = summarizer
        self._messages: list[Message] = []
        self._summary = ""
        self._elided = 0

    def add(self, message: Message) -> None:
        self._messages.append(message)

    def note_elided(self, count: int) -> None:
        """Record messages elided *before* they reached this window (Phase 6: the API
        rebuilds sessions from a SQL ``LIMIT`` — history past the window never loads,
        but the context header must still say so honestly)."""
        if count < 0:
            raise ValueError("count must be >= 0")
        self._elided += count

    async def context(self) -> list[Message]:
        """The window-bounded conversation: [summary?] + the most recent messages."""
        await self._compact()
        if not self._summary and not self._elided:
            return list(self._messages)
        note = (
            self._summary
            if self._summary
            else f"{self._elided} earlier message(s) elided to fit the context window"
        )
        header = Message(
            role="user",
            content=f"<conversation_summary>\n{note}\n</conversation_summary>",
        )
        return [header, *self._messages]

    async def _compact(self) -> None:
        if len(self._messages) <= self._window:
            return
        overflow = self._messages[: -self._window]
        self._messages = self._messages[-self._window :]
        if self._summarizer is None:
            self._elided += len(overflow)
            return
        fold: list[Message] = []
        if self._summary:
            fold.append(Message(role="user", content=self._summary))
        fold.extend(overflow)
        self._summary = await self._summarizer(fold)


class WorkedResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    deduped: bool


class WorkingEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int  # 1-based, in arrival order of *distinct* results
    tool: str
    content_hash: str


class WorkingMemory:
    """Per-run tool-result scratchpad with content-hash dedup."""

    def __init__(self) -> None:
        self.entries: list[WorkingEntry] = []
        self._seen: dict[str, WorkingEntry] = {}

    @staticmethod
    def _hash(tool: str, content: str) -> str:
        return hashlib.sha256(f"{tool}\x00{content}".encode()).hexdigest()

    def record(self, tool: str, content: str) -> WorkedResult:
        digest = self._hash(tool, content)
        hit = self._seen.get(digest)
        if hit is not None:
            marker = f"[duplicate of {hit.tool} result #{hit.index} — identical content elided]"
            return WorkedResult(content=marker, deduped=True)
        entry = WorkingEntry(index=len(self.entries) + 1, tool=tool, content_hash=digest)
        self.entries.append(entry)
        self._seen[digest] = entry
        return WorkedResult(content=content, deduped=False)
