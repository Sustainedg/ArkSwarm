"""OpenAI Codex CLI 适配器。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
)
from ..events import (
    AgentEvent,
    error,
    log,
    session,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage,
)
from ..registry import register_agent
from ..skills import SkillMount, SkillSpec, scan_skill_root
from .util import as_list, flatten_content, truthy


#: Codex 的登录态与配置文件名；隔离运行时要一并带过去，否则会丢登录或丢自定义供应商。
_CARRY_FILES = ("config.toml", "auth.json")


def codex_home() -> Path:
    """Codex 的配置目录（``CODEX_HOME`` 优先，其次 ``~/.codex``）。"""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


@register_agent
class CodexAgent(AgentDefinition):
    id = "codex"
    name = "Codex CLI"
    vendor = "OpenAI"
    description = (
        "OpenAI 的 Codex 编码智能体。通过 codex exec --json 输出 JSONL，"
        "支持沙箱模式、审批策略与线程续接。"
    )
    runtime = Runtime.CLI
    executable = "codex"
    api_key_env = "OPENAI_API_KEY"
    base_url_env = "OPENAI_BASE_URL"
    default_gateway = "https://api.openai.com/v1"
    default_model = "gpt-5-codex"
    docs_url = "https://developers.openai.com/codex/cli/"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=False,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
        skills=True,
    )
    config_fields = (
        ConfigField(
            "sandbox_mode",
            "沙箱模式",
            FieldType.SELECT,
            default="workspace-write",
            options=[
                {"value": "read-only", "label": "read-only · 只读"},
                {"value": "workspace-write", "label": "workspace-write · 可写工作区"},
                {"value": "danger-full-access", "label": "danger-full-access · 完全访问（危险）"},
            ],
        ),
        ConfigField(
            "full_auto",
            "全自动（不询问）",
            FieldType.BOOLEAN,
            default=False,
            help="等价于 --full-auto：在沙箱范围内直接写入，不再逐步征求同意。",
        ),
        ConfigField(
            "provider_name",
            "自定义供应商名称",
            FieldType.STRING,
            advanced=True,
            placeholder="例如 deepseek、openrouter",
            help="填写后，Codex 会通过一个具名 model_provider 指向你填的网关。",
        ),
        ConfigField(
            "wire_api",
            "供应商接口协议",
            FieldType.SELECT,
            default="chat",
            options=[
                {"value": "chat", "label": "chat · /chat/completions"},
                {"value": "responses", "label": "responses · /responses"},
            ],
            advanced=True,
        ),
        ConfigField(
            "resume_style",
            "多轮对话策略",
            FieldType.SELECT,
            default="subcommand",
            options=[
                {"value": "subcommand", "label": "codex exec resume <thread>（推荐）"},
                {"value": "transcript", "label": "每轮重放完整对话记录"},
                {"value": "none", "label": "仅单轮，不带上下文"},
            ],
            help="旧版 Codex 没有 exec resume，这类版本请选「每轮重放完整对话记录」。",
        ),
        ConfigField(
            "reasoning_effort",
            "推理强度",
            FieldType.SELECT,
            default="",
            options=[
                {"value": "", "label": "跟随默认"},
                {"value": "minimal", "label": "minimal · 最小"},
                {"value": "low", "label": "low · 低"},
                {"value": "medium", "label": "medium · 中"},
                {"value": "high", "label": "high · 高"},
            ],
            advanced=True,
        ),
        ConfigField("add_dirs", "额外工作目录", FieldType.STRING_LIST, advanced=True),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "额外环境变量", FieldType.KEYVALUE, advanced=True),
    )

    def discover_skills(self) -> list[SkillSpec]:
        """扫描 Codex 的技能根：``$CODEX_HOME/skills``（含 ``.system`` 系统技能）。"""
        root = codex_home() / "skills"
        specs = scan_skill_root(root, type_id=self.id, source_kind="user")
        specs += scan_skill_root(root / ".system", type_id=self.id, source_kind="system")
        return specs

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """严格仅分配：把运行环境指向只含「角色分配到的技能」的隔离 ``CODEX_HOME``。

        ``config.toml`` 与登录态（``auth.json``）会被带过去，避免丢登录或丢自定义供应商。
        """
        root = codex_home()
        carry = [(str(root / name), name) for name in _CARRY_FILES if (root / name).is_file()]
        return SkillMount(home_name="codex-home", carry=carry)

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        base = [self.executable or "codex", "exec"]
        argv = list(base)

        resume_style = options.get("resume_style") or "subcommand"
        prompt = ctx.prompt
        if ctx.session_id and resume_style == "subcommand":
            argv += ["resume", ctx.session_id]

        argv.append("--json")
        argv.append("--skip-git-repo-check")

        if ctx.model:
            argv += ["-m", ctx.model]
        sandbox = options.get("sandbox_mode")
        if sandbox:
            argv += ["-s", sandbox]
        if truthy(options.get("full_auto")):
            argv.append("--full-auto")
        if ctx.workspace_dir:
            argv += ["-C", ctx.workspace_dir]

        for key, value in self._config_overrides(ctx).items():
            argv += ["-c", f"{key}={value}"]

        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        # Codex 没有系统提示词参数，人格只能折进提示词里。
        if ctx.system_prompt:
            prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        env = self.effective_env(ctx)
        env.setdefault("CODEX_DISABLE_TELEMETRY", "1")
        if ctx.skill_home_dir:
            # 严格仅分配：Codex 只从隔离 CODEX_HOME 的 skills/ 读取技能。
            env["CODEX_HOME"] = ctx.skill_home_dir
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt)

    def _config_overrides(self, ctx: AgentContext) -> dict[str, str]:
        overrides: dict[str, str] = {}
        provider = ctx.option("provider_name")
        if provider and ctx.gateway_url:
            env_key = self.api_key_env or "OPENAI_API_KEY"
            overrides["model_provider"] = provider
            overrides[f"model_providers.{provider}.name"] = f'"{provider}"'
            overrides[f"model_providers.{provider}.base_url"] = f'"{ctx.gateway_url}"'
            overrides[f"model_providers.{provider}.env_key"] = f'"{env_key}"'
            overrides[f"model_providers.{provider}.wire_api"] = (
                f'"{ctx.option("wire_api") or "chat"}"'
            )
        effort = ctx.option("reasoning_effort")
        if effort:
            overrides["model_reasoning_effort"] = f'"{effort}"'
        return overrides

    def resume_supported(self, ctx: AgentContext) -> bool:
        return (ctx.option("resume_style") or "subcommand") == "subcommand"

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            yield log(line, "debug")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        if "msg" in payload and isinstance(payload["msg"], dict):  # 旧版事件结构
            payload = {**payload["msg"], "type": payload["msg"].get("type")}

        kind = payload.get("type")

        if kind == "thread.started":
            thread_id = payload.get("thread_id")
            if thread_id:
                run.state["thread"] = thread_id
                yield session(thread_id)
            return

        if kind in ("item.started", "item.completed", "item.updated"):
            yield from self._from_item(payload.get("item") or {}, kind, run)
            return

        if kind == "turn.completed":
            if payload.get("usage"):
                yield usage(**payload["usage"])
            return

        if kind == "turn.failed":
            yield error(flatten_content(payload.get("error") or "本轮执行失败"))
            run.state["is_error"] = True
            return

        if kind == "error":
            yield error(payload.get("message") or "Codex 报错")
            run.state["is_error"] = True
            return

        if kind == "agent_message":  # 旧版事件结构
            if payload.get("message"):
                yield text_delta(payload["message"])
            return

    def _from_item(self, item: dict[str, Any], kind: str, run: AgentRun) -> Iterable[AgentEvent]:
        item_type = item.get("type")
        item_id = item.get("id")

        if item_type == "agent_message":
            # item.completed 携带完整文本，增量则通过 agent_message_delta 到达。
            if kind == "item.completed" and item.get("text"):
                if not run.state.get("saw_delta"):
                    yield text_delta(item["text"])
            return

        if item_type == "agent_message_delta":
            if item.get("delta"):
                run.state["saw_delta"] = True
                yield text_delta(item["delta"])
            return

        if item_type in ("reasoning", "reasoning_delta"):
            text = item.get("text") or item.get("delta")
            if text and kind != "item.started":
                yield thinking_delta(text)
            return

        if item_type in ("command_execution", "local_shell_call", "shell"):
            command = item.get("command")
            if kind == "item.started":
                yield tool_call("shell", item_id, command=flatten_content(command))
            elif kind == "item.completed":
                yield tool_result(
                    item_id,
                    flatten_content(item.get("aggregated_output") or item.get("output") or ""),
                    bool(item.get("exit_code")),
                )
            return

        if item_type in ("file_change", "patch_apply", "apply_patch"):
            if kind == "item.completed":
                yield tool_call("patch", item_id, changes=item.get("changes") or item)
            return

        if item_type in ("mcp_tool_call", "web_search", "todo_list"):
            if kind == "item.completed":
                yield tool_call(item_type, item_id, **{k: v for k, v in item.items() if k != "type"})
            return
