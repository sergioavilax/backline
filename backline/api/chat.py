"""Chat orchestration: one user message → routed, traced agent run → SSE events.

The event protocol a client sees on ``POST /sessions/{id}/messages``:

    accepted     {message_id}                          user turn persisted
    routed       {target, confidence, reason, router_run_id, artists, demo}
    clarify      {question}                            router punt — turn ends here
    run_started  {run_id, agent}                       subscribe to the span stream now
    final        {run_id, agent, status, text, citations, abstained, batch_id, ...}
    error        {detail}
    done         {}

The run executes in a background task that owns the whole turn; the HTTP generator
only drains an event queue. A client that disconnects mid-run therefore never kills
the run — a batch submit either happens completely or not at all, regardless of who
is watching the stream (D-024).

Context: the agent sees the session's recent history through ``SessionMemory``,
rebuilt per turn from ``app.messages`` with a SQL window — the summarizer hook stays
unwired here (deterministic elision instead; D-026 records why).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from backline.agents.configs import build_agent
from backline.agents.recall import compose_user_message, recall_block
from backline.agents.router import RouteDecision, Router
from backline.api.demo import (
    DEMO_PLANNER_MODEL,
    DEMO_ROUTER_MODEL,
    DEMO_UTILITY_MODEL,
    build_demo_plan,
)
from backline.api.sse import sse_event
from backline.api.state import AppState, jload
from backline.core.memory import SessionMemory
from backline.core.runtime import AgentRuntime, RunResult
from backline.jsonutil import canonical_dumps
from backline.providers.base import Message, Provider

_HISTORY_WINDOW = 20  # assistant+user turns folded into context, newest last


@dataclass(frozen=True)
class _Routed:
    decision: RouteDecision
    providers: dict[str, Provider]
    agent_model: str | None
    utility_model: str | None
    router_run_id: uuid.UUID | None


async def _load_session_memory(state: AppState, session_id: uuid.UUID) -> SessionMemory:
    """Rebuild the rolling window from persisted turns (window bounded in SQL)."""
    rows = await state.pool.fetch(
        "SELECT role, content, (SELECT count(*) FROM app.messages m2 "
        " WHERE m2.session_id = $1) AS total "
        "FROM app.messages WHERE session_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2",
        session_id,
        _HISTORY_WINDOW,
    )
    memory = SessionMemory(window=_HISTORY_WINDOW)
    if rows and rows[0]["total"] > len(rows):
        memory.note_elided(rows[0]["total"] - len(rows))
    for row in reversed(rows):
        text = str(jload(row["content"]).get("text", ""))
        if not text:
            continue
        role: Literal["user", "assistant"] = "assistant" if row["role"] == "assistant" else "user"
        memory.add(Message(role=role, content=text))
    return memory


async def _route(
    state: AppState, text: str, pinned_agent: str | None, session_id: uuid.UUID
) -> _Routed:
    if state.demo_mode:
        plan = await build_demo_plan(state.pool, text, pinned_agent=pinned_agent)
        provider = plan.provider()
        # The scripted router turn still runs as a real traced run.
        router = Router(
            providers={"mock": provider},
            registry=state.registry,
            tracer=state.tracer,
            model=DEMO_ROUTER_MODEL,
        )
        decision = await router.route(text, session_id=session_id)
        return _Routed(
            decision=decision,
            providers={"mock": provider},
            agent_model=DEMO_PLANNER_MODEL,
            utility_model=DEMO_UTILITY_MODEL,
            router_run_id=None,
        )
    if pinned_agent is not None:
        decision = RouteDecision(
            target=pinned_agent,  # type: ignore[arg-type]  # Literal narrowed by MessageIn
            confidence=1.0,
            reason="user pinned the agent",
        )
        return _Routed(
            decision=decision,
            providers=state.providers,
            agent_model=None,
            utility_model=None,
            router_run_id=None,
        )
    router = Router(providers=state.providers, registry=state.registry, tracer=state.tracer)
    decision = await router.route(text, session_id=session_id)
    return _Routed(
        decision=decision,
        providers=state.providers,
        agent_model=None,
        utility_model=None,
        router_run_id=None,
    )


async def _persist_message(
    state: AppState, session_id: uuid.UUID, role: str, content: dict[str, Any]
) -> uuid.UUID:
    message_id: uuid.UUID = await state.pool.fetchval(
        "INSERT INTO app.messages (session_id, role, content) VALUES ($1, $2, $3::jsonb) "
        "RETURNING id",
        session_id,
        role,
        canonical_dumps(content),
    )
    return message_id


async def _maybe_title(state: AppState, session_id: uuid.UUID, text: str) -> None:
    """First message names the session (UI session list) unless a title was given."""
    await state.pool.execute(
        "UPDATE app.sessions SET title = left($2, 80) WHERE id = $1 AND title IS NULL",
        session_id,
        text.strip().splitlines()[0],
    )


async def _actual_batch_id(state: AppState, run_id: uuid.UUID) -> int | None:
    """The batch a run really submitted — the DB is the truth, not the model's text."""
    batch_id: int | None = await state.pool.fetchval(
        "SELECT id FROM staging.statement_batches WHERE submitted_by_run = $1 "
        "ORDER BY id DESC LIMIT 1",
        run_id,
    )
    return batch_id


