# 架构说明

## 1. 分层的目标

这个系统要长期承接「越来越多智能体」。因此把**易变**的部分（各种智能体的调用方式与输出格式）
压缩到一个极薄的插件层，把**稳定**的部分（流式传输、会话记忆、持久化、UI 表单、取消与超时）
全部沉淀到框架里。

```
             ┌────────────────────────── 稳定层（很少改） ──────────────────────────┐
 HTTP/SSE ──→ api/ ──→ runtime/service.py ──→ runtime/runner.py ──→ 规范化事件 ──→ 前端
                            │                       │
                            │                       └──→ agents/builtin/*   ← 易变层（唯一扩展点）
                            └──→ SQLModel / SQLite
             └──────────────────────────────────────────────────────────────────────┘
```

## 2. 一次运行的完整时序

```
前端  POST /api/conversations/{id}/messages  {content}
 │
 ├─ api/conversations.send_message()
 │    └─ service.prepare_run()
 │         ├─ 载入 Conversation → Role → AgentInstance → AgentDefinition
 │         ├─ 角色技能：role_skills → skills 表 → 物化到 agent-homes/<对话>/<内核配置目录>
 │         ├─ capability 决策：
 │         │    支持 session  → 只发本轮 prompt，靠智能体自己的记忆续接
 │         │    不支持 session → 回放数据库里的最近 N 轮（transcript）
 │         │    无原生人格   → 把 system prompt 折进 prompt
 │         ├─ 落库 user message + 占位 assistant message(status=streaming)
 │         ├─ 自动为首轮对话生成标题
 │         └─ registry.register() 生成 run_id（供取消/追踪）
 │
 ├─ 返回 StreamingResponse(text/event-stream)
 │    └─ service.consume_run()
 │         └─ runner.stream_run()
 │              ├─ Runtime.CLI → create_subprocess_exec(argv, env, cwd, stdin)
 │              │     ├─ 逐行读取 stdout → definition.parse_line() → 规范化事件
 │              │     ├─ 物化 Invocation.files（如 gemini 的系统提示词文件）
 │              │     ├─ 每秒检查取消信号 / 全局超时
 │              │     └─ stderr 汇总，非 0 退出时作为 error 事件上报
 │              └─ Runtime.API → definition.stream(ctx)
 │                    └─ 流式 chat/completions + 工具循环（工具在工作目录内执行）
 │
 └─ 每个事件：既 yield 给 SSE，也在 consume_run 里被累积
      （文本、推理、工具调用/结果、usage、session_id）
    finally 块无论成功/失败/取消都会写回 assistant message 并 commit
```

客户端断开连接时，`StreamingResponse` 的生成器被关闭，`guarded()` 捕获
`CancelledError` 并调用 `registry.cancel(run_id)`，子进程被 kill，消息落地为 `interrupted`。

## 3. 规范化事件协议

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `run.started` | `payload.run_id` | 建立运行标识 |
| `message.delta` | `text` | 正文增量 |
| `thinking.delta` | `text` | 推理增量（DeepSeek `reasoning_content`、Claude thinking） |
| `tool.call` | `name`, `call_id`, `payload.input` | 工具调用 |
| `tool.result` | `call_id`, `payload.output`, `payload.is_error` | 工具结果 |
| `usage` | 任意数值 | token / 费用 / 耗时 |
| `session` | `payload.session_id` | 智能体侧会话 id，落库以支持多轮 |
| `log` | `text`, `payload.level` | 命令回显、诊断信息 |
| `error` | `text` | 可展示的错误 |
| `run.finished` | `payload.status` ∈ ok/error/cancelled | 终态 |

前端 `applyEvent()` 与后端 `consume_run()` 都只依赖这张表。

## 4. 适配器的契约

