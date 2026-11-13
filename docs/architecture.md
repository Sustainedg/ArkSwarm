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
前端  POST /api/conversations/{id}/messages  {content, attachments}
 │
 ├─ api/conversations.send_message()
 │    └─ service.prepare_run()
 │         ├─ 载入 Conversation → AgentProfile → AgentDefinition（+ Gateway 提供密钥/地址）
 │         ├─ 智能体技能：agent_skills → skills 表 → 物化到 agent-homes/<对话>/<内核配置目录>
 │         ├─ 智能体 MCP：agent_mcp_servers → mcp_servers 表 → 解密后按内核形状写进同一个隔离目录
 │         ├─ capability 决策：
 │         │    支持 session  → 只发本轮 prompt，靠智能体自己的记忆续接
 │         │    不支持 session → 回放数据库里的最近 N 轮（transcript）
 │         │    无原生人格   → 把 system prompt 折进 prompt
 │         ├─ 附件（可选）：先 POST /{id}/attachments 存进工作目录，这里把相对路径并进 prompt
 │         ├─ 落库 user message + 占位 assistant message(status=streaming)
 │         ├─ 自动为首轮对话生成标题
 │         └─ registry.register() 生成 run_id（供取消/追踪）
 │
 ├─ 返回 StreamingResponse(text/event-stream)
 │    └─ 后台任务 service._execute_prepared()（与 HTTP 连接解耦）
 │         └─ service.consume_run()
 │              └─ runner.stream_run()
 │              ├─ Runtime.CLI → create_subprocess_exec(argv, env, cwd, stdin)
 │              │     ├─ 逐行读取 stdout（自建行缓冲，不受 asyncio 64 KiB 上限约束）
 │              │     │     → definition.parse_line() → 规范化事件
 │              │     ├─ 物化 Invocation.files（如 gemini 的系统提示词文件）
 │              │     ├─ 每秒检查取消信号 / 全局超时
 │              │     └─ stderr 汇总，非 0 退出时作为 error 事件上报
 │              └─ Runtime.API → definition.stream(ctx)
 │                    └─ 流式 chat/completions + 工具循环（工具在工作目录内执行）
 │
 └─ 每个事件：既 publish 给当前订阅者，也在 consume_run 里被累积
      （文本、推理、工具调用/结果、usage、session_id）
    finally 块无论成功/失败/取消都会写回 assistant message 并 commit
```

**执行与连接解耦**：`send_message` 在 `prepare_run` 之后用 `service.start_run()` 把这一轮丢进
后台任务（`ActiveRun` 持有事件缓冲与订阅者队列），HTTP 的 SSE 只负责订阅。客户端刷新 /
关页面只会让订阅者离开，**不会** kill 子进程或中断这一轮；重新打开对话时，详情里的
`active_run` 会让前端改为订阅 `GET /api/conversations/{id}/stream`，先重放缓冲、再收实时增量
（另一台设备同样如此）。真正取消只走 `POST /api/conversations/{id}/cancel`。

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
      └─ 根工单收尾 → 交回入口智能体做一次收尾汇报（附全流程工单汇总）→ 结论回到用户那条主会话
      └─ 入口智能体聊天（并行）→ prepare_chat() → 只读工单快照 + 最近聊天（+ 附件路径）→ 独立 chat_bus 推给用户
```

协作域使用自有的信封协议 `collab.*`（`app/collab/protocol.py`），与下面的智能体事件协议**分属两层**，
互不干扰；因此新增协作事件**不需要**改 `agents/events.py` / `consume_run` / `applyEvent`。

## 3. 规范化事件协议

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `run.started` | `payload.run_id` | 建立运行标识 |
| `message.delta` | `text` | 正文增量 |
| `thinking.delta` | `text` | 推理增量（如 `reasoning_content`、Claude thinking） |
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
    protocol / default_auth_style / default_models                 # 官方链路与自定义网关的匹配依据
    executable                                                    # 安装探测

    def availability() -> Availability          # 依赖 shutil.which + --version
    def coerce_options(raw) -> dict             # 按 config_fields 校验/补默认值
    def effective_env(ctx) -> dict              # 凭据 → 框架约定的环境变量
    def system_defaults() -> dict               # 读取本机真实配置（网关/模型/凭据）
    def resolve_gateway/resolve_model(value)    # 「未自定义」时回落到本机真实配置

    def build_invocation(ctx) -> Invocation      # CLI：argv/env/cwd/stdin/files
    def parse_line(line, run) -> Iterable[Event] # CLI：逐行解析（run.state 存放解析状态）
    async def stream(ctx) -> AsyncIterator[Event]# API：进程内直接产事件

    def skill_roots() -> list[SkillRoot]         # 技能：本内核的技能根（路径 + 来源 + 是否可安装）
    def discover_skills() -> list[SkillSpec]     # 技能：扫描上面这些根（默认由框架 scan_skill_roots 完成）
    def skill_mount(ctx) -> SkillMount | None    # 技能：把分配到的技能物化到哪里

    def discover_mcp_servers() -> list[McpServerSpec]  # MCP：只读扫描本机已有的 MCP 配置
    def mcp_mount(ctx) -> McpMount | None              # MCP：把分配到的服务器写到哪里

    logo                                          # 官方 Logo（/icons/kernels/*.svg，前端当遮罩染色）
    provisioner                                   # 运行时：如何安装 / 更新 / 卸载本内核
