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
 │              │     ├─ 逐行读取 stdout（自建行缓冲，不受 asyncio 64 KiB 上限约束）
 │              │     │     → definition.parse_line() → 规范化事件
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

CLI 的 stdout 由 `runner._StreamLines` 拆行：`asyncio.StreamReader.readline()` 在单行超过 64 KiB
缓冲上限时会抛 `ValueError`（「Separator is found, but chunk is longer than limit」），而内核常常把整段代码
或工具结果塞进一行 JSONL，所以框架层不能把行长当成已知量。超过 64 MiB 的单行只丢该行并补一条
`log(warn)`，不会把整轮运行判成 error（铁律 10 / 11）。

**协作域**（`app/collab/`，见 `docs/collaboration-design.md`）是第二个编排器：它**不重写执行链路**，
每一步仍然调用 `runtime.runner.stream_run()`，因此取消、超时、脱敏、`run_timeout_seconds` 全都复用。
区别只在编排与记忆：

```
collab.engine.consume_turn()  ── 串行取一张就绪工单
      └─ collab.execution.prepare_step()   # 全域唯一接触 runtime/agents 的边界
           ├─ ctx.history = []、ctx.session_id = None   ← 每一步都是全新会话，不共享记忆
           └─ ctx.prompt = prompt.build_step_prompt(...) # 只含工单字段与 gist 摘要
      └─ runner.stream_run(ctx) → AgentEvent → 累积 → 解析 work_order → 更新工单 → 交给下一个岗位
      └─ 根工单收尾 → 交回入口角色做一次收尾汇报（附全流程工单汇总）→ 结论回到用户那条主会话
      └─ 入口角色聊天（并行）→ prepare_chat() → 只读工单快照 + 最近聊天 → 独立 chat_bus 推给用户
```

协作域使用自有的信封协议 `collab.*`（`app/collab/protocol.py`），与下面的智能体事件协议**分属两层**，
互不干扰；因此新增协作事件**不需要**改 `agents/events.py` / `consume_run` / `applyEvent`。

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

> 协作运行另有一层 `collab.*` 信封（`collab.run.started` / `collab.step.started` / `collab.step.event`
> / `collab.step.finished` / `collab.ticket.created` / `collab.ticket.updated` / `collab.run.status`
> / `collab.notice` / `collab.chat.started` / `collab.chat.event` / `collab.chat.finished`），
> 它包住原始 `AgentEvent` 后发给协作页；**不是**对这张表的扩展。
>
> 「运行时」页的安装 / 更新 / 卸载也走自有信封（`provision.started` / `provision.output` /
> `provision.finished`），由 `runtime/provisioning.py` 产出，同样**不属于**这张事件表。

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

    provisioner                                   # 运行时：如何安装 / 更新 / 卸载本内核
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

### 4.1 运行时管理（安装 / 更新 / 卸载）

「运行时」= 某个 CLI 适配器依赖的**本机可执行文件**。界面上的「运行时」页列出每个运行时
的安装状态、当前版本、安装位置与有多少实例在用它，并可直接安装 / 更新 / 卸载 / 检查更新。

和技能一样，**「用哪个包管理器、包名叫什么、装到哪儿」是适配器的知识**：适配器声明
`provisioner = RuntimeProvisioner(manager=…, package=…, install_argv=…, update_argv=…,
uninstall_argv=…, latest_argv=…)`，框架（`runtime/provisioning.py`）只负责按声明执行：

```
适配器声明命令 ──▶ runtime/provisioning.stream_provision() ──▶ 受控子进程
                                                              ├─ 逐行流式回显（SSE provision.output）
                                                              ├─ 总超时（settings.provision_timeout_seconds）
                                                              ├─ 命令脱敏（runner._redact）
                                                              └─ 客户端断开即 kill（同一次运行的取消语义）
```

- `provision.*` 是安装器**自有的信封**（`provision.started / provision.output /
  provision.finished`），与协作域的 `collab.*` 同理，**不是**适配器事件协议，
  因此不参与铁律 5 的四处同步。
- 未声明 `provisioner` 的适配器（进程内 API 内核、`custom-cli`）在界面上标成「进程内 / 自备」，
  平台不代管它们的环境。卸载属于破坏性操作，接口要求显式 `confirm`。

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
agent_instances(id, name, type_id, gateway_url, model, api_key_encrypted, options JSON, enabled,
                deleted_at)
roles(id, name, avatar, tagline, system_prompt, agent_id→agent_instances, workspace_dir,
      parameters JSON, deleted_at)
conversations(id, title, role_id→roles, agent_id→agent_instances, session_id, workspace_dir, archived)
messages(id, conversation_id→conversations, role, content, parts JSON, usage JSON, status, error)
skills(id="<type_id>:<slug>", type_id, slug, name, description, source_kind, path, fingerprint,
       version, author, when_to_use, allowed_tools JSON, files JSON, total_bytes, meta JSON,
       available, note)
role_skills(role_id→roles, skill_id→skills，复合主键 + position)

