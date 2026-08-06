"""Prompt files: versioned, non-empty, content-hashed (keyless)."""

import pytest

from backline.agents.configs import AGENT_NAMES
from backline.agents.promptfiles import PROMPTS_DIR, load_prompt


def test_every_agent_has_a_prompt_file() -> None:
    for name in (*AGENT_NAMES, "router"):
        prompt = load_prompt(name)
        assert prompt.name == name
        assert len(prompt.text) > 200, f"{name} prompt suspiciously short"
        assert len(prompt.sha256) == 64
        assert prompt.short_hash == prompt.sha256[:12]


def test_hashes_pin_content_and_differ_between_agents() -> None:
    counsel = load_prompt("counsel")
    again = load_prompt("counsel")
    assert counsel.sha256 == again.sha256  # stable (and cached)
    assert counsel.sha256 != load_prompt("analyst").sha256


def test_unknown_prompt_lists_available() -> None:
    with pytest.raises(FileNotFoundError, match="counsel"):
        load_prompt("no-such-agent")


def test_prompt_dir_holds_only_known_prompts() -> None:
    on_disk = {p.stem for p in PROMPTS_DIR.glob("*.md")}
    assert on_disk == {*AGENT_NAMES, "router"}


def test_core_rules_present() -> None:
    """The load-bearing prompt rules tests and evals rely on."""
    counsel = load_prompt("counsel").text
    assert "ABSTAIN:" in counsel
    assert "calc_royalties" in counsel
    assert "<document>" in counsel  # injection-defense framing
    analyst = load_prompt("analyst").text
    assert "ABSTAIN:" in analyst
    assert "single" in analyst  # one round trip for simple asks
    reconciler = load_prompt("reconciler").text
    assert "BATCH:" in reconciler and "FLAGS:" in reconciler
    assert "cannot approve" in reconciler
    router = load_prompt("router").text
    assert "route" in router and "clarify" in router
    # Phase 6 verification: terms-language vs revenue-language examples ("sync rate"
    # → counsel, "how much did X make" → analyst) must stay in the prompt.
    assert "sync rate" in router
    assert "How much" in router
