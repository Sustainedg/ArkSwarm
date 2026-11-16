from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..models import User
from ..runtime import registry
from .auth import current_user

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("")
async def list_runs(user: Annotated[User, Depends(current_user)] = None) -> dict:
    owner_id = None if user.role == "admin" else user.id
    return {"items": registry.list(owner_id=owner_id)}


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str, user: Annotated[User, Depends(current_user)] = None
) -> dict:
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行任务不存在")
    if user.role != "admin" and run.owner_id not in (None, user.id):
        raise HTTPException(status_code=404, detail="运行任务不存在")
    registry.cancel(run_id)
    return {"ok": True}
