# AGENTS.md · Magenta Agents 工程约定

本文件对**所有在本仓库工作的 AI 编码代理**具有约束力。只要在这个仓库里改代码，就必须遵守。
动手之前请先读完本文件；需要背景与理由，再读 `docs/ai-coding-standard.md` 与 `docs/architecture.md`。

优先级：**用户当次指令 > 本文件 > 既有代码风格**。但若本文件与代码现状冲突，先按本文件执行，
并在最终回复里说明你改动了什么、为什么。

---

## 0. 事实基线（不要凭想象）

| 项 | 事实 |
| --- | --- |
| 产品 | Magenta Agents —— 多智能体对话控制台，管理 Claude Code / Codex / Gemini CLI 等智能体，为它们配置网关、模型与人格 |
| 后端 | Python 3.11 · FastAPI 0.115 · SQLModel + SQLite(aiosqlite) · Pydantic v2 · httpx（依赖见 `backend/requirements.txt`） |
| 前端 | React 18 + TypeScript + Vite 6 · react-router · **无 UI 框架，手写 CSS**（`frontend/src/index.css`） |
| 后端入口 | `backend/app/main.py`（`uvicorn app.main:app --app-dir backend`） |
| API 前缀 | `/api`，路由聚合在 `backend/app/api/__init__.py` |
| **唯一扩展点** | `backend/app/agents/builtin/`（新增一个模块 + `@register_agent` 即接入新智能体） |
| 端口/部署 | 监听 `0.0.0.0:8010`，由 PyPanel 服务 `e0c0620a5786` 托管（`scripts/serve.sh`） |
| 移动端 | **物理独立的第二条链接 `/m/`**：入口 `frontend/m/index.html` + `frontend/src/mobile/`，PWA 资产在 `frontend/public/m/`（manifest + Service Worker） |
| 测试 | `bash scripts/test.sh` → pytest（`backend/tests/`：适配器 · API · 架构守卫）+ 前端 `tsc --noEmit` |
| 版本源 | `backend/app/__init__.py` 的 `__version__`（前端从 `/api/system/overview` 读取展示） |
| 真实数据 | `backend/data/`（SQLite + 工作目录 + 内核隔离配置目录）**含用户真实对话**，受 `.gitignore` 忽略 |
| 技能 | 各内核自带技能（`SKILL.md` 目录）由适配器只读扫描，统一编目进 `skills` 表，**只能通过智能体分配生效**（`agent_skills`） |
| MCP | MCP 服务器（stdio/http/sse）统一编目进 `mcp_servers` 表：平台可新建「通用」服务器，各内核配置里的只读收录；**只能通过智能体分配生效**（`agent_mcp_servers`） |
| 协作域 | `backend/app/collab/` —— 多智能体「工单」协作（可视化编排 + 串行引擎），独立于对话域，见 `docs/collaboration-design.md` |

---

## 1. 铁律（MUST / MUST NOT）

1. **新增智能体只能走适配器。** 在 `agents/builtin/` 新增模块并 `@register_agent`。
   禁止在 `runtime/`、`api/`、前端里为某个具体 `type_id` 写分支。
2. **适配器是叶子节点。** 适配器**禁止** `import` 任何 `app.api.*`、`app.runtime.*`、`app.db.*`、`app.models`。
   它只能依赖 `agents/base.py`、`agents/events.py`、`agents/skills.py`、`agents/mcp.py`、
   `agents/builtin/util.py`、标准库与 `httpx`。
3. **适配器无状态、不碰数据库。** 一次运行所需的一切都从 `AgentContext` 取；解析过程的状态放进 `AgentRun.state`。
4. **所有可配置项必须声明为 `ConfigField`。** 禁止在 React 里为某个智能体硬编码表单字段。
5. **事件协议是框架与适配器之间唯一的方言。** 新增事件类型必须**同时**改：
   `agents/events.py`（枚举 + 构造器）、`runtime/service.py:consume_run`（累积/持久化）、
   前端 `lib/types.ts` + `pages/ChatPage.tsx:applyEvent`、`docs/architecture.md` 的事件表。
6. **凭据只经过 `app/security.py`。** 明文密钥禁止入库、进日志、进 SSE payload、进接口响应；
   命令行回显必须经 `runtime/runner.py:_redact` 脱敏。
7. **界面文案、代码注释、docstring 一律用中文。** 专有名词（Claude Code、MCP、SSE、JSONL、Fernet…）
   与代码标识符保持原样。术语表见 §6。
8. **改完必须验证。** 后端 ≥ `pytest`；前端改动还要 `tsc --noEmit` + `npm run build`；
   两者都用 `bash scripts/test.sh` 一次跑完。**前端改完不 build 不重启，线上看不到效果。**
9. **数据库变更必须向后兼容。** 本地库含真实数据，禁止"删库重建"式变更；加字段要能容忍旧行。
10. **长任务必须可取消、可超时。** 子进程/HTTP 调用都要响应 `run.cancel_event` 与 `run_timeout_seconds`。
11. **错误必须显式暴露**：要么抛 `HTTPException`（API 层），要么产出 `error` 事件（运行层）。
    禁止 `except Exception: pass` 静默吞掉。
12. **不要新增依赖**（后端 `requirements*.txt`、前端 `package.json`），除非任务确实需要；
    新增必须说明理由并与现有版本风格一致（精确版本号）。
13. **智能体能用的技能只能来自分配。** 技能的权威来源是 `agent_skills` 表；运行时必须「严格仅分配」——
    把分配到的技能物化进该内核的隔离配置目录，不允许把本机个人/插件技能顺手带进一次运行。
    某个内核把技能放在哪、怎么隔离，是**适配器**的知识（`discover_skills()` / `skill_mount()`）；
    框架层禁止硬编码技能目录或内核配置变量。

---

## 2. 功能分层与依赖方向

