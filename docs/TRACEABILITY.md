# Traceability — job-spec requirement → artifact

Maintained from [BUILD_PLAN.md](../BUILD_PLAN.md) §1. Every listing requirement maps to a
concrete artifact in this repo. Column 3 tracks build status as phases land.

| Listing requirement | Where it lives in Backline | Status |
|---|---|---|
| "Architect AI agents and the orchestration, tool-use, memory, and routing patterns **they share** — building toward a cohesive **agent platform**" | `backline/core/` shared primitives; three agents (`counsel`, `analyst`, `reconciler`) built on identical primitives; `router` front door | Phase 2 core primitives (runtime loop, trace, cost, guardrail frame, memory) ✅ · agents + router Phase 4 — pending |
| "RAG pipelines, retrieval architectures, semantic search grounded in structured data (**contracts, royalty statements, catalog metadata, deal terms**)" | `backline/rag/` — clause-aware chunking, hybrid (lexical+vector) retrieval with RRF, cross-encoder rerank, structured-first governing-document filter | Phase 3 ✅ — full pipeline + idempotent `make embed` + retrieval probe (D-002, D-011) |
| "Guardrails, **evaluation**, observability, and **human-in-the-loop** standards" | `evals/` three-tier harness (exact-match / trace assertions / LLM-judge), hallucination + abstention + injection suites, CI regression gate; `staging`→Review Queue HITL flow; span tracing + cost meter | Phase 2 span tracing + cost meter + guardrail frame ✅ · evals Phase 5, HITL surfaces Phase 6 — pending |
| "Model, prompt, and tool-use choices — **cost and latency tradeoffs at production scale**" | `benchmarks/` sweep runner → accuracy × $/query × p50/p95 latency × tool-efficiency table across Opus/Sonnet/Haiku/local | Phase 7 — pending |
| "Integrate frontier LLMs (OpenAI, Anthropic) **and selected open-source models**" | `backline/providers/` — `AnthropicProvider`, `OpenAICompatProvider` (vLLM/OpenAI), `MockProvider`; local model = a URL in `.env` | Phase 2 ✅ — three providers + model registry (`config/models.yaml`) |
| "Ship full-stack AI-native features end-to-end — chat interfaces, copilot tools, workflow automation surfaces — from data model to UI" (React/Next.js named) | `ui/` Next.js app: Chat, Trace Inspector, Review Queue, Eval Dashboard | Phase 0 scaffold ✅ · surfaces Phase 6 — pending |
| "Reliability, observability, performance expectations of revenue-critical software" / "CI/CD, testing standards" | Dockerized integration tests, eval-as-regression-gate in GitHub Actions, budget guards, structured tracing, `make doctor` | Phase 0 CI + doctor ✅ · rest pending |
| "Proficiency in relational databases (PostgreSQL); comfortable writing and optimizing SQL" | Postgres 16 + pgvector as the only datastore; 450K-row statement fact table; read-only SQL tool with parser-level policy | Phase 0 db ✅ · Phase 1 schemas + 468K-row fact table ✅ · Phase 3 SQL tool (sqlglot allowlist, LIMIT injection, cost ceiling) ✅ |
| "Document decisions and rationale" | `docs/DECISIONS.md` (ADR style, numbered), `docs/PHASE_LOG.md`, per-phase PR descriptions | Phase 0 ✅ — live |
| "Partner with Data Engineering to consume internal pipelines... third-party feeds (DSPs, distributors)" | `datagen/` framed as a mock distributor/DSP feed: monthly statement drops into `/data/inbox` exactly like a real feed lands (6 dialects; `datagen emit-period` drops fresh months) | Phase 1 ✅ |
