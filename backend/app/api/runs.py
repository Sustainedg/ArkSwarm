from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..runtime import registry

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def list_runs() -> dict:
    return {"items": registry.list()}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    registry.cancel(run_id)
    return {"ok": True}