```
 ┌ 前端 frontend/src ────────────────────────────────────────────────┐
 │  pages/ (Chat·Collab·Agents·Skills·MCP·Settings) ·                 │
 │  mobile/ (独立入口：/m/ 的页面与样式表，PWA) ·                      │
 │  components/ (DynamicForm·MessageView·Modal·CollabCanvas…) ·       │
 │  lib/ (api·types·agentStream·collabStream)                         │
 │  —— 只认「规范化事件」与 REST，不知道某个智能体长什么样            │
 └──────────────────────────────┬────────────────────────────────────┘
                        REST /api/*   ·   SSE POST /messages · GET /stream
 ┌──────────────────────────────▼────────────────────────────────────┐
 │ api/            路由、请求校验(Pydantic)、SSE 封装、HTTP 状态码    │
 ├───────────────────────────────────────────────────────────────────┤
 │ collab/         协作域（旁路）：编排图 · 工单协议 · 串行引擎 · 运行 API │
 ├───────────────────────────────────────────────────────────────────┤
 │ runtime/        service=编排+持久化 · runner=执行(CLI/API) ·       │
 │                 manager=运行表(注册/取消/追踪) · skills=技能编目/分配/物化 │
 │                 mcp=MCP 编目/分配/物化                              │
 │                 provisioning=运行时(CLI 内核)的安装/更新/卸载执行   │
 ├───────────────────────────────────────────────────────────────────┤
 │ agents/         ★ 适配器插件层：base(契约) · events(事件) ·         │
 │                 registry(注册) · skills(技能契约) · mcp(MCP 契约) · builtin/*(实现) │
 ├───────────────────────────────────────────────────────────────────┤
 │ models/db · schemas · settings · security · attachments · bootstrap│
 └───────────────────────────────────────────────────────────────────┘
```

依赖方向**只能自上而下**：`api → runtime → agents`；`api/runtime` 可依赖
`models/db/settings/security/attachments`；
**`agents` 不得反向依赖任何上层**。跨层共享的数据结构定义在叶子层（如 `agents/base.py` 的 `AgentContext`）。

协作域 `collab/` 是**旁路功能**，不在主干上：`api` 只把它当一条子路由挂上去，它在内部经**唯一边界**
`collab/execution.py` 调用 `runtime.runner`。硬约束（有守卫测试 `tests/test_collab_architecture.py`）：
`collab/*` 只允许依赖 `agents.events`（事件契约）、`models/db/settings` 与叶子工具模块
`app.credentials`（凭据解密 / 掩码 / 事件擦除，与对话域共用同一份）、
`app.attachments`（附件落盘 / 拼提示词，与对话域共用同一份），**不得** `import app.api.*`，
也**不得**绕过 `execution.py` 直接 `import app.runtime.*`；`tickets.py`/`graph.py`/`prompt.py` 必须是
不碰存储与 HTTP 的纯函数。框架层禁止反向依赖 `collab`。协作事件是自有的 `collab.*` 信封
（`collab/protocol.py`），**不属于**铁律 5 的适配器事件协议。

各层职责（改代码前先确认你改的是哪一层）：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| `agents/builtin/*` | 怎么调用某个智能体、怎么把它的输出翻译成事件 | 存储、HTTP、UI、取消（框架统一做） |
| `agents/base.py` `events.py` `registry.py` `skills.py` `mcp.py` | 契约与协议（改动影响面最大，慎改） | 具体产品逻辑 |
| `runtime/runner.py` | 起子进程/发 HTTP、读流、超时、取消、脱敏 | 业务规则 |
| `runtime/service.py` | 载入 对话→智能体→适配器（网关提供密钥与地址）、能力决策、历史回放、落库 | 具体命令拼装 |
| `runtime/skills.py` | 技能统一编目、智能体分配、物化进内核隔离目录 | 哪个内核把技能放哪（那是适配器的事） |
| `runtime/mcp.py` | MCP 服务器统一编目、新建/编辑/删除、智能体分配、写进内核隔离目录 | 哪个内核把 MCP 配置放哪、长什么形状（那是适配器的事） |
| `api/*` | 参数校验、状态码、把 service 的结果转成响应/SSE | 业务决策（尽量薄） |
| `models.py` `schemas.py` | 表结构与请求/响应契约 | 编排逻辑 |
| 前端 | 展示与交互 | 任何智能体特异逻辑 |

**违规示例**（出现即为评审不通过）：
```python
# ✗ runtime/service.py 里为具体智能体开小灶
if agent.type_id == "codex":
    ctx.prompt = special_codex_wrapper(ctx.prompt)

# ✗ 适配器直接读数据库
from ..models import AgentProfile   # in agents/builtin/foo.py

# ✗ 前端硬编码某适配器的字段
{typeId === 'gemini-cli' && <input name="approval_mode" />}
```

---

## 3. 核心契约（改动这些等于改公共 API）

### 3.1 规范化事件协议（`agents/events.py`）
`run.started · message.delta · thinking.delta · tool.call · tool.result · usage · session · log · error · run.finished`
外加传输层自有的 `turn.started`（`runtime/service.start_run()` 在启动后台运行前同步发出，携带 run_id 与消息 id）
与 `stream.closed`（`transport/sse.py` 收尾）。
**适配器只能产出前 10 种**；`turn.started`/`stream.closed` 由框架产出，适配器不得越权。
新增事件类型必须走铁律 5 的四步同步改动。
框架自有的旁路信封**不属于**本协议：协作域是 `collab.*`（`collab/protocol.py`），
「运行时」页的安装 / 更新 / 卸载是 `provision.*`（`runtime/provisioning.py`）——
两者只在各自的页面内部消费，由框架层产出，不参与铁律 5 的四处同步。