def _final_payload(
    result: RunResult, agent: str, demo: bool, batch_id: int | None
) -> dict[str, Any]:
    final = result.final
    payload: dict[str, Any] = {
        "run_id": str(result.run_id),
        "agent": agent,
        "status": result.status,
        "iterations": result.iterations,
        "cost_usd": str(result.cost_usd),
        "demo": demo,
        "text": final.answer if final is not None else "",
        "citations": [c.model_dump() for c in final.citations] if final is not None else [],
        "abstained": final.abstained if final is not None else False,
        "batch_id": batch_id,
        "flags_summary": getattr(final, "flags_summary", "") if final is not None else "",
    }
    if final is None:
        payload["text"] = (
            "The run ended without a final answer "
            f"(status: {result.status}) — see the trace for what happened."
        )
    return payload


async def run_chat_turn(
    state: AppState,
    session_id: uuid.UUID,
    text: str,
    pinned_agent: str | None,
    queue: asyncio.Queue[str | None],
) -> None:
    """The whole turn: persist → route → run → persist. Emits SSE frames to ``queue``."""
    demo = state.demo_mode
    user_message_id = await _persist_message(state, session_id, "user", {"text": text})
    await _maybe_title(state, session_id, text)
    queue.put_nowait(sse_event("accepted", {"message_id": str(user_message_id)}))

    routed = await _route(state, text, pinned_agent, session_id)
    decision = routed.decision
    queue.put_nowait(
        sse_event(
            "routed",
            {
                "target": decision.target,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "artists": decision.artists,
                "demo": demo,
            },
        )
    )

    if decision.target == "clarify":
        question = decision.clarifying_question or "Which agent should take this?"
        await _persist_message(
            state,
            session_id,
            "assistant",
            {
                "text": question,
                "kind": "clarify",
                "route": decision.model_dump(mode="json"),
                "demo": demo,
            },
        )
        queue.put_nowait(sse_event("clarify", {"question": question}))
        queue.put_nowait(sse_event("done", {}))
        return

    agent_name = decision.target
    run_id = uuid.uuid4()
    queue.put_nowait(sse_event("run_started", {"run_id": str(run_id), "agent": agent_name}))

    recalled = await recall_block(state.pool, decision.artists)
    memory = await _load_session_memory(state, session_id)
    agent = build_agent(
        agent_name,
        state.tool_context,
        model=routed.agent_model,
        utility_model=routed.utility_model,
    )
    runtime = AgentRuntime(providers=routed.providers, registry=state.registry, tracer=state.tracer)
    result = await runtime.run(
        agent,
        compose_user_message(text, recalled),
        session_id=session_id,
        session=memory,
        run_id=run_id,
    )

    batch_id = await _actual_batch_id(state, run_id)
    payload = _final_payload(result, agent_name, demo, batch_id)
    await _persist_message(
        state,
        session_id,
        "assistant",
        {
            **payload,
            "route": decision.model_dump(mode="json"),
            "recalled_notes": bool(recalled),
        },
    )
    queue.put_nowait(sse_event("final", payload))
    queue.put_nowait(sse_event("done", {}))
