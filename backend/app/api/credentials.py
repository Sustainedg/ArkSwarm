"""凭据库接口：全局维护一批凭据，智能体按引用使用。

凭据是**工具 / 服务**要用的第三方密钥（GitHub token、数据库口令…），与「网关」里的
模型密钥不是一回事。值只经 :mod:`app.security` 加密保存，接口永远只回掩码。
入口有两处：设置页的「凭据库」（增删改查），以及智能体配置里的挑选 / 新建。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..credentials import CredentialError, reads
from ..db import get_session
from ..models import Credential
from ..runtime import credentials as credential_service
from ..schemas import CredentialCreate, CredentialRead, CredentialUpdate

router = APIRouter(prefix="/credentials", tags=["credentials"])


def _to_read(row: Credential, used_by: int = 0) -> CredentialRead:
    (view,) = reads([row])
    return CredentialRead(**view, used_by_agents=used_by, created_at=row.created_at, updated_at=row.updated_at)


async def _active_row(credential_id: str, session: AsyncSession) -> Credential:
    row = await credential_service.get_row(session, credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    return row


@router.get("")
async def list_credentials(session: AsyncSession = Depends(get_session)) -> dict:
    """列出凭据库里的全部凭据（只回名字、说明与掩码）。"""
    counts = await credential_service.usage_counts(session)
    rows = await credential_service.list_rows(session)
    return {"items": [_to_read(row, counts.get(row.id, 0)).model_dump() for row in rows]}


@router.post("", status_code=201)
async def create_credential(
    payload: CredentialCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        row = await credential_service.create_row(
            session, name=payload.name, description=payload.description, value=payload.value
        )
    except CredentialError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return _to_read(row).model_dump()


@router.patch("/{credential_id}")
async def update_credential(
    credential_id: str, payload: CredentialUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    row = await _active_row(credential_id, session)
    # 值留空＝不换值（界面回显的是掩码，不能把掩码当新值写回去）。
    value = payload.value or None
    try:
        row = await credential_service.update_row(
            session, row, name=payload.name, description=payload.description, value=value
        )
    except CredentialError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    counts = await credential_service.usage_counts(session)
    return _to_read(row, counts.get(row.id, 0)).model_dump()


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(credential_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """删除凭据（软删除）。

    仍被**未删除**的智能体引用时返回 409：先让那些智能体解除引用，避免它们运行时
    悄悄少了一组环境变量。删掉后历史对话照旧可读（凭据本来就不进对话记录）。
    """
    row = await _active_row(credential_id, session)
    used = await credential_service.referencing_profiles(session, row.id)
    if used:
        names = "、".join(profile.name for profile in used[:5])
        raise HTTPException(
            status_code=409,
            detail=f"还有 {len(used)} 个智能体在引用这条凭据（{names}），请先解除引用再删除",
        )
    row.deleted_at = row.updated_at
    session.add(row)
    await session.commit()
    return Response(status_code=204)