### 3.2 `AgentDefinition` 契约（新增适配器要实现/声明）
必需：`id`、`name`、`runtime`、`build_invocation()`、`parse_line()`（CLI）或 `stream()`（API）。
常用可选：`description`、`vendor`、`capabilities`、`config_fields`、`api_key_env`、`base_url_env`、
`default_gateway`、`default_model`、`protocol`（openai / anthropic / gemini）、`default_auth_style`、
`default_models`（官方链路预置模型）、`docs_url`、`logo`（官方 Logo 的前端静态资源路径，如
`/icons/kernels/claude.svg`）、`executable`、`auto_provision`、
`availability()`、`probe()`/`probe_api()`、`resume_supported(ctx)`（可选钩子）。
- `parse_line(line, run)` 里**不要吞异常**：解析不了就 `yield log(...)` 或忽略该行，别让整个运行崩掉。
- 需要"运行结束时补发事件"的适配器，请覆盖 **`AgentRun.finish()`**（基类钩子）。
  ⚠️ 注意：不要自创 `finalize()` 之类的方法名，运行层的唯一结尾钩子是 `AgentRun.finish()`。

### 3.3 `Capabilities` 驱动框架行为（声明即生效，别在别处打补丁）
`streaming`、`sessions`（服务端可续接→只发本轮 prompt，否则回放 transcript）、
`native_system_prompt`（False→框架把人格折进 prompt）、`tools`、`mcp`、`permission_modes`、
`working_directory`（True→为每个对话分配独立工作目录作 cwd）。
声明与实际行为不符 = bug。

### 3.4 `ConfigField` 契约
`key · label · FieldType · required · default · placeholder · help · options · advanced`。
`FieldType ∈ {string, secret, number, boolean, select, textarea, keyvalue, string_list}`。
用户输入的 options 由 `coerce_options()` 规整（含未知键透传，保持前向兼容）。
**新增一个可配置项 = 在适配器加一个 `ConfigField`**，前端表单自动出现，无需动前端。

### 3.5 `AgentContext` / `AgentRun`
`AgentContext` 是框架→适配器的唯一输入（`agent_id/name`、`options`、`gateway_url`、`api_key`、
`credentials`、`auth_style`、`model`、`system_prompt`、`workspace_dir`、`session_id`、`history`、
`prompt`、`skills`、`mcp_servers`、`home_dir`），用 `ctx.option(key, default)` 取值。
`gateway_url` / `api_key` / `auth_style` 由框架从智能体引用的**网关**解密后注入（`runtime/gateway.py:bind_gateway`）；
`credentials` 是智能体自己的凭据（解密后的「环境变量名 → 明文」），由框架合并进子进程环境变量，
适配器不需要自己处理，也**不得**把它写进提示词、日志或事件。
`AgentRun` 是"无状态定义 + 一次具体运行"的绑定，持有 `state`（解析状态）、`emitted_text`、`thinking`、
`session_id`、`tool_calls`。
- **双向 stdin（常驻型 JSON-RPC 内核，如 Codex app-server）**：适配器在 `build_invocation()` 里置
  `Invocation.stdin_hold=True`（`stdin` 只写第一帧且不关闭），解析到应答后用 `AgentRun.send(line)`
  排队下一帧、用 `AgentRun.mark_done()` 声明收工。**写 stdin / 关 stdin 由执行层统一做**
  （照旧由 `runner` 负责），适配器仍然只生产数据、不碰子进程。

### 3.6 领域模型不变量（`models.py`）
- `Gateway`（网关，API 密钥的唯一来源）→ `AgentProfile`（智能体：内核 + 网关 + 模型 + 人格 + 技能）
  → `Conversation`（对话绑定一个智能体）。原「角色」与「智能体实例」已合并为 `AgentProfile`。
- **Conversation 快照 `agent_profile_id`**：智能体改了内核 / 网关 / 模型后，历史对话仍按原智能体复现，
  别改成实时跟随。
- 外键 `ON DELETE RESTRICT`：**不要**为了"方便"改成级联删除。删智能体 / 删自定义网关走**软删除**
  （写 `deleted_at`，新列登记在 `models.py` 的 `ADDITIVE_COLUMNS`）：实体从所有可选列表消失，
  但历史对话与协作历史照旧可读（历史对话仍能显示当时的名字/头像，也仍可继续）。
  只有仍被**未删除**的智能体引用的网关才返回 **409**；官方链路（`kind="official"`）一律不可删。
- 删除对话需显式清理消息（`api/conversations.py` 已处理）；SQLite 已开 `PRAGMA foreign_keys=ON`。

### 3.7 网关与密钥（`gateways` 表 + `api/gateways.py`）
网关是 API 密钥的**唯一来源**，分两类：`kind="official"`（启动播种按适配器声明生成、不可删）与
`kind="custom"`（用户在「设置 → 自定义网关」创建）。智能体只引用一个 `gateway_id`，模型必须从网关的
`selected_models` 白名单里选。
- **密钥只经 `app/security.py`**：明文密钥禁止入库 / 进日志 / 进 SSE payload / 进接口响应；
  接口只回 `api_key_set` 与掩码 `api_key_preview`。
- **「必须成功才能保存」是服务端不变量**：`selected_models` 至少一项；`POST /api/gateways/probe`
  只探测不落库，探测失败可由用户「手动旁路」录入模型名。
- 协议与鉴权方式固定为 `PROTOCOLS` / `AUTH_STYLES`（`runtime/gateway_probe.py`），新增要在那里声明。
- **界面只维护自定义网关**：设置页只列 `kind="custom"`；智能体配置里的「官方默认」是
  `gateway_id = null`（平台不接管路由，回落到适配器读到的本机配置），官方链路不作为可选项。

---

### 3.8 技能契约（`agents/skills.py`）
- `SkillSpec`：一个技能的只读描述（`type_id` / `slug` / `name` / `description` / `source_kind` / `path` /
  `fingerprint` / `files` …），`id` 固定为 `<type_id>:<slug>`。
- `SkillMount`：适配器声明的**物化计划**（`home_name`、`skills_subdir`、`carry` 要携带的配置文件、
  `patch` 配置文件补丁、`notes` 用户提示）。框架只按这份声明执行，不认识具体内核。
