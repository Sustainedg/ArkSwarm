# AI 编码规范（详版）

> 本文件是 [`AGENTS.md`](../AGENTS.md) 的展开版：`AGENTS.md` 是**硬约束清单**（必须遵守，会被编码代理自动加载），
> 这里解释**为什么这样分层、边界在哪里、怎么扩展、怎么评审**。
> 配套阅读：[`architecture.md`](./architecture.md) 讲"一次运行发生了什么"，本文件讲"代码应该怎么组织"。

---

## 1. 设计目标与原则

这个项目的生命周期里，唯一确定的事情是：**智能体会越来越多**（Claude Code、Codex、Gemini CLI、
DeepSeek 内核之后还会有更多 CLI 与 API 型智能体）。所以架构的目标不是"支持 N 个智能体"，
而是"**增加第 N+1 个智能体的成本趋近于零**"。

四条原则，按优先级排序：

1. **易变的东西必须被压缩到最小。** 每个智能体"怎么被调用、输出长什么样"是易变的；
   流式传输、会话记忆、持久化、表单渲染、取消与超时是稳定的。易变部分全部塞进 `agents/builtin/`。
2. **稳定层只认一种方言。** 框架与前端只理解"规范化事件"；不理解 Claude 的 `stream_event`，
   也不理解 Codex 的 `item.completed`。方言翻译在适配器里完成。
3. **能力用声明表达，不用 if 判断。** `capabilities` 与 `config_fields` 是**数据**，
   框架读数据决定行为；一旦某处开始写 `if type_id == ...`，架构就开始腐烂。
4. **依赖单向、叶子最稳。** `api → runtime → agents`，反向依赖一律禁止；
   公共数据结构放在被依赖的叶子层。

---

## 2. 功能分层（为什么这么分）

### 2.1 分层与职责

```
frontend/src        pages · components · lib(api·types)        纯展示 + 交互（桌面端 /）
frontend/src/mobile 移动端 pages/components + mobile.css · PWA   纯展示 + 交互（移动端 /m/）
        │  REST /api/*  ·  SSE POST /api/conversations/{id}/messages
api/                路由 · Pydantic 校验 · SSE 封装 · HTTP 状态码   薄
        │
runtime/service.py  编排：对话→角色→智能体→适配器、能力决策、历史回放、落库
runtime/runner.py   执行：起子进程 / 发 HTTP、逐行/逐块转事件、超时、取消、脱敏
runtime/manager.py  运行表：注册、查询、取消、快照
        │
agents/base.py      契约（AgentDefinition/AgentContext/AgentRun/Invocation/ConfigField/Capabilities）
agents/events.py    事件协议（EventType + 构造器）
agents/registry.py  插件注册（@register_agent + 自动加载 builtin/*）
agents/builtin/*    ★ 各智能体的实现（唯一的扩展点）
        │
models.py db.py schemas.py settings.py security.py bootstrap.py   存储 / 契约 / 配置 / 密钥
```

### 2.2 每条依赖规则背后的理由

| 规则 | 理由 |
| --- | --- |
| 适配器**不得** import `api/runtime/db/models/settings` | 适配器是叶子。一旦它能读数据库或读全局配置，就无法独立测试，也无法保证"新增智能体不动框架" |
| 适配器**不得**自己持久化 | 持久化是 `service.consume_run` 的职责；适配器只产出事件。这样才能保证"换个存储不影响任何适配器" |
| `api/` 保持薄 | 参数校验用 `schemas.py`，业务决策在 `runtime/`。这样同一套逻辑既能被 REST 复用，也能被未来的 WebSocket/CLI 复用 |
| 前端**不得**知道任何智能体细节 | 表单由 `config_fields` 生成（`DynamicForm`），消息展示只认 `MessagePart`/`AgentEvent`。加智能体不该改一行前端 |
| 公共结构定义在叶子层 | `AgentContext` 定义在 `agents/base.py`，被 `runtime` 依赖，而不是反过来 |

### 2.3 分层自检（可执行的守卫）

`backend/tests/test_architecture.py` 把上述规则固化成了断言：

