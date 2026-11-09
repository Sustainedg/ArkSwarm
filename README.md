# Magenta Agents · 多智能体对话控制台

一个用于**管理并对话各种 CLI / API 型 agent** 的网站。把 Claude Code、Codex、Gemini CLI、
DeepSeek 等 agent 注册成可配置的实例（自定义 Gateway + API Key + 模型），再给它们套上
**角色（人格）**，就能像聊天一样使用。

> 设计重点：**新 agent 是插件，不是改动**。加一个文件即可接入新 agent；前端表单、SSE 流、
> 会话记忆、权限与权限模式全部由框架统一处理。

---

## 核心概念

| 概念 | 说明 | 存储 |
| --- | --- | --- |
| **Agent Type** | 内置适配器（`claude-code` / `codex` / `gemini-cli` / `deepseek-harness` / `openai-harness` / `custom-cli`），声明能力位与配置项 schema | 代码（插件注册表） |
| **Agent** | 一种类型的**实例**：名称 + Gateway + API Key + 模型 + 适配器选项 | `agent_instances` |
| **Role** | 角色：头像 + 系统提示词 + 绑定某个 Agent（可覆盖模型 / Gateway） | `roles` |
| **Conversation** | 一次对话：绑定角色，记住 agent 侧 session id 与工作目录 | `conversations` |
| **Message** | 一轮消息，含工具调用 / 推理过程 / token 用量 | `messages` |

关系：`Agent Type → Agent 实例 → Role → Conversation`。
多个角色可以复用同一个 Agent；一个 Agent 可以承载多个角色。

---

## 架构

```
frontend (React + TS + Vite)
   │  REST /api/*            SSE  POST /api/conversations/{id}/messages
   ▼
FastAPI  ── api/          agents · roles · conversations · runs · system
         ── runtime/      service(编排+持久化) · runner(执行) · manager(取消/追踪)
         ── agents/       ★ 适配器插件层（唯一需要扩展的地方）
         ── models/db      SQLite + SQLModel
```

适配器层由三块组成：

1. **`AgentDefinition`** — 声明式元数据：`capabilities`（是否支持会话续接、原生人格、
   工具、MCP、权限模式…）、`config_fields`（可配置项 schema）、凭据环境变量约定、
   可选的可执行文件（用于探测是否安装）。
2. **`Invocation` / `parse_line`** — CLI 型 agent：把运行请求编译成 `argv + env + stdin`，
   再把 stdout 的每一行翻译成**规范化事件**。
3. **`async stream()`** — API 型 agent：在进程内直接产出规范化事件（内置工具循环）。

规范化事件协议（前端与持久化只认这一套，与 agent 无关）：

```
run.started   message.delta   thinking.delta   tool.call   tool.result
usage         session         log              error       run.finished
```

因此新增一个 agent **不需要**碰前端、流式传输、数据库或权限逻辑。

### 已内置的适配器

| id | 运行时 | 会话续接 | 原生人格 | 备注 |
| --- | --- | --- | --- | --- |
| `claude-code` | CLI `claude` | ✅ `--session-id` / `--resume` | ✅ `--append-system-prompt` | 支持权限模式、MCP、工具白名单 |
| `codex` | CLI `codex` | ✅ `codex exec resume` | ➖ 注入到 prompt | 支持 `-c model_providers.*` 指向自定义网关 |
| `gemini-cli` | CLI `gemini` | ✅ `--resume` | ✅ `GEMINI_SYSTEM_MD` | 自动把人格写入运行期临时文件 |
| `deepseek-harness` | 进程内 API | 由平台回放上下文 | ✅ | 内置工具循环，默认走 DeepSeek |
| `openai-harness` | 进程内 API | 同上 | ✅ | 任意 OpenAI 兼容端点：OpenRouter、vLLM、Ollama… |
| `custom-cli` | CLI 任意命令 | 可声明 | ➖ | **零代码**接入私有 CLI agent |

---

## 快速开始

```bash
# 生产单端口：构建前端 + 由后端托管 UI 与 API，监听 0.0.0.0
./scripts/serve.sh          # → http://<本机IP>:8010

# 强制重新构建前端后再启动
./scripts/build.sh

# 前后端分离开发（热更新）
./scripts/dev.sh            # → API :8000 + Web UI :5173
```

环境变量：`MAGENTA_HOST`（默认 `0.0.0.0`）、`MAGENTA_PORT`（默认 `8010`）。

### 部署状态（本机 PyPanel）

已注册为 PyPanel 服务 **Magenta Agents**，由面板托管启停、日志与资源监控：

| 项 | 值 |
| --- | --- |
| 访问地址 | `http://192.168.2.168:8010` |
| 监听 | `0.0.0.0:8010`（局域网可直接访问） |
| 启动命令 | `bash scripts/serve.sh`（cwd 为仓库根） |
| 服务环境变量 | `MAGENTA_HOST=0.0.0.0` · `MAGENTA_PORT=8010` |
| 开机自启 | 已开启 |
| API 文档 | `http://192.168.2.168:8010/docs` |

