"""Corpus token counting — evidence for the "context stuffing is impossible" claim (§3.1).

Counts tokens over the agent-facing corpus: contract text (the .txt renderings of the
PDFs) and the raw statement CSV drops. Uses tiktoken's ``o200k_base`` when its encoding
file is available (first use downloads it); falls back to a clearly-labeled bytes/4
estimate offline, because a sandboxed machine without egress should still get numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONTEXT_WINDOW = 200_000  # tokens; the frontier-model reference window for the claim


@dataclass
class CorpusCount:
    method: str  # "tiktoken:o200k_base" | "estimate:bytes/4"
    contract_files: int
    contract_bytes: int
    contract_tokens: int
    statement_files: int
    statement_bytes: int
    statement_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.contract_tokens + self.statement_tokens

    @property
    def window_multiple(self) -> float:
        return self.total_tokens / CONTEXT_WINDOW


def _count_group(paths: list[Path], encode: object | None) -> tuple[int, int]:
    """-> (bytes, tokens) for a file group."""
    total_bytes = 0
    total_tokens = 0
    for path in paths:
        data = path.read_bytes()
        total_bytes += len(data)
        if encode is not None:
            total_tokens += len(encode.encode(data.decode("utf-8", errors="replace")))  # type: ignore[attr-defined]
        else:
            total_tokens += (len(data) + 3) // 4
    return total_bytes, total_tokens


def count_corpus(data_dir: Path) -> CorpusCount:
    contract_txt = sorted((data_dir / "contracts" / "txt").glob("*.txt"))
    inbox = sorted(p for p in (data_dir / "inbox").glob("*") if p.is_file())
    if not contract_txt and not inbox:
        raise FileNotFoundError(
            f"no corpus under {data_dir} — run `make seed` (datagen seed) first"
        )

    encoding: object | None = None
    method = "estimate:bytes/4 (tiktoken encoding unavailable offline)"
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("o200k_base")
        method = "tiktoken:o200k_base"
    except Exception:
        encoding = None

    contract_bytes, contract_tokens = _count_group(contract_txt, encoding)
    statement_bytes, statement_tokens = _count_group(inbox, encoding)
    return CorpusCount(
        method=method,
        contract_files=len(contract_txt),
        contract_bytes=contract_bytes,
        contract_tokens=contract_tokens,
        statement_files=len(inbox),
        statement_bytes=statement_bytes,
        statement_tokens=statement_tokens,
    )


def format_report(count: CorpusCount) -> str:
    lines = [
        f"corpus tokens ({count.method})",
        f"  contracts:  {count.contract_files:>5} files  "
        f"{count.contract_bytes / 1_048_576:>8.1f} MiB  {count.contract_tokens:>12,} tokens",
        f"  statements: {count.statement_files:>5} files  "
        f"{count.statement_bytes / 1_048_576:>8.1f} MiB  {count.statement_tokens:>12,} tokens",
        f"  total:      {count.total_tokens:>12,} tokens"
        f"  = {count.window_multiple:.1f}x a {CONTEXT_WINDOW:,}-token context window",
    ]
    return "\n".join(lines)