- 适配器模块 AST 里出现指向上层模块的导入（如 `from ...models import ...`、`import app.runtime`）→ 测试失败；
- 事件枚举与文档中的协议表不一致 → 测试失败；
- `api/` 或 `runtime/` 里出现硬编码的适配器 id → 测试失败；
- 适配器的 `description` / 字段 `label` 不是中文 → 测试失败。

**新增能力时，如果守卫测试开始变红，先怀疑自己是不是在违反分层，而不是去改测试。**

---

## 3. 一次运行的链路（定位改动点用）

```
POST /api/conversations/{id}/messages
  api/conversations.send_message()
    └ runtime/service.prepare_run()          ← 载入 Conversation→Role→AgentInstance→AgentDefinition
         ├ build_context()                   ← 组装 AgentContext（合并 options、解密密钥、算最终 model/gateway）
         ├ supports_sessions() / transcript_prompt()  ← 能力决策：续接 or 回放
         └ registry.register()               ← 生成 run_id，供取消/追踪
  StreamingResponse(sse_stream(event_source()))
    └ runtime/service.consume_run()
         └ runtime/runner.stream_run()
              ├ Runtime.CLI → create_subprocess_exec() → AgentRun.feed(每行) → parse_line()
              └ Runtime.API → definition.stream(ctx)
    finally: 累积 text/thinking/tool/usage/session → 写回 assistant Message → commit
```

要点：
- **能力决策只在 `prepare_run` 里做一次**，不要在适配器里重复实现"要不要回放历史"。
- **落库只在 `consume_run` 的 finally 里做一次**，保证成功/失败/取消都会写回消息（状态标记为
  `complete` / `error` / `interrupted`）。
- 客户端断连 → `api/conversations._stream_prepared.guarded()` 捕获 `CancelledError` → `registry.cancel()`。

---

## 4. 契约详解

### 4.1 事件协议（唯一方言）

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `run.started` | `payload.run_id` | 建立运行标识 |
| `message.delta` | `text` | 正文增量 |
| `thinking.delta` | `text` | 推理增量（DeepSeek `reasoning_content`、Claude thinking） |
| `tool.call` | `name`、`call_id`、`payload.input` | 工具调用 |
| `tool.result` | `call_id`、`payload.output`、`payload.is_error` | 工具结果 |
| `usage` | 任意数值键 | token / 费用 / 耗时 |
| `session` | `payload.session_id` | 智能体侧会话 id，落库以支持多轮 |
| `log` | `text`、`payload.level` | 命令回显、诊断信息 |
| `error` | `text` | 可展示的错误 |
| `run.finished` | `payload.status ∈ ok/error/cancelled` | 终态 |

框架额外产出两个传输层事件：`turn.started`（`api/conversations.py`，携带 run_id 与消息 id）
与 `stream.closed`（`transport/sse.py`，收尾）。**适配器不允许产出这两个。**

新增事件类型 = 改公共协议，必须同步四处：`events.py` → `service.consume_run` → 前端
`types.ts` + `applyEvent` → `docs/architecture.md`；`test_architecture.py` 会拦住漏改。

