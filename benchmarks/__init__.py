"""Phase 7 model benchmark sweep (BUILD_PLAN §7).

``run_sweep.py`` iterates the committed model matrix over the full eval suite and
distills each run into ``benchmarks/results/{model}.json``; ``report.py`` renders
the results into the README table + comparison chart. ``LOCAL.md`` is the turnkey
procedure for the optional local-model row.
"""