-- 协作域（独立于对话域，见 docs/collaboration-design.md）
collab_teams(id, name, description, entry_key, workspace_dir, graph JSON, budget JSON)
collab_runs(id, team_id→collab_teams, title, goal, status, entry_key, workspace_dir, budget, usage,
            steps_used, tickets_used, error)
collab_tickets(id, run_id→collab_runs, parent_ticket_id→collab_tickets, from_role_id→roles,
               to_role_id→roles, owner_key, holder_key, submitter_key, title, brief JSON, state,
               round, max_rounds, verdict, review_comment, result JSON, closed_at, report_at)
collab_steps(id, run_id→collab_runs, ticket_id→collab_tickets, role_id→roles, agent_id→agent_instances,
             role_key, duty, sequence, action, status, content, gist, parts JSON, usage JSON,
             error, audit JSON)
collab_messages(id, run_id→collab_runs, sender, role_key, role_id→roles, agent_id→agent_instances,
                sequence, status, content, parts JSON, usage JSON, error, meta JSON)
```

设计取舍：

- **对话快照 `agent_id`**：之后即便角色改绑别的智能体，历史对话仍可复现。
- **`parts` 用 JSON**：工具调用/结果/推理的形态随智能体变化，无需为每种智能体建表；
  它是按真实发生顺序排列的时间线（`thinking` / `text` / `tool_call` / `tool_result`），
  前端据此原样渲染「思考→正文→工具调用→工具结果→正文…」，不做按类型归组。
  旧消息没有内联 `text` 段，前端会用 `content` 合成一段兜底，保持向后兼容。
- **`parameters` 允许角色级覆盖** `model` / `gateway_url`：同一个智能体可以让不同角色走不同模型。
- **技能只增不删**：技能是从文件系统扫描出来的，目录消失只标记 `available=false`，
  因此角色分配（`role_skills`）永远指向真实存在的记录；角色被删除时分配关系保留（见下条）。
- **删除角色/智能体只做软删除**：外键全是 `RESTRICT`，而历史对话与历史工单都引用着角色与智能体，
  硬删会连历史一起打断。所以 `DELETE /api/roles/{id}`、`DELETE /api/agents/{id}` 只写
  `deleted_at`：实体从所有可选列表里消失（新建对话、新建角色都拒绝它），
  但历史对话仍能显示当时的角色名/头像、也仍能继续对话；因此角色删除不再受"有没有历史对话"影响。
- **协作域的分工键**：工单用 `owner_key`（谁拆的谁汇总）、`holder_key`（现在谁握着）、
  `submitter_key`（最近提交者，打回时默认退回给他）记岗位流转；步骤用 `gist` 存**引擎派生**的
  一句话摘要 —— 拼下游 prompt 时只读 `gist`、不读 `content`，从数据通路上保证"下游只看工单、
  看不到上游对话"。岗位用 key 而不是角色 id，是因为角色可以改绑，历史工单仍要能复现。
- **加字段要能容忍旧行**：`create_all` 只建新表、**不会给已存在的表加列**，所以"后加的可选列"
  登记在 `models.py` 的 `ADDITIVE_COLUMNS`（对话域）与 `collab/tables.py` 的
  `ADDITIVE_COLUMNS`（协作域），由 `db.init_db` 用 `ALTER TABLE ... ADD COLUMN`（带默认值）
  幂等补齐：现有 `magenta.db` 启动后自动对齐，不重建、不丢数据。
- **外键用 `ON DELETE RESTRICT`**：仍被**未删除**的角色引用的智能体不能删（409，避免历史断裂）；
  删除对话时显式清理消息（SQLite 已开启 `PRAGMA foreign_keys`）。

## 7. 扩展点

| 想做的事 | 需要改的地方 |
| --- | --- |
| 接入新 CLI 智能体 | 在 `agents/builtin/` 加一个模块 |
| 接入新模型供应商（无官方 CLI） | 复用 `openai_harness.py` 的基类，加一个子类声明默认网关/模型 |
| 零代码接入私有 CLI | 用 `custom-cli` 适配器填命令 + JSON 取值路径 |
| 让运行时可由平台安装 / 更新 | 在该适配器声明 `provisioner`（`RuntimeProvisioner`），界面自动出现按钮 |
| 新增可配置项 | 在该适配器加一个 `ConfigField`，前端表单自动出现 |
| 新增移动端页面 | 在 `frontend/src/mobile/` 加页面并在 `MobileApp.tsx` 注册路由（桌面端不受影响） |
| 让某个适配器支持技能 | 声明 `capabilities.skills=True`，实现 `discover_skills()` 与 `skill_mount()` |
| 新增协作能力（多角色工单流转） | 全部在 `app/collab/`（独立域，不碰框架层），见 `docs/collaboration-design.md` |
| 换掉事件传输（SSE→WebSocket） | 只改 `transport/sse.py` 与前端 `streamApi` |
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
- 运行时的安装 / 更新按适配器声明的命令执行（内置 CLI 内核为官方 npm 包）。若某台机器上的
  内核是用别的方式装的（例如发行版包、容器、垫片脚本），平台更新会按声明的包管理器**另装一份**；
  页面会显示当前可执行文件路径供核对，但不会自动识别、也不会去卸载非本平台安装的副本。

## 9. 移动端与 PWA

移动端不是桌面端的「窄屏适配」，而是**另一条物理独立的链接**：`/m/`。

```
frontend/
├── index.html              # 桌面端入口 → /
├── m/index.html            # 移动端入口 → /m/
├── public/m/               # 移动端 PWA 资产（manifest + Service Worker）
├── public/icons/           # 两端口共用的应用图标（由 scripts/gen_icons.py 生成）
└── src/
    ├── App.tsx · pages/ · components/ · index.css    # 桌面端
    └── mobile/                                       # 移动端（独立页面与样式表）
        ├── main.tsx · MobileApp.tsx · mobile.css
        ├── components/（底部 Tab · 顶栏 · 底部弹层）
        └── pages/（对话列表 · 对话 · 角色 · 智能体 · 技能 · 协作 · 设置）