### 4.2 `AgentDefinition` 元数据

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` / `name` | ✅ | `id` 是稳定标识（落库、路由、配置都用它），改名视为破坏性变更 |
| `description` / `vendor` | 推荐 | 中文；展示在配置页 |
| `runtime` | ✅ | `Runtime.CLI` 或 `Runtime.API` |
| `capabilities` | ✅ | 见 4.3，声明式驱动框架行为 |
| `config_fields` | 视需要 | 见 4.4 |
| `api_key_env` / `base_url_env` | 视需要 | 走"自带网关/自带密钥"约定时声明 |
| `default_gateway` / `default_model` | 视需要 | 新建实例时的默认值，同时供播种使用 |
| `executable` | CLI 必填 | 用于 `shutil.which` 探测是否安装 |
| `auto_provision` | 视需要 | 首次播种是否自动建实例（模板类设为 `False`，如 `custom-cli`） |
| `docs_url` | 可选 | 配置页的"查看文档"链接 |

### 4.3 `Capabilities` 语义（声明即生效）

| 能力 | 声明为 True 时框架会做什么 |
| --- | --- |
| `streaming` | 前端按增量渲染（关闭时按整段渲染） |
| `sessions` | 把 `session_id` 存到对话上，下一轮只发本轮 prompt；否则回放最近 N 轮 transcript |
| `native_system_prompt` | 人格作为原生 system prompt 传入；为 False 时框架把人格折进 prompt |
| `tools` / `mcp` / `permission_modes` | 仅作能力徽标展示与对应配置项渲染，并提示用户该适配器能用哪些选项 |
| `working_directory` | 为每个对话分配独立工作目录并作为 cwd |

可选钩子 `resume_supported(ctx)`：当"声明支持续接、但某些配置下退化为回放"时使用
（参考 `codex.py` 的 `resume_style`）。**声明与实际行为必须一致。**

### 4.4 `ConfigField` 与 `FieldType`

`FieldType ∈ {string, secret, number, boolean, select, textarea, keyvalue, string_list}`。
`secret` 会被前端渲染成密码框；`*_path`、`*_env` 这类高级项用 `advanced=True` 折叠。
用户提交的 options 经 `coerce_options()` 处理：按声明的 `default` 补值、按类型转换、**未知键透传**
（保证老版本前端提交的字段不会丢）。

> 关键约定：**新增可配置项只加 `ConfigField`。** 一旦为了某个智能体去改 `DynamicForm.tsx`，
> 就违反了"配置声明式"这条线。`DynamicForm` 只应该认识 `FieldType`，不应该认识任何 `agent.id`。

### 4.5 `AgentContext` / `AgentRun`

`AgentContext` 字段语义：`options`（合并后的配置）、`gateway_url`/`api_key`/`model`（已解析最终值，
角色级覆盖优先）、`role_name`/`system_prompt`（人格）、`workspace_dir`（cwd）、`session_id`（
续接或 `None`）、`history`（回放用的历史轮次）、`prompt`（本轮输入）。

`AgentRun` 提供：`state`（本次运行私有字典）、`emitted_text`/`thinking`/`tool_calls`（框架自动累积）、
`build_invocation()`、`feed(line)`，以及**结束时补发事件的钩子 `finish()`**。

---

## 5. 数据模型与存储演进

- 关系链：`AgentInstance → Role → Conversation → Message`。
- **Conversation 快照 `agent_id`**：历史对话必须可复现，即使角色之后改绑了别的智能体。
- 外键 `ON DELETE RESTRICT`：仍被**未删除**的角色引用的智能体删除返回 409（避免历史断裂）；
  角色与智能体本身用 `deleted_at` 软删除 —— 被历史引用时只打标记，从可选列表消失但不打断历史。
- `parts` / `usage` / `parameters` / `options` 用 JSON 列：智能体之间形态差异极大，不为其建表。
- `Role.parameters` 支持角色级覆盖 `model` / `gateway_url`，让同一智能体可按角色走不同模型。

**后加的可选列**：SQLite 的 `create_all` 不会给已存在的表加列，所以新列要登记在
`models.py`（对话域）或 `collab/tables.py`（协作域）的 `ADDITIVE_COLUMNS`，由 `db.init_db` 幂等补齐。

**schema 变更规则**（本地库有真实数据）：
1. 只做**加列/加表**这类向后兼容变更，新列必须有默认值或可空。
2. 不允许"删库重建"；如需数据迁移，写一次性脚本并说明如何回滚。
3. 变更后在**现有** `backend/data/magenta.db` 上启动一次，确认历史对话仍可读、可继续对话。
4. 生产环境当前用 `create_all`（无迁移框架）。若将来要频繁演进 schema，**先引入 Alembic**，
   而不是在 `init_db` 里堆手写 DDL。

---

## 6. 扩展决策树

```
我要做的事是什么？
├─ 支持一个新的智能体
│   ├─ 有官方 CLI，输出可解析/可探测
│   │     → 写 CLI 适配器（§6.1）
│   ├─ 没有 CLI，只有 OpenAI 兼容 /chat/completions
│   │     → 继承 OpenAIHarnessBase 写子类（§6.2）
│   └─ 私有 CLI，不想写代码
│         → 用内置「自定义 CLI」声明式配置（§6.3）
├─ 让某个智能体多一个可调参数
│     → 加一个 ConfigField（§6.4）
├─ 让界面多展示一种内容（例如"计划"）
│     → 新增规范化事件（§6.5）
├─ 换存储 / 换传输
│     → 只动 db.py+settings（存储）或 transport/sse.py+前端 streamPost（传输）
└─ 其它（角色、对话、页面、样式）
      → 按 §2.1 分层表定位到唯一一层，不要在多层同时改