- 适配器钩子：`skill_roots()`（声明本内核的技能根：路径 + 来源 + 是否可安装；框架据此
  统一扫描与安装技能）、`discover_skills()`（只读扫描，尽力而为、不得抛错；常见实现是
  `scan_skill_roots(self.skill_roots(), ...)`）与 `skill_mount(ctx)`（返回 `None` 表示该内核不支持技能）。
  技能可来自「技能市场」（`source_kind=market`，装在 `skill_roots()` 里 `installable` 的根下，可卸载），
  但落盘路径仍由适配器声明、框架不硬编码。
- `AgentContext.skills` / `AgentContext.home_dir`：框架注入的「本次运行生效的技能」与
  「物化好的隔离目录」（技能与 MCP 共用同一个「家」），适配器在 `build_invocation()` 里据此
  设置自己的配置目录环境变量。
- 声明了 `Capabilities.skills=True` 的适配器**必须**实现 `skill_mount()`（有架构守卫测试）。

### 3.9 MCP 契约（`agents/mcp.py`）
- `McpServerSpec`：一个 MCP 服务器的只读描述（`name` / `slug` / `transport`(stdio|http|sse) /
  `command` / `args` / `url` / `env` / `headers` / `type_id` / `source_kind`），`source_id` 为
  `<内核 id>:<slug>`（平台通用的服务器没有）。`env` / `headers` 的值可能含密钥。
- `McpMount`：适配器声明的**物化计划**（`home_name`、写进隔离目录的 `files`、写进工作目录的
  `workspace_files`、`notes`）。`home_name` 必须与同一适配器 `skill_mount()` 声明的一致：
  技能与 MCP 共用同一个隔离目录。
- 适配器钩子：`discover_mcp_servers()`（只读扫描本机已有的 MCP 配置，尽力而为、不得抛错）与
  `mcp_mount(ctx)`（返回 `None` 表示该内核不支持 MCP）。解析 / 渲染的共享工具（JSON / TOML 两种
  形状、`safe_slug`）在 `agents/mcp.py`；**「哪个内核把配置放哪、长什么形状」是适配器的知识**，
  框架不硬编码路径、键名或命令行开关（有架构守卫测试）。
- `AgentContext.mcp_servers` / `AgentContext.home_dir`：框架解密后注入的「本次运行生效的服务器」与
  「物化好的隔离目录」。密钥只能写进配置物化的产物（`files` / `workspace_files`），
  **禁止写进提示词、日志或事件**。
- 声明了 `Capabilities.mcp=True` 的适配器**必须**实现 `mcp_mount()`（有架构守卫测试）。

## 4. 扩展手册（照着做，别自创路径）

### 4.1 接入新的 CLI 智能体（例：Aider）
1. 新建 `backend/app/agents/builtin/aider.py`，`@register_agent` 装饰 `AiderAgent(AgentDefinition)`。
2. 声明 `id/name/vendor/logo/runtime=Runtime.CLI/executable/capabilities/config_fields` 与凭据/网关环境变量；
   `logo` 指向 `frontend/public/icons/kernels/` 下的 24×24 单色 SVG（只画形状，染色由界面遮罩完成）。
3. 实现 `build_invocation(ctx) -> Invocation`（`argv/env/cwd/stdin/files`）与 `parse_line(line, run)`。
4. 用 `builtin/util.py` 的 `as_list/truthy/flatten_content/dig`，不要重复造轮子。
5. 加测试：解析用真实样本；在 `backend/tests/` 参照 `test_adapters.py` 的写法。

### 4.2 接入新的模型供应商（无官方 CLI）
复用 `agents/builtin/openai_harness.py` 的 `OpenAIHarnessBase`，只写一个子类声明默认网关/模型/`config_fields`/`logo`，
参考同文件的 `OpenAICompatibleHarness`（`openai-harness`）。**不要再写一遍工具循环。**

### 4.3 零代码接入私有 CLI
用内置 `custom-cli` 适配器：填 `command/args/prompt_mode/output_format/..._path` 等 `ConfigField` 即可。
只有在"声明式表达不了"时才考虑写适配器。

### 4.4 新增可配置项
在对应适配器的 `config_fields` 加 `ConfigField(...)` → 前端自动渲染（`DynamicForm`）。**不要改前端。**

### 4.5 新增规范化事件
按铁律 5，四处 + 文档同步；同时更新 `docs/architecture.md` 的事件表。

### 4.6 让某个适配器支持技能
1. `capabilities` 里声明 `skills=True`。
2. 实现 `skill_roots()` 声明本内核的技能根（`SkillRoot(path, source_kind, installable, max_depth)`；
   平台「技能市场」的安装目录是其中 `installable` 的一个，通常把 `.skillhub` 单独列一条、
   `source_kind=MARKET_SOURCE`），再用 `agents/skills.py` 的 `scan_skill_roots()` 实现 `discover_skills()`，
   标注来源（`user` / `project` / `plugin` / `system` / `market`），**只读、不得抛错**。
3. 实现 `skill_mount(ctx)`：声明隔离目录名、需要携带的配置文件、要关闭的内核自带技能，
   并在 `build_invocation()` 里把 `ctx.home_dir` 写进对应环境变量（如 `CLAUDE_CONFIG_DIR`）。
4. 加测试：参考 `backend/tests/test_skills.py`（解析、物化、隔离变量）。

### 4.7 新增前端页面
- **桌面端**：`src/pages/XxxPage.tsx`，在 `src/App.tsx` 的 `Routes` 与 `NAV` 注册；样式写进
  `src/index.css`，沿用既有 class 命名（`card`/`btn`/`pill`/`banner`…）。
- **移动端（`/m/`）**：`src/mobile/pages/MobileXxxPage.tsx`，在 `src/mobile/MobileApp.tsx` 注册路由；
  样式写进 `src/mobile/mobile.css`。移动端页面**不得**引用 `src/pages/`、`src/App.tsx` 或
  `src/index.css`（有守卫测试 `tests/test_mobile_web.py`），但可以复用 `src/lib/*` 与
  `DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` / `CollabAgentThread` /
  `KernelLogo` / `TicketBoardKit` / `Composer` / `Icons` / `McpPicker` / `McpServerForm` /
  `McpAssignPanel`
  这些纯展示组件（对话气泡只有 `MessageView` 一份，线程装配只有 `ConversationThread` 一份）。
