"""首次启动播种，避免界面空空如也。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .models import AgentInstance, Role
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
    "deepseek-harness": (
        "🐳",
        "DeepSeek 分析员",
        "你是有条理的分析员。把任务拆成步骤，用 shell 工具验证假设，"
        "并简明地汇报结论。",
    ),
    "openai-harness": (
        "🔗",
        "网关通用助手",
        "你是通过自定义网关接入的通用助手。回答要精确，"
        "遇到含糊的需求先请求澄清。",
    ),
}


async def seed_if_empty(session: AsyncSession, definitions: Sequence[AgentDefinition]) -> None:
    """按传入的适配器定义播种默认智能体与角色（适配器列表由入口组合根提供）。"""
    settings = get_settings()
    marker = settings.data_dir / ".seeded"
    if marker.exists():
        return
    existing = (await session.execute(select(AgentInstance))).scalars().all()
    if existing:
        marker.write_text("skipped\n", encoding="utf-8")
        return

    created: list[AgentInstance] = []
    for definition in definitions:
        if not definition.auto_provision or not definition.availability().installed:
            continue
        defaults = definition.system_defaults()
        agent = AgentInstance(
            name=f"{definition.name}（默认）",
            type_id=definition.id,
            description=definition.description,
            gateway_url=defaults.get("gateway_url") or definition.default_gateway,
            model=defaults.get("model") or definition.default_model,
            options=definition.coerce_options({}),
            enabled=True,
        )
        session.add(agent)
        created.append(agent)

    if not created:
        return
    await session.commit()
    for agent in created:
        await session.refresh(agent)

    for agent in created:
        avatar, name, prompt = STARTER_PERSONAS.get(
            agent.type_id, ("🤖", f"{agent.name} 助手", "你是一个乐于助人的助手。")
        )
        session.add(
            Role(name=name, avatar=avatar,
                 tagline=f"由 {agent.name.removesuffix('（默认）')} 驱动",
                 system_prompt=prompt, agent_id=agent.id)
        )
    await session.commit()
    marker.write_text("done\n", encoding="utf-8")
