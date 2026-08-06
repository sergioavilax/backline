"""World assembly: structure -> revenue -> anomalies -> statements -> answer key.

``build_world`` is pure and deterministic: everything in memory, no I/O, one seed.
"""

from __future__ import annotations

from dataclasses import dataclass

from datagen.anomalies import apply_anomalies
from datagen.config import WorldConfig
from datagen.revenue import Cells, build_cells, build_statements, synth_period
from datagen.rng import world_generator
from datagen.truthengine import TruthEngine
from datagen.world import Structure, build_structure
from datagen.worldmodel import World


@dataclass
class BuiltWorld:
    structure: Structure
    cells: Cells
    world: World


def build_world(config: WorldConfig, seed: int) -> BuiltWorld:
    gen = world_generator(seed)
    structure = build_structure(config, seed, gen)
    cells = build_cells(structure, gen)
    world = structure.world

    clean_by_period = {
        pidx: synth_period(structure, cells, pidx) for pidx in range(config.n_periods)
    }
    outcome = apply_anomalies(structure, cells, clean_by_period, gen)

    world.statements = build_statements(structure, list(range(config.n_periods)), "ingested")
    world.statement_lines = outcome.dirty_lines
    world.clean_lines = sorted(
        [line for rows in clean_by_period.values() for line in rows] + outcome.clean_additions,
        key=lambda line: line.id,
    )
    world.dashboard_streams = outcome.dashboard
    world.anomalies = outcome.registry
    world.ledger = TruthEngine(structure).compute_ledger(world.clean_lines)
    return BuiltWorld(structure=structure, cells=cells, world=world)