```python
class AgentDefinition(ABC):
    id / name / vendor / runtime / capabilities / config_fields   # 元数据（驱动 UI）
    api_key_env / base_url_env / default_gateway / default_model  # BYO gateway 约定
    executable                                                    # 安装探测

    def availability() -> Availability          # 依赖 shutil.which + --version
    def coerce_options(raw) -> dict             # 按 config_fields 校验/补默认值
    def effective_env(ctx) -> dict              # 凭据 → 框架约定的环境变量
    def system_defaults() -> dict               # 读取本机真实配置（网关/模型/凭据）
    def resolve_gateway/resolve_model(value)    # 「未自定义」时回落到本机真实配置

    def build_invocation(ctx) -> Invocation      # CLI：argv/env/cwd/stdin/files
    def parse_line(line, run) -> Iterable[Event] # CLI：逐行解析（run.state 存放解析状态）
    async def stream(ctx) -> AsyncIterator[Event]# API：进程内直接产事件

    def discover_skills() -> list[SkillSpec]     # 技能：本内核的技能根（只读扫描）
    def skill_mount(ctx) -> SkillMount | None    # 技能：把分配到的技能物化到哪里
```

`capabilities` 决定了平台在框架层面**如何**照顾这个智能体：

- `sessions=True` → 平台把 `session_id` 存到对话上，下一轮只发增量。
- `native_system_prompt=False` → 平台把人格折进 prompt（或按适配器自己的方式注入）。
- `working_directory=True` → 为每个对话分配独立工作目录作为 cwd。
- `permission_modes / mcp / tools` → 供前端展示能力徽标，并按需渲染对应配置项。

`resume_supported(ctx)` 是可选钩子：Codex 老版本没有 `exec resume`，适配器可以在
选项里让用户切换成「回放 transcript」，框架会据此走另一条分支。

`system_defaults()` 是可选钩子：需要对齐 CLI 自身系统级配置的适配器可以覆盖它，
从本机读取真实默认值（Claude Code 读 `~/.claude/settings*.json` 的 `env` 与 `model`，
进程环境变量优先）。框架在新建智能体、展示配置与构建运行上下文时，会把「仍等于内置
默认值」的网关/模型视为未自定义，回落到这份真实配置；用户显式填写过的值则保持不变。

## 5. 技能（Skill）

技能是各智能体内核自带的能力包（一个目录 + ``SKILL.md``）。平台把它们**统一编目**，
再通过**角色分配**决定某个角色能用哪些技能：

```
各内核技能根 ──discover_skills()──▶ 统一目录(skills 表) ──分配──▶ role_skills ──▶ 运行时物化
  ~/.claude/skills                        （只读索引）        （角色弹窗）       agent-homes/<对话>/…
  $CODEX_HOME/skills
```

- **编目**：适配器的 `discover_skills()` 只读扫描自己内核的技能根（个人 / 项目 / 插件 / 系统），
  框架去重（同名技能按「个人 > 项目 > 插件 > 系统」取优先级最高的一份）后写入 `skills` 表。
  目录消失时只把 `available` 置 False，**不删行**，角色分配不会断链。
- **分配**：`role_skills` 是角色能用的技能的**唯一来源**；`PUT /api/roles/{id}/skills` 整表替换。
  类型与角色绑定的智能体不一致的技能，界面直接禁止选择，服务端再兜底返回 422。
- **物化（严格仅分配）**：适配器的 `skill_mount(ctx)` 声明「我的内核从哪里读技能」——
  例如 Claude Code 用 `CLAUDE_CONFIG_DIR` 指向一个只含「分配到的技能 + 本机 settings」的隔离
  配置目录，并关闭内核自带的 bundled 技能与内置插件；Codex 用 `CODEX_HOME`。
  框架按这份声明把技能目录复制进 `agent-homes/<对话ID>/<内核配置目录>`（每次运行重建技能目录，
  取消分配即立刻消失），并携带/打补丁必要的配置文件。

  隔离目录放在数据目录而不是临时目录：内核的会话记录也写在自己的配置目录下，
  每次运行都换目录会导致多轮对话无法续接。