- **两端功能对齐、交互各表**：桌面端有的能力（例如协作组编排）移动端也要能用，但按触屏重做交互 ——
  移动端**不引用**桌面端的编排画布（`components/CollabCanvas` 等），协作组改用表单式编辑
  （`mobile/pages/MobileCollabTeamPage.tsx`，与桌面端共用 `lib/collabDraft.ts` 的字段语义）。
- **两端通用**：类型加在 `src/lib/types.ts`，接口调用加在 `src/lib/api.ts`；禁止在组件里直接 `fetch`。
- **移动端必须保持 PWA 可用**：新增页面不得依赖 `/m/` 之外的顶层路由；离线只会命中
  Service Worker 预缓存的外壳，任何新资源都要能从 `/m/` 入口按相对/绝对路径取到。
  **改了外壳（入口 HTML / 样式表 / 构建产物）必须把 `public/m/sw.js` 的缓存名版本 +1**，
  否则已添加到主屏的独立应用会继续吃旧缓存（它没有刷新按钮；设置页的「更新到最新版本」
  走 `pwa.ts:refreshShell()` 清缓存重载，是自助出口）。
- **移动端外壳钉在可视视口上**：`.m-app` 用 `position: fixed` + 四边贴 0，**不要**用
  `100vh`/`100dvh` 配 `height: 100%` 父链 —— 真机上地址栏与底部工具栏会伸缩，两者高度经常
  对不齐，底栏就会浮起来留缝或被工具栏压住；`env(safe-area-inset-*)` 负责刘海与 Home 条。
- **外观（深色 / 浅色 / 跟随系统）只有一份实现**：`src/lib/theme.ts`（模式存在 localStorage，
  解析结果写在 `<html data-theme>`；两个入口 HTML 各有一段等价的内联脚本做首屏防闪）。
  配色一律走 CSS 变量：`index.css` / `mobile.css` 的 `:root` 是深色默认，浅色只加
  `[data-theme='light']` 覆盖块 —— **新写颜色不要写死深色**，先加变量、再补浅色值；
  原生外壳（状态栏 / 系统导航条）的配色由 `applyTheme()` 同步 `meta[name=theme-color]`。
- **移动端外壳贴住屏幕底边**：输入区（`.m-composer`）**不照搬**底部安全区 —— iOS 独立应用里
  `env(safe-area-inset-bottom)` 是 34px，而 Home 条本身只占 ~13px，照搬会在输入框下面留出一条
  空白（「界面底部空出一截」）；现在只留 6px 收边，并给 `html` 铺主题底色。输入框本身就是那个
  「框」：附件 / 语音在框内左侧，发送 / 停止在框内右侧，**不要**再写背景提示文字。
- **输入区 / 顶栏的图标是线条 SVG**：`components/Icons.tsx`（两端共用，`stroke="currentColor"`，
  尺寸走 `size`）。各系统的 emoji 字体大小、粗细都不一样（📎 在 iOS 上尤其大），一排小图标永远
  对不齐，也不跟主题换色；这类「要跟文字同色的操作图标」**不要**再用 emoji，新的就往这个文件里加。
  （抽屉导航、空状态里那种大号装饰性图标仍可用 emoji。）
- **输入区只有一份**：`components/Composer.tsx`（两端共用，受控：草稿 / 附件 / 上传 / 发送都
  由页面给）。对话页与协作运行页（含聊天弹层）都用它，**不要**再各写一个输入框 —— 两处的交互与
  样式必须完全一致（样式仍是两端各一份：共享段逐字一致，有守卫测试
  `tests/test_mobile_web.py::test_composer_styles_are_identical_on_both_ends`）。
  附件与语音的提示通过 `onNotice` 回给页面，输入框不自己弹东西。页面自己的
  语义（协作的模式切换按钮、状态条）走 `header` 插槽传进去；要允许空发送就开
  `allowEmptySend`（别为这类差异复制一份输入区，也别把页面语义塞进组件）。
- **协作运行页主页面就是那间办公室**：`MobileCollabRunPage` 主区整屏铺 `MobileCollabProgress`，
  时间线与「需要你决定」卡片收进 `MobileSheet full` 的对话弹层 —— **点入口智能体工牌**才出来
  （顶栏不要再放对话按钮），点其他岗位开只读岗位会话。输入区上方有一个**模式按钮**（不是两档
  选项框）：点一下切「推进任务」，再点回「普通对话」。推进模式下空内容直接发 = 让领导者继续推进
  （`/resume`）；工单正在推进（接口 409）、带着附件（工单通道没有附件位）、有待回答的问题时，
  界面直接接管模式并把按钮置灰；任务还没开始时先不显示按钮（第一句就是任务描述）。
- **附件能力下沉在 `backend/app/attachments.py`**：校验 / 落盘 / 拼提示词只有一份，对话
  （`POST /api/conversations/{id}/attachments`）与协作（`POST /api/collab/runs/{id}/attachments`）
  都调用它，别再各写一份 base64 解码与目录穿越校验。
- **办公室底图有两张**：`frontend/public/office/iso-office.svg`（深）与 `iso-office-light.svg`
  （浅），同一份几何 —— `scripts/gen_office_bg.py` 的 `light_color()` 在构建期按「反相 + 色相旋转
  180°」换算（不要把实时 `filter` 加到手机上的大图上），`CollabOfficeMap` 按 `lib/theme.ts` 的当前
  主题选。办公室里的工牌 / 图例 / 工具按钮 / 信息卡颜色一律走 `--office-*` 变量（两端样式表各一份，
  浅色只在 `[data-theme='light']` 覆盖），**不要**写死深色。