```

### 6.1 新增 CLI 适配器骨架

```python
"""<厂商> <产品> CLI 适配器。"""
from __future__ import annotations

from typing import Any, Iterable

from ..base import (AgentContext, AgentDefinition, AgentRun, Capabilities,
                    ConfigField, FieldType, Invocation, Runtime)
from ..events import AgentEvent, log, session, text_delta, usage
from ..registry import register_agent
from .util import as_list, flatten_content, truthy


@register_agent
class AcmeAgent(AgentDefinition):
    id = "acme"
    name = "Acme CLI"
    vendor = "Acme"
    description = "一句话中文说明。"
    runtime = Runtime.CLI
    executable = "acme"
    api_key_env = "ACME_API_KEY"
    base_url_env = "ACME_BASE_URL"
    default_gateway = "https://api.acme.dev/v1"
    default_model = "acme-pro"
    capabilities = Capabilities(sessions=True, native_system_prompt=True, tools=True,
                               working_directory=True)
    config_fields = (
        ConfigField("mode", "运行模式", FieldType.SELECT, default="auto",
                    options=[{"value": "auto", "label": "自动"},
                             {"value": "safe", "label": "安全"}]),
    )

    def build_invocation(self, ctx: AgentContext) -> Invocation: ...
    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]: ...
```

要求：`parse_line` 必须容忍脏行（解析失败 → `log(...)` 或忽略，不得抛出导致整轮失败）；
需要的解析状态放 `run.state`；结束时要补发事件就覆盖 `AgentRun.finish()`。

### 6.2 新增 API 型供应商

只写子类，复用工具循环：

```python
@register_agent
class AcmeHarness(OpenAIHarnessBase):
    id = "acme-harness"
    name = "Acme 内核"
    vendor = "Acme"
    description = "……"
    api_key_env = "ACME_API_KEY"
    base_url_env = "ACME_BASE_URL"
    default_gateway = "https://api.acme.dev/v1"
    default_model = "acme-chat"
