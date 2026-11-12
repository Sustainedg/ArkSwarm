# Magenta Agents · 多智能体对话控制台

一个用于**管理并对话各种 CLI / API 型智能体**的网站。把 Claude Code、Codex、Gemini CLI
等内核配成**智能体**（内核 + 网关 + 模型 + 人格），就能像聊天一样使用。

> 设计重点：**新智能体是插件，不是改动**。加一个文件即可接入新智能体；前端表单、SSE 流、
> 会话记忆、权限与权限模式全部由框架统一处理。

---

## 核心概念

| 概念 | 说明 | 存储 |
| --- | --- | --- |
| **智能体类型** | 内置适配器（`claude-code` / `codex` / `gemini-cli` / `openai-harness` / `custom-cli`），声明能力位与配置项 schema | 代码（插件注册表） |
| **运行时** | 智能体类型依赖的**本机可执行文件**（CLI 内核）；可在「运行时」页查看版本并安装 / 更新 / 卸载 | 宿主机（命令由适配器声明） |
| **网关** | **API 密钥的唯一来源**。官方链路（按适配器声明播种）或自定义网关（名称 + 地址 + 协议 + 鉴权 + 密钥 + 选中模型） | `gateways` |
| **智能体**（`AgentProfile`） | 名称 + 头像 + 系统提示词 + 选一个内核 + 引用一个网关 + 从网关白名单里选模型 + 技能 | `agent_profiles` |
| **对话** | 一次对话：绑定智能体，记住智能体侧会话 id 与工作目录 | `conversations` |
| **技能** | 从各智能体内核的技能目录（`SKILL.md`）统一编目出来的能力包，**只能通过智能体分配生效** | `skills` + `agent_skills` |
| **消息** | 一轮消息，含工具调用 / 推理过程 / token 用量 | `messages` |

关系：`智能体类型 → 网关（密钥）→ 智能体 → 对话`，技能从各内核编目后由 `智能体 ← 技能` 分配。
网关是密钥的唯一来源（智能体不另存密钥）；智能体能用的技能只来自它自己的分配。

---

## 架构

```
frontend (React + TS + Vite)
   │  REST /api/*            SSE  POST /api/conversations/{id}/messages
   ▼
FastAPI  ── api/          agents · gateways · conversations · runs · system
         ── runtime/      service(编排+持久化) · runner(执行) · manager(取消/追踪)
         ── agents/       ★ 适配器插件层（唯一需要扩展的地方）
         ── models/db      SQLite + SQLModel
```

适配器层由三块组成：

1. **`AgentDefinition`** — 声明式元数据：`capabilities`（是否支持会话续接、原生人格、
   工具、MCP、权限模式…）、`config_fields`（可配置项 schema）、凭据环境变量约定、
   可选的可执行文件（用于探测是否安装）。
2. **`Invocation` / `parse_line`** — CLI 型智能体：把运行请求编译成 `argv + env + stdin`，
   再把 stdout 的每一行翻译成**规范化事件**。
3. **`async stream()`** — API 型智能体：在进程内直接产出规范化事件（内置工具循环）。

规范化事件协议（前端与持久化只认这一套，与具体智能体无关）：

```
run.started   message.delta   thinking.delta   tool.call   tool.result
usage         session         log              error       run.finished
```

因此新增一个智能体 **不需要**碰前端、流式传输、数据库或权限逻辑。

### 已内置的适配器

| id | 运行时 | 会话续接 | 原生人格 | 备注 |
| --- | --- | --- | --- | --- |
| `claude-code` | CLI `claude` | ✅ `--session-id` / `--resume` | ✅ `--append-system-prompt` | 支持权限模式、MCP、工具白名单 |
| `codex` | CLI `codex` | ✅ `codex exec resume` | ➖ 注入到 prompt | 支持 `-c model_providers.*` 指向自定义网关 |
| `gemini-cli` | CLI `gemini` | ✅ `--resume` | ✅ `GEMINI_SYSTEM_MD` | 自动把人格写入运行期临时文件 |
| `openai-harness` | 进程内 API | 同上 | ✅ | 任意 OpenAI 兼容端点：OpenRouter、vLLM、Ollama… |
| `custom-cli` | CLI 任意命令 | 可声明 | ➖ | **零代码**接入私有 CLI 智能体 |

