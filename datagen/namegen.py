"""Deterministic name generation from the world.yaml pools.

Every draw takes the caller's ``Generator``; uniqueness is enforced by retry, which is
deterministic given the generator's sequence.
"""

from __future__ import annotations

import numpy as np

from datagen.config import NamePools


def _pick(gen: np.random.Generator, pool: tuple[str, ...]) -> str:
    return pool[int(gen.integers(0, len(pool)))]


def legal_name(gen: np.random.Generator, pools: NamePools) -> str:
    return f"{_pick(gen, pools.given)} {_pick(gen, pools.surname)}"


def stage_name(gen: np.random.Generator, pools: NamePools, legal: str) -> str:
    pattern = int(gen.integers(0, 6))
    if pattern == 0:
        return f"{_pick(gen, pools.stage_adjectives)} {_pick(gen, pools.stage_nouns)}"
    if pattern == 1:
        return f"The {_pick(gen, pools.stage_adjectives)} {_pick(gen, pools.stage_nouns)}"
    if pattern == 2:
        return _pick(gen, pools.stage_nouns)
    if pattern == 3:
        return f"{_pick(gen, pools.given)} {_pick(gen, pools.stage_nouns)}"
    if pattern == 4:
        return f"{_pick(gen, pools.given)} & The {_pick(gen, pools.stage_nouns)}"
    return legal  # performs under their legal name


def unique_stage_name(
    gen: np.random.Generator, pools: NamePools, legal: str, taken: set[str]
) -> str:
    for _ in range(64):
        candidate = stage_name(gen, pools, legal)
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError("stage-name pool exhausted — grow the pools in world.yaml")


def release_title(gen: np.random.Generator, pools: NamePools, taken: set[str]) -> str:
    for _ in range(64):
        pattern = int(gen.integers(0, 5))
        a, b = _pick(gen, pools.title_a), _pick(gen, pools.title_b)
        if pattern == 0:
            candidate = f"{a} {b}"
        elif pattern == 1:
            candidate = f"{b} & {_pick(gen, pools.title_b)}"
        elif pattern == 2:
            candidate = f"{a} {b} EP"
        elif pattern == 3:
            candidate = f"Live at {_pick(gen, pools.places)}"
        else:
            candidate = b
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError("release-title pool exhausted — grow the pools in world.yaml")


def compilation_title(gen: np.random.Generator, pools: NamePools, volume: int) -> str:
    theme = f"{_pick(gen, pools.title_a)} {_pick(gen, pools.title_b)}"
    return f"{theme}: A Foldback Collection, Vol. {volume}"


def track_title(gen: np.random.Generator, pools: NamePools, taken: set[str]) -> str:
    for _ in range(64):
        pattern = int(gen.integers(0, 4))
        a, b = _pick(gen, pools.title_a), _pick(gen, pools.title_b)
        if pattern == 0:
            candidate = f"{a} {b}"
        elif pattern == 1:
            candidate = f"{b} ({a})"
        elif pattern == 2:
            candidate = f"{a} {b}, Pt. {int(gen.integers(1, 4))}"
        else:
            candidate = b
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError("track-title pool exhausted for one release — grow the pools")