#### 部署注意事项：`HOME`

PyPanel 会给子进程注入**面板自己的** `HOME`（`…/pypanel/data/home`），而 `claude` 这类 CLI
会把凭据与配置放在 `$HOME/.claude/`。因此在服务里显式指定了真实用户的家目录：

```
HOME=/root
```

否则会出现 `Not logged in · Please run /login`。若你改用「在网页里填 Gateway + API Key」的方式，
这一点不影响；只有依赖环境里既存凭据（ambient credentials）时才需要。

改了前端代码后需要重新构建，面板里的「重启」不会自动 build：

```bash
cd /vol1/1000/同步库/python/项目/Bot
(cd frontend && npm install && npm run build)
curl -X POST http://127.0.0.1:8899/api/services/e0c0620a5786/restart -H "X-API-Key: <你的 Key>"
```

首次启动会自动播种：为当前机器上**已安装**的 agent 类型各创建一个默认实例，并配套一个起始角色。
数据落在 `backend/data/`（SQLite + 每个对话独立的 workspace）。

### 用起来的三步

1. **Agent 配置** → 新建 Agent，选择类型（例如 Claude Code），填自定义 Gateway 与 API Key，
   按需调整适配器选项（权限模式、鉴权头风格、沙箱模式…），点「连通性测试」。
2. **角色** → 新建角色，选头像、写系统提示词、绑定上一步的 Agent。
3. **对话** → 新建对话选择角色，开始聊天；工具调用、推理过程、token 用量都会实时展开。

---

## 接入一个新的 Agent

### 方式 A：写一个适配器（推荐，能力完整）

在 `backend/app/agents/builtin/` 新建模块：

```python
from ..base import AgentContext, AgentDefinition, Capabilities, ConfigField, FieldType, Invocation, Runtime
from ..events import AgentEvent, session, text_delta, usage
from ..registry import register_agent

@register_agent
class AcmeAgent(AgentDefinition):
    id = "acme"
    name = "Acme Agent"
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

重启后即可在「Agent 配置」里看到它，表单由 `config_fields` 自动生成。

**API 型 agent** 改为实现 `async def stream(self, ctx)`；参考 `openai_harness.py`，
它把「模型 + 工具循环 + 流式输出」打包成了一个可复用的 harness。

### 方式 B：Custom CLI（零代码）

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

占位符：`{prompt} {model} {gateway} {api_key} {workspace} {session} {role} {agent}`。

---

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent-types` | 所有适配器及其 config schema、安装状态 |
| GET/POST | `/api/agents` | 列出 / 创建 Agent 实例 |
| PATCH/DELETE | `/api/agents/{id}` | 更新 / 删除（被角色引用时返回 409） |
| POST | `/api/agents/{id}/test` | 连通性探测（适配器 / 运行时 / 网关三段检查） |
| GET/POST | `/api/roles` | 角色管理 |
| GET/POST | `/api/conversations` | 对话管理 |
| POST | `/api/conversations/{id}/messages` | **SSE** 发送消息并流式接收回复 |
| POST | `/api/conversations/{id}/regenerate` | 重跑最后一轮 |
| POST | `/api/conversations/{id}/cancel` | 中断正在运行的 agent |
| POST | `/api/conversations/{id}/session/reset` | 丢弃 agent 侧会话记忆 |
| GET | `/api/system/overview` · `/api/runs` | 运行状态与实时任务 |

交互式文档：`http://127.0.0.1:8000/docs`

---

## 安全与运维

- API Key 使用 **Fernet 加密**后落库，接口只回传掩码（`sk-a…9f2c`），永不回传明文；
  密钥来自 `MAGENTA_SECRET_KEY`，未设置时自动生成 `data/secret.key`（0600）。
- 命令行日志会**自动脱敏**所有出现在 env 中的凭据。
- `Runtime.API` 型 agent 的工具被限制在工作目录内，`../` 逃逸与越界路径会被拒绝。
- 单个 agent turn 默认 3600s 超时；可随时取消（进程会被 kill，消息标记为 `interrupted`）。
- `permission_mode=bypassPermissions` / `danger-full-access` 等无人值守模式请只在沙箱里使用。

---

## 目录结构

```
backend/
  app/agents/            适配器层（base · events · registry · builtin/*）
  app/runtime/           service（编排/持久化）· runner（执行）· manager（运行表）
  app/api/               路由 · SSE
  app/models.py db.py    数据模型与引擎
  tests/                 23 个测试：适配器解析、API 契约、harness 工具循环
frontend/
  src/pages/             Chat · Agents · Roles · Settings
  src/components/        DynamicForm（由 schema 生成表单）· MessageView · Modal
docs/architecture.md     深挖：一次运行的完整时序、事件协议、扩展点
```

## 校验

```bash
./scripts/test.sh     # pytest + 前端 typecheck
```
