# LOCAL.md — the local-model row (one Linux boot, then done)

The sweep's fourth row is a local Qwen served by vLLM on the 4090 rig
(BUILD_PLAN §7). It is optional-but-wanted: the report and the README table
degrade gracefully to API-only, and `benchmarks/results/REPORT.md` lists the row
as *pending* until `local-qwen.json` lands. Everything below is the exact
procedure; budget one boot into Ubuntu and about an hour of wall clock.

## 1. Serve the model (on the rig)

One boot into Ubuntu on the 4090 rig. Known-good vLLM flags for the Qwen3 family:

```bash
docker run --gpus '"device=0"' -p 8000:8000 vllm/vllm-openai:latest \
  --model <Qwen3.x-AWQ-INT4> --gpu-memory-utilization 0.97 --max-model-len 8192 \
  --enforce-eager --tool-call-parser qwen3_xml --reasoning-parser qwen3
```

- **The `hermes` tool-call parser silently fails on this family — do not use.**
  Symptom: completions come back as prose with the tool call embedded in text,
  every eval question scores as a tool-format failure, and nothing errors.
- `--max-model-len 8192` matches `context_window: 8192` for `local-qwen` in
  `config/models.yaml`. If you serve a longer context, bump both together.
- Sanity check from the dev machine before spending an hour:
  `curl http://<rig-ip>:8000/v1/models` should list the model.

## 2. Run the row (from the repo, dev machine)

The sweep needs the seeded world (`make seed && make embed`) and — because the
T3 judge stays pinned to `claude-sonnet-5` for score comparability across rows
(D-031) — `ANTHROPIC_API_KEY` in `.env`. Judge spend for the row is ≈ $0.35
(≈ $0.50 once sonnet's standard tier starts on 2026-09-01); the model's own
cost is honest zero (electricity is free at this scale).

```bash
OPENAI_COMPAT_BASE_URL=http://<rig-ip>:8000/v1 \
  python benchmarks/run_sweep.py --model local-qwen --budget 0 --yes
```

- `--budget 0` = **uncapped** at the sweep layer: the row is zero-priced in the
  registry, so a dollar gate would either be meaningless or (taken literally by
  the runner's `spent + reserved >= budget` stop) skip every question.
- No key on hand? `--no-judge` runs T1+T2 only — the row still measures accuracy
  and tool reliability, but its scores stop being comparable with the judged API
  rows and the report should say so wherever you cite them.
- Interrupted (rig fell over, vLLM OOM)? Re-run the same command — sweep state
  (`data/benchmarks/sweep_state.json`) remembers the eval run and the runner
  skips every already-scored question. `--fresh` starts over deliberately.

## 3. Land the results

```bash
# The row's artifact is written by the sweep:
#   benchmarks/results/local-qwen.json
python benchmarks/report.py     # regenerate REPORT.md + comparison.svg with the row
```

Commit `benchmarks/results/local-qwen.json` plus the regenerated report
artifacts; add the row's numbers to `docs/BENCHMARK_NOTES.md` (the local-model
section is pre-structured for them). Reboot to Windows; done with Linux forever.
