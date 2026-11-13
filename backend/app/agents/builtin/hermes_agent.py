"""Nous Research Hermes Agent CLI 适配器。"""
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
    RuntimeProvisioner,
)
from ..events import (
    AgentEvent,
    error,
    log,
    session,
    text_delta,
    tool_call,
    tool_result,
    usage,
)
from ..mcp import McpMount, McpServerSpec, parse_json_servers, to_json_servers
from ..registry import register_agent
from ..skills import (
    MARKET_SOURCE,
    SkillMount,
    SkillRoot,
    SkillSpec,
    scan_skill_roots,
)
from .util import as_list, flatten_content, truthy


#: Hermes 的配置目录（默认 ``~/.hermes``），``HERMES_HOME`` 可整体挪走。
_CONFIG_DIR = Path.home() / ".hermes"

#: Hermes 的主配置文件（YAML 格式），MCP 服务器声明在其中的 ``mcp_servers`` 块。
_CONFIG_FILE = "config.yaml"

#: 平台「技能市场」安装目录（``<config>/skills/.skillhub/<skill>``），可卸载。
_MARKET_DIRNAME = ".skillhub"


def hermes_home() -> Path:
    """Hermes 的配置目录（认 ``HERMES_HOME`` 环境变量）。"""
    override = os.environ.get("HERMES_HOME")
    return Path(override).expanduser() if override else _CONFIG_DIR