- **朗读（语音合成）只有一份实现**：`src/lib/tts.ts`（开关存在 localStorage 的 `magenta.tts`，
  **默认关闭**；开启后新回复会被念出来，Markdown 先转成纯文本）。两个 iOS 坑必须一起处理：
  ① 首次发声要在**用户手势里**解锁（点开关时 `unlockSpeech()`）；② 打断上一条时 `cancel()` 与
  `speak()` 不能在同一轮里（iOS 会吞掉），要丢到下一个任务。对话页顶栏右上角只有「分享 + 朗读」
  两个按钮；会话级操作（重新生成 / 重置记忆 / 复制工作目录 / 删除）挂在抽屉的对话行「⋯」上，
  由聊天页弹面板 —— 不要再往顶栏塞「⋯」。
- **分享成长图**：`src/lib/shareImage.ts`（Canvas 手绘，不引依赖）：对话页顶栏的分享按钮把当前
  对话渲染成 PNG，优先走 `navigator.share({ files })`，不支持就退回下载。
- **安装归「运行时」，配置归「智能体」**：一个内核 = 一份安装 + N 个智能体，所以内核的
  安装 / 更新 / 卸载留在运行时页，智能体（网关 · 模型 · 人格 · 技能 · 适配器选项）有自己的
  配置页 `mobile/pages/MobileAgentsPage.tsx`。适配器选项仍由 `DynamicForm` 渲染，**不要**在前端硬编码字段。
- **网关是 API 密钥的唯一来源**：移动端设 `mobile/pages/MobileGatewaysPage.tsx`（自定义网关 CRUD +
  获取模型 + 手动旁路）。官方链路由启动播种生成，用户在智能体配置里直接选。
- **移动端设置页只留「现在要动的开关」**：外观 + 配置入口（网关 / 技能 / MCP / 运行时 / 智能体）+
  一行「更多」。运行概览、版本记录这些信息看桌面端即可；「更新到最新版本」
  （`pwa.ts:refreshShell()`）必须留着 —— 独立应用里没有刷新按钮，那是卡在旧外壳时的唯一出口。
- **MCP 有两个入口**：主导航里的「MCP」页负责编目（新建 / 编辑 / 停用 / 删除 + 重新扫描），
  「分配给哪些智能体」既可在 MCP 页做（`McpAssignPanel`），也可在智能体表单里勾（`McpPicker`，
  两端共用同一份组件）。
- **移动端锁定缩放**：移动端是「固定外壳」的应用形态（顶栏 + 左侧抽屉 + 内部滚动），入口视口写
  `maximum-scale=1.0, user-scalable=no`，并在 `mobile.css` 用 `touch-action: pan-x pan-y` 兜底
  （iOS Safari 忽略 `user-scalable=no`，只有 `touch-action` 真正生效）。配套约束：移动端输入框字号
  一律 ≥ 16px，否则 iOS 聚焦时会强制放大页面。**桌面端不做此限制**（浏览器缩放是它的无障碍能力）。

### 4.8 扩展协作域
协作的一切都留在 `backend/app/collab/`：新增动作/状态改 `schemas.py` + `engine.py`（并同步
`docs/collaboration-design.md`）；纯逻辑放 `tickets.py` / `graph.py` / `prompt.py`（有"必须是纯函数"的
守卫）；对 `runtime`/`agents` 的调用只能加在 `execution.py`。给协作表加列要同时登记
`ADDITIVE_COLUMNS`（SQLite 的 `create_all` 不会给旧表补列）。**不要**为了让协作跑通去改适配器或 `runtime/`。
协作聊天也能带附件：落盘用共享的 `app.attachments`，接口是
`POST /api/collab/runs/{id}/attachments`，`chat` 只收相对路径 —— 附件是工作目录里的普通文件，
不是协作域新表（也别往对话域塞）。

### 4.9 让运行时可由平台安装 / 更新 / 卸载
1. 在适配器声明 `provisioner = RuntimeProvisioner(manager=…, package=…, install_argv=…,
   update_argv=…, uninstall_argv=…, latest_argv=…, prepare_argv=…)` —— 命令（用哪个包管理器、包名、装到哪、
   动作前要清掉什么残留）是**适配器的知识**，框架只负责按声明在受控子进程里执行、流式回显、超时与脱敏。
   `prepare_argv` 是动作前的清扫命令（清掉上一次被中断的动作留下的残留），失败只回一行告警、不中断动作。
   npm 系内核的安装 / 更新命令由 `builtin/util.py:npm_install_argv()` 生成：它会先让出被别的安装
   占着的同名可执行文件（否则 npm 直接失败在 `EEXIST`），失败时再把对方放回去。
2. 不要把「怎么装」写进 `api/` 或 `runtime/`（框架层不得硬编码包管理器或内核 id）。
3. 不支持由平台代管的适配器（进程内 API 内核、用户自备的 `custom-cli`）保持 `provisioner = None`，
   卡片副标题会写明「进程内运行」/「自备可执行文件」。

### 4.10 让某个适配器支持 MCP
1. `capabilities` 里声明 `mcp=True`。
2. 实现 `discover_mcp_servers()`：只读扫描本机已有的 MCP 配置，用 `agents/mcp.py` 的
   `parse_json_servers()` / `parse_toml_servers()` 解析成 `McpServerSpec`，`type_id` 填 `self.id`；
   **不得抛错**（配置不存在 / 损坏 / 无权限都当没有）。
3. 实现 `mcp_mount(ctx)`：用 `to_json_config()` / `render_toml_servers()` 把 `ctx.mcp_servers`
   序列化成自己内核要的形状，落点二选一（可同时用）——
   `files`（写进隔离配置目录，配 `home_dir` 环境变量使用）或 `workspace_files`（写进工作目录的项目级
   配置）；`home_name` 必须与 `skill_mount()` 一致。要「只认分配到的服务器」就同时关闭内核的其它来源
   （例如给内核加一个「只读这份配置」的命令行开关）。
4. 加测试：参照 `backend/tests/test_mcp.py`（解析、物化、隔离变量、密钥只回掩码）。

