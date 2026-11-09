"""执行边界：协作域里**唯一**接触 ``runtime`` / ``agents`` 的模块。

它做四件事，其余一律不管（编排决策在 ``engine.py``）：

1. 把「节点绑定的角色 → 智能体实例 → 适配器定义」解析出来（读库，只读）；
2. 组装 :class:`AgentContext` —— 这里是「不共享记忆」的执行侧落地：
   ``history=[]``、``session_id=None``，每次步骤都是全新会话；
3. 调用 :func:`runtime.runner.stream_run` 拿规范化事件流（取消、超时、命令脱敏全部复用）；
4. 越权审计：对只读/无目录的节点，比对共享工作目录运行前后的指纹。

把这个文件收窄的意义：框架怎么跑智能体、技能怎么物化，将来怎么变，都只在这里适配。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from ..agents import AgentContext, AgentDefinition, get_agent
from ..agents.events import AgentEvent
from ..models import AgentInstance, Role, RoleSkill, new_id
from ..runtime.manager import ActiveRun
from ..runtime.runner import stream_run
from ..security import decrypt
from ..settings import get_settings
from .graph import effective_workspace_scope
from .schemas import NodeSpec, WorkspaceScope
from .tables import CollabRun

#: 审计快照的体积上限（避免把巨大的工作目录翻一遍）
MAX_AUDIT_FILES = 2000
MAX_HASH_BYTES = 512_000


class StepError(Exception):
    """这一步没配好或没法执行（消息面向用户）。"""


@dataclass(slots=True)
class PreparedStep:
    role: Role
    agent: AgentInstance
    definition: AgentDefinition
    ctx: AgentContext
    workspace: str
    scope: WorkspaceScope
    #: 需要以 notice 暴露给用户的说明（例如「技能这次没注入」）
    warnings: list[str] = field(default_factory=list)
    #: 共享工作目录在运行前的指纹（只读节点才做）
    audit_before: dict[str, Any] = field(default_factory=dict)
    audit_root: str | None = None


class StepRegistry:
    """运行中的步骤句柄表（供取消与「正在跑什么」查询）。

    复用 ``runtime.manager.ActiveRun``：它是执行层对「一次运行」的最小描述，
    同时也是 ``stream_run`` 要求的入参类型，因此不必在协作域再造一个。
    该类的 ``conversation_id`` 在这里承载的是**协作运行 ID**（同一个字段，
    两种运行作用域都用它来标识"这次运行属于谁"）。
    """

    def __init__(self) -> None:
        self._handles: dict[str, ActiveRun] = {}

    def register(self, run_id: str, step_id: str, agent_name: str) -> ActiveRun:
        handle = ActiveRun(id=new_id()[:12], conversation_id=run_id, agent_name=agent_name)
        handle.step_id = step_id  # 动态属性：仅供协作域自己查，不影响执行层
        self._handles[step_id] = handle
        return handle

    def finish(self, step_id: str) -> None:
        handle = self._handles.pop(step_id, None)
        if handle is not None:
            handle.finished = True

    def active_for(self, run_id: str) -> list[ActiveRun]:
        return [h for h in self._handles.values() if h.conversation_id == run_id and not h.finished]

    def cancel_run(self, run_id: str) -> bool:
        handles = self.active_for(run_id)
        for handle in handles:
            handle.cancel_event.set()
            process = handle.process
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:  # 进程已经退出
                    pass
        return bool(handles)

    def cancel_step(self, step_id: str) -> bool:
        handle = self._handles.get(step_id)
        if handle is None:
            return False
        handle.cancel_event.set()
        if handle.process is not None and handle.process.returncode is None:
            try:
                handle.process.kill()
            except ProcessLookupError:
                pass
        return True


registry = StepRegistry()


# --- 工作目录 ---------------------------------------------------------------


def _shared_workspace_dir(run: CollabRun) -> Path:
    raw = run.workspace_dir or str(get_settings().workspaces_root / f"collab-{run.id}")
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _step_cwd(run: CollabRun, node: NodeSpec, step_id: str) -> tuple[str, WorkspaceScope]:
    """按节点的 ``workspace_scope`` 决定这一步的 cwd。"""
    scope = effective_workspace_scope(node)
    if scope is WorkspaceScope.NONE:
        # 协调者拿不到共享工作目录，也就没有代码可写。
        scratch = get_settings().data_dir / "collab-scratch" / run.id / step_id
        scratch.mkdir(parents=True, exist_ok=True)
        return str(scratch), scope
    return str(_shared_workspace_dir(run)), scope


def snapshot_workspace(root: Path) -> dict[str, Any]:
    """工作目录指纹：路径 + 大小 + 修改时间（小文件再算内容哈希）。"""
    entries: dict[str, str] = {}
    truncated = False
    try:
        for index, path in enumerate(sorted(root.rglob("*"))):
            if index >= MAX_AUDIT_FILES:
                truncated = True
                break
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
                if stat.st_size <= MAX_HASH_BYTES:
                    stamp += ":" + hashlib.blake2b(path.read_bytes(), digest_size=8).hexdigest()
                entries[str(path.relative_to(root))] = stamp
            except OSError:
                continue
    except OSError:
        return {"entries": {}, "truncated": False, "error": "无法读取工作目录"}
    return {"entries": entries, "truncated": truncated}


def audit_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """比对两次指纹，找出被改动/新增/删除的文件。"""
    old = before.get("entries") or {}
    new = after.get("entries") or {}
    changed = sorted(
        path
        for path, stamp in new.items()
        if path in old and old[path] != stamp
    )
    added = sorted(path for path in new if path not in old)
    removed = sorted(path for path in old if path not in new)
    violations = added + changed + removed
    return {
        "checked": True,
        "unchanged": not violations,
        "added": added[:50],
        "modified": changed[:50],
        "removed": removed[:50],
        "count": len(violations),
        "truncated": bool(before.get("truncated") or after.get("truncated")),
    }


# --- 步骤准备与执行 ---------------------------------------------------------


async def _role_skill_count(session: AsyncSession, role_id: str) -> int:
    query = select(func.count()).select_from(RoleSkill).where(RoleSkill.role_id == role_id)
    return int((await session.execute(query)).scalar() or 0)


async def prepare_step(
    session: AsyncSession,
    *,
    run: CollabRun,
    node: NodeSpec,
    step_id: str,
    prompt: str,
) -> PreparedStep:
    """解析角色与智能体、组装一次运行的上下文。"""
    role = await session.get(Role, node.role_id)
    if role is None:
        raise StepError(f"节点「{node.label or node.key}」绑定的角色已不存在")
    agent = await session.get(AgentInstance, role.agent_id)
    if agent is None:
        raise StepError(f"角色「{role.name}」绑定的智能体已不存在")
    if not agent.enabled:
        raise StepError(f"智能体「{agent.name}」已停用")

    try:
        definition = get_agent(agent.type_id)
    except KeyError as exc:
        raise StepError(str(exc)) from None

    # 角色参数优先于智能体实例默认值，节点级覆盖优先级最高
    options = definition.coerce_options(agent.options or {})
    options = {**options, **(role.parameters or {})}
    if node.options_override:
        options = {**options, **definition.coerce_options({**options, **node.options_override})}

    model = (role.parameters or {}).get("model") or definition.resolve_model(agent.model)
    gateway = (role.parameters or {}).get("gateway_url") or definition.resolve_gateway(agent.gateway_url)
    cwd, scope = _step_cwd(run, node, step_id)

    text = prompt
    system_prompt = role.system_prompt or None
    if system_prompt and not definition.capabilities.native_system_prompt:
        # 适配器不接受独立人格参数时，把人格折进这一轮的输入（同对话链路的做法）
        text = f"[system]\n{system_prompt}\n\n{prompt}"
        system_prompt = None

    ctx = AgentContext(
        agent_id=agent.id,
        agent_name=agent.name,
        options=options,
        gateway_url=gateway,
        api_key=decrypt(agent.api_key_encrypted),
        model=model,
        role_name=role.name,
        system_prompt=system_prompt,
        workspace_dir=cwd,
        # 「不共享记忆」：每次步骤都是全新会话，历史只走工单载荷
        session_id=None,
        history=[],
        prompt=text,
    )

    warnings: list[str] = []
    skill_count = await _role_skill_count(session, role.id)
    if skill_count:
        warnings.append(
            f"角色「{role.name}」分配了 {skill_count} 个技能，但协作运行暂未注入技能"
            "（技能物化安排在 M5，见 docs/collaboration-design.md §11）"
        )

    prepared = PreparedStep(
        role=role,
        agent=agent,
        definition=definition,
        ctx=ctx,
        workspace=cwd,
        scope=scope,
        warnings=warnings,
    )
    if scope is not WorkspaceScope.WRITE:
        prepared.audit_root = str(_shared_workspace_dir(run))
        prepared.audit_before = snapshot_workspace(Path(prepared.audit_root))
    return prepared


async def stream_step(prepared: PreparedStep, handle: ActiveRun) -> AsyncIterator[AgentEvent]:
    """把一次步骤变成规范化事件流（取消/超时/脱敏都由执行层负责）。"""
    async for event in stream_run(prepared.definition, prepared.ctx, handle):
        yield event


def finish_audit(prepared: PreparedStep) -> dict[str, Any]:
    """只读节点运行结束后的越权检查。"""
    if not prepared.audit_root:
        return {}
    after = snapshot_workspace(Path(prepared.audit_root))
    return audit_diff(prepared.audit_before, after)


def cancel_run(run_id: str) -> bool:
    """取消这个协作运行下所有还在跑的步骤。"""
    return registry.cancel_run(run_id)


def cancel_step(step_id: str) -> bool:
    return registry.cancel_step(step_id)


def is_running(run_id: str) -> bool:
    return bool(registry.active_for(run_id))
