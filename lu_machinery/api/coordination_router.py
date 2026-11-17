"""Read-only Monitor API surface for the parallel-agent ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from lu_machinery.orchestration import agent_ledger

from .monitor_context import MonitorContext, get_ctx, resolve_context

router = APIRouter(tags=["coordination"])




def _state_root(ctx: MonitorContext | None = None) -> Path:
    return resolve_context(ctx).roots.state_root




@router.get("/tasks/{task_id}")
async def coordination_task(task_id: str, ctx: MonitorContext = Depends(get_ctx)):
    try:
        task = await asyncio.to_thread(agent_ledger.get_task, _state_root(ctx), task_id)
    except agent_ledger.LedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return task