---

## 5. 编码风格

**Python**
- 文件顶部 `from __future__ import annotations`；统一 `from __future__` 后接标准库、三方、本地分组。
- 类型标注齐全；数据容器优先 `@dataclass(slots=True)`（沿用 `base.py` 风格）。
- 面向用户的字符串中文；异常信息中文且可操作（告诉用户"哪里不对、怎么修"）。
- 不引入全局可变状态到适配器；并发安全只依赖 `AgentRun.state`。
- 缩进 4 空格，行宽 ~100；长签名换行对齐沿用现有写法。

**TypeScript / React**
- 函数组件 + hooks；单文件单默认/命名导出，沿用现有命名（`AgentsPage`、`DynamicForm`）。
- 类型集中放 `src/lib/types.ts`，与后端 `schemas.py` 的字段名保持一致（`snake_case`）。
- 网络请求一律走 `src/lib/api.ts`；**不要**在组件里直接 `fetch`。
- 复用 `DynamicForm` 渲染所有适配器表单；复用 `MessageView` 渲染消息。
- 不加状态管理库、不加 UI 库；CSS 手写在 `index.css`。

---

## 6. 语言与术语

- **面向用户的一切（界面文案、后端提示、错误信息、接口 `description`、文档）用中文。**
- **代码注释与 docstring 用中文。**
- 保留的专有名词：Claude Code、Codex、Gemini CLI、MCP、SSE、JSONL、Fernet、token、WebSocket、
  以及适配器 `id`（`claude-code`…）、环境变量名、字段名等标识符。

| 统一用词 | 不要写成 |
| --- | --- |
| 智能体（agent） | Agent、agent（正文里） |
| 智能体类型 | Agent Type、adapters（指概念时） |
| 网关 | Gateway |
| API 密钥 | API Key、API key |
| 适配器 | adapter（指概念时） |
| 智能体 · 对话 · 消息 | Agent、Conversation、Message（指概念时） |
| 智能体配置（`AgentProfile`） | Agent Instance、Role |
| 工作目录 | workspace |

---

## 7. 凭据与安全

- 读写密钥只用 `security.encrypt/decrypt/mask`；接口只回传掩码，永不回传明文。
- **网关密钥**（模型提供方）只属于 `Gateway`；**凭据**（工具/服务要用的第三方密钥）是**全局一份**
  （`credentials` 表），某个智能体能拿到哪些由 `agent_credentials` 引用关系决定（同「技能」的思路）。
  两者都只经 `security` 加解密。凭据的纯逻辑在 `app/credentials.py`、读库 / 落库 / 引用关系在
  `runtime/credentials.py`：
  校验（`validate_name`）、解密（`decrypt_all`）、只读掩码视图（`reads`）、提示词里的名字清单（`notice`）、
  文本擦除（`scrub` / `scrub_event`）只此一份；整表替换引用关系走 `runtime/credentials.py:set_agent_credentials`。
- 凭据**只以环境变量**注入子进程（`runner` 合并 `ctx.credentials`，网关/适配器给的同名变量优先），
  模型只知道变量名；运行层逐条事件擦除凭据值**与网关密钥**（`runner._run_secrets` → `scrub_event`），
  因此它们进不了 SSE、落库内容与日志。
- 新增会把凭据放进 env/argv 的适配器时，确认 `runner._redact` 能覆盖（env 键名含 KEY/TOKEN/SECRET/PASSWORD）。
- `Runtime.API` 型智能体的文件/命令工具必须限制在工作目录内（见 `builtin/harness_tools.py` 的 `_resolve`）。
- `bypassPermissions` / `danger-full-access` 这类无人值守选项只应出现在显式配置里，不要设为默认值。

---

## 8. 完成定义（Definition of Done）

```bash
bash scripts/test.sh              # 必须全绿：pytest + 前端 tsc
cd frontend && npm run build      # 只要改了前端就必须执行
curl -s http://127.0.0.1:8010/api/health
```

改动类型 → 必须做的验证：

| 改动 | 必须验证 |
| --- | --- |
| 后端逻辑/适配器 | `pytest`（新增能力要有新测试） |
| 前端任意改动 | `tsc --noEmit` + `npm run build` + **重启 PyPanel 服务**后自测页面 |
| 移动端 / PWA 改动 | 同上，并额外自测 `/m/` 深链可刷新、断网后外壳仍能打开（`pytest tests/test_mobile_web.py`） |
| 事件协议/持久化 | 端到端跑一次真实对话（SSE 事件齐全、消息落库正确） |
| 数据库结构 | 用**现有** `backend/data/magenta.db` 启动验证旧数据可读，不得重置 |
| 新增适配器 | `/api/agent-types` 能看到、表单可渲染、连通性测试可用 |
| 技能（编目/分配/物化） | `pytest` 覆盖解析与物化；端到端跑一次真实对话确认技能生效、未分配的不出现 |
| MCP（编目/分配/物化） | `pytest` 覆盖解析、物化、密钥只回掩码；端到端跑一次真实对话确认分配到的服务器写进隔离配置、未分配的不出现 |

重启托管服务：
```bash
curl -X POST http://127.0.0.1:8899/api/services/e0c0620a5786/restart \
  -H "X-API-Key: <PyPanel Key>"
```

---

## 9. 变更影响面矩阵（改前对照，改后自检）

