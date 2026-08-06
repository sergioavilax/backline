"""The Phase 2 demo script must keep working end-to-end, keylessly."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dev_run_completes_and_writes_a_jsonl_trace(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL",)}
    env["DATA_DIR"] = str(tmp_path)  # keep the demo's trace out of the working tree
    env["ANTHROPIC_API_KEY"] = ""  # prove the demo is keyless

    proc = subprocess.run(
        [sys.executable, "scripts/dev_run.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert "status=completed" in proc.stdout
    assert "final answer: Nova Reyes" in proc.stdout
    assert "iteration:1" in proc.stdout and "iteration:3" in proc.stdout

    match = re.search(r"run ([0-9a-f-]{36})", proc.stdout)
    assert match is not None
    trace = tmp_path / "traces" / f"{match.group(1)}.jsonl"
    assert trace.exists()
    lines = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "run_start"
    assert lines[-1]["type"] == "run_end"
    assert lines[-1]["run"]["status"] == "completed"
    kinds = [ln["span"]["kind"] for ln in lines if ln["type"] == "span_end"]
    assert kinds.count("iteration") == 3
    assert kinds.count("llm_call") == 3
    assert kinds.count("tool_call") == 2