- **边界**：内核自带的少量技能（如 Claude Code 的 `doctor`）需要通过 `skillOverrides` 逐个关闭；
  内核还会读取工作目录下的 `<工作目录>/.claude/skills`，这类技能不在分配范围内，运行时会给出提示。

## 6. 数据模型

```
agent_instances(id, name, type_id, gateway_url, model, api_key_encrypted, options JSON, enabled)
roles(id, name, avatar, tagline, system_prompt, agent_id→agent_instances, workspace_dir, parameters JSON)
conversations(id, title, role_id→roles, agent_id→agent_instances, session_id, workspace_dir, archived)
messages(id, conversation_id→conversations, role, content, parts JSON, usage JSON, status, error)
skills(id="<type_id>:<slug>", type_id, slug, name, description, source_kind, path, fingerprint,
       version, author, when_to_use, allowed_tools JSON, files JSON, total_bytes, meta JSON,
       available, note)
role_skills(role_id→roles, skill_id→skills，复合主键 + position)
```

设计取舍：

- **对话快照 `agent_id`**：之后即便角色改绑别的智能体，历史对话仍可复现。
- **`parts` 用 JSON**：工具调用/结果/推理的形态随智能体变化，无需为每种智能体建表；
  它是按真实发生顺序排列的时间线（`thinking` / `text` / `tool_call` / `tool_result`），
  前端据此原样渲染「思考→正文→工具调用→工具结果→正文…」，不做按类型归组。
  旧消息没有内联 `text` 段，前端会用 `content` 合成一段兜底，保持向后兼容。
- **`parameters` 允许角色级覆盖** `model` / `gateway_url`：同一个智能体可以让不同角色走不同模型。
- **技能只增不删**：技能是从文件系统扫描出来的，目录消失只标记 `available=false`，
  因此角色分配（`role_skills`）永远指向真实存在的记录；删除角色时分配关系级联删除。
- **外键用 `ON DELETE RESTRICT`**：删除被引用的智能体/角色会被拒绝（409），避免历史断裂；
  删除对话时显式清理消息（SQLite 已开启 `PRAGMA foreign_keys`）。

## 7. 扩展点

| 想做的事 | 需要改的地方 |
| --- | --- |
| 接入新 CLI 智能体 | 在 `agents/builtin/` 加一个模块 |
| 接入新模型供应商（无官方 CLI） | 复用 `openai_harness.py` 的基类，加一个子类声明默认网关/模型 |
| 零代码接入私有 CLI | 用 `custom-cli` 适配器填命令 + JSON 取值路径 |
| 新增可配置项 | 在该适配器加一个 `ConfigField`，前端表单自动出现 |
| 让某个适配器支持技能 | 声明 `capabilities.skills=True`，实现 `discover_skills()` 与 `skill_mount()` |
| 换掉事件传输（SSE→WebSocket） | 只改 `api/sse.py` 与前端 `streamApi` |
| 换掉存储（SQLite→Postgres） | 只改 `settings.database_url` 与 `db.py` |

## 8. 已知边界

- CLI 适配器的多轮依赖各工具自身的会话续接；`custom-cli` 若声明不出 `session_path`，
  平台会退化为 transcript 回放。
- `codex exec resume` 的子命令形式在不同 Codex 版本间有差异，所以做成了可选策略。
- Gemini CLI 的人格注入依赖 `GEMINI_SYSTEM_MD`，若目标版本不支持可切换为 prepend 模式。
- 工具循环（API harness）的权限模型是「工作目录沙箱」，没有做命令级审批。
- 技能「严格仅分配」只保证**平台注入的技能 == 角色分配**：内核自带的个别技能（Claude Code 的
  `doctor`）要靠 `skillOverrides` 关闭；工作目录自带的 `.claude/skills` 会被内核直接读取，
  平台只提示、不删除用户文件。
- 技能目前只支持「查看 + 分配」；编辑、安装与上传留待后续（技能目录仍由各内核自己管理）。