> `claude-code` 与 `codex` 支持**技能**：平台只读扫描它们各自的技能目录并统一编目，
> 智能体勾选哪些技能，运行时就把哪些技能物化进该内核的隔离配置目录（「严格仅分配」，
> 本机个人/插件技能不会被顺手带进一次运行）。详见 `docs/architecture.md` §5。

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

否则会出现 `Not logged in · Please run /login`。若你改用「在网页里填网关 + API 密钥」的方式，
这一点不影响；只有依赖环境里既存凭据（ambient credentials）时才需要。

改了前端代码后需要重新构建，面板里的「重启」不会自动 build：

```bash
cd /vol1/1000/同步库/python/项目/Bot
(cd frontend && npm install && npm run build)
curl -X POST http://127.0.0.1:8899/api/services/e0c0620a5786/restart -H "X-API-Key: <你的 Key>"
```

首次启动会自动播种：为当前机器上**已安装**的智能体类型各创建一条官方网关，并配套一个起始智能体。
数据落在 `backend/data/`（SQLite + 每个对话独立的工作目录）。

### 用起来的三步

1. **设置 → 自定义网关** → 新建网关（名称、地址、协议、鉴权方式、API 密钥），
   点「获取模型」拿到可选模型并勾选（必须成功才能保存；也可手动旁路录入模型名）。
2. **智能体** → 新建智能体：内核类型用下拉框选（例如 Claude Code）、选网关、
   定「默认模型」（新对话默认用它，具体对话里还能改），写头像与系统提示词、
   按需从「技能目录」里勾选技能，点「连通性测试」。网关选「官方默认」表示平台不接管路由、
   沿用内核自带的网关与凭据；选自定义网关则由平台注入地址、密钥与模型白名单。
   智能体还可以引用**凭据**（GitHub token、数据库口令…）：点名字输入框就是**凭据库**，
   挑一条即引用、输入新名字 + 值即顺手新建。凭据全局一份、本地加密保存、运行时只以环境变量
   注入，值不会出现在提示词、日志与接口响应里 —— 它和「网关密钥」是两回事。
   完整清单与换值在「设置 → 凭据库」。工作目录不在这里设：新建对话 / 新建协作任务时才指定。
3. **对话** → 新建对话选择智能体，开始聊天；工具调用、推理过程、token 用量都会实时展开。

---

## 接入一个新的智能体

### 方式 A：写一个适配器（推荐，能力完整）

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

**API 型智能体** 改为实现 `async def stream(self, ctx)`；参考 `openai_harness.py`，
它把「模型 + 工具循环 + 流式输出」打包成了一个可复用的 harness。

### 方式 B：自定义 CLI（零代码）

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