| 你改了 | 必须同步检查 |
| --- | --- |
| `agents/base.py` 契约 | 全部内置适配器 + 测试 |
| `agents/events.py` | `runtime/service.consume_run`、前端 `applyEvent` + `types.ts`、`docs/architecture.md` |
| `agents/skills.py` / 技能契约 | 全部声明 `skills=True` 的适配器 + `runtime/skills.py` + `tests/test_skills.py` |
| `runtime/skills.py` | 智能体分配接口、`service.prepare_run` 的物化步骤、前端技能页与智能体弹窗 |
| `agents/mcp.py` / MCP 契约 | 全部声明 `mcp=True` 的适配器 + `runtime/mcp.py` + `api/mcp.py` + `tests/test_mcp.py` |
| `runtime/mcp.py` · `api/mcp.py` | 智能体分配接口、`service.prepare_run` 的物化步骤、前端 MCP 页（两端）与智能体弹窗 |
| `runtime/gateway_probe.py` · `api/gateways.py` | 网关 CRUD、官方案例播种（`bootstrap.py`）、前端 `GatewayManager` / `MobileGatewaysPage`、适配器的 `protocol`/`default_auth_style`/`default_models` |
| `models.py` / `schemas.py` | `api/*` 的读写、前端 `types.ts`、旧数据兼容性 |
| `runtime/runner.py` | CLI 与 API 两条路径、取消/超时、脱敏 |
| `runtime/provisioning.py` | 运行时维护动作：命令来自适配器、SSE 事件信封、超时、取消、脱敏 |
| 新增「运行时可由平台安装 / 更新」 | 适配器的 `provisioner`（`RuntimeProvisioner`）+ `docs/architecture.md` §4.1 |
| 新增 `ConfigField` | 无需改前端；但要在 `bootstrap.py` 播种默认值里确认不影响首次启动 |
| `AgentProfile` 新增字段 | `schemas.py` 读写、`ADDITIVE_COLUMNS` 登记、前端 `types.ts` 与智能体弹窗、旧数据兼容 |
| 新增「全局一份 + 多智能体引用」的实体（如凭据） | 独立表 + 引用表（仿 `agent_skills` / `agent_credentials`）、运行时按引用注入、接口只回掩码、设置页与智能体弹窗两条入口 |
| 新增「统一编目 + 智能体分配」的实体（如 MCP 服务器） | 编目表 + 分配表（仿 `skills` / `agent_skills`）、适配器声明扫描与物化钩子、`runtime/` 的编目/分配/物化、接口只回掩码、两端页面 + 智能体弹窗两条分配入口 |
| 新增适配器模块 | `registry` 自动加载（确认 import 无副作用/无网络请求）、`/api/agent-types`、文档表格 |
| 前端新增页面 | `App.tsx` 的 `NAV` + `Routes`、`index.css` |
| `frontend/src/mobile/*` · `frontend/m/index.html` | `MobileApp.tsx` 路由、`mobile.css`、`vite.config.ts` 的多入口、`tests/test_mobile_web.py` |
| `frontend/public/m/*`（manifest / Service Worker）· `public/icons/*` | `main.py:resolve_spa_target` 的前缀分派；应用图标由 `scripts/gen_icons.py` 重新生成（`tests/test_frontend_assets.py` 会核对）；`public/icons/kernels/*.svg` 是各内核的品牌图，属于适配器声明，不归生成脚本管 |
| `collab/*`（协作域） | `pytest tests/test_collab_architecture.py`（分层守卫）；新增表要在 `db.init_db` 注册；给协作表**加列**必须登记 `collab/tables.py` 的 `ADDITIVE_COLUMNS`（旧库不会被 `create_all` 补列）；碰执行层只能改 `execution.py`；同步 `docs/collaboration-design.md` |
| 版本号 | 只改 `backend/app/__init__.py`（唯一版本源） |

---

## 10. 反模式清单（出现即打回）

- ❌ 在 `runtime/`、`api/`、前端里 `if type_id == ...` 为单个智能体开分支。
- ❌ 适配器里 `import app.models` / 直接读写数据库 / 读全局配置。
- ❌ 前端硬编码某个智能体的表单字段（应走 `config_fields`）。
- ❌ 把明文密钥写进日志、SSE 事件、接口响应或测试快照。
- ❌ 新增事件类型却漏改 `consume_run` 或 `applyEvent`（界面直接看不到）。
- ❌ 新增可配置项却只在后端读、忘了声明 `ConfigField`（界面上无法配置）。
- ❌ 用"清空/重建数据库"来解决 schema 变更。
- ❌ 在框架层硬编码某个内核的技能目录或配置变量（应通过 `discover_skills()` / `skill_mount()` 声明）。
- ❌ 让智能体用到未分配的技能（把本机个人/插件技能顺手带进一次运行）。
- ❌ 把技能目录删了就顺手删 `skills` 记录（应只把 `available` 置 False，保住智能体分配）。
- ❌ 在框架层硬编码某个内核的 MCP 配置文件、键名或命令行开关（应通过 `discover_mcp_servers()` /
  `mcp_mount()` 声明）。
- ❌ 让智能体连上未分配的 MCP 服务器（把本机配置里的服务器顺手带进一次运行）。
- ❌ 把 MCP 的 `env` / `headers` 明文写进日志、SSE 事件或接口响应（只能加密落库 + 只回掩码）。
- ❌ 在网关之外另存 API 密钥（或让智能体直填密钥）——密钥只经网关、只经 `security`。
- ❌ 把凭据值写进提示词 / 日志 / 事件 / 接口响应（只能以环境变量注入，值一律掩码或擦除）。
- ❌ 前端改完不 build、不重启就宣称完成。
- ❌ 静默吞异常，或把错误只打到 stdout 不产出 `error` 事件。
- ❌ 引入 UI 框架/状态管理库/新依赖来解决一个 CSS 或几行状态能解决的问题。

---

## 11. 部署速查

- 生产：`bash scripts/serve.sh`（首次会自动建 venv、缺 `frontend/dist` 时自动构建），监听 `0.0.0.0:8010`。
- 环境变量：`MAGENTA_HOST`（默认 `0.0.0.0`）、`MAGENTA_PORT`（默认 `8010`）；
  PyPanel 注入的 `HOME` 会导致 `claude` 报 "Not logged in"，故服务显式设 `HOME=/root`。
- 前后端分离开发：`bash scripts/dev.sh` → API `:8000` + Web `:5173`。
- 数据目录：`backend/data/`（SQLite `magenta.db`、`workspaces/`、`secret.key`）；**不要在未备份时动它**。
