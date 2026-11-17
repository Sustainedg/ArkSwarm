"""首次启动播种：为每个可用内核建一条官方网关 + 一个默认智能体，避免界面空空如也。

多用户系统启用时，这里还负责：

* :func:`seed_initial_admin` —— 按环境变量建首任管理员；
* :func:`active_admin` —— 取当前启用中的管理员（老库已有用户时回填用）；
* :func:`backfill_ownership` —— 把老库（原本单用户、所有数据无归属）的存量行
  ``owner_id`` 回填给管理员，用 ``.ownership_backfilled`` 标记防重复。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .auth import hash_password
from .models import AgentProfile, Conversation, Credential, Gateway, McpServer, User
from .security import encrypt
from .settings import get_settings

if TYPE_CHECKING:  # 仅供类型标注：播种由入口组合根传入适配器，避免运行期反向依赖 agents
    from .agents.base import AgentDefinition

STARTER_PERSONAS = {
    "claude-code": (
        "🛠️",
        "资深工程师",
        "你是一名严谨的资深软件工程师。动手前先通读仓库，简要说明取舍，"
        "优先给出最小且经过验证的改动。",
    ),
    "codex": (
        "🧪",
        "结对编程搭子",
        "你是务实的结对编程伙伴。每次改动尽量小而可评审，改完立刻运行相关测试，"
        "并总结改了什么。",
    ),
    "gemini-cli": (
        "🔎",
        "调研助手",
        "你是研究型助手。先收集证据，引用你读过的文件路径，"
        "并明确区分「已验证的事实」与「推测」。",
    ),
    "openai-harness": (
        "🔗",
        "网关通用助手",
        "你是通过自定义网关接入的通用助手。回答要精确，"
        "遇到含糊的需求先请求澄清。",
    ),
}


async def seed_if_empty(session: AsyncSession, definitions: Sequence[AgentDefinition]) -> None:
    """按传入的适配器声明播种「官方网关 + 默认智能体」（适配器列表由入口组合根提供）。"""
    settings = get_settings()
    marker = settings.data_dir / ".seeded"
    if marker.exists():
        return
    existing = (await session.execute(select(AgentProfile))).scalars().all()
    if existing:
        marker.write_text("skipped\n", encoding="utf-8")
        return

    created: list[tuple[Gateway, AgentDefinition, str | None]] = []
    for definition in definitions:
        if not definition.auto_provision or not definition.availability().installed:
            continue
        defaults = definition.system_defaults()
        base_url = defaults.get("gateway_url") or definition.default_gateway
        if not base_url:
            # 没有官方地址的内核（例如自定义 CLI）：由用户自己建网关
            continue
        model = defaults.get("model") or definition.default_model
        models = [model] if model else list(definition.default_models)
        gateway = Gateway(
            name=f"{definition.name} 官方",
            kind="official",
            type_id=definition.id,
            protocol=definition.protocol,
            auth_style=definition.default_auth_style,
            base_url=base_url,
            api_key_encrypted=encrypt(defaults.get("api_key")),
            selected_models=models,
            model_catalog=models,
        )
        session.add(gateway)
        created.append((gateway, definition, model))

    if not created:
        return
    await session.commit()
    for gateway, _definition, _model in created:
        await session.refresh(gateway)

    for index, (gateway, definition, model) in enumerate(created):
        avatar, name, prompt = STARTER_PERSONAS.get(
            definition.id, ("🤖", f"{definition.name} 助手", "你是一个乐于助人的助手。")
        )
        session.add(
            AgentProfile(
                name=name,
                avatar=avatar,
                tagline=f"由 {definition.name} 官方链路驱动",
                description=definition.description,
                system_prompt=prompt,
                type_id=definition.id,
                gateway_id=gateway.id,
                model=model,
                options=definition.coerce_options({}),
                enabled=True,
                # 播种出来的第一个就是默认智能体：装好点「新建对话」就能直接聊。
                is_default=index == 0,
            )
        )
    await session.commit()
    marker.write_text("done\n", encoding="utf-8")


async def ensure_default_agent(session: AsyncSession) -> None:
    """保证「总有一颗五角星」：老库升级上来时补一次。

    只在「一个默认都没有、但已经有智能体」时动手，并优先挑启用的那个；
    用户自己点出来的星永远不会被这里覆盖。
    """
    has_default = (
        await session.execute(
            select(AgentProfile.id)
            .where(AgentProfile.is_default == True, AgentProfile.deleted_at.is_(None))  # noqa: E712
            .limit(1)
        )
    ).first()
    if has_default is not None:
        return
    rows = (
        await session.execute(
            select(AgentProfile)
            .where(AgentProfile.deleted_at.is_(None))
            .order_by(AgentProfile.enabled.desc(), AgentProfile.created_at)
        )
    ).scalars().all()
    if not rows:
        return
    rows[0].is_default = True
    session.add(rows[0])
    await session.commit()


async def seed_initial_admin(session: AsyncSession) -> User | None:
    """按环境变量建首任管理员；已存在任意用户时不生效。

    返回建出的管理员，或 ``None``（未配置环境变量 / 已有用户）。
    不写标记文件：判断依据是「表里有没有用户」，比文件标记更可靠。
    """
    settings = get_settings()
    if not settings.admin_username or not settings.admin_password:
        return None
    has_any = (await session.execute(select(User.id).limit(1))).first()
    if has_any is not None:
        return None
    user = User(
        username=settings.admin_username,
        display_name=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        role="admin",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def active_admin(session: AsyncSession) -> User | None:
    """取一个启用中的管理员；老库回填需要在「已有用户、但还没回填」时也能找到它。"""
    return (
        await session.execute(
            select(User)
            .where(User.role == "admin", User.is_active == True)  # noqa: E712
            .order_by(User.created_at)
            .limit(1)
        )
    ).scalars().first()


async def backfill_ownership(session: AsyncSession, admin: User) -> None:
    """把老库存量行的 ``owner_id`` 回填给管理员。

    用 ``.ownership_backfilled`` 标记防重复；回填 ``owner_id IS NULL``（老库）与
    ``owner_id = '__system__'``（鉴权关闭期间以虚拟管理员身份创建）的行，已有真实归属
    （新创建的）不动。幂等。
    """
    settings = get_settings()
    marker = settings.data_dir / ".ownership_backfilled"
    if marker.exists():
        return
    owner_id = admin.id
    # 逐表回填无归属（NULL）与虚拟管理员名下（__system__）的行。用原生 UPDATE 避免整行加载进 ORM。
    for model in (Gateway, AgentProfile, Conversation, Credential, McpServer):
        table_name = model.__tablename__  # type: ignore[attr-defined]
        await session.execute(
            # SQLModel 的表名稳定，这里用 text 走原生 SQL 最直接。
            text(
                f"UPDATE {table_name} SET owner_id = :owner "
                "WHERE owner_id IS NULL OR owner_id = '__system__'"
            ).bindparams(owner=owner_id)
        )
    # 协作域表 + IM 域表（同样带 owner_id；漏掉 IM 的话，鉴权关闭期间建的连接
    # 打开鉴权后会永远看不到，且没人能接管）
    for table_name in ("collab_teams", "collab_runs", "im_connections"):
        await session.execute(
            text(
                f"UPDATE {table_name} SET owner_id = :owner "
                "WHERE owner_id IS NULL OR owner_id = '__system__'"
            ).bindparams(owner=owner_id)
        )
    await session.commit()
    marker.write_text("done\n", encoding="utf-8")
