"""Seeded-RNG discipline (BUILD_PLAN §0 invariant 4, §9).

Everything random in datagen flows through the one seeded Generator constructed in
``datagen/rng.py``. This grep-based test keeps it that way: no ``random`` module, no
``np.random.*`` constructors/calls, and no wall-clock reads anywhere else in the package.
"""

import re
from pathlib import Path

DATAGEN_DIR = Path(__file__).resolve().parents[2] / "datagen"

STDLIB_RANDOM = re.compile(r"^\s*(?:import random\b|from random import)|(?<![.\w])random\.\w")
WALL_CLOCK = re.compile(r"\.now\(\)|\.today\(\)|time\.time\(")


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(DATAGEN_DIR.glob("*.py"))}


def test_np_random_only_in_rng_module() -> None:
    for name, text in _sources().items():
        if name == "rng.py":
            continue
        # The Generator *type annotation* is the one allowed np.random reference.
        stripped = text.replace("np.random.Generator", "").replace("numpy.random.Generator", "")
        offending = [
            line
            for line in stripped.splitlines()
            if "np.random." in line or "numpy.random." in line
        ]
        assert not offending, f"{name} touches np.random directly: {offending}"


def test_stdlib_random_never_used() -> None:
    for name, text in _sources().items():
        offending = [line for line in text.splitlines() if STDLIB_RANDOM.search(line)]
        assert not offending, f"{name} uses the stdlib random module: {offending}"


def test_no_wall_clock_reads() -> None:
    for name, text in _sources().items():
        offending = [line for line in text.splitlines() if WALL_CLOCK.search(line)]
        assert not offending, f"{name} reads the wall clock (breaks determinism): {offending}"


def test_time_module_only_for_progress_reporting() -> None:
    # cli.py may time its own steps (perf_counter); nothing else imports time at all.
    for name, text in _sources().items():
        if name == "cli.py":
            assert "time.time(" not in text
            continue
        assert not re.search(r"^\s*import time\b", text, re.M), f"{name} imports time"