```

`OpenAIHarnessBase` 已提供：流式补全、工具循环、`extra_headers`/`extra_body`、
工作目录沙箱工具（`harness_tools.WorkspaceTools`）。**不要重写一遍。**

### 6.3 零代码接入

用 `custom-cli`：填 `command`、`args`（支持 `{prompt} {model} {gateway} {api_key} {workspace} {session} {role} {agent}`
占位符）、`prompt_mode`、`output_format`、以及 `text_path/tool_path/..._path` JSON 取值路径。
只有在"声明式表达不了"时才升级为写适配器。

### 6.4 新增配置项
加 `ConfigField` → 前端自动出现。同时检查 `bootstrap.py` 的播种是否受影响（正常情况下不受影响，
因为 `coerce_options` 会用 `default` 补齐）。

### 6.5 新增事件类型
按 §4.1 的四处 + 文档同步；并补一个"事件 → 前端渲染"的测试或手测用例。

### 6.6 新增前端页面
`src/pages/XxxPage.tsx` + 在 `src/App.tsx` 的 `NAV`/`Routes` 注册 + 类型进 `src/lib/types.ts`
+ 接口进 `src/lib/api.ts` + 样式进 `src/index.css`（复用既有 class）。

---

## 7. 面向未来：如何把新智能体接进来

架构已经为下列方向预留了位置，接入时**不应**触碰 `api/`、`frontend/`、`models.py`：

| 目标 | 现状 | 接入方式 |
| --- | --- | --- |
| Gemini CLI | 已有 `gemini_cli.py`（`GEMINI_SYSTEM_MD` 注入人格、`--resume` 续接） | 已接入；新版本差异用 `ConfigField` 暴露开关 |
| DeepSeek 内核 | 已有 `openai_harness.py` 里的 `DeepSeekHarness` 子类（`deepseek-chat` / `deepseek-reasoner`） | 已接入；新增模型族只需加 `options` |
| 任意 OpenAI 兼容端点 | 已有 `openai_harness.py`（OpenRouter/vLLM/Ollama/Kimi/GLM） | 新供应商 = 一个子类 |
| 未来的 Aider / Cline / Continue 等 CLI | 无适配器 | 按 §6.1 写模块；或先用 §6.3 |
| 非 OpenAI 协议的 API（如 Anthropic Messages API） | 复用 `OpenAIHarnessBase` 不合适 | 以 `Runtime.API` 直接实现 `async stream(ctx)`，产出标准事件即可 |
| 需要"多智能体协作/编排" | 已有独立协作域 `app/collab/`（可视化编排 + 工单协议 + 串行引擎），见 `docs/collaboration-design.md` | 在 `app/collab/` 内扩展；每一步仍复用 `runtime.runner` 与事件协议，不要改适配器，也不要让框架层反向依赖协作域 |

判断标准始终是：**这次改动是否只落在 `agents/builtin/` 一个文件里？** 如果能，就说明架构还健康。

---

## 8. 前端规范

- 请求必须走 `src/lib/api.ts`；组件里禁止裸 `fetch`。
- 类型集中在 `src/lib/types.ts`，字段名与后端 `schemas.py` 保持一致。
- 表单统一用 `DynamicForm`（读 `config_fields`）；消息统一用 `MessageView`（读 `MessagePart`）。
- **对话只有一份展示模块**：所有对话（单聊、协作主对话、协作岗位实时对话，含移动端）都渲染
  `ConversationThread`（内部只用 `MessageView` 画气泡）；岗位线程的装配逻辑只有
  `CollabRoleThread` 一份。禁止再写第二套气泡 / 线程布局。
- 事件并入状态统一在 `ChatPage.applyEvent`，新增事件类型在这里加一个 `case`。
- 无 UI 框架 / 无状态管理库；CSS 手写，沿用 `card`、`btn`、`pill`、`banner`、`grid` 等既有 class。
- 面向用户的文案中文；错误展示沿用 `describe(error)`。
- **移动端（`/m/`）是独立入口，不是桌面端的窄屏适配**：入口 `frontend/m/index.html` + `src/mobile/`，
  有自己的路由（`MobileApp.tsx`）、样式表（`mobile.css`）、底部 Tab 与底部弹层。桌面端页面 / 入口 /
  样式表与移动端**互不引用**（守卫见 `backend/tests/test_mobile_web.py`）；两端只共享 `lib/*` 与
  `DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` / `CollabRoleThread`
  等纯展示组件，以及 `lib/agentStream` 里的事件归约。
- 移动端原语沿用既有类名（`btn` / `pill` / `banner` / `field` / `input` / `card` / `grid` /
  `card-section-title`…——定义在 `mobile.css`，因为移动端不加载 `index.css`）；只有移动端专有结构才用
  `m-` 前缀（`m-tabbar` / `m-sheet` / `m-item` / `m-thread`…）。
- **PWA 资产只放 `frontend/public/m/`**（`manifest.webmanifest` + `sw.js`）：`scope`/`start_url` 固定
  `/m/`，`/api/`（含 SSE 长连接）**永不进入缓存**。新增页面要保证断网时应用外壳仍能打开（离线只保证
  「界面能打开」，对话仍需联网）。
- **移动端锁定缩放**：它是「固定外壳」的应用形态，入口视口写 `maximum-scale=1.0, user-scalable=no`，
  并在 `mobile.css` 用 `touch-action: pan-x pan-y` 兜底（iOS Safari 忽略 `user-scalable=no`）；
  因此移动端输入框字号必须 ≥ 16px（iOS 聚焦小于 16px 的输入框会强制放大页面）。桌面端不禁用缩放。
- **前端依赖只允许这两个**：`react-markdown@10.1.0` 与 `remark-gfm@4.0.1`（精确版本）。理由：各内核的
  产出以 Markdown 为主（代码块、表格、列表、任务清单），此前用 `white-space: pre-wrap` 直出不可读；
  手写解析器等于重复造轮子，且转义/边界极易写错。为此新增 `components/MarkdownView.tsx`
  （统一代码块/表格/列表样式、复用既有 CSS 变量，并显式 `skipHtml` —— 不渲染原始 HTML，避免注入），
  `MessageView` 的文本段统一经它渲染。**除这两个库外不得再加前端依赖**；确实需要时先在本文件写明理由。

---

## 9. 维护规则

- **版本**：只改 `backend/app/__init__.py` 的 `__version__`（前端从 `/api/system/overview` 读取展示）。
- **依赖升级**：后端 `requirements*.txt` 用精确版本号；前端升级后必须 `npm run build` 通过。
  升级 FastAPI / Pydantic / SQLModel 大版本时，重点回归：SSE 流、外键 RESTRICT、JSON 列的读写。
- **弃用**：要下线一个配置项或事件类型，先在文档里标注，再在一个版本内保留兼容读取，
  最后删除；不要在同一次改动里既删旧又加新。
- **文档同步**：改了事件协议 → 改 `architecture.md`；改了分层/扩展方式 → 改本文件与 `AGENTS.md`。
- **不要引入**未被要求的中间件、DI 框架、日志框架、ORM 迁移工具；先写进文档讨论，再动手。

---

## 10. 评审 / 自检清单

改动提交前逐条对照（AI 与人都适用）：

- [ ] 这次改动落在**哪一层**？有没有越层或反向依赖？
- [ ] 有没有在框架层写 `if type_id == ...`？（守卫测试会拦）
- [ ] 新增的配置项是否用 `ConfigField` 声明，而不是改前端？
- [ ] 如果动了事件协议，四处是否同步？
- [ ] 凭据有没有可能进入日志 / SSE / 接口响应？命令行回显是否被 `_redact` 覆盖？
- [ ] 错误是否显式暴露（HTTPException 或 `error` 事件），而不是被吞掉？
- [ ] 长任务是否响应取消与超时？
- [ ] 中文规范：界面文案、注释、docstring 是否都是中文？专有名词是否保留？
- [ ] 移动端改动是否守住了物理隔离（不引用 `src/pages/`、`App.tsx`、`index.css`），且没有把内核专有知识（技能目录、配置文件路径）写进前端？
- [ ] PWA 相关改动是否仍然**不缓存 `/api/`**（含 SSE）？
- [ ] `bash scripts/test.sh` 是否全绿？新增能力是否有对应测试？
- [ ] 前端改动是否 `npm run build` 且重启了服务？
- [ ] schema 变更是否向后兼容，并在**现有数据库**上验证过？

---

## 11. 已知技术债（改到相关代码时顺手清理，但别扩大范围）

| 位置 | 问题 | 处理建议 |
| --- | --- | --- |
| 无迁移框架 | `init_db()` 用 `metadata.create_all`，无法处理列变更 | 频繁演进 schema 前引入 Alembic |
| `AgentDefinition.stream` | 基类用 `NotImplementedError`，靠 `Runtime.API` 分支兜底 | 可接受；新增 API 适配器务必实现 `stream()` |

---

## 12. 快速命令

```bash
bash scripts/test.sh                                  # pytest + 前端 tsc（含架构守卫）
cd frontend && npm run build                          # 改前端后必须构建
bash scripts/serve.sh                                 # 生产：0.0.0.0:8010
bash scripts/dev.sh                                   # 开发：API :8000 + Web :5173
curl -s http://127.0.0.1:8010/api/health              # 线上自检
curl -s http://127.0.0.1:8010/api/agent-types | python3 -m json.tool | head -40
```
