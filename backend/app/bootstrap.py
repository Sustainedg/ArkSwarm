"""First-run seeding so the UI is never an empty shell."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .agents import all_agents
from .models import AgentInstance, Role
from .settings import get_settings

STARTER_PERSONAS = {
    "claude-code": (
        "🛠️",
        "Senior engineer",
        "You are a meticulous senior software engineer. Inspect the repository before "
        "changing anything, explain trade-offs briefly, and prefer minimal, tested diffs.",
    ),
    "codex": (
        "🧪",
        "Pair programmer",
        "You are a pragmatic pair programmer. Keep changes small and reviewable, run the "
        "relevant tests after every edit, and summarise what changed.",
    ),
    "gemini-cli": (
        "🔎",
        "Research assistant",
        "You are a research-oriented assistant. Gather evidence first, cite the files you "
        "read, and separate findings from speculation.",
    ),
    "deepseek-harness": (
        "🐳",
        "DeepSeek analyst",
        "You are a systematic analyst. Break the task into steps, use the shell tools to "
        "verify assumptions, and report results concisely.",
    ),
    "openai-harness": (
        "🔗",
        "Gateway generalist",
        "You are a general-purpose assistant connected through a custom gateway. Be precise "
        "and ask for clarification when the request is ambiguous.",
    ),
}


async def seed_if_empty(session: AsyncSession) -> None:
    settings = get_settings()
    marker = settings.data_dir / ".seeded"
    if marker.exists():
        return
    existing = (await session.execute(select(AgentInstance))).scalars().all()
    if existing:
        marker.write_text("skipped\n", encoding="utf-8")
        return

    created: list[AgentInstance] = []
    for definition in all_agents():
        if not definition.auto_provision or not definition.availability().installed:
            continue
        agent = AgentInstance(
            name=f"{definition.name} (default)",
            type_id=definition.id,
            description=definition.description,
            gateway_url=definition.default_gateway,
            model=definition.default_model,
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
            agent.type_id, ("🤖", f"{agent.name} assistant", "You are a helpful assistant.")
        )
        session.add(
            Role(name=name, avatar=avatar, tagline=f"Powered by {agent.name}",
                 system_prompt=prompt, agent_id=agent.id)
        )
    await session.commit()
    marker.write_text("done\n", encoding="utf-8")
