"""The only place datagen constructs randomness (BUILD_PLAN §0 invariant 4).

Everything derives from ``WORLD_SEED`` through named ``SeedSequence`` streams:

- the *world stream* drives all structural generation (catalog, deals, activity cells,
  the seeded-window anomaly plan), and
- one *period stream* per statement month drives that month's volume jitter, so
  ``datagen emit-period 2026-07`` reproduces its month exactly without replaying the
  seeded window's draws.

A test greps ``datagen/`` to keep ``random`` / ``np.random`` calls out of every other
module — new randomness must take a ``Generator`` as a parameter.
"""

import numpy as np

_WORLD_STREAM = 0
_PERIOD_STREAM = 1
_EMIT_ANOMALY_STREAM = 2


def world_generator(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, _WORLD_STREAM])))


def period_generator(seed: int, absolute_period_index: int) -> np.random.Generator:
    return np.random.Generator(
        np.random.PCG64(np.random.SeedSequence([seed, _PERIOD_STREAM, absolute_period_index]))
    )


def emit_anomaly_generator(seed: int, absolute_period_index: int) -> np.random.Generator:
    """Stream for the unregistered anomalies injected into `emit-period` drops."""
    return np.random.Generator(
        np.random.PCG64(np.random.SeedSequence([seed, _EMIT_ANOMALY_STREAM, absolute_period_index]))
    )