```

`capabilities` 决定了平台在框架层面**如何**照顾这个智能体：

- `sessions=True` → 平台把 `session_id` 存到对话上，下一轮只发增量。
- `native_system_prompt=False` → 平台把人格折进 prompt（或按适配器自己的方式注入）。
- `working_directory=True` → 为每个对话分配独立工作目录作为 cwd。
- `permission_modes / tools` → 供前端展示能力徽标，并按需渲染对应配置项。
- `mcp=True` → 该内核支持 MCP：适配器**必须**实现 `discover_mcp_servers()` 与 `mcp_mount()`
  （有架构守卫测试）；框架据 `mcp_mount` 把智能体分配到的 MCP 服务器写进隔离目录。

`resume_supported(ctx)` 是可选钩子：Codex 老版本没有 `exec resume`，适配器可以在
选项里让用户切换成「回放 transcript」，框架会据此走另一条分支。

`system_defaults()` 是可选钩子：需要对齐 CLI 自身系统级配置的适配器可以覆盖它，
从本机读取真实默认值（Claude Code 读 `~/.claude/settings*.json` 的 `env` 与 `model`，
进程环境变量优先）。框架在新建智能体、展示配置与构建运行上下文时，会把「仍等于内置
默认值」的网关/模型视为未自定义，回落到这份真实配置；用户显式填写过的值则保持不变。

### 4.1 运行时管理（安装 / 更新 / 卸载）

「运行时」= 某个 CLI 适配器依赖的**本机可执行文件**。界面上的「运行时」页每张卡片只留
**官方 Logo + 内核名 + 所属公司 + 当前版本**（其余信息收进按钮后面的动作里），并可直接
安装 / 更新 / 卸载 / 检查更新：

- **Logo 由适配器声明**（`AgentDefinition.logo` 指向 `/icons/kernels/*.svg`），前端用
  `components/KernelLogo.tsx` 把它当遮罩染成当前文字色 —— 深浅两套外观共用一份单色图形，
  不必为每个内核准备两张图。前端**不**按 `type_id` 写映射（铁律 1）。
- **已经是最新版就不给「更新」按钮**：界面先查一次最新版本，只有确实更高才显示
  「更新到 <版本>」；查不到（该适配器没声明 `latest_argv`、查询失败或还没查）时保留普通
  「更新」，免得用户失去升级手段。

和技能一样，**「用哪个包管理器、包名叫什么、装到哪儿」是适配器的知识**：适配器声明
`provisioner = RuntimeProvisioner(manager=…, package=…, install_argv=…, update_argv=…,
uninstall_argv=…, latest_argv=…, prepare_argv=…)`，框架（`runtime/provisioning.py`）只负责按声明执行：

```
适配器声明命令 ──▶ runtime/provisioning.stream_provision() ──▶ 受控子进程
                                                              ├─ 动作前先跑 prepare_argv（清扫残留，失败只告警）
                                                              ├─ 逐行流式回显（SSE provision.output）
                                                              ├─ 总超时（settings.provision_timeout_seconds）
                                                              ├─ 命令脱敏（runner._redact）
                                                              └─ 客户端断开即 kill（同一次运行的取消语义）
```

- **动作前先清扫（`prepare_argv`）**：上一次动作被中途打断（超时、服务重启）可能在本机
  留下让包管理器再也无法覆写安装目录的残留 —— 典型是 npm 的中转目录（卸载 / 覆盖安装前
  先把旧目录 rename 成 `.<包名>-<安装路径哈希>` 再删掉），而中转名只由绝对路径决定，
  于是之后**每一次**安装 / 更新 / 卸载都会永久撞上 `ENOTEMPTY`。适配器声明这条清扫命令
  （内置的三个 npm 内核用 `builtin/util.py:npm_prepare_argv()` 生成），框架在同一次动作的
  最前面跑一遍并回显它的输出；清扫与动作共用同一个总时限，清扫自己失败只回一行告警、
  **不**中断动作 —— 成败始终只由动作自己的退出码决定。

- **安装 / 更新还要自己处理「本机既有的同名可执行文件」**：`<npm 前缀>/bin/<命令>` 可能
  已经被别的安装占着（系统里另一个 node 装的同一内核、用户手写的垫片），而 npm 只肯覆盖
  **指向自己安装目录**的链接，否则整个动作直接失败在 `EEXIST`（连包都不装）。所以 npm 内核的
  `install_argv` / `update_argv` 由 `builtin/util.py:npm_install_argv()` 生成：先判断那个占位
  是不是自己的（解析后落在 npm 全局目录里就不动它），不是就挪到一个隐藏备份名
  （`.<命令>.magenta-bak`）让出位置，npm 失败再把备份放回原位 —— 不留「命令没了、包也没装上」
  的半截状态。包装命令顺带负责调用 npm，所以它天然只挂在安装 / 更新上；卸载不建链接，
  `uninstall_argv` 保持原样，确认框里显示的就是它。

**安装状态与版本是缓存出来的，不是每次打开页面现探的**：探测要起一次 `内核 --version`
子进程（gemini 这类 Node CLI 冷启动接近 2 秒），而后端是单进程单事件循环 —— 同步探测会
把**整站所有接口**一起冻住（实测开网页时并发的读数请求被拖到 20 秒）。因此
`runtime/availability.py` 采用与技能编目同构的「编目 + 定时刷新」：

```
读接口 ──▶ runtime/availability.statuses() ──▶ runtime_status 表（只读，缺行时只查 PATH）
                     │ 未命中 / 过期 → 登记进待测集合（不等待、不建任务）
                     ▼
runtime/availability.scheduler()（lifespan 启动，随服务关闭取消）
        ├─ 启动补齐：缺行 / 过期才补测（重启频繁，光靠定点容易整天漏掉）
        ├─ 消费待测集合（每 WATCH_INTERVAL 秒看一次）
        └─ 每天 DAILY_CHECK_HOUR=7 点（东八区）全量重测
                     │
安装 / 更新 / 卸载收尾 ──┴─▶ refresh_definition() + 界面「重新检测」按钮
```

- 探测一律在**工作线程**里跑（`asyncio.to_thread`），即使慢也不占用事件循环；
- 界面卡片据此显示「检测于 …」；可用性只是展示元数据，一次运行并不依赖它
  （`runtime/service.prepare_run` 不读它），所以缓存过期最多是版本号晚几小时。
- `provision.*` 是安装器**自有的信封**（`provision.started / provision.output /
  provision.finished`），与协作域的 `collab.*` 同理，**不是**适配器事件协议，
  因此不参与铁律 5 的四处同步。
- 未声明 `provisioner` 的适配器（进程内 API 内核、`custom-cli`）卡片副标题写明
  「进程内运行」/「自备可执行文件」，平台不代管它们的环境。卸载属于破坏性操作，接口要求显式 `confirm`。

### 4.2 凭据库（Credential）

网关密钥是**模型提供方**的密钥；智能体干活时还会用到别的密钥（GitHub token、数据库口令…），
这类叫**凭据**，与网关彻底分开。凭据是**全局一份**：

- `credentials(id, name, description, value_encrypted)`：凭据本身，在「设置 → 凭据库」里维护；
- `agent_credentials(agent_profile_id, credential_id, position)`：智能体 → 凭据的引用关系，
  与 `agent_skills` 同一套思路 —— 智能体运行时能拿到的凭据只能来自这张表。

纯逻辑在 `app/credentials.py`（叶子模块，对话域与协作域共用），读库 / 落库 / 引用关系在
`runtime/credentials.py`：

- **落库即加密**：值只经 `security.encrypt()`；接口只回 `{id, name, description, has_value, masked}`，
  明文永不出库（`reads()`）。改值时值留空 = 不换值，所以回显掩码不会被写回去。
- **名即环境变量名**：`name` 必须是合法环境变量名（`^[A-Za-z_][A-Za-z0-9_]{0,63}$`），否则 422；
  库里重名同样 422。
- **两条入口**：智能体弹窗里点名字输入框就是凭据库（挑一条＝建立引用；输入新名字 + 值＝顺手新建一条
  并引用），设置页的「凭据库」负责改名 / 换值 / 删除。仍被未删除的智能体引用时删除返回 409。
- **只以环境变量注入**：`runner` 把 `ctx.credentials` 合并进子进程环境（适配器/网关给的同名变量
  优先，网关仍是模型密钥的唯一来源）；进程内 API 内核把同一份交给它的 `run_shell`。
  模型只知道变量名 —— 框架把人名的名字清单（`notice()`）并进人格文本，**值不进提示词**。
- **值会被擦除**：运行层对每条事件过一遍 `scrub_event()`，即使子进程把值回显出来，
  也进不了 SSE、落库内容与命令行回显（`runner._redact`）。

### 4.2.1 凭据信任边界与网络出口过滤

**信任边界声明**：有 shell 访问或网络访问的智能体（Claude Code、Codex、Gemini CLI、custom-cli、
OpenAI 兼容内核）原则上能读到注入的凭据值。平台做了多层防护，但无法阻止一个有 shell + 网络的 AI
把信息间接传出去——这是信任边界，不是 bug。

**网络出口过滤**（新增）：

- 设置项 `MAGENTA_EGRESS_MODE`（`off` / `block` / `allowlist`，默认 `off`）与
  `MAGENTA_EGRESS_ALLOWED_HOSTS`（逗号分隔域名白名单）。
- `off`：不限制（默认，保持向后兼容）。
- `block`：禁止所有出站网络——子进程注入 blackhole 代理环境变量
  `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 指向不可达端口；API 内核的 httpx 客户端用
  `EgressTransport` 包装，拒绝非白名单连接。
- `allowlist`：只允许白名单里的域名（网关地址自动加入白名单）。
- 纯逻辑在 `app/egress.py`（叶子模块），CLI 注入在 `runner.py`，API 注入在 `openai_harness.py`。

**API 内核工具结果擦除**（新增）：进程内 API 内核的工具输出（`run_shell` 等）在回填给模型的
`messages` 列表前，先用 `credentials.scrub()` 擦掉凭据值。这样模型在后续推理的上下文里也看不到
明文（之前只擦了事件流，没擦回填给模型的工具结果）。

**纵深防御建议**：对高危服务（如 GitHub、数据库），可选用「凭据代理 MCP」模式——把凭据封装在一个
MCP 服务器里，AI 只能调用暴露的工具（如 `create_pr`），永远拿不到原始密钥。这是**可选项**，不是
通用方案：要为每个服务单独封装，且无法覆盖用户自定义凭据。当前平台不内置这类 MCP，但支持用户自己建。

**已有的保护链路**（简要回顾，不重复细节）：加密落库 → 只以环境变量注入 → 事件流 `scrub_event`
擦除 → 命令回显 `_redact` 脱敏 → 接口只回掩码。新增的两层（网络出口 + 工具结果擦除）补上了之前
缺失的两个环节。

### 4.3 网关与密钥（Gateway）

**网关是 API 密钥的唯一来源**（`gateways` 表）：每个网关一条 `base_url` + 一份加密密钥，
由两类组成 ——

- **官方链路**（`kind="official"`）：启动播种按适配器声明的 `default_gateway` / `default_model`
  / `protocol` / `default_auth_style` / `default_models` 生成，一个内核一条，不能被删除；
- **自定义网关**（`kind="custom"`）：用户在「设置 → 自定义网关」里创建，带 `protocol`
  （openai / anthropic / gemini）与 `auth_style`（bearer / x-api-key / query / none）。

界面约定：设置页只列**自定义网关**（官方链路不在这里维护）；智能体配置里的「官方默认」
就是 `gateway_id = null` —— 等价于「平台不接管路由」，`resolve_runtime()` 回落到适配器从本机
读到的配置（`system_defaults()`）并且不注入密钥，由内核自己按环境 / 配置文件里的凭据处理。
因此智能体侧永远只有两个选项：官方默认，或一条自定义网关。

智能体（`AgentProfile`）只引用一个 `gateway_id` 并从它的 `selected_models` 白名单里选模型；
运行时由 `runtime/gateway.py:bind_gateway()` 决定这次注入什么（`resolve_runtime()` 只做转发）：
只有**自定义**网关才注入 `base_url` + 解密后的密钥 + `auth_style`，官方默认（或没选）一律
回落到 `resolve_gateway(None)`。密钥**从不下发**：接口只回掩码（`api_key_set` /
`api_key_preview`）。

模型优先级：**这条对话单独指定的模型**（`Conversation.model`，聊天页输入框上方的芯片可改）→
智能体默认模型（`AgentProfile.model`）→ 内核默认。对话级模型只影响下一轮，不清空会话记忆；
换智能体时会自动清掉（旧模型可能属于另一个网关）。可选清单由
`GET /api/conversations/{id}/models` 给出：自定义网关只返回白名单，官方默认链路返回空清单、
由界面改为手填模型名。

- **「获取模型」= 服务端探测**：`POST /api/gateways/probe` 按协议打对应端点
  （openai `/models`、anthropic `/v1/models`、gemini `/v1beta/models`），返回清单但不落库；
  `runtime/gateway_probe.py` 负责 URL 归一化与各协议的鉴权头。
- **「必须成功才能保存」是服务端不变量**：`selected_models` 至少一项 —— 探测成功勾选，
  或走「手动旁路」录入（界面在探测失败时自动展开）。
- 被**未删除**的智能体引用的网关不能删（409）；官方链路一律 409。

## 5. 技能（Skill）

技能是各智能体内核的能力包（一个目录 + ``SKILL.md``）。平台把它们**统一编目**，
再通过**智能体分配**决定某个智能体能用哪些技能。技能在磁盘上是**按内核就地维护**的：
一份技能只对写它的那个内核成立（`allowed-tools`、`scripts/`、依赖的插件都是内核特有的），
因此平台不存储技能内容，只做编目与分配，外加一个「技能市场」把远程技能**装进对应内核**。

```
各内核技能根 ──discover_skills()──▶ 统一目录(skills 表) ──分配──▶ agent_skills ──▶ 运行时物化
  ~/.claude/skills(+ .skillhub)            （只读索引）       （智能体弹窗）      agent-homes/<对话>/…
  $CODEX_HOME/skills(+ .skillhub)
```

- **编目**：适配器用 `skill_roots()` 声明自己的技能根（路径 + 来源 + 是否可安装），框架用
  `scan_skill_roots()` 统一扫描，去重（同名技能按「平台技能 > 市场技能 > 个人 > 项目 > 插件 > 系统」
  取优先级最高的一份）后写入 `skills` 表。目录消失时只把 `available` 置 False，**不删行**，
  智能体分配不会断链。来源标签见 `runtime/skills.py:SOURCE_LABELS`。
- **分配**：`agent_skills` 是智能体能用的技能的**唯一来源**；`PUT /api/agents/{id}/skills` 整表替换。
  类型与智能体的内核不一致的技能，界面直接禁止选择，服务端再兜底返回 422。
- **物化（严格仅分配）**：适配器的 `skill_mount(ctx)` 声明「我的内核从哪里读技能」——
  例如 Claude Code 用 `CLAUDE_CONFIG_DIR` 指向一个只含「分配到的技能 + 本机 settings」的隔离
  配置目录，并关闭内核自带的 bundled 技能与内置插件；Codex 用 `CODEX_HOME`。
  框架按这份声明把技能目录复制进 `agent-homes/<对话ID>/<内核配置目录>`（每次运行重建技能目录，
  取消分配即立刻消失），并携带/打补丁必要的配置文件。

  隔离目录放在数据目录而不是临时目录：内核的会话记录也写在自己的配置目录下，
  每次运行都换目录会导致多轮对话无法续接。

- **调用统计**：适配器的 `skill_invocation(tool_name, tool_input)` 声明「本内核的技能调用长什么样」
  （Claude Code 是内置的 `Skill` 工具，参数带技能目录名；默认不识别）。「用量」页据此从已落库的
  消息 `parts` 里数出技能**调用次数**（`GET /api/usage/overview`），不额外落库、对历史消息同样生效。

- **边界**：内核自带的少量技能（如 Claude Code 的 `doctor`）需要通过 `skillOverrides` 逐个关闭；
  内核还会读取工作目录下的 `<工作目录>/.claude/skills`，这类技能不在分配范围内，运行时会给出提示。

### 5.1 技能市场（SkillHub）

平台的「技能」页可以把第三方技能社区 [skillhub.cn](https://skillhub.cn) 的技能**装进某个内核**：

```
skillhub.cn ──/api/skills 列表 / /api/v1/skills/{slug} 详情──▶ 浏览
           └─/api/v1/download?slug=…（zip）──▶ 安全解压到 <内核技能根>/<slug>
```

- **安装 = 写内核的原生技能根**（不是物化隔离目录）：Claude Code 落到 `~/.claude/skills/.skillhub/`，
  Codex 落到 `$CODEX_HOME/skills/.skillhub/`。落地点来自适配器声明的、`installable` 的那个
  `SkillRoot`，框架不硬编码路径。
- **装完仍需分配**：安装只是把技能放进了内核的编目范围（`source_kind=market`），要
  智能体真正用上，还得在「智能体 → 技能」里分配——与内核自带技能同一套「严格仅分配」规则。
- **可卸载，但有护栏**：只有当技能目录内有市场安装标记（``.skillhub.json``）**且**目录落在
  `installable` 技能根内时，卸载才允许删；内核自带技能永远不会被市场卸载接口删掉。
- **解压安全**：拒绝 zip-slip（绝对路径 / `..`）、跳过符号链接、限制文件数与解压后总字节数。
- **接口**：`GET/POST /api/skillhub/*`（列表 / 内核 / 详情 / 安装 / 卸载），见 ``api/skillhub.py``。

## 6. MCP 服务器（Model Context Protocol）

MCP（Model Context Protocol）服务器是内核使用的**外部工具服务**：一个本地进程（stdio）或一个
HTTP / SSE 端点。与技能完全对称，平台把它们**统一编目**，再通过**智能体分配**决定某个智能体
能连哪些服务器；「哪个内核把 MCP 配置放在哪、长什么形状」是**适配器的知识**，框架不硬编码。

```
各内核 MCP 配置 ──discover_mcp_servers()──▶ 统一目录(mcp_servers) ──分配──▶ agent_mcp_servers ──▶ 运行时物化
  ~/.claude.json                          （只读索引 + 平台新建通用项） （智能体弹窗）      agent-homes/<对话>/…
  $CODEX_HOME/config.toml
  ~/.gemini/settings.json
```

- **两类来源**：`type_id is None`（`source_kind=manual`）是用户在「MCP」页新建的**平台通用**服务器，
  可以分配给任意内核的智能体，可编辑 / 停用 / 删除；`type_id` 非空（`source_kind=user`）是从某个
  内核自己的配置里只读扫出来的，**只能分配给同内核**的智能体，平台不接管它的内容（改 / 删返回 403）。
- **编目**：适配器用 `discover_mcp_servers()` 只读扫描本机配置（解析 / 渲染的共享工具在
  `agents/mcp.py`），`POST /api/mcp-servers/rescan` 把结果同步进 `mcp_servers` 表。配置里不再存在的
  服务器只标记 `available=False`，**不删行**（分配关系不断链）。
- **分配**：`agent_mcp_servers` 是智能体能挂的 MCP 的**唯一来源**；
  `PUT /api/agents/{id}/mcp-servers` 整表替换。不在同一内核的扫描项、以及同一次分配里两个重名标识
  （写进内核配置会互相覆盖）都会被拒（422）。换内核时，来自原内核配置的分配自动丢弃，平台通用的保留。
- **物化（严格仅分配）**：适配器的 `mcp_mount(ctx)` 声明「把分配到的服务器写到哪里」——写进隔离
  配置目录的文件（`McpMount.files`）或工作目录里的项目级配置（`McpMount.workspace_files`）：

  | 内核 | 落点 | 隔离手段 |
  | --- | --- | --- |
  | Claude Code | 隔离目录的 `mcp.json` | `--mcp-config <file> --strict-mcp-config`（本机 / 项目的其它 MCP 一律不认） |
  | Codex | 隔离 `CODEX_HOME/config.toml` 的 `[mcp_servers.*]` | 先剥掉技能物化带过来的本机配置，再追加分配到的 |
  | Gemini CLI | 工作目录 `.gemini/settings.json` | 项目级 `mcpServers` + `--allowed-mcp-server-names` 收窄启用范围（它会合并用户级配置） |

  技能与 MCP **共用同一个「家」**（`AgentContext.home_dir`）：同一个隔离目录里既有分配到的技能，
  也有分配到的 MCP 声明，每次运行重建，取消分配即立刻消失。（只把配置写进工作目录的内核 ——
  Gemini CLI —— 没有这个家，框架不会凭空给它建一个空目录。）
- **密钥只经 `app/security.py`**：`env` / `headers` 的值加密落库（键明文、值密文），接口只回键名与
  掩码，明文只在「解密后注入适配器」的一瞬间存在。编辑语义：**列出的键留空＝保持原值、删掉整行＝
  删除、新键必须给值**（`runtime/mcp.py:_merge_pairs`）。
- **被引用时不可删**：`DELETE /api/mcp-servers/{id}` 在仍有未删除的智能体挂着它时返回 409；
  平台新建的服务器走软删除（`deleted_at`），历史对话与协作历史照旧可读。
- **接口**：`GET/POST /api/mcp-servers`、`POST /api/mcp-servers/rescan`、
  `GET/PATCH/DELETE /api/mcp-servers/{id}`、`PUT /api/agents/{id}/mcp-servers`，见 `api/mcp.py`。

## 7. 数据模型

```
gateways(id, name, kind(official|custom), type_id, protocol, auth_style, base_url,
         api_key_encrypted, selected_models JSON, model_catalog JSON, extra_headers JSON,
         enabled, last_probed_at, deleted_at)                ← API 密钥的唯一来源
agent_profiles(id, name, avatar, tagline, description, system_prompt, type_id,
               gateway_id→gateways, model, options JSON, enabled, deleted_at)
credentials(id, name, description, value_encrypted, created_at, updated_at, deleted_at)
agent_credentials(agent_profile_id→agent_profiles, credential_id→credentials，复合主键 + position)
conversations(id, title, agent_profile_id→agent_profiles, session_id, workspace_dir, model, archived,
              last_read_at)   # last_read_at 为空 = 升级前的旧对话，一律当已读
messages(id, conversation_id→conversations, role, content, parts JSON, usage JSON, status, error)
skills(id="<type_id>:<slug>", type_id, slug, name, description, source_kind, path, fingerprint,
       version, author, when_to_use, allowed_tools JSON, files JSON, total_bytes, meta JSON,
       available, note, source_url)   # source_kind 含 market（市场技能，可卸载）
agent_skills(agent_profile_id→agent_profiles, skill_id→skills，复合主键 + position)
mcp_servers(id, name, slug, description, type_id(空=平台通用), source_kind(manual|user),
            transport(stdio|http|sse), command, args JSON, url, env_encrypted JSON,
            headers_encrypted JSON, enabled, available, note, discovered_at, updated_at, deleted_at)
agent_mcp_servers(agent_profile_id→agent_profiles, mcp_server_id→mcp_servers，复合主键 + position)

-- 协作域（独立于对话域，见 docs/collaboration-design.md）
collab_teams(id, name, description, entry_key, workspace_dir, graph JSON, budget JSON)
collab_runs(id, team_id→collab_teams, title, goal, status, entry_key, workspace_dir, budget, usage,
            steps_used, tickets_used, error)
collab_tickets(id, run_id→collab_runs, parent_ticket_id→collab_tickets,
               from_agent_profile_id→agent_profiles, to_agent_profile_id→agent_profiles,
               owner_key, holder_key, submitter_key, title, brief JSON, state,
               round, max_rounds, verdict, review_comment, result JSON, closed_at, report_at)
collab_steps(id, run_id→collab_runs, ticket_id→collab_tickets, agent_profile_id→agent_profiles,
             role_key, duty, sequence, action, status, content, gist, parts JSON, usage JSON,
             error, audit JSON)
collab_messages(id, run_id→collab_runs, sender, role_key, agent_profile_id→agent_profiles,
                sequence, status, content, parts JSON, usage JSON, error, meta JSON)
```

设计取舍：

- **对话快照 `agent_profile_id`**：之后即便智能体改了内核 / 网关 / 模型，历史对话仍可复现。
- **默认智能体全局只有一颗星**：`agent_profiles.is_default`（至多一个，点亮时会摘掉别人的星，
  删掉带星的那颗会自动补一颗）；新建对话不传 `agent_profile_id` 时回落到它 —— 还没有星时
  先用「唯一启用的那个」，一个可用的都没有才报 422。老库启动时由 `ensure_default_agent` 补星。
- **`parts` 用 JSON**：工具调用/结果/推理的形态随智能体变化，无需为每种智能体建表；
  它是按真实发生顺序排列的时间线（`thinking` / `text` / `tool_call` / `tool_result`），
  前端据此按**真实顺序**渲染，只把**连续**出现的「思考 / 工具调用 / 工具结果」收进一个默认折叠的
  「过程」块（点开可按原顺序逐条查看），正文仍内联显示 —— 折叠不改变先后顺序，也不按类型归组。
  旧消息没有内联 `text` 段，前端会用 `content` 合成一段兜底，保持向后兼容。
- **密钥只存在网关里**：智能体不直接持有密钥，只引用 `gateway_id`；要换密钥改网关一处即可，
  多个智能体共享同一条链路时不会各存一份。
- **凭据是全局一份 + 引用**：`credentials` 存值、`agent_credentials` 存「哪个智能体能拿到」，
  多个智能体能共用同一条（改一处即全体生效）；加密落库、只以环境变量注入、值不进提示词 /
  日志 / 事件 / 响应。工作目录不在智能体上（建对话、建协作任务时才指定，
  `workspace_dir` 只属于 `Conversation` / `collab_*`）。
- **技能只增不删**：技能是从文件系统扫描出来的，目录消失只标记 `available=false`，
  因此智能体分配（`agent_skills`）永远指向真实存在的记录；智能体被删除时分配关系保留（见下条）。
- **MCP 也是只增不删 + 严格仅分配**：扫描来的服务器在配置里消失只标记 `available=false`，
  `agent_mcp_servers` 是智能体能挂什么 MCP 的唯一来源；平台新建的服务器可软删除，
  但仍被未删除的智能体引用时拒绝删除（409）。
- **删除智能体 / 网关只做软删除**：外键全是 `RESTRICT`，而历史对话与历史工单都引用着智能体，
  硬删会连历史一起打断。所以 `DELETE /api/agents/{id}` 只写 `deleted_at`：它从所有可选列表里
  消失（新建对话、新建协作节点都拒绝它），但历史对话仍能显示当时的名字 / 头像、也仍能继续对话。
- **协作域的分工键**：工单用 `owner_key`（谁拆的谁汇总）、`holder_key`（现在谁握着）、
  `submitter_key`（最近提交者，打回时默认退回给他）记岗位流转；步骤用 `gist` 存**引擎派生**的
  一句话摘要 —— 拼下游 prompt 时只读 `gist`、不读 `content`，从数据通路上保证"下游只看工单、
  看不到上游对话"。岗位用 key 而不是智能体 id，是因为智能体可以改绑，历史工单仍要能复现。
- **加字段要能容忍旧行**：`create_all` 只建新表、**不会给已存在的表加列**，所以"后加的可选列"
  登记在 `models.py` 的 `ADDITIVE_COLUMNS`（对话域）与 `collab/tables.py` 的
  `ADDITIVE_COLUMNS`（协作域），由 `db.init_db` 用 `ALTER TABLE ... ADD COLUMN`（带默认值）
  幂等补齐：现有 `magenta.db` 启动后自动对齐，不重建、不丢数据。
- **外键用 `ON DELETE RESTRICT`**：仍被**未删除**的智能体引用的网关不能删（409，避免历史断裂）；
  删除对话时显式清理消息（SQLite 已开启 `PRAGMA foreign_keys`）。

## 8. 扩展点

| 想做的事 | 需要改的地方 |
| --- | --- |
| 接入新 CLI 智能体 | 在 `agents/builtin/` 加一个模块 |
| 接入新模型供应商（无官方 CLI） | 复用 `openai_harness.py` 的基类，加一个子类声明默认网关/模型 |
| 零代码接入私有 CLI | 用 `custom-cli` 适配器填命令 + JSON 取值路径 |
| 让运行时可由平台安装 / 更新 | 在该适配器声明 `provisioner`（`RuntimeProvisioner`），界面自动出现按钮 |
| 新增可配置项 | 在该适配器加一个 `ConfigField`，前端表单自动出现 |
| 新增移动端页面 | 在 `frontend/src/mobile/` 加页面并在 `MobileApp.tsx` 注册路由（桌面端不受影响） |
| 让某个适配器支持技能 | 声明 `capabilities.skills=True`，实现 `skill_roots()`、`discover_skills()` 与 `skill_mount()` |
| 从技能市场安装 / 卸载技能 | 框架已内置（`app/skillhub.py` + `api/skillhub.py`）；只要适配器声明了 `installable` 的技能根 |
| 让某个适配器支持 MCP | 声明 `capabilities.mcp=True`，实现 `discover_mcp_servers()` 与 `mcp_mount()` |
| 接入一个新的 MCP 服务器 | 平台新建（「MCP」页）或写进内核自己的配置后「重新扫描」；分配在「智能体 → MCP」里做 |
| 新增协作能力（多智能体工单流转） | 全部在 `app/collab/`（独立域，不碰框架层），见 `docs/collaboration-design.md` |
| 换掉事件传输（SSE→WebSocket） | 只改 `transport/sse.py` 与前端 `streamApi` |
| 换掉存储（SQLite→Postgres） | 只改 `settings.database_url` 与 `db.py` |

## 9. 已知边界

- CLI 适配器的多轮依赖各工具自身的会话续接；`custom-cli` 若声明不出 `session_path`，
  平台会退化为 transcript 回放。
- `codex exec` 与 `codex exec resume` 认的旗标不一样：`-s/--sandbox`、`-C/--cd` 只挂在 `exec` 上，新版 `exec` 也去掉了 `--full-auto`。适配器按子命令分别表达 —— 沙箱在 `exec` 上用 `-s`、恢复会话时走 `-c sandbox_mode=…`；全自动走 `-c approval_policy=never`。`resume` 这一子命令形式在不同 Codex 版本间也有差异，所以做成了可选策略（app-server 模式下对应 `thread/resume`）。
- 新版 Codex 不再读 `OPENAI_BASE_URL`（内置 provider 拿着它也会直奔 api.openai.com），所以自定义网关
  一律经一个具名 `model_provider` 注入（名字取「自定义供应商名称」，留空用内置名 `magenta`）；
  没走网关时（官方默认 / 本机登录态）不注入任何 provider。`wire_api` 也不再由平台下发 ——
  0.4x 起只剩 `responses`、更早的版本默认 `chat`，写死任何一边都会在另一边直接报错。
- Codex 默认走「接口模式 = app-server」：`codex app-server --listen stdio://` 是 Codex 自己的
  JSON-RPC 通道（TUI 也用这条），回复按 `item/agentMessage/delta` **逐字**推送，思考走
  `item/reasoning/summaryTextDelta`，用量走 `thread/tokenUsage/updated`。个别不支持 app-server
  的旧版本可在智能体选项里切回「接口模式 = exec」（`codex exec --json` 只在整条消息完成时给一个
  `item.completed`，回复是整段出现的）。
- app-server 是**常驻**内核：适配器只声明第一帧 `initialize`（`Invocation.stdin_hold=True`），
  拿到应答后再用 `AgentRun.send()` 排队 `thread/start`（或 `thread/resume`）与 `turn/start`，
  由执行层统一代写 stdin，并在适配器 `mark_done()` 后关掉 stdin 收工（关不掉则 5 秒后强杀）。
  适配器仍然只生产数据、不碰子进程，取消 / 超时 / 凭据擦除依旧只有一份实现。
- app-server 没有人工确认入口，所以服务端反向请求（命令 / 补丁审批、`requestUserInput`、
  MCP elicitation）一律自动拒绝，并在对话里显式提示——否则该轮会一直等一个不会到来的答复。
- Codex 默认把 OTEL 指标导出到它的分析后端，连不上时会在**退出前**干等满 10 秒
  （表现为「回复完还要等一会儿才结束」）。适配器用 `-c otel.metrics_exporter="none"` 关掉它；
  要自己接 OTEL 的可以在「额外命令行参数」里再覆盖回来。
- Gemini CLI 的人格注入依赖 `GEMINI_SYSTEM_MD`，若目标版本不支持可切换为 prepend 模式。
- 工具循环（API harness）的权限模型是「工作目录沙箱」，没有做命令级审批。
- 技能「严格仅分配」只保证**平台注入的技能 == 智能体分配**：内核自带的个别技能（Claude Code 的
  `doctor`）要靠 `skillOverrides` 关闭；工作目录自带的 `.claude/skills` 会被内核直接读取，
  平台只提示、不删除用户文件。
- 技能目前只支持「查看 + 分配」；编辑、安装与上传留待后续（技能目录仍由各内核自己管理）。
- MCP 的「严格仅分配」同样只保证**平台注入的服务器 == 智能体分配**，各内核的隔离强度不一样：
  Claude Code 用 `--strict-mcp-config` 能彻底关掉其它 MCP（工作目录自带的 `.mcp.json` 一律不生效，
  运行时会提示）；Codex 靠改写隔离 `config.toml` 做到；Gemini CLI 没有「换配置目录」的开关，
  只能把项目级设置与启用白名单收窄 —— 当某次运行**没有分配任何服务器**时，它在用户级
  `~/.gemini/settings.json` 里自己配的 MCP 仍会被内核加载（内核的合并语义决定，平台无法从外部关掉）。
- 协作域（`collab/`）与技能一样**暂不注入 MCP**：挂着 MCP 的智能体参加协作运行时，
  引擎只在工单里给一行提示（要真正用上 MCP，得先按对话运行）。
- 运行时的安装 / 更新按适配器声明的命令执行（内置 CLI 内核为官方 npm 包）。若某台机器上的
  内核是用别的方式装的（例如发行版包、容器、垫片脚本），平台更新会按声明的包管理器**另装一份**；
  页面会显示当前可执行文件路径供核对，但不会自动识别、也不会去卸载非本平台安装的副本。

## 10. 移动端与 PWA

移动端不是桌面端的「窄屏适配」，而是**另一条物理独立的链接**：`/m/`。

```
frontend/
├── index.html              # 桌面端入口 → /
├── m/index.html            # 移动端入口 → /m/
├── public/m/               # 移动端 PWA 资产（manifest + Service Worker）
├── public/icons/           # 两端口共用的应用图标（由 scripts/gen_icons.py 生成）
└── src/
    ├── App.tsx · pages/ · index.css                  # 桌面端
    ├── components/                                   # 两端共用的纯展示组件（Composer · Icons · MessageView…）
    └── mobile/                                       # 移动端（独立页面与样式表）
        ├── main.tsx · MobileApp.tsx · mobile.css
        ├── components/（顶栏 · 左侧抽屉 · 底部弹层）
        └── pages/（对话 · 智能体 · 技能 · MCP · 协作 · 设置）
```

- **只共享基础设施，不共享界面**：两端共用 `lib/`（`api` / `types` / `agentStream` / `usage` / `collabDraft`）
  与纯展示组件（`DynamicForm` / `MessageView` / `MarkdownView` / `ConversationThread` /
  `CollabAgentThread` / `Composer` / `Icons`）；页面、路由、样式表各自独立
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
- **移动端没有底部 Tab，只有左侧抽屉**：打开就是聊天界面（`/chat` 自动进入最近一次对话，
  一条都没有时给空状态引导），点顶栏左上角的 logo 弹出左侧抽屉
  （`components/MobileDrawer.tsx`）—— **上半部分是主导航**（对话 / 智能体 / 协作组 / 技能 / MCP /
  设置，与曾经的底部 Tab 一一对应），**下半部分是对话列表**（`＋ 新建` 直接选智能体开聊）。
  桌面端左侧栏（`App.tsx` 的 `NAV` + `components/ConversationList.tsx`）现在是**同一份菜单**
  （顺序、命名、行样式都对齐）；「运行时」与「自定义网关」两边都不占菜单，收在「设置」页的配置入口。
  抽屉由 `MobileApp` 统一持有，任何顶级页面的顶栏默认就是那个 logo 按钮（`MobileTopBar`
  在没有 `onBack` 时自动渲染），子页面仍是返回箭头。
  两端的对话列表项都显示时间：今天只显示 `14:23`，其余显示日期 + 时间（口径统一在 `lib/datetime.ts`）。
  抽屉里每个对话行右侧的「⋯」是会话级操作（重新生成 / 重置记忆 / 复制工作目录 / 删除）的入口：
  它只把路由 state（`openActions`）交给聊天页，由聊天页弹面板 —— 那几件事要用到消息与会话状态。
- **朗读（语音合成）**：开关在对话页顶栏右上角（原有「⋯ 更多操作」的位置），**默认关闭**，
  开启后新回复会被念出来（`lib/tts.ts`：Markdown 先转纯文本，长文按句读切段排队，新输出打断旧的
  一条；开关存 `localStorage['magenta.tts']`，只影响这台设备）。判定「新回复」放在聊天页观察
  `messages` 的效果里，所以另一台设备写入、由 `useAutoRefresh` 拉回来的回复同样会念；iOS 两个坑
  一起处理：首次发声在点开关的手势里 `unlockSpeech()` 解锁，打断旧的一条时 `cancel()` 与 `speak()`
  分开两轮执行。
- **分享成长图**：对话页顶栏的分享按钮把当前对话（标题 + 气泡 + 代码块 + 页脚）用 Canvas 画成
  一张长图（`lib/shareImage.ts`，不引第三方依赖），优先交系统分享面板（`navigator.share({files})`），
  不支持就退回下载；过长时整体降采样，避免撞上 iOS 的画布面积上限。
- **配置在「智能体」页，安装在「运行时」页**：网关升格为密钥的唯一来源后，「实例配置」这个概念
  取消了 —— 智能体（内核 + 网关 + 模型 + 人格 + 技能）在两端各自的「智能体」页维护，
  适配器选项仍由 `DynamicForm` 渲染；运行时页只管装 / 升 / 卸，卡片上的按钮跳到「智能体」页。
- **移动端设置页**只留「外观 / 配置 / 更多」三节：运行概览、版本记录这类信息看桌面端即可；
  「更新到最新版本」（自助清缓存重载）保留在「更多」里 —— 独立应用没有刷新按钮。
- **移动端输入框**：附件下沉成共享模块 `app/attachments.py`（校验 / 落盘 / 拼提示词只有一份）；
  先 `POST /api/conversations/{id}/attachments` 落到对话工作目录的 `attachments/`（内容走 base64，
  省掉 multipart 依赖），发消息时随 `attachments` 一起提交，由 `service.compose_user_content()` 把
  路径写进用户提示词 —— 与内核无关，任何能读工作目录的智能体都自动支持；单个附件上限 8 MB。
  协作聊天走同一套：`POST /api/collab/runs/{id}/attachments` + `engine.prepare_chat()` 的
  `compose_with_attachments()`（只有附件、没有正文也允许）。
  语音走浏览器的 Web Speech API（`lib/speech.ts`，两端共用；不支持时按钮仍显示，点一下给出中文说明），
  只把识别文字填进草稿。
  输入区就是屏幕最下面那一块，只留 6px 收边：token 用量在输入框**上方**，底部安全区**不照搬** ——
  iOS 独立应用里安全区 34px，Home 条本身只占 ~13px，照搬会在输入框下面空出一截；输入区底色一直
  铺到屏幕底边。附件 / 语音 / 发送（运行中是停止）都是**框内**的图标按钮（线条 SVG，见
  `components/Icons.tsx`；emoji 在各系统上大小不一），输入框不再有背景提示文字。
- **输入区只有一份**：`components/Composer.tsx`（受控：草稿 / 附件 / 上传 / 发送都由
  页面给），两端的对话页与协作运行页（含聊天弹层）共用，交互与样式必须完全一致；
  样式段在 `index.css` 与 `mobile.css` 各写一份（桌面端 / 移动端各一段微调），共享那段逐字一致。
- **协作运行页 = 一间办公室**：`/m/collab/runs/:id` 主区整屏铺那间可双指缩放 / 单指拖动的等距办公室
  （`MobileCollabProgress`），时间线与「需要你决定」卡片收进整屏对话弹层 —— **点入口智能体工牌**才
  出来（顶栏不放对话按钮），点其他岗位开只读岗位会话。输入框上方是一个模式按钮：点一下切
  「推进任务」、再点回「普通对话」。对话走 `/chat`（可带附件），推进走 `/messages`，推进模式下
  空内容直接发就是 `/resume`；工单正在推进、带着附件、或有待回答的问题时，界面接管模式并置灰按钮。
  办公室底图有深 / 浅两张（`iso-office.svg` 与
  `iso-office-light.svg`，同一份几何，`scripts/gen_office_bg.py` 构建期按「反相 + 色相旋转 180°」
  换算），`CollabOfficeMap` 按当前主题选；工牌 / 图例 / 工具按钮 / 信息卡颜色走 `--office-*`
  变量，浅色下整间办公室一起变浅。
- **外观（深色 / 浅色 / 跟随系统）**：两端共用 `lib/theme.ts` —— 模式存在 localStorage 的
  `magenta.theme`，解析结果写在 `<html data-theme>`，`applyTheme()` 同时更新
  `meta[name=theme-color]`（状态栏 / Android 系统导航条这些「原生外壳」跟着换色）与 iOS 独立应用的
  状态栏样式。两个入口 HTML 各有一段等价的内联脚本，在首屏渲染前就把主题定下来（否则浅色用户每次
  打开都会先闪一下深色外壳）。两份样式表各自的 `:root` 仍是深色默认，浅色只是
  `[data-theme='light']` 覆盖块（变量 + 少量写死深色的装饰），`html` 也铺主题底色 —— 系统预留或
  回弹露出的那一条才不会跟页面两个色。「系统架构」是 iframe + srcDoc 的自包含文档，主题由
  `lib/theme.ts:withDocumentTheme()` 写进它的 `<html>`（`api/architecture_doc.py` 认同一个属性）。
- **移动端的取舍**：协作域不提供可视化画布（连线需要精确拖拽，手机上体验很差），
  改在「协作组」详情页用**表单**编排：岗位是卡片、连线是「从谁到谁」的下拉选择；
  两端的字段语义、默认值与中文标签共用 `lib/collabDraft.ts` 一份，校验一律走服务端
  `POST /api/collab/teams/{id}/validate`。
- **外壳钉在可视视口上**：`.m-app` 用 `position: fixed; top/right/bottom/left: 0`，而不是
  `100vh`/`100dvh` 配 `height: 100%` 父链 —— 真机上地址栏与底部工具栏会伸缩，两者高度经常对不齐，
  底栏就会浮起来留一条缝或被工具栏压住。固定定位跟的是可视视口，刘海与 Home 条交给
  `env(safe-area-inset-*)`（守卫测试 `test_mobile_shell_is_pinned_to_the_visible_viewport`）。
- **移动端锁定缩放**：移动端采用「固定外壳」布局（顶栏 + 内部滚动区 + 左侧抽屉），随手指缩放会
  让外壳错位，因此入口视口写 `maximum-scale=1.0, user-scalable=no`，并用 `mobile.css` 的
  `touch-action: pan-x pan-y` 兜底（iOS Safari 自 iOS 10 起忽略 `user-scalable=no`，只有
  `touch-action` 真正生效）。因此移动端输入框字号统一 ≥ 16px —— iOS 聚焦小于 16px 的输入框会
  强制放大页面。桌面端不禁用缩放（浏览器缩放是它的无障碍能力）。