@register_agent
class HermesAgent(AgentDefinition):
    id = "hermes-agent"
    name = "Hermes Agent"
    vendor = "Nous Research"
    description = (
        "Nous Research 的自进化智能体。内置学习循环——从经验中创建技能、"
        "使用中改进技能，支持流式 JSONL 输出、会话续接、技能与 MCP。"
    )
    runtime = Runtime.CLI
    executable = "hermes"
    provisioner = RuntimeProvisioner(
        manager="script",
        package="hermes-agent",
        #: 官方安装脚本：自动获取 uv / Python、克隆仓库、发布命令到 PATH。
        #: 不依赖 pipx / npm，只需 curl + bash。
        install_argv=[
            "sh", "-c",
            "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
        ],
        #: Hermes 自带更新命令（重新跑上游安装器）。
        update_argv=["hermes", "update", "--yes"],
        #: 卸载：删掉安装目录与配置目录。
        uninstall_argv=[
            "sh", "-c",
            'rm -rf "${HERMES_INSTALL_DIR:-$HOME/.local/share/hermes-agent}" "$HOME/.hermes"',
        ],
        #: 查询远端最新版本（不安装，只检查）。需网络访问 GitHub；
        #: 若服务器无法直连，可在设置页配置代理。
        latest_argv=["hermes", "update", "--check"],
        homepage="https://github.com/NousResearch/hermes-agent",
    )
    api_key_env = "OPENROUTER_API_KEY"
    base_url_env = "OPENROUTER_BASE_URL"
    default_gateway = "https://openrouter.ai/api/v1"
    default_model = "anthropic/claude-sonnet-4.6"
    protocol = "openai"
    default_auth_style = "bearer"
    default_models = [
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.6",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
    ]
    docs_url = "https://hermes-agent.nousresearch.com/docs/"
    logo = "/icons/kernels/hermes.svg"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=True,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
        skills=True,
    )
    config_fields = (
        ConfigField(
            "approval_mode",
            "审批模式",
            FieldType.SELECT,
            default="yolo",
            options=[
                {"value": "yolo", "label": "yolo · 自动批准所有工具调用"},
                {"value": "safe", "label": "safe · 安全模式（禁用个人配置与规则）"},
                {"value": "default", "label": "default · 每次询问"},
            ],
        ),
        ConfigField(
            "system_prompt_mode",
            "人格注入方式",
            FieldType.SELECT,
            default="prepend",
            options=[
                {"value": "prepend", "label": "拼接到提示词最前面"},
                {"value": "config", "label": "写入 config.yaml 的 system_prompt"},
            ],
        ),
        ConfigField(
            "toolsets",
            "启用的工具集",
            FieldType.STRING_LIST,
            placeholder="web,terminal,skills",
            help="逗号分隔；留空则用 Hermes 默认工具集。",
        ),
        ConfigField(
            "provider",
            "模型供应商",
            FieldType.STRING,
            advanced=True,
            placeholder="openrouter / openai / anthropic",
            help="覆盖 config.yaml 里的默认供应商（配合网关使用）。",
        ),
        ConfigField(
            "oneshot",
            "单次模式",
            FieldType.BOOLEAN,
            default=True,
            help="回答后即退出，不启动交互式会话（平台默认开启）。",
        ),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    # -- 技能 ---------------------------------------------------------------

    def skill_roots(self) -> list[SkillRoot]:
        """Hermes 的技能根：``~/.hermes/skills/`` 是主目录，``.skillhub`` 是市场安装目录。"""
        home = hermes_home()
        skills_dir = home / "skills"
        return [
            SkillRoot(str(skills_dir), source_kind="user", installable=True, max_depth=2),
            SkillRoot(
                str(skills_dir / _MARKET_DIRNAME),
                source_kind=MARKET_SOURCE,
                installable=True,
                max_depth=2,
            ),
        ]

    def discover_skills(self) -> list[SkillSpec]:
        """只读扫描 ``~/.hermes/skills/`` 下的技能（尽力而为、不得抛错）。"""
        return scan_skill_roots(self.skill_roots(), type_id=self.id)

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """把分配到的技能物化进隔离配置目录的 ``skills/`` 子目录。

        Hermes 通过 ``HERMES_HOME`` 环境变量认配置目录，技能放在其下的 ``skills/`` 里。
        """
        return SkillMount(
            home_name="hermes-home",
            skills_subdir="skills",
        )

    # -- MCP 服务器 ----------------------------------------------------------

    def discover_mcp_servers(self) -> list[McpServerSpec]:
        """扫描 ``~/.hermes/config.yaml`` 里的 ``mcp_servers`` 块（只读、尽力而为）。"""
        try:
            import yaml

            raw = yaml.safe_load(
                (hermes_home() / _CONFIG_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ImportError):
            return []
        if not isinstance(raw, dict):
            return []
        servers = raw.get("mcp_servers")
        if not isinstance(servers, dict):
            return []
        # Hermes 的 MCP 配置形状与 Claude Code 的 mcpServers 一致。
        return parse_json_servers(
            {"mcpServers": servers}, type_id=self.id, source_kind="user"
        )

    def mcp_mount(self, ctx: AgentContext) -> McpMount:
        """把分配到的服务器写进隔离配置目录的 ``config.yaml``。

        Hermes 的 MCP 配置在 ``config.yaml`` 的 ``mcp_servers`` 块里，
        形状与 Claude Code 的 ``mcpServers`` 一致（command/args/env 或 url/headers）。
        """
        import yaml

        content = {
            "mcp_servers": to_json_servers(ctx.mcp_servers),
        }
        return McpMount(
            home_name="hermes-home",
            files={
                _CONFIG_FILE: yaml.dump(content, default_flow_style=False, allow_unicode=True),
            },
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv = [self.executable or "hermes", "chat"]

        # 单次模式：回答后退出（平台默认）
        if truthy(options.get("oneshot", True)):
            argv.append("--oneshot")

        # 流式 JSONL 输出
        argv += ["--format", "stream-json"]

        # 模型
        if ctx.model:
            argv += ["--model", ctx.model]

        # 供应商
        provider = options.get("provider")
        if provider:
            argv += ["--provider", str(provider)]

        # 审批模式
        mode = options.get("approval_mode") or "yolo"
        if mode == "safe":
            argv.append("--safe-mode")

        # 工具集
        toolsets = as_list(options.get("toolsets") or [])
        if toolsets:
            argv += ["--toolsets", ",".join(toolsets)]

        # 会话续接
        if ctx.session_id:
            argv += ["--session", ctx.session_id]

        # 额外参数
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        # 提示词与人格
        prompt = ctx.prompt
        env = self.effective_env(ctx)
        files: dict[str, str] = {}

        # 隔离配置目录（技能与 MCP 共用）
        if ctx.home_dir:
            env["HERMES_HOME"] = ctx.home_dir

        # 人格注入
        if ctx.system_prompt:
            sp_mode = options.get("system_prompt_mode") or "prepend"
            if sp_mode == "config":
                import yaml

                files[_CONFIG_FILE] = yaml.dump(
                    {"system_prompt": ctx.system_prompt},
                    default_flow_style=False,
                    allow_unicode=True,
                )
            else:
                prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        # 用 -q 把提示词走命令行参数（Hermes 要求 --format stream-json 配合 -q）
        argv += ["-q", prompt]

        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, files=files)

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
        kind = payload.get("type")

        # system: 初始化事件，携带 session_id 与 model
        if kind == "system":
            subtype = payload.get("subtype")
            if subtype == "init":
                sid = payload.get("session_id")
                if sid:
                    run.state["session"] = sid
                    yield session(sid)
                if payload.get("model"):
                    yield log(f"Hermes 已就绪 · 模型 {payload['model']}")
            return

        # text: 流式文本增量
        if kind == "text":
            text = payload.get("text")
            if text:
                yield text_delta(text)
            return

        # tool_use: 工具调用
        if kind == "tool_use":
            name = payload.get("name") or "tool"
            input_ = payload.get("input") or {}
            yield tool_call(
                name,
                payload.get("id") or payload.get("tool_id"),
                **(input_ if isinstance(input_, dict) else {"input": input_}),
            )
            return

        # tool_result: 工具结果
        if kind == "tool_result":
            yield tool_result(
                payload.get("id") or payload.get("tool_id") or payload.get("name"),
                flatten_content(payload.get("output") or payload.get("result")),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        # result: 终止事件，携带用量统计与退出码
        if kind == "result":
            tokens = payload.get("tokens")
            if isinstance(tokens, dict) and tokens:
                yield usage(**tokens)
            exit_code = payload.get("exit_code")
            if payload.get("error"):
                run.state["is_error"] = True
                yield error(flatten_content(payload["error"]))
            elif exit_code is not None and exit_code not in (0, 130):
                run.state["is_error"] = True
                yield error(f"Hermes 以退出码 {exit_code} 结束")
            return

        if kind == "error":
            run.state["is_error"] = True
            yield error(payload.get("message") or flatten_content(payload))
            return
