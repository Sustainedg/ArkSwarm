<div align="center">

# ArkSwarm

**多智能体对话控制台** —— 把 Claude Code、Codex、Gemini CLI 等 AI 内核配置成「智能体」，像聊天一样使用它们。

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?style=flat-square&logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![SQLite](https://img.shields.io/badge/SQLite-aiosqlite-003B57?style=flat-square&logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![License](https://img.shields.io/badge/License-查看仓库-blue?style=flat-square)](./LICENSE)

把内核配成智能体：**内核 + 网关 + 模型 + 人格 + 技能 + MCP**，一条命令启动，浏览器里开箱即用。

</div>

---

## ✨ 为什么是 ArkSwarm

| | |
| --- | --- |
| 🔌 **新智能体是插件，不是改动** | 加一个文件即可接入新智能体；表单、SSE 流、会话记忆、权限全部由框架统一处理 |
| 🗣️ **多智能体同屏协作** | 可视化编排「工单」协作：CEO 派活、员工执行、状态机与护栏兜底 |
| 🔑 **密钥统一治理** | 网关是 API 密钥的唯一来源，Fernet 加密落库、接口只回掩码、日志自动脱敏 |
| 🧰 **技能与 MCP 编目** | 各内核的技能与 MCP 服务器统一编目，「严格仅分配」才生效 |
| 📱 **桌面 + 移动双端** | 主站之外物理独立的 `/m/` PWA 移动端 |
| 📊 **用量可视** | token 消耗按日期 / 智能体 / 对话汇总 |

---

## 🚀 快速开始

```bash
git clone https://github.com/Sustainedg/ArkSwarm.git
cd ArkSwarm

# 生产单端口：自动建 venv、构建前端，UI 与 API 由后端统一托管
./scripts/serve.sh            # → http://<本机IP>:8010

# 前后端分离开发（热更新）
./scripts/dev.sh              # → API :8000 + Web UI :5173
```

**环境变量**（全部可选）：`ARKSWARM_HOST`（默认 `0.0.0.0`）、`ARKSWARM_PORT`（默认 `8010`）、`ARKSWARM_DATA_DIR`（数据目录）、`ARKSWARM_AUTH_ENABLED`（多用户鉴权，默认开启）、`ARKSWARM_REGISTRATION_OPEN`（默认开放注册）——完整清单见 [`backend/.env.example`](./backend/.env.example)。开启鉴权后可用 `ARKSWARM_ADMIN_USERNAME` / `ARKSWARM_ADMIN_PASSWORD` 创建管理员；不填时第一个注册的账号自动成为管理员。

首次启动自动播种：为本机**已安装**的智能体类型各创建一条官方网关，并配套一个起始智能体。数据落在 `backend/data/`（SQLite + 每个对话独立的工作目录）。

### 用起来的三步

1. **设置 → 自定义网关** — 新建网关（名称、地址、协议、鉴权、API 密钥），点「获取模型」拿到可选模型并勾选
2. **智能体 → 新建** — 选内核类型、选网关、定默认模型，写头像与系统提示词，按需勾选技能 / MCP，点「连通性测试」
3. **对话 → 新建对话** — 选择智能体开始聊天；工具调用、推理过程、token 用量实时展开
4. **设置 → IM**（可选）— 把智能体连到 Telegram / 飞书 / 钉钉 / 企业微信 / QQ / Discord / Slack，配对账号后就能在聊天软件里直接对话（只用长连接，不需要公网地址）

---

## 🧩 核心概念

| 概念 | 说明 | 存储 |
| --- | --- | --- |
| **智能体类型** | 内置适配器（`claude-code` / `codex` / `gemini-cli` / `openai-harness` / `custom-cli`），声明能力位与配置项 schema | 代码（插件注册表） |
| **运行时** | 智能体类型依赖的本机可执行文件（CLI 内核）；可查看版本并安装 / 更新 / 卸载 | 宿主机 |
| **网关** | **API 密钥的唯一来源**：官方链路或自定义（地址 + 协议 + 鉴权 + 密钥 + 模型白名单） | `gateways` |
| **智能体** | 名称 + 头像 + 系统提示词 + 内核 + 网关 + 模型 + 技能 | `agent_profiles` |
| **对话** | 一次对话：绑定智能体，记住会话 id 与工作目录 | `conversations` |
| **技能** | 从各内核技能目录（`SKILL.md`）统一编目的能力包，**只能通过智能体分配生效** | `skills` + `agent_skills` |
| **MCP 服务器** | 统一编目的工具服务（stdio / http / sse），同样严格仅分配 | `mcp_servers` + `agent_mcp_servers` |
| **消息** | 一轮消息，含工具调用 / 推理过程 / token 用量 | `messages` |
| **IM 连接** | 把聊天软件里的一个机器人绑到一个智能体（Telegram / 飞书 / 钉钉 / 企业微信 / QQ / Discord / Slack），配对后直接在那边对话 | `im_connections` + `im_peers` + `im_chats` |

关系：`智能体类型 → 网关（密钥）→ 智能体 → 对话`。

### 已内置的适配器

| id | 运行时 | 会话续接 | 原生人格 | 备注 |
| --- | --- | --- | --- | --- |
| `claude-code` | CLI `claude` | ✅ `--session-id` / `--resume` | ✅ `--append-system-prompt` | 权限模式、MCP、工具白名单 |
| `codex` | CLI `codex` | ✅ `codex exec resume` | ➖ 注入到 prompt | `-c model_providers.*` 指向自定义网关 |
| `gemini-cli` | CLI `gemini` | ✅ `--resume` | ✅ `GEMINI_SYSTEM_MD` | 自动把人格写入运行期临时文件；支持 MCP |
| `openai-harness` | 进程内 API | 同上 | ✅ | 任意 OpenAI 兼容端点：OpenRouter、vLLM、Ollama… |
| `custom-cli` | CLI 任意命令 | 可声明 | ➖ | **零代码**接入私有 CLI 智能体 |

---

## 🏗️ 架构

```
frontend (React + TS + Vite)
   │  REST /api/*            SSE  POST /api/conversations/{id}/messages
   ▼
FastAPI  ── api/          agents · gateways · conversations · runs · system
         ── runtime/      service(编排+持久化) · runner(执行) · manager(取消/追踪)
         ── agents/       ★ 适配器插件层（唯一需要扩展的地方）
         ── models/db      SQLite + SQLModel
```

所有智能体输出统一翻译成**规范化事件协议**，前端与持久化只认这一套：

```
run.started   message.delta   thinking.delta   tool.call   tool.result
usage         session         log              error       run.finished
```

因此新增一个智能体**不需要**碰前端、流式传输、数据库或权限逻辑。深挖见 [`docs/architecture.md`](./docs/architecture.md)。

---

## 🔌 接入一个新的智能体

<details>
<summary><b>方式 A：写一个适配器</b>（推荐，能力完整）</summary>

在 `backend/app/agents/builtin/` 新建模块：

```python
from ..base import AgentContext, AgentDefinition, Capabilities, ConfigField, FieldType, Invocation, Runtime
from ..events import AgentEvent, session, text_delta, usage
from ..registry import register_agent

@register_agent
class AcmeAgent(AgentDefinition):
    id = "acme"
    name = "Acme 智能体"
    vendor = "Acme"
    runtime = Runtime.CLI
    executable = "acme"
    api_key_env = "ACME_API_KEY"
    base_url_env = "ACME_BASE_URL"
    default_gateway = "https://api.acme.dev/v1"
    default_model = "acme-pro"
    capabilities = Capabilities(streaming=True, sessions=True, native_system_prompt=True, tools=True)
    config_fields = (
        ConfigField("mode", "运行模式", FieldType.SELECT, default="auto",
                    options=[{"value": "auto", "label": "auto"}, {"value": "safe", "label": "safe"}]),
    )

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        argv = ["acme", "run", "--json"]
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.session_id:
            argv += ["--resume", ctx.session_id]
        if ctx.system_prompt:
            argv += ["--system-prompt", ctx.system_prompt]
        return Invocation(argv=argv, env=self.effective_env(ctx),
                          cwd=ctx.workspace_dir, stdin=ctx.prompt)

    def parse_line(self, line: str, run):
        payload = json.loads(line)                     # 按自己的输出格式解析
        if payload.get("session"): yield session(payload["session"])
        if payload.get("text"):    yield text_delta(payload["text"])
```

重启后即可在「智能体配置」里看到它，表单由 `config_fields` 自动生成。

**API 型智能体**改为实现 `async def stream(self, ctx)`；参考 `openai_harness.py`，它把「模型 + 工具循环 + 流式输出」打包成了一个可复用的 harness。

</details>

<details>
<summary><b>方式 B：自定义 CLI</b>（零代码）</summary>

用内置的 `custom-cli` 适配器声明一个任意命令：

```jsonc
{
  "command": "aider",
  "args": ["--model", "{model}", "--message", "{prompt}"],
  "prompt_mode": "arg",
  "output_format": "jsonl",
  "text_path": "message.content",      // JSON 取值路径 → 文本增量
  "delta_mode": true,
  "session_path": "session_id",        // 取到即启用多轮
  "tool_path": "tool.name",            // 工具调用
  "tool_result_path": "tool.output",
  "usage_path": "usage",
  "api_key_env": "ACME_API_KEY",
  "gateway_env": "ACME_BASE_URL"
}
```

占位符：`{prompt} {model} {gateway} {api_key} {workspace} {session} {agent}`。

</details>

---

## 🔐 安全

- **密钥加密**：API 密钥与凭据使用 Fernet 加密落库，接口只回掩码（`sk-a…9f2c`）；密钥来自 `ARKSWARM_SECRET_KEY`，未设置时自动生成 `data/secret.key`（0600）
- **凭据只以环境变量注入**：模型只知道变量名（如 `$GITHUB_TOKEN`），值不进提示词、日志与接口响应；运行层逐条事件擦除凭据值
- **日志自动脱敏**所有出现在 env 中的凭据
- **路径护栏**：`Runtime.API` 型智能体的工具被限制在工作目录内，`../` 逃逸会被拒绝
- **超时与取消**：普通对话默认不限时，可随时中断（消息标记 `interrupted`）；需要护栏的部署可设 `ARKSWARM_RUN_TIMEOUT_SECONDS`
- ⚠️ `bypassPermissions` / `danger-full-access` 等无人值守模式请只在沙箱里使用

---

## 📚 更多

<details>
<summary><b>HTTP API 摘要</b></summary>

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent-types` | 所有适配器及其 config schema、安装状态 |
| GET/POST | `/api/agents` | 列出 / 创建智能体 |
| PATCH/DELETE | `/api/agents/{id}` | 更新 / 删除（软删除：历史对话保留） |
| POST | `/api/agents/{id}/test` | 连通性探测（适配器 / 运行时 / 网关三段检查） |
| PUT | `/api/agents/{id}/skills` | 设置该智能体的技能分配 |
| PUT | `/api/agents/{id}/mcp-servers` | 设置该智能体的 MCP 分配 |
| GET/POST | `/api/mcp-servers` | 列出 / 新建 MCP 服务器（统一目录） |
| POST | `/api/mcp-servers/rescan` | 重新扫描各内核配置里的 MCP 服务器 |
| GET/PATCH/DELETE | `/api/mcp-servers/{id}` | 查看 / 更新 / 删除 |
| GET/POST | `/api/gateways` | 列出 / 创建网关 |
| PATCH/DELETE | `/api/gateways/{id}` | 更新 / 删除（官方链路不可删） |
| POST | `/api/gateways/probe` | 按填写的地址 / 协议 / 鉴权 / 密钥**获取模型**（只探测，不落库） |
| POST | `/api/gateways/{id}/refresh-models` | 重新拉取模型清单 |
| GET | `/api/runtimes` | 各运行时的安装状态、版本与维护方式 |
| POST | `/api/runtimes/{id}/install` · `/update` · `/uninstall` | **SSE** 安装 / 更新 / 卸载 |
| POST | `/api/runtimes/{id}/check` | 查询远端最新版本 |
| GET/POST | `/api/conversations` | 对话管理 |
| POST | `/api/conversations/{id}/messages` | **SSE** 发送消息并流式接收回复 |
| POST | `/api/conversations/{id}/regenerate` | 重跑最后一轮 |
| POST | `/api/conversations/{id}/cancel` | 中断正在运行的智能体 |
| POST | `/api/conversations/{id}/session/reset` | 丢弃智能体侧会话记忆 |
| GET | `/api/system/overview` · `/api/runs` | 运行状态与实时任务 |
| GET | `/api/usage/overview` | 用量汇总：token 消耗与技能分配 |
| GET | `/api/im/platforms` | 可连接的 IM 平台及字段（添加表单由它生成） |
| GET/POST | `/api/im/connections` | 列出 / 新建 IM 连接（保存前先向平台核对凭据） |
| PATCH/DELETE | `/api/im/connections/{id}` | 改绑定 / 启停 / 移除（移除时一并删配对与聊天） |
| POST | `/api/im/connections/{id}/retry` | 重连一次 |
| GET | `/api/im/peers` | 等待允许的连接请求与已配对账号 |
| POST/DELETE | `/api/im/connections/{id}/peers/{peer_id}` | 允许 / 拒绝 / 移除配对账号 |
| GET | `/api/im/connections/{id}/chats` | 连接见过的聊天（群可单独指定智能体） |

交互式文档：启动后访问 `http://127.0.0.1:8010/docs`。

</details>

<details>
<summary><b>目录结构</b></summary>

```
backend/
  app/agents/            适配器层（base · events · registry · builtin/*）
  app/runtime/           service（编排/持久化）· runner（执行）· manager（运行表）·
                         provisioning（运行时的安装 / 更新 / 卸载）
  app/im/                IM 域：base（平台元数据/契约）· platforms/*（各平台收发）·
                         service（连接登记表/配对/发送队列）· bridge（与对话域的唯一交界）
  app/api/               路由 · SSE
  app/models.py db.py    数据模型与引擎
  tests/                 适配器解析、API 契约、harness 工具循环、架构守卫
frontend/
  src/pages/             Chat · Agents · Runtimes · Skills · MCP · Usage · Settings
  src/components/        DynamicForm（由 schema 生成表单）· MessageView · Modal
  src/mobile/            移动端（PWA）
docs/
  architecture.md             一次运行的完整时序、事件协议、扩展点
  im.md                       IM：支持哪些平台、配对与消息路由、分层
  ai-coding-standard.md       编码规范详版
  collaboration-design.md     多智能体协作设计详版
  collab-guide.html           协作机制图文版（浏览器直接打开）
```

</details>

---

## 🤝 参与开发

改代码前先读 [`AGENTS.md`](./AGENTS.md)（分层铁律与扩展手册），需要理由与边界时读 [`docs/ai-coding-standard.md`](./docs/ai-coding-standard.md)。分层约束由 `backend/tests/test_architecture.py` 自动校验。

```bash
./scripts/test.sh     # pytest（含架构守卫）+ 前端 typecheck
```

---

## ⬆️ 更新

```bash
git pull
(cd frontend && npm install && npm run build)   # 前端有变动时
./scripts/serve.sh                                # 重启服务
```
