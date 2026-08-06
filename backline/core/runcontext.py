"""Ambient run identity for tool handlers.

Tool handlers receive only their validated params (``Tool.handler``), but the gated
write path must stamp *which run* proposed a change (``staging.statement_batches
.submitted_by_run``, ``app.notes.created_by``). The runtime publishes the active run id
here for the duration of each run; tools read it. A ``ContextVar`` keeps concurrent
runs in the same process correctly isolated per task tree.
"""

from contextvars import ContextVar
from uuid import UUID

current_run_id: ContextVar[UUID | None] = ContextVar("current_run_id", default=None)