---

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent-types` | 所有适配器及其 config schema、安装状态 |
| GET/POST | `/api/agents` | 列出 / 创建智能体 |
| PATCH/DELETE | `/api/agents/{id}` | 更新 / 删除（软删除：从列表消失，历史对话保留） |
| POST | `/api/agents/{id}/test` | 连通性探测（适配器 / 运行时 / 网关三段检查） |
| PUT | `/api/agents/{id}/skills` | 设置该智能体的技能分配 |
| GET/POST | `/api/gateways` | 列出 / 创建网关（含官方链路与自定义网关） |
| PATCH/DELETE | `/api/gateways/{id}` | 更新 / 删除（官方链路不可删；仍被智能体引用时返回 409） |
| POST | `/api/gateways/probe` | 按填写的地址 / 协议 / 鉴权 / 密钥**获取模型**（只探测，不落库） |
| POST | `/api/gateways/{id}/refresh-models` | 重新拉取某个网关的模型清单 |
| GET | `/api/runtimes` | 列出各运行时（CLI 内核）的安装状态、版本与维护方式 |
| POST | `/api/runtimes/{id}/install` · `/update` · `/uninstall` | **SSE** 安装 / 更新 / 卸载（卸载需 `confirm`） |
| POST | `/api/runtimes/{id}/check` | 查询该运行时远端最新版本 |
| GET/POST | `/api/conversations` | 对话管理 |
| POST | `/api/conversations/{id}/messages` | **SSE** 发送消息并流式接收回复 |
| POST | `/api/conversations/{id}/regenerate` | 重跑最后一轮 |
| POST | `/api/conversations/{id}/cancel` | 中断正在运行的智能体 |
| POST | `/api/conversations/{id}/session/reset` | 丢弃智能体侧会话记忆 |
| GET | `/api/system/overview` · `/api/runs` | 运行状态与实时任务 |

交互式文档：`http://127.0.0.1:8000/docs`

---

## 安全与运维

- API 密钥与智能体凭据都使用 **Fernet 加密**后落库，接口只回传掩码（`sk-a…9f2c`），永不回传明文；
  密钥来自 `MAGENTA_SECRET_KEY`，未设置时自动生成 `data/secret.key`（0600）。
- **凭据库**（设置页）是全局一份、可被多个智能体引用；智能体**凭据只以环境变量注入**子进程
  （`app/credentials.py` + `runtime/credentials.py`）：模型只知道变量名（`$GITHUB_TOKEN`），
  值不进提示词、落库内容与 SSE；运行层还会逐条事件擦除凭据值，即使内核把它回显出来也只会看到 `***`。
- 命令行日志会**自动脱敏**所有出现在 env 中的凭据。
- `Runtime.API` 型智能体的工具被限制在工作目录内，`../` 逃逸与越界路径会被拒绝。
- 单轮智能体运行默认 3600 秒超时；可随时取消（进程会被 kill，消息标记为 `interrupted`）。
- `permission_mode=bypassPermissions` / `danger-full-access` 等无人值守模式请只在沙箱里使用；
  Claude Code 适配器在**以 root 运行时**会自动声明 `IS_SANDBOX=1`（内核要求），否则它会以
  `--dangerously-skip-permissions cannot be used with root/sudo privileges` 拒绝启动。

---

## 目录结构

```
backend/
  app/agents/            适配器层（base · events · registry · builtin/*）
  app/runtime/           service（编排/持久化）· runner（执行）· manager（运行表）·
                         provisioning（运行时的安装 / 更新 / 卸载）
  app/api/               路由 · SSE
  app/models.py db.py    数据模型与引擎
  tests/                 适配器解析、API 契约、harness 工具循环、架构守卫
frontend/
  src/pages/             Chat · Agents · Runtimes · Skills · Settings
  src/components/        DynamicForm（由 schema 生成表单）· MessageView · Modal
AGENTS.md                ★ 工程约定（AI/人都要遵守的硬约束）
docs/architecture.md     深挖：一次运行的完整时序、事件协议、扩展点
docs/ai-coding-standard.md  编码规范详版：分层理由、契约、扩展决策树、自检清单
docs/collaboration-design.md 多智能体协作（工单机制 · 状态机 · 护栏）设计详版
docs/collab-guide.html       协作的机制 · 权限 · 流程（小白版图文，浏览器直接打开）
```

## 参与开发

改代码前先读 [`AGENTS.md`](./AGENTS.md)（分层铁律与扩展手册），需要理由与边界时读
[`docs/ai-coding-standard.md`](./docs/ai-coding-standard.md)。分层约束由
`backend/tests/test_architecture.py` 自动校验，跑 `./scripts/test.sh` 即可。

## 校验

```bash
./scripts/test.sh     # pytest（含架构守卫）+ 前端 typecheck
```