```

- **只共享基础设施，不共享界面**：两端共用 `lib/`（`api` / `types` / `agentStream` / `usage` / `collabDraft`）
  与纯展示组件（`DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` /
  `CollabRoleThread`）；页面、路由、样式表各自独立
  （移动端用底部弹层替代桌面端的居中 Modal）。
- **后端只保留一个 SPA 兜底**：`main.py:resolve_spa_target()` 按前缀把深链分派到 `dist/index.html`
  或 `dist/m/index.html`，其余静态文件原样返回；`/assets` 由两端口共享。
  ⚠️ `/api/` 下的漏网请求**不**走这个兜底：`main.py` 明确回 JSON 404（`接口不存在：/api/xxx`）。
  兜底一旦回 HTML，前端 `response.json()` 就会解析失败 —— WebKit 抛出的是与业务无关的
  "The string did not match the expected pattern."（Chromium 又是另一种说法），排查成本极高。
- **接口错误文案不交给浏览器**：`lib/api.ts` 先把响应读成文本再自己 `JSON.parse`：
  空响应体按「没有返回」处理，非 JSON（反向代理的登录页 / 静态兜底）转成中文提示，
  并带上 HTTP 状态与 `Content-Type`，不暴露浏览器方言的解析异常。
- **PWA**：`public/m/manifest.webmanifest`（`scope` 与 `start_url` 都是 `/m/`）+
  `public/m/sw.js`（安装期预缓存入口 HTML 与其引用的构建产物；导航网络优先、离线回落外壳）。
  Service Worker 的作用域只在 `/m/`，且 `/api/`（含 SSE 长连接）永不缓存、一律直连后端——
  离线只保证「界面能打开」，对话仍需联网。
- **改了外壳就要 bump 缓存版本**：`sw.js` 的缓存名带版本号（`magenta-m-shell-v2`…），
  动过入口 HTML / 样式表 / 构建产物就 +1 —— 已安装的独立应用才会在下次打开时换掉旧外壳。
  独立应用没有地址栏也没有刷新按钮，因此设置页提供了「更新到最新版本」（`pwa.ts:refreshShell()`：
  注销 SW、清掉 `magenta-m-*` 缓存再重载），作为卡在旧外壳时的自助出口。
- **移动端的取舍**：协作域不提供可视化画布（连线需要精确拖拽，手机上体验很差），
  改在「协作组」详情页用**表单**编排：岗位是卡片、连线是「从谁到谁」的下拉选择；
  两端的字段语义、默认值与中文标签共用 `lib/collabDraft.ts` 一份，校验一律走服务端
  `POST /api/collab/teams/{id}/validate`。
- **外壳钉在可视视口上**：`.m-app` 用 `position: fixed; top/right/bottom/left: 0`，而不是
  `100vh`/`100dvh` 配 `height: 100%` 父链 —— 真机上地址栏与底部工具栏会伸缩，两者高度经常对不齐，
  底栏就会浮起来留一条缝或被工具栏压住。固定定位跟的是可视视口，刘海与 Home 条交给
  `env(safe-area-inset-*)`（守卫测试 `test_mobile_shell_is_pinned_to_the_visible_viewport`）。
- **移动端锁定缩放**：移动端采用「固定外壳」布局（顶栏 + 底部 Tab + 内部滚动区），随手指缩放会
  让外壳错位，因此入口视口写 `maximum-scale=1.0, user-scalable=no`，并用 `mobile.css` 的
  `touch-action: pan-x pan-y` 兜底（iOS Safari 自 iOS 10 起忽略 `user-scalable=no`，只有
  `touch-action` 真正生效）。因此移动端输入框字号统一 ≥ 16px —— iOS 聚焦小于 16px 的输入框会
  强制放大页面。桌面端不禁用缩放（浏览器缩放是它的无障碍能力）。
