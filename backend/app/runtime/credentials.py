"""凭据库：统一维护 + 智能体引用 + 运行时解密。

凭据是**全局一份**（``credentials`` 表），某个智能体能拿到哪些由 ``agent_credentials``
决定 —— 与「技能」是同一套思路（见 :mod:`runtime.skills`）。加解密 / 掩码 / 事件擦除的
纯逻辑在 :mod:`app.credentials`，这里只负责「读库、落库、维护引用关系」。

界面上的两条入口共用这里的逻辑：智能体弹窗里挑一条 / 新建一条，以及「设置 → 凭据库」。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from ..credentials import CredentialError, clean_description, decrypt_all, validate_name
from ..models import AgentCredential, AgentProfile, Credential
from ..security import encrypt


async def list_rows(session: AsyncSession) -> list[Credential]:
    """凭据库里的全部凭据（不含软删除的），按名字排序。"""
    return list(
        (
            await session.execute(
                select(Credential)
                .where(Credential.deleted_at.is_(None))
                .order_by(Credential.name)
            )
        )
        .scalars()
        .all()
    )


async def get_row(session: AsyncSession, credential_id: str) -> Credential | None:
    row = await session.get(Credential, credential_id)
    if row is None or row.deleted_at is not None:
        return None
    return row


async def find_by_name(session: AsyncSession, name: str, owner_id: str | None = None) -> Credential | None:
    stmt = select(Credential).where(Credential.name == name, Credential.deleted_at.is_(None))
    if owner_id is not None:
        stmt = stmt.where(Credential.owner_id == owner_id)
    return (await session.execute(stmt)).scalars().first()


async def usage_counts(session: AsyncSession) -> dict[str, int]:
    """每条凭据被多少个**未删除**的智能体引用。"""
    rows = (
        await session.execute(
            select(AgentCredential.credential_id, func.count())
            .join(AgentProfile, AgentProfile.id == AgentCredential.agent_profile_id)
            .where(AgentProfile.deleted_at.is_(None))
            .group_by(AgentCredential.credential_id)
        )
    ).all()
    return {credential_id: count for credential_id, count in rows}


async def referencing_profiles(session: AsyncSession, credential_id: str) -> list[AgentProfile]:
    """仍在引用这条凭据的（未删除的）智能体。"""
    return list(
        (
            await session.execute(
                select(AgentProfile)
                .join(AgentCredential, AgentCredential.agent_profile_id == AgentProfile.id)
                .where(
                    AgentCredential.credential_id == credential_id,
                    AgentProfile.deleted_at.is_(None),
                )
                .order_by(AgentProfile.name)
            )
        )
        .scalars()
        .all()
    )


async def linked_rows(session: AsyncSession, agent_profile_id: str) -> list[Credential]:
    """某个智能体引用到的凭据（按界面里的顺序）。"""
    links = (
        await session.execute(
            select(AgentCredential)
            .where(AgentCredential.agent_profile_id == agent_profile_id)
            .order_by(AgentCredential.position)
        )
    ).scalars().all()
    rows: list[Credential] = []
    for link in links:
        row = await session.get(Credential, link.credential_id)
        if row is not None and row.deleted_at is None:
            rows.append(row)
    return rows


async def decrypt_for_profile(session: AsyncSession, agent_profile_id: str) -> dict[str, str]:
    """运行时用：把智能体引用到的凭据解密成「环境变量名 → 明文」。"""
    return decrypt_all(await linked_rows(session, agent_profile_id))


# --- 写 -------------------------------------------------------------------


def _field(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


async def _resolve_one(
    session: AsyncSession, item: Any, owner_id: str | None = None
) -> Credential:
    """把界面提交的一项（可能是「已存在的 id / 同名凭据 / 新名字 + 值」）解析成一条凭据行。

    多用户下按 ``owner_id`` 隔离：不能引用别人的凭据（否则运行时会把别人的密钥注入自己的
    智能体），同名判断也只在同一用户范围内。
    """
    credential_id = _field(item, "id")
    value = _field(item, "value")
    description = clean_description(_field(item, "description"))

    row: Credential | None = None
    if credential_id:
        row = await get_row(session, str(credential_id))
        if row is None:
            raise CredentialError("选择的凭据已经被删除了，请刷新后重试")
        if owner_id is not None and row.owner_id != owner_id:
            raise CredentialError("选择的凭据不存在或无权使用")
    name = validate_name(str(_field(item, "name") or (row.name if row else "")))
    if row is None:
        row = await find_by_name(session, name, owner_id)
    if row is None:
        # 新名字：必须带值，否则存进去也是一条空凭据
        if not value:
            raise CredentialError(f"凭据「{name}」还没有填值")
        row = Credential(
            name=name,
            description=description,
            value_encrypted=encrypt(value),
            owner_id=owner_id,
        )
        session.add(row)
        await session.flush()
        return row

    # 已存在的凭据：只在显式给了新值 / 新说明时才更新（界面里挑一条不会改动它）
    if value:
        row.value_encrypted = encrypt(value)
    if description is not None:
        row.description = description
    if row.name != name:
        row.name = name
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.flush()
    return row


async def set_agent_credentials(
    session: AsyncSession,
    profile: AgentProfile,
    items: Sequence[Any],
    *,
    owner_id: str | None = None,
) -> list[str]:
    """整表替换智能体引用的凭据；返回实际关联的 credential_id 列表。

    ``owner_id`` 是凭据必须归属的用户；管理员替别人配置时传对方的 owner_id。
    """
    resolved: list[Credential] = []
    seen: set[str] = set()
    for item in items:
        row = await _resolve_one(session, item, owner_id)
        if row.id in seen:
            raise CredentialError(f"凭据「{row.name}」重复了，请改成不同的名字")
        seen.add(row.id)
        resolved.append(row)

    for link in (
        await session.execute(
            select(AgentCredential).where(AgentCredential.agent_profile_id == profile.id)
        )
    ).scalars().all():
        await session.delete(link)
    await session.flush()

    for position, row in enumerate(resolved):
        session.add(
            AgentCredential(agent_profile_id=profile.id, credential_id=row.id, position=position)
        )
    await session.commit()
    return [row.id for row in resolved]


async def create_row(
    session: AsyncSession,
    *,
    name: str,
    description: Any = None,
    value: Any = None,
    owner_id: str | None = None,
) -> Credential:
    """凭据库页里新建一条（名字在当前用户范围内唯一、必须带值）。"""
    clean_name = validate_name(name)
    if await find_by_name(session, clean_name, owner_id) is not None:
        raise CredentialError(f"凭据「{clean_name}」已经存在了")
    if not value:
        raise CredentialError(f"凭据「{clean_name}」还没有填值")
    row = Credential(
        name=clean_name,
        description=clean_description(description),
        value_encrypted=encrypt(value),
        owner_id=owner_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_row(
    session: AsyncSession,
    row: Credential,
    *,
    name: Any = None,
    description: Any = None,
    value: Any = None,
    owner_id: str | None = None,
) -> Credential:
    """改名 / 改说明 / 换值（``None`` 表示这一项不改）。"""
    if name is not None:
        clean_name = validate_name(str(name))
        conflict = await find_by_name(session, clean_name, owner_id)
        if conflict is not None and conflict.id != row.id:
            raise CredentialError(f"凭据「{clean_name}」已经存在了")
        row.name = clean_name
    if description is not None:
        row.description = clean_description(description)
    if value:
        row.value_encrypted = encrypt(value)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
