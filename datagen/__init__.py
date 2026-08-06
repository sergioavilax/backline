"""datagen — the deterministic Foldback Records universe (BUILD_PLAN §3).

Builds the synthetic label (catalog, contracts, statements, anomalies) and its answer
key from one seed. Framed as a mock distributor/DSP feed: monthly statement drops land
in ``/data/inbox`` exactly like a real feed lands.
"""
