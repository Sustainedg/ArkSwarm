# 协作（多角色工单协作）设计方案

> 状态：**方案已评审通过，正在按 §11 里程碑落地**（进度见 §13）。本文档描述的是完整设计，
> M0（契约冻结）、M1（引擎 MVP：后端跑通含返工的闭环）、M2（画布编排 + 协作页）已落地，
> 未实现的部分在表格里标了里程碑。
> 相关约束：`AGENTS.md`（铁律与分层）、`docs/architecture.md`（现有运行时序）。

---

## 0. 一句话结论

新增一个**物理独立**的协作域 `backend/app/collab/`：用户在画布上把「角色」拖成节点、连成有向图，
一次运行由引擎**串行推进**，角色之间**只传工单**（结构化交付物）而不传对话历史；
每个节点的运行仍然走现有的 `runtime.runner.stream_run()`，因此取消、超时、脱敏、
SSE、前端 Markdown 渲染全部直接复用，**不新增事件类型、不新增依赖、不改适配器**。

评审决策已全部确认（§12），M0「契约冻结」已落地（§13）。

---

## 1. 需求拆解

### 1.1 用户故事（来自需求原文）

```
用户 ──对话──▶ CEO（只沟通、只派活，不能写代码）
                 │  创建工单
                 ▼
                CTO（分析需求 + 编码规范，拆成子工单）
                 │  派发
                 ▼
                SE（实施，写代码）
                 │  提交
                 ▼
              审查员（审查）── 有问题 ──签署审查意见──▶ CTO ──▶ SE 返工 ──▶ 再审
                 │
                 └── 标注「完成」──▶ 工单关闭
```

### 1.2 硬性约束（逐条对应设计）

| # | 需求 | 设计落点 |
| --- | --- | --- |
| C1 | 左侧菜单新增「协作」，与「对话」同级 | §7.1 导航 + `/collab` 路由 |
| C2 | 可视化自定义角色设定 | §7.3 画布节点属性面板（复用 `DynamicForm`、角色下拉） |
| C3 | 可视化连线串接角色 | §7.3 拖拽连线 + 有向边 + 边类型 |
| C4 | 角色之间用「工单」分配任务 | §4 工单协议（结构化交接载荷） |
| C5 | **不共享记忆**，只把任务内容传给下一个角色 | §4.2 交付载荷；强制 `ctx.history=[]`、`ctx.session_id=None` |
| C6 | 角色能力范围受限（如 CEO 不能写代码） | §4.3 四层约束 + 越权审计 |
| C7 | 审查不过 → 带意见退回 → 返工 → 再审，直到完成 | §3.3 工单状态机（`rework` 回边） |
| C8 | 功能独立，不污染现有代码 | §2 物理分层 + §2.4 宿主改造点清单（后端共 4 处、每条 1~2 行） |

### 1.3 设计目标 / 非目标

**目标**：可理解（每一步都能在 UI 上讲清"谁把什么交给了谁"）、可终止（预算与防死循环）、
可复现（工单与产出全落库）、可审计（越权可发现）。

**非目标（一期不做）**：并行执行、共享黑板/记忆、跨机分布式、节点内子图嵌套、
非结构化自由流转（必须走工单协议）。

---

## 2. 物理分层：协作域自成一体

### 2.1 模块树

```
backend/app/collab/            ← 新目录，协作域的全部逻辑都在这里
  __init__.py        只导出 router 与 self-check 入口
  tables.py          4 张表（SQLModel），FK 用字符串表名，不 import app.models
  schemas.py         请求/响应 + 编排图（nodes/edges）的 Pydantic 校验
  protocol.py        协作信封事件（collab.*）——独立于 agents/events.py
  tickets.py         ★ 工单协议：解析角色输出 / 渲染交付载荷（纯函数）
  graph.py           ★ 编排图校验与查询（纯函数）
  prompt.py          ★ 节点上下文 → prompt 组装（纯函数）
  engine.py          ★ 编排状态机（无 HTTP、无 SSE、无 SQL 细节之外的东西）
  store.py           持久化（只依赖 tables.py + sqlmodel）
  execution.py       ★★ 唯一与 runtime/agents 交互的边界
  api.py             APIRouter(prefix="/api/collab")，薄

frontend/src/
  pages/CollabPage.tsx             协作页骨架：页内两个页签 + 数据加载（M2 ✅）
  components/CollabRunsPanel.tsx   协作运行页签：运行列表 + 时间线 + 工单条 + 输入框（M2 ✅）
  components/CollabSettingsPanel.tsx 协作设置页签：协作组列表 + 画布 + 属性面板（M2 ✅）
  components/CollabCanvas.tsx      画布：节点拖动 + 四向端口 + 正交避障连线 + 拖放添加节点（M2 ✅）
  lib/collabRouting.ts            连线端口推断与正交避障路由（纯函数）
  components/NodeInspector.tsx     节点 / 边 / 团队属性面板（复用 DynamicForm）（M2 ✅）
  lib/agentStream.ts               从 ChatPage 抽出的智能体事件纯归约器（两页共用，M2 ✅）
  lib/collabStream.ts              协作信封事件归约（时间线 + 工单，M2 ✅）
  components/TicketBoard.tsx       工单看板 / 工单抽屉（M4）
  （样式按 AGENTS.md §4.7 写进 index.css，不新增 collab.css）
```

### 2.2 协作域内部的依赖方向（单向、无环）

```
api.py ──▶ engine.py ──▶ store.py ──▶ tables.py
                │
                ├──▶ execution.py ──▶ app.runtime.* / app.agents.*   ← 唯一的对外依赖
                ├──▶ tickets.py / graph.py / prompt.py               ← 纯函数叶子，零依赖
                └──▶ protocol.py
```

`tickets.py` / `graph.py` / `prompt.py` 是**纯函数叶子**（只依赖标准库与 dataclass）：
工单解析、图校验、载荷渲染这三块最容易出错的核心逻辑，可以**完全不启动数据库/HTTP** 单测。

### 2.3 复用 vs 隔离

| 复用（直接调用） | 方式与收益 |
| --- | --- |
| 智能体执行 | `execution.py` 调用 `runtime.runner.stream_run(definition, ctx, handle)`；取消、超时、命令回显脱敏、stdin/文件物化全部白得 |
| 运行句柄 | 复用 `runtime.manager.ActiveRun` 这个数据类（`cancel_event` / `process`），协作层自己持句柄表 |
| 角色与智能体 | 直接外键引用 `roles` / `agent_instances`，不复制、不派生 |
| 技能 | 角色的技能分配（`role_skills`）→ `runtime.skills.prepare_skill_home()`（§2.4 第 4 条） |
| 前端渲染 | `ConversationThread`（内部 `MessageView`）/ `MarkdownView` / `DynamicForm` / `Modal` |
| 前端流式 | `lib/api.ts:streamPost()` + 从 `ChatPage` 提取出的 `applyEvent` 归约器（§7.4） |
| SSE 传输 | `transport/sse.py:sse_stream()`（M0 已下沉为通用设施，`api/` 与 `collab/` 共用） |

| 隔离（不碰） | 原因 |
| --- | --- |
| `agents/events.py` 的事件枚举 | **不新增事件类型**：适配器协议原样保留，`test_event_protocol_matches_the_documented_contract` 依旧全绿 |
| `agents/base.py` 契约 | 不新增钩子，协作节点就是一个普通的 `AgentContext` |
| `runtime/service.py` | 它是 Conversation 中心的编排，与工单编排无关；强行复用会把它污染成两用模块 |
| `Conversation` / `Message` 表 | 协作的"发言"语义是交付物而非聊天；用协作域自己的表 |
| `models.py` / `schemas.py` / `security.py` | 零改动 |
| 全部现有测试 | 零改动（协作只新增测试文件） |

> **跨层问题已按评审意见解决**：`api/sse.py` 已平移到 `app/transport/sse.py`，成为与业务无关的
> 通用传输设施（只依赖 `asyncio/json`）。因此协作层不再需要任何"允许 import 上层"的例外，
> 架构守卫里也就没有白名单，只有硬规则（§2.5）。

### 2.4 对宿主的全部改造点（这是"防污染"的核心承诺）

| # | 文件 | 改动 | 状态 |
| --- | --- | --- | --- |
| 1 | `backend/app/api/__init__.py` | +2 行：挂载 `/api/collab` 路由 | ✅ M0 |
| 2 | `backend/app/db.py::init_db` | import `app.collab.tables` 注册新表；再调 `_add_missing_columns()` 幂等补齐后加的可选列（SQLite 的 `create_all` 不会 ALTER 旧表，见 §8） | ✅ M0/M1 |
| 3 | `backend/app/main.py::lifespan` | +3 行：启动时把残留的 `running` 运行标记为 `interrupted` | ✅ M0 |
| 4 | `backend/app/transport/sse.py` | `api/sse.py` 平移到通用传输设施；`api/conversations.py` 改 1 行 import | ✅ M0 |
| 5 | `backend/app/runtime/skills.py` | `prepare_skill_home(..., scope_id=None)`：隔离目录改用调用方给的作用域 | M5 |
| 6 | `backend/app/collab/*` | 协作域全部实现（见 §2.1 模块树） | M0 起 |
| 7 | `frontend/src/App.tsx` | `NAV` 加 1 项 + `Routes` 加 4 条 | ✅ M2 |
| 8 | `frontend/src/lib/api.ts`、`lib/types.ts` | 追加协作类型与接口（不改现有导出；`streamPost` 加一个泛型参数） | ✅ M2 |
| 9 | `frontend/src/index.css` | 追加协作样式（按 AGENTS.md §4.7，不新增 `collab.css`） | ✅ M2 |
| 10 | `frontend/src/pages/ChatPage.tsx` | 把 `applyEvent` 提到 `lib/agentStream.ts` 复用（协作页也要用它，所以提前到 M2 做） | ✅ M2 |
| 11 | `frontend/src/components/MessageView.tsx` | 加可选 `label` 属性（时间线上显示岗位名，默认仍是「智能体」） | ✅ M2 |
| 11 | `docs/architecture.md` | §7 扩展点表加一行指向本文档 | ✅ M0 |
| 12 | `backend/app/collab/schemas.py` `store.py` `execution.py` `prompt.py` | 新建协作任务（run）时可指定工作路径：留空 → 团队默认 → 自动分配；所有岗位统一 cwd，`workspace_scope` 只约束读写权限与越权审计 | ✅ |
| 13 | `frontend/src/components/ConversationThread.tsx` `CollabRoleThread.tsx` `RoleConversationPanel.tsx`、`src/mobile/components/MobileRoleConversationSheet.tsx` | 对话只有一份展示模块：单聊 / 协作主对话 / 协作看板岗位实时对话（含移动端岗位弹层）共用同一线程与气泡样式 | ✅ |

改动总量：后端 4 处（M0 约 7 行；M1 又在 `db.py` 追加约 20 行的"补列"工具）、前端约 6 行，
全部是"挂载"性质，不含任何业务分支注入。
（注意：**没有**在 `runtime/`、`api/`、前端里写 `if type_id == ...`，符合铁律 1。）

### 2.5 架构守卫（`backend/tests/test_collab_architecture.py`，已落地）

1. `collab/` 的任何模块都不得 import `app.api.*`、`app.runtime.*` 与上层的 `agents.*`
   （`agents.events` 这个事件数据契约除外）。
2. `collab/execution.py` 是**唯一**允许访问执行层的文件（M1 落地；守卫已就位）。
3. `api/`（除 `__init__.py`）、`runtime/`、`agents/` 不得反向依赖 `app.collab`。
4. `tickets.py` / `graph.py` / `prompt.py` 必须是纯函数模块：不得依赖 `sqlalchemy` / `sqlmodel`
   / `fastapi` / `httpx` / `starlette`（保证核心逻辑能脱离数据库与 HTTP 单测）。
5. 4 张 `collab_*` 表必须出现在 `SQLModel.metadata` 里（防止有人忘了改 `init_db`）。
6. `api/sse.py` 不得再存在，且 `api/conversations.py` 必须引用 `transport.sse`。
7. 协作信封事件必须全部以 `collab.` 为前缀，且与 `EventType` 的 10 个成员无交集
   （确保协作没有偷偷扩协议）。

## 3. 概念模型

### 3.1 协作组（Team）= 一张图

```
Team {
  id, name, description,
  entry_key,              # 唯一入口节点（唯一能和用户对话的角色）
  workspace_dir,          # 团队默认工作路径；新建协作任务时可覆盖，留空则用团队默认
  graph: { nodes[], edges[] },        # 可视化编排结果，整图 JSON 存储
  budget: { max_steps, max_tickets, max_rounds_per_ticket, max_tokens, timeout_seconds }
}

Node {
  key,                    # 稳定标识（工单里引用它），如 "ceo"
  role_id,                # 绑定的角色（人格 + 智能体 + 技能 + 参数都来自这里）
  label, duty,            # duty ∈ coordinator | engineer | reviewer
  instructions,           # 职责说明（追加到人格之后）
  capability_scope,       # 能力边界文字（"不得写代码、不得访问工作目录"）
  workspace_scope,        # none | read | write → 权限声明与越权审计（cwd 统一为本次运行的工作路径）
  output_contract,        # 该节点必须产出的动作集合（delegate/review/…）
  options_override,       # 节点级适配器参数覆盖（复用 DynamicForm 渲染）
  x, y,                   # 画布坐标
  max_rounds              # 该节点同一工单的最大返工轮次
}

Edge {
  id, from_key, to_key,
  kind,                   # assign（派发/交付） | review（审查） | rework（打回回边）
  label
}
```

### 3.2 协作运行（Run）与工单（Ticket）

- **Run**：一次协作实例（左侧列表里的一项，类比"对话"）。有自己的目标、共享工作目录、状态、运行内预算消耗。
- **Ticket**：一次可交接的工作单元。**它可以有父工单**（CTO 把大工单拆成若干子工单给 SE）。
- **StepMessage**：工单时间线上的一条"发言/交付"（谁、在哪个工单上、做了什么动作、产出什么、花了多少 token）。
  它等同于 UI 上的一张卡片，承载 `parts`（思考/正文/工具调用），复用现有 `MessageView` 渲染。

### 3.3 工单状态机（这是 C7 的落地）

工单状态：`open`(待领取) · `working`(处理中) · `waiting_children`(等子工单) · `review`(待审查)
· `rework`(打回返工) · `done` · `rejected` · `blocked`(等用户)

| 动作 | 允许的 duty | 工单变化 | 下一个持有者 |
| --- | --- | --- | --- |
| `delegate` 派发 | coordinator / engineer | 本工单转交，或新建**子工单**并把父工单置 `waiting_children` | 目标节点（必须在 `assign` 边白名单内） |
| `submit` 提交 | engineer | 有 `review` 边 → `review`；没有 → 直接 `done` | 指向该节点的 `review` 边的另一端（审查者）；无审查者则结束 |
| `review` + `verdict=pass` | reviewer | 工单 `→ done`；父工单（若有）回到拆解者手里汇总 | 父工单的拆解者（`owner_key`）/ 运行收尾 |
| `review` + `verdict=changes_requested` | reviewer | `→ rework`，追加审查意见，`round += 1` | `rework_to` 指明的节点；缺省退回最近提交者（`submitter_key`） |
| `done` 确认 | coordinator / engineer | 工单 `→ done`（含子工单全通过） | 父工单发起者 / 运行收尾 |
| `ask_user` 提问 | 仅入口节点 | 运行 `→ waiting_user`；工单落库 `question + options`，界面渲染决策卡 | 用户 |
| `reject` 否决 | coordinator / reviewer | `→ rejected`，向上抛错误 | 父工单发起者（分支终止） |

**运行状态**：`running` · `waiting_user` · `done` · `error` · `cancelled` · `interrupted`。

**打回目标**：`rework` 的终点允许是**协调者**（把审查意见交回 CTO 重新拆解），不限于原实施者。
图校验只强制"有 `review` 边就必须有 `rework` 回边"；具体退回给谁由审查者在工单里用 `rework_to`
指明，不指明时默认退回最近的提交者（`submitter_key`）。如果最近的提交者不在退路里、但审查者
只有一条 `rework` 出边，就按这条唯一退路自动打回（例如审查意见交回 CTO，不需要额外填
`rework_to`）。审查者有多条退路又没指明 → 挂起等人指定，**引擎不替你猜**。

**用户决策卡**：`ask_user` 不再只给一段问题文本，而是应当带 2~12 条 `options`（推荐 2~6 条）：
`{"label": "选项标题", "value": "点选后回传给角色的完整答复", "description": "可选说明"}`。
需要用户自由填写时追加一条 `{"label": "我来补充", "value": "", "freeform": true}`，点它只把焦点交给输入框。
`question` 与 `options` 一起落进 `collab_tickets.result`，`collab.notice(level=ask)` 也携带同样字段；
桌面端与移动端都据此在输入框上方渲染可点卡片。旧记录只有 `question` 时，界面会补出「按你的默认方案来」
（问题里提到默认方案时）与「我来补充说明」两张兼容卡片，不要求重跑历史。模型漏给结构化选项时，
引擎还会从问题正文的项目符号或「A 还是 B」句式中恢复选项，并至少补出可继续的推荐项，
保证 `waiting_user` 不会退化成一张只有协议错误的死路。

**防死循环（必须显式；M1 落底座，M3 收口）**：
- 返工轮次上限：工单建立时先按 `min(节点 max_rounds, 运行预算 max_rounds_per_ticket)` 定
  `ticket.max_rounds`；审查者打回时若 `rework` 边带了 `max_rounds`，再收紧为
  `min(工单上限, 边上限)` 并写回工单（界面显示的「返工 x / y 轮」就是它）；
- 超过上限 → 工单置 `blocked`、运行置 `waiting_user`，`run.error` 与 `collab.notice(error)`
  都写明「已返工 N 轮仍未通过（上限 M 轮），需要你介入」，**不静默**；
- **拆解树深度上限** `MAX_TICKET_DEPTH = 5`：第 6 层还继续 `split` 会被拒绝，提示改用 `handoff`
  直接交给实施者（防"拆子工单再拆子工单"无穷套娃）；
- **重名子工单上限** `MAX_DUPLICATE_CHILD_TITLES = 2`：同一个父工单下第三次拆出**同名**子工单会被拒绝，
  提示换更具体的标题或由用户拍板（防"复读式拆解"空转烧钱）；
- `max_steps` / `max_tickets` / `max_tokens` 任一超限 → 挂起并明确提示；`timeout_seconds`
  按「距上次进展」的时长判断，用户隔一段时间回来继续不会因创建时间较早而永久失效；
- 单次推进的防御性硬上限 `MAX_STEPS_PER_TURN = 200`，杜绝一条消息内无限自转。

上述任何一条触发时，动作**整体被拒**：不会先建子工单再挂起，也不会留下半截状态。

### 3.4 调度规则（一期：串行）

- 引擎维护**就绪队列**：状态 ∈ {`open`, `rework`} 的工单，按 `(深度, created_at)` 排序。
- **同一时刻只有一个步骤在跑**：谁在等谁，UI 才说得清、日志才好查、取消语义才确定。
- **谁拆的谁汇总**：子工单全部完成 → 父工单自动回到**拆解者**手里（`ticket.owner_key` 记着"谁拆的"，
  `waiting_children → open`），协调者据此继续 `delegate` 或 `done`。
- 没有就绪工单且没有 `waiting_children` 的工单 → 运行 `done`。

---

## 4. 工单协议（C4 / C5 / C6 的核心）

### 4.1 角色输出格式

每个节点在**一轮发言的末尾**必须附一个可解析的工单块（框架在 prompt 里给出模板与示例）：

````
（这里是给人看的说明文字，进入时间线卡片正文）

```work_order
{
  "action": "delegate",
  "ticket": {
    "title": "登录页改造为暗色主题",
    "goal": "在不改动后端的前提下，让登录页支持暗色主题",
    "requirements": ["新增主题开关", "持久化用户选择"],
    "acceptance": ["刷新后主题保持", "对比度符合 WCAG AA"],
    "constraints": ["只改 frontend/src/**", "不得新增依赖"]
  },
  "assignee": "cto",
  "parent_ticket": null,
  "verdict": null,
  "review_comment": null,
  "artifacts": [{"kind": "file", "path": "frontend/src/pages/Login.tsx"}]
}
```
````

需要用户拍板时输出决策卡数据，而不是把选项写进正文：

````
```work_order
{
  "action": "ask_user",
  "question": "首期先做最小闭环，还是一次做全？",
  "options": [
    {"label": "先做最小闭环", "value": "先做材料与人工台账，报表导出下一阶段再做", "description": "最快可用"},
    {"label": "一次做全", "value": "材料、人工、成本汇总、报表导出一起做"},
    {"label": "我来补充", "value": "", "freeform": true}
  ]
}
```
````

**解析容错（`tickets.py`，已实现）**，按优先级依次尝试：

1. **严格 JSON**：` ```work_order ` 围栏（围栏缺失/被截断时按"到文本末尾"处理）→ 直接 `json.loads`。
2. **正则清理后再解析**：删除不可见控制字符（`\x00-\x1f`、BOM）、把中文智能引号 `“ ”` 还原成 `"`、
   迭代修掉尾随逗号（`,}` / `,]`）。这一步能救回大量因复制粘贴、截断、转义造成的低级语法错误。
3. **扫描正文里平衡的 `{...}`**：先内层后外层逐一尝试，能处理"JSON 前后裹了多余文字"的情况；
   仍然失败时启用**截断补齐**——从后往前回退到最后一个完整 `}`，或补齐未闭合的 `{`/`[`
   （必要时先补上被截断的引号），再回退到最后一个逗号重试。

三级都失败 → 产出 `error` 事件（"「CTO」的产出不符合工单协议，已暂停等待你介入/重试"），
工单**不推进**、运行置 `waiting_user`。**绝不猜、绝不静默**（铁律 11）。
特别地：**一轮出现多个合法工单块时取最后一个**，并额外产出一条 `collab.notice(warn)` 说明这次偏差。
工具型内核（Claude Code 等）会把「中间轮次的草稿」与「最终答复」拼进同一段输出，
一律判死会让整条链路白跑（实测：码农写完草稿又补了一份最终 `done`，运行被迫挂起）。
取最后一版与"模型改了主意"的语义一致，而且 `assignee` 仍要过边的白名单校验，
取错的代价被限制在**合法**对象之间（铁律 11 的可观测性由 notice 保证）。

解析后再做**语义校验**：`action` 是否在节点的 `output_contract` 内、`assignee` 是否在边的白名单内、
`pass` 是否带 `review_comment`、必填字段是否齐全。任一不通过 → 同上，作为显式错误。

### 4.2 交付载荷：下一个角色到底看到什么（C5）

框架用**模板**（不是上一轮的对话）拼出下游 prompt：

```
[协作] 你在协作组「客户端研发组」中担任「SE」，职责：<node.instructions>
能力边界（不得越界）：<node.capability_scope>
流转规则：本轮结束必须附 ```work_order 块；你只能把工单交给：CTO（assign）。

[工单 T-12] 登录页改造为暗色主题          ← 只含工单字段
目标：…
需求：1) … 2) …
验收标准：1) … 2) …
约束：1) …
上游交付摘要：                           ← 摘要，不是对话
  - 「CTO」于 14:03 提交：把主题开关拆成 2 个子任务（子工单 T-13/T-14 已通过）
  - 变更文件：frontend/src/theme.ts
本工单历史：                             ← 只有同一工单的历史
  - 第 1 轮「SE」提交 → 「审查员」打回：「第 2 条验收未满足：…」
本轮要求：请按审查意见修改后重新提交。
```

**交付的两种形态**（`DeliveryMode`，M1 已落地）：
- `handoff` **转交**：同一张工单换个人做，新字段与旧字段合并（CEO→CTO 这种"同一件事往下传"）；
- `split` **拆子工单**：新建一张子工单并把父工单置 `waiting_children`（CTO→SE 这种"大任务拆小"），
  父工单由拆解者（`owner_key`）在子工单全通过后负责汇总。

隔离保证（有测试断言，且**在数据通路上结构性成立**，不靠实现者自觉）：
- `ctx.history = []`、`ctx.session_id = None`（**每个步骤都是全新会话**），绝不续接内核会话；
- prompt 里**不含**任何角色的完整对话、其他工单正文、其他分支内容；
- 上游只能以「工单字段 + 一句话摘要 + 文件路径清单」的形式被看见；
- 这一步的摘要取自引擎派生的 `collab_steps.gist`（只由结构化字段拼成），拼 prompt 时**只读
  `gist`、不读 `content`** —— 下游看不到对话原文不是靠自律，而是没有那条通路。

### 4.3 能力范围（C6）：分四层，写在方案里而不是指望提示词

| 层 | 机制 | 覆盖面 | 说明 |
| --- | --- | --- | --- |
| L1 提示词 | `capability_scope` + 输出契约注入 | 所有内核 | 只是约束，不是保证 |
| L2 工作目录 | 协作组在新建时可填「工作路径」；留空则按运行 ID 分配默认目录。所有岗位都拿到同一个绝对路径并作为 cwd，prompt 明确它是唯一允许访问的目录 | 所有内核 | 路径不再因角色而异；`none` / `read` 仍由提示词约束 + 越权审计兜底 |
| L3 参数覆盖 | 节点级 `options_override`（复用 `DynamicForm` 按该智能体类型的 `config_fields` 渲染），如只读/计划模式、关闭工具 | 支持相应能力的内核 | 框架**不写任何内核分支**，纯透传 |
| L4 越权审计 | 运行前后对共享工作目录做指纹快照（文件路径 + 大小 + mtime + 内容哈希，带文件数/体积上限）；`read`/`none` 节点被检出改动 → `error` 事件 + 卡片标红 + 工单打回 | 所有内核 | 通用、无需内核知识 |

诚实声明（写进文档"已知限制"）：L3 的效果取决于内核自身能力；L4 是**事后发现**而非事前阻止；
真正的强隔离需要容器/沙箱，属于后续独立议题。

### 4.4 「能力范围」与「角色设定」的分工（C2）

- **角色（Role）**回答"他是谁"：人格、绑定智能体、参数、技能 —— 沿用现有「角色」页，不重复造。
- **节点（Node）**回答"他在这个协作里干什么"：职责、能力边界、可发起的动作、可派发给谁、参数覆盖。
- 两者通过节点上的 `role_id` 下拉绑定；同一个角色可以出现在多个协作组里、担任不同 duty。

---

## 5. 运行时引擎

### 5.1 一次步骤的时序

```
engine.prepare_turn(run, content)        # 校验图、把用户这句话接到入口工单上
engine.consume_turn(prepared)            # 串行推进直到挂起/完成
 ├─ 选出就绪工单 + 目标节点
 ├─ store 落库 StepMessage(status=streaming) 与 ActiveRun 句柄
 ├─ 载入 Role / AgentInstance → get_agent(type_id) → 组装 AgentContext
 │    ├─ ctx.prompt / system_prompt：由 prompt.py 拼装（人格 + 协作段 + 工单载荷）
 │    ├─ ctx.history=[]、ctx.session_id=None            ← 不共享记忆
 │    ├─ ctx.workspace_dir：所有岗位统一为「本次协作任务」的工作路径（任务指定 > 团队默认；
 │    │                     workspace_scope 只约束读写权限与越权审计）
 │    └─ ctx.skills / ctx.skill_home_dir：按节点绑定的角色分配物化（scope_id = 运行内的作用域）
 ├─ async for event in execution.stream(node, ctx, handle):
 │    ├─ 原始 AgentEvent → 包进 collab.step.event 信封 → 立刻 SSE 推给前端（逐字打字效果）
 │    └─ 同时累积 text / parts / usage（复用 consume_run 同样的累积方式）
 ├─ 审计：read/none 节点的共享目录指纹比对
 ├─ 解析工单块 → tickets.parse() → 得到 action 与载荷
 ├─ store 落库 StepMessage + 更新 Ticket + 产出 collab.ticket.* 元事件
 └─ 决定下一个持有者；就绪队列空则收尾
```

### 5.2 传输协议（两层，互不干扰）

- **第一层（智能体协议）**：`agents/events.py` 的 10 种事件，**一字不改**。
- **第二层（协作信封，`collab/protocol.py`）**，只被新的协作页面消费：

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `collab.run.started` | `run_id, team_id, entry_key` | 建立运行上下文 |
| `collab.step.started` | `step_id, ticket_id, role_key, duty, agent_name` | 时间线插入"正在处理"卡片 |
| `collab.step.event` | `step_id`, `event`（内嵌原始 AgentEvent） | 逐字流式渲染 |
| `collab.step.finished` | `step_id, status, action, usage` | 卡片收尾 |
| `collab.ticket.created` / `collab.ticket.updated` | 工单快照 | 看板增改 |
| `collab.run.status` | `status, reason` | 运行终态 / 等待用户 |
| `collab.notice` | `text, level, question?, options?` | 预算、审计、解析失败等提示；`level=ask` 时带决策卡数据 |

> 注意用词：这两个协议"层次不同"是刻意的 —— 铁律 5 约束的是**适配器协议**，
> 协作信封属于协作域自有传输协议，因此不需要改 `consume_run` / `applyEvent` / `architecture.md` 的事件表。

### 5.3 取消 / 超时 / 异常（铁律 10 / 11）

- 取消：`POST /api/collab/runs/{id}/cancel` → 置 `handle.cancel_event` + kill 子进程；
  当前步骤落库为 `cancelled`，运行置 `cancelled`，未完成工单保留（便于查看与续跑）。
- 超时：`budget.timeout_seconds` 是两次进展之间的空闲时限，不是从创建时间起累计的整轮上限；
  每次用户输入与步骤落库都会刷新 `run.updated_at`，因此 `waiting_user` 期间隔再久，用户回来继续时
  也会获得新的推进窗口。单步超时仍沿用 `settings.run_timeout_seconds`。
- 异常：任何步骤异常 → `error` 事件 + 运行置 `error`（工单保留现场，可重试该步）。
- 解析失败 / 语义非法：不抛异常、不猜 —— 当前步骤落库 `error`、工单置 `blocked`、运行置
  `waiting_user`；用户修好模型/剧本后 `POST /api/collab/steps/{id}/retry` 把工单放回队列，再
  `POST /api/collab/runs/{id}/resume` 续跑（后端 M1 已实现，UI 按钮留到 M4）。
- 客户端断开：**不再打断运行**。推进跑在 `collab/bus.py` 的独立后台任务里，SSE 只是它的一个订阅者；
  连接被关闭（手机锁屏、切后台、隧道抖动）只退订，重新打开页面或换设备用
  `GET /api/collab/runs/{id}/stream` 订阅同一份实时事件即可续看。前端在检测到流被切断时（没收到
  `stream.closed`）会自动重连订阅并提示「正在自动重连」。

### 5.4 重启恢复

`collab.recover_interrupted()`：启动时把所有 `running` 运行标记为 `interrupted`，并把残留的
`streaming` 步骤一并收尾（否则界面会一直显示「处理中」却点不出任何输出）。工单保留原状态，
UI 提供「继续」按钮（从就绪队列重新推进）。不删数据、不重建库（铁律 9）。

---

## 6. API 设计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/collab/teams` | 协作组列表（含节点数/是否可运行） |
| `POST` | `/api/collab/teams` | 新建（可先空图） |
| `GET` | `/api/collab/teams/{id}` | 详情：元信息 + 完整图 + 上次校验结果 |
| `PUT` | `/api/collab/teams/{id}` | **整图保存**（元信息 + nodes + edges），返回校验结果 |
| `POST` | `/api/collab/teams/{id}/validate` | 只校验不保存（编辑器实时校验用） |
| `POST` | `/api/collab/teams/{id}/duplicate` | 复制 |
| `DELETE` | `/api/collab/teams/{id}` | 删除（有运行记录 → 409） |
| `GET` | `/api/collab/runs?team_id=` | 运行列表（左栏） |
| `POST` | `/api/collab/runs` | 创建运行 `{team_id, title?}`；目标由第一条 `messages` 给出（保持"用户在跟入口角色对话"的语义） |
| `GET` | `/api/collab/runs/{id}` | 运行详情（工单 + 时间线 + 状态 + 预算消耗） |
| `POST` | `/api/collab/runs/{id}/messages` | **SSE**：用户对入口角色说话 / 回复提问 |
| `GET` | `/api/collab/runs/{id}/stream` | **SSE**：订阅一次运行的实时事件（断开后重连、或在另一台设备上围观） |
| `POST` | `/api/collab/runs/{id}/cancel` | 停止 |
| `POST` | `/api/collab/runs/{id}/resume` | 从就绪队列继续推进 |
| `POST` | `/api/collab/steps/{id}/retry` | 把某一步所在的工单放回队列（解析失败/中断后修复，随后 `resume` 续跑） |
| `DELETE` | `/api/collab/runs/{id}` | 删除运行（级联删工单与步骤消息） |

校验错误用 **422** + 结构化 `detail`（含 `errors[]`，编辑器逐条高亮节点/边）；
角色/智能体的删除是**软删除**（见 `docs/architecture.md` §6）：协作历史里引用的角色/智能体
不会因为删除而消失，仍未被删除的角色引用智能体时删除智能体返回 **409**。

---

## 7. 前端设计

### 7.1 导航（已确认：单菜单 + 页内页签）

主导航（`App.tsx` 的 `NAV`）新增一项：`{ to: '/collab', icon: '🤝', label: '协作' }`
（放在「对话」之后）。页面内两个页签，四个路由都指向同一个 `CollabPage`：

| 路由 | 页签 | 内容 |
| --- | --- | --- |
| `/collab` | 协作运行 | 左栏运行列表，主区时间线 + 工单条 + 输入框 |
| `/collab/runs/:runId` | 协作运行 | 直接打开某次运行（刷新/分享链接不丢上下文） |
| `/collab/settings` | 协作设置 | 左栏协作组列表，主区提示选择或新建 |
| `/collab/settings/:teamId` | 协作设置 | 画布编排器（可刷新还原） |

### 7.2 协作运行页

```
┌ 左栏（复用 chat-list 样式） ┐┌ 主区 ────────────────────────────────────────┐
│ 🤝 客户端研发组              ││ 顶栏：组名 · 目标 · 状态 · [🧭进度][📋工单][停止] │
│   · 登录页暗色主题  进行中    │├──────────────────────────────────────────────┤
│   · 支付流程重构    完成      ││ 主会话只展示用户 ⇄ 入口领导者                  │
│ 协作组切换 ▾                 ││  👤 你：把登录页改成暗色主题                  │
│                              ││  🧑💼 CEO：…（说明文字）+ 📋 工单卡 T-12 →CTO │
│                              ││  🧑🔧 CTO：…（拆解）+ 📋 子工单 T-13 →SE      │
│                              ││  👩💻 SE：…（实现）+ 📎 变更文件 3 个         │
│                              ││  🔍 审查员：… ❌ 打回：第 2 条验收未满足       │
│                              ││  👩💻 SE：…（返工）✅ 提交                     │
│                              ││  🔍 审查员：✅ 通过 → 工单 T-12 完成           │
│                              │├──────────────────────────────────────────────┤
│                              ││ 底部输入框：直接和入口角色对话（走 SSE）        │
└──────────────────────────────┘└──────────────────────────────────────────────┘
                                    岗位抽屉：内部步骤与实时输出；工单看板：工单详情
```

> 上图中 CTO、SE、审查员的发言现在只出现在各自岗位抽屉里；主会话只保留用户与入口领导者的往来。

**M2 已落地的部分**：左栏运行列表、主区只展示「用户 ⇄ 入口领导者」的对话、顶栏「协作进度」按钮、
横向的工单摘要条（状态 / 持有者 / 返工轮次 / 审查意见）、底部输入框直接和入口角色说话、
顶部状态与预算消耗、停止 / 继续 / 重试 / 删除，以及 `collab.notice` 的即时提示。
`waiting_user` 且存在 `ask_user` 工单时，输入框上方显示决策卡；点选项等价于发送一条用户消息。
点击「协作进度」打开全屏进度页；每个岗位都可继续点开，查看该角色在本次协作里的历史步骤与当前流式输出；其他角色的发言不会
混进主会话。流式推进期间，用户刚发出的消息和入口领导者已完成的回复会先以临时步骤显示，
整轮结束后再由落库详情替换，避免“领导者明明回复了，主会话却空着”的断层。
**留到 M4**：右侧工单抽屉（子工单树、产出文件、越权审计）、按状态分列的看板、卡片视觉细化。

**跨设备同步**：桌面端与移动端共用 `lib/useAutoRefresh.ts`。当前页可见时，对话/运行详情每
3~4 秒、列表每 8 秒从后端对齐一次；切回标签页或应用回到前台时立即刷新。本机正在跑 SSE 时暂停
轮询，避免把尚未落库的流式内容清掉。对话详情若发现 `active_run` 属于另一台设备，输入区会暂时
禁用并提示等待完成，防止两端同时提交。

**岗位会话的交接链**：一个岗位的抽屉按时间顺序展示「谁对我说了什么 → 我做了什么」——入口领导者
先显示用户的开场消息，其余岗位显示上游角色派工时的发言（步骤 `content`，即角色说的人话）与该工单
的要点（`brief`）。派工步骤落库 `target_key`（把活交给了哪个节点），据此把交接卡精确挂到接收方；
旧数据没有 `target_key` 时退化为只显示本岗位自己的步骤（不影响展示，只是少了上游那句话）。

**岗位内部会话的「进行中」表达**：角色的输出只在**这一步跑完时**才落库，所以非本机发起的会话
（另一台设备、刷新后的页面）拿不到逐字流，只能看到该步落库后的结果。为了避免「进度显示处理中、
点开却什么都没有」，岗位抽屉对没有产出的步骤渲染明确的占位：`streaming` → 「正在处理中……」，
`interrupted` / 无产出 → 「被中断、没有留下输出，可点继续重跑」。服务重启时
（`collab.store.recover_interrupted`）除把残留 `running` 运行标记为 `interrupted` 外，也会把残留的
`streaming` 步骤一并收尾为 `interrupted`，否则这些步骤会永远停在「处理中」。

- 时间线卡片类型：用户发言 / 角色发言 / **工单卡**（`delegate`）/ **审查卡**（`verdict`）/ **系统提示**（预算、审计、解析失败）。
- 工单卡可点击 → 抽屉展示工单字段、子工单树、历史轮次、产出文件、token 用量。
- "谁把什么交给了谁"用卡片上的 `→ 角色名` 与看板列变化表达，无需用户读日志。

### 7.3 可视化编排器（C2 / C3）

```
┌ 左：角色抽屉 ┐┌ 中：画布（网格背景） ────────────────────────┐┌ 右：属性面板 ┐
│ 🧑💼 学究CEO   │   ┌────────┐   assign    ┌────────┐            ││ 节点/边属性   │
│ 🧑🔧 CTO       │   │  CEO   │────────────▶│  CTO   │            ││ 角色 ▾        │
│ 👩💻 SE        │   │协调者 ⭐│             │实施者   │            ││ 职责说明      │
│ 🔍 审查员      │   └────────┘             └───┬────┘            ││ 能力边界      │
│ （拖到画布）   │         ▲  rework            │ assign          ││ 工作目录范围 ▾│
│               │         │  （虚线回边）      ▼                 ││ 参数覆盖 ▾    │
│               │   ┌─────┴────┐  review  ┌──────────┐           ││ 最大返工轮次  │
│               │   │   审查员  │◀─────────│    SE    │           ││ 入口节点 ☑    │
│               │   └──────────┘          └──────────┘           ││              │
└───────────────┘└────────────────────────────────────────────────┘└──────────────┘
```

交互（全部手写，**不引入图库**，铁律 12；M2 已落地）：
- 节点：`position: absolute` 的 div，`pointerdown/move/up` 拖动（监听挂在 window 上，移出画布也不卡），
  坐标存进 `graph.nodes[].x/y`。
- 添加节点：三个入口都通往同一个 `addNode()` —— 点选角色抽屉里的角色、把角色**拖放**到画布上、
  或由第一个节点自动当入口。新节点默认 `engineer`，第一个节点自动设为 `coordinator` + 入口。
- 连线：SVG `<path>` + 箭头 marker；三种边用颜色/线型区分
  （assign 灰实线 · review 青实线 · rework 黄虚线）。
- 端口：输入在节点左/上，输出在右/下；从任一出输出端口拖到目标输入端口即可。端口方向写进
  `edges[].from_side/to_side`，旧图没有该字段时按节点相对位置自动推断（上下排列走「下→上」，
  左右排列走「右→左」）。
- 走线：采用正交折线并做避障；路径避开所有节点卡片，且对既有连线的重叠、交叉施加额外代价，
  因此审查者从下方回接上方组织时，线路会沿空白通道绕回，而不是穿过节点或叠在别的线上。
  不做自动布局；边类型仍由两端岗位自动推断（目标是审查者→`review`；来源是审查者→`rework`；
  其余→`assign`），随后可在右侧面板改。
- 删除：选中节点/边后按 `Delete`、或点面板里的删除按钮；删节点会级联删掉它的边。
- 校验：每次改动**防抖 500ms** 调一次 `POST /api/collab/teams/{id}/validate`（与运行时同一套规则），
  错误/提醒就地高亮节点、连线和面板，并把「人话提示」列在画布下方；前端不重复实现一遍规则。
- 属性面板：角色用现有 `listRoles()` 下拉；`options_override` 用 `DynamicForm`
  按该角色绑定智能体类型的 `config_fields` 渲染（C2 直接白得）。
- 校验实时提示（本地 + 服务端双保险）：
  - 必须**唯一入口节点**；非入口节点必须至少有一条入边；
  - 存在 `review` 边则必须有 `rework` 回边（否则无法返工），反之亦然；
  - 环必须显式声明为 `rework`；`assign` 边不许成环；
  - 每个 duty 允许的动作集合与出边类型必须相容（如 `reviewer` 不能有 `assign` 出边）；
  - 引用的角色存在且绑定了智能体。

**移动端（`/m/`）同样能编排，只是不画线**：`mobile/pages/MobileCollabTeamPage.tsx` 用表单做同一件事 ——
岗位是卡片列表（点开底部弹层编辑），连线是「从谁到谁 + 类型」的下拉选择，校验与保存走同一套接口。
岗位/连线表单在 `mobile/components/CollabNodeForm.tsx` / `CollabEdgeForm.tsx`；字段语义、默认值与
中文标签与桌面端共用 `lib/collabDraft.ts`（`DUTIES` / `EDGE_KINDS` / `SCOPES` / `ACTIONS` /
`clampBudget` / 草稿组装），有守卫测试盯着「移动端不得引用画布组件」。

### 7.4 复用与新增（避免两份流式归约器）

- `frontend/src/lib/agentStream.ts`（**已新增**）：把 `ChatPage.applyEvent` 的纯归约逻辑搬出来（行为不变），
  `ChatPage` 改为 import；协作页对 `collab.step.event` 解包后复用同一个归约器 —— 时间线渲染与对话页一字不差。
- `frontend/src/lib/collabStream.ts`（**已新增**）：协作信封事件的归约（进行中的步骤 + 工单 + 运行状态 + 提示）。
- 类型放 `lib/types.ts`、接口放 `lib/api.ts`（`streamPost` 加一个泛型参数以复用同一份 SSE 读取逻辑）。
- 样式按 **AGENTS.md §4.7** 写进 `index.css`（沿用 `card`/`btn`/`pill`/`banner`/`field` 等既有命名与 CSS 变量），
  不再单独引入 `collab.css`；`main.tsx` 不动。

---

## 8. 数据库设计

新增 4 张表（`collab/` 前缀，与现有表共存，**不改任何现有表**）：

```
collab_teams(id PK, name, description, entry_key, workspace_dir,
             graph JSON, budget JSON, created_at, updated_at)

collab_runs(id PK, team_id → collab_teams RESTRICT, title, goal, status,
            entry_key, workspace_dir, budget JSON, usage JSON,
            steps_used, tickets_used, error, created_at, updated_at)
            idx(team_id), idx(status)

collab_tickets(id PK, run_id → collab_runs CASCADE, parent_ticket_id → collab_tickets SET NULL,
               from_role_id → roles RESTRICT (可空，用户发起时为空),
               to_role_id → roles RESTRICT,
               title, brief JSON, holder_key, state, round, max_rounds,
               verdict, review_comment, result JSON, created_at, updated_at, closed_at)
            idx(run_id), idx(state), idx(parent_ticket_id)

collab_steps(id PK, run_id → collab_runs CASCADE, ticket_id → collab_tickets SET NULL,
             role_id → roles RESTRICT, agent_id → agent_instances RESTRICT,
             role_key, duty, sequence, action, status, content, parts JSON, usage JSON,
             error, audit JSON, started_at, finished_at)
            idx(run_id), idx(ticket_id), idx(status)
```

设计取舍：

- **工单 `brief` / `result` 用 JSON**：工单字段（需求/验收/约束/产出）形态会演进，
  与现有 `messages.parts` 同一思路；同时也是"交付载荷"的直接数据源。
- **`collab_steps.content` + `parts`**：与 `messages` 表同构 → 前端能直接喂给 `MessageView`。
- **外键 `RESTRICT`（引用角色/智能体）**：沿用现有不变量，因此删角色/智能体只能软删除
  （打 `deleted_at`，从可选列表消失、历史照旧可读），协作表的外键不会被打断；
  删除"运行"是显式操作，级联清理工单与步骤（等价于"删对话必须清消息"的既有惯例）。
- **`parent_ticket_id` 用 `SET NULL`**：父工单被拒绝/删除时不连带毁掉子工单记录。
- **零迁移风险**：全部是新增表，`create_all` 会自动建；现有 `data/magenta.db` 直接启动即可（铁律 9）。
- **后加的可选列怎么补**：SQLite 的 `create_all` **不会给已存在的表加列**。因此协作域把"后加的可选列"
  登记在 `collab/tables.py` 的 `ADDITIVE_COLUMNS`（对话域登记在 `models.py` 的同名常量），
  由 `db.init_db` 用 `ALTER TABLE ... ADD COLUMN`（带默认值）幂等补齐 —— 旧行原样保留，不重建库。
  加新列时必须同时登记，否则旧库启动后会报 "no such column"（有测试 `test_collab_migration.py` 守着）。

---

## 9. 测试与验收

**单元（纯函数，不启 DB）**
- `tickets.parse()`：标准围栏 / 无围栏裸 JSON / 缺字段 / 非法 `action` / 越权 `assignee` /
  同一轮出现多个块（取最后一个 + `warning`）/ 完全无法解析（必须产出显式错误而非猜）。
- `tickets.render()`：**断言下游 prompt 不含上游对话原文**（C5 的守护测试）、只含本工单历史。
- `graph.validate()`：无入口 / 双入口 / 孤立节点 / `assign` 成环 / `review` 无边 / 缺 `rework` 回边 /
  reviewer 带 assign 出边 / 引用不存在的角色。
- 状态机：§3.3 转移表逐条（含 `round` 超限、`max_steps` 超限）。

**集成（httpx + ASGI，沿用 `tests/conftest.py`）**
- 用 `tests/fakes/fake_collab_agent.py`（按 prompt 里的岗位 key 回不同剧本，能模拟协议违规）
  驱动一个 CEO→CTO→SE→审查（拆子工单 + 打回一次后通过）的完整流程：
  断言工单状态流转、`collab_steps` 落库顺序、SSE 事件序列、运行终态、
  以及**每个下游 prompt 的隔离性**。
- 取消 / 超时 / 解析失败 → 运行状态与事件正确。
- 重启恢复：造 `running` 残留 → `recover_interrupted()` → `interrupted`。

**架构守卫**：§2.5 的 6 条。

**验收（对应 §8 DoD）**
```bash
bash scripts/test.sh              # pytest 全绿 + 前端 tsc
cd frontend && npm run build      # 前端改动必须
curl -s http://127.0.0.1:8010/api/health
# 前端改完需重启 PyPanel 服务再自测画布与协作页
```

**手工验收清单**：导航出现「协作」；画布能拖节点/连线/存图/刷新后还原；
错误图给得出人话提示；跑一次真实协作（≥3 个角色、≥1 次打回）；
工单看板与时间线一致；取消按钮真的能杀掉子进程；`/api/agent-types`、现有对话页无回归。

---

## 10. 风险与取舍

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| 模型不吐合法工单块 | 流程卡住 | §4.1 三级容错 + 显式错误 + 运行挂起 + 单步重试；提示词给足模板与示例 |
| 返工死循环 / token 爆炸 | 成本失控 | §3.3 三重轮次上限 + 运行预算 + 达到上限挂起等用户 |
| 能力边界只是"软约束" | CEO 仍可能写文件 | §4.3 L2/L3/L4；文档明确写"强隔离需沙箱" |
| 串行导致长流程慢 | 体验 | 一期接受（可解释性优先）；`max_parallel` 留作演进 |
| 与现有代码耦合 | 维护成本 | §2.4 文件级改造清单 + §2.5 守卫测试 |
| 引擎与 UI 双份事件语义 | 漂移 | 后端唯一信封协议（§5.2）；前端只做归约 |
| 技能契约改动（§2.4 第 4 条） | 影响面 | 向后兼容的可选参数；跑 `tests/test_skills.py` + 全量 pytest |

---

## 11. 分阶段实施计划

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| **M0 契约冻结** ✅ | `tables.py` / `schemas.py` / `protocol.py` / `tickets.py` / `graph.py` + 单测；协作组路由；`init_db` 注册；`sse.py` 下沉 | 已完成，见 §13 |
| **M1 引擎 MVP** ✅ | `prompt.py` + `execution.py` + `engine.py` + 运行 API：`/runs/{id}/messages`（SSE）串行跑通 CEO→CTO→SE→审查（含拆子工单与一次打回返工），并支持 `cancel`/`resume`/`retry`、预算与轮次上限、`ask_user` 挂起、越权审计 | 已完成，见 §13 |
| **M2 编排设置** ✅ | 画布编辑器（拖角色/拖节点/连线/实时校验/整图保存）+ 协作设置页；另建了「协作运行」页签的最小可用形态 | 已完成，见 §13（前端 `npm run build` 通过 + 后端契约已按前端调用顺序冒烟） |
| **M3 防死循环收尾** ✅ | 边级 `max_rounds`、工单树深度上限与重名子工单去重、`run.error` 与提示的统一表达 | 已完成：`test_collab_guards.py` 覆盖 §3.3 全表（4 项端到端） |
| **M4 协作运行页打磨** ✅ | 工单看板抽屉（子工单树 + 工单字段 + 产出文件 + 审查意见 + 越权审计）、顶栏预算「已用 / 上限」与工单状态摘要、编辑器里的边级返工上限 | 已完成：`npm run build` 通过 + 抽屉 SSR 冒烟（空/嵌套/脏数据）；时间线仍复用 `MessageView`（未拆出独立"工单卡"组件） |
| **M4b 用户决策卡** ✅ | `ask_user` 的 `question + options` 协议、解析与语义校验；桌面端与移动端把等待用户拍板渲染成可点选项；旧记录无选项时按默认方案/自由填写兼容；移动端外壳缓存版本 v6 | 已完成：后端协议/引擎测试；`bash scripts/test.sh` + `npm run build` |
| **M4c 领导者主会话 + 协作进度** ✅ | 主会话只显示用户与入口领导者；进度条展示岗位顺序、当前执行者、工单与连线；点岗位打开只读实时会话；流式期间即时回显；完成后可继续追问并重新打开根工单；空闲超时而非创建时长超时 | 已完成：桌面/移动端共用进度模型、两端 build、协作引擎与 API 测试 |
| **M4d 进行中岗位可见性修复** ✅ | 岗位抽屉对未产出步骤渲染「正在处理中 / 已中断」占位；进度对「运行已停但步骤还停在 streaming／被中断」的岗位标为「已中断」而非「处理中」；启动恢复同时收尾残留 `streaming` 步骤 | 已完成：`test_recover_interrupted_also_settles_streaming_steps`；`bash scripts/test.sh` + `npm run build` |
| **M4e 运行与连接解耦 + 岗位交接链** ✅ | 推进改跑后台任务（`collab/bus.py`），SSE 断开只退订、不再打断运行；新增 `GET /runs/{id}/stream` 订阅与前端自动重连；步骤记录 `target_key`（把活交给谁），岗位抽屉按时间还原「上游对我说了什么 → 我做了什么」 | 已完成：`test_client_disconnect_does_not_stop_the_run`、`test_stream_endpoint_replays_buffered_events_after_disconnect`（重连先补发再续看）、`test_delegate_step_records_target_for_handoff_chain`、`backend/tests/test_collab_bus.py`（补发 / 断线退订 / 缓冲回收）；`bash scripts/test.sh` + `npm run build` |
| **M4f 超长单行输出不再打断运行** ✅ | 执行层自建行缓冲（`runtime/runner.py` 的 `_StreamLines`）：内核把整段代码或工具结果塞进一行 JSONL 时，不再抛 「Separator is found, but chunk is longer than limit」把整轮运行判成 error；单行越过 64 MiB 上限只丢该行并补一条 `log(warn)`，stdout 与 stderr 同一条路径 | 已完成：`backend/tests/test_runner.py`（默认 `readline` 会炸的前提、整行读出、内存上限兜底、端到端超长单行跑通）；`bash scripts/test.sh` |
| **M4g 一轮多个工单块不再判死** ✅ | 同一轮里出现多个 `work_order` 块时取最后一个，并产出一条 `collab.notice(warn)` 说明偏差；草稿块不再进正文；提示词同步要求「整轮只输出一个、正文里不要粘贴草稿」 | 已完成：`test_parse_takes_the_last_block_when_a_round_has_several`、`test_round_with_two_work_order_blocks_uses_the_last_one`；`bash scripts/test.sh` |
| **M5 打磨** | 技能物化接入协作运行、用量统计与导出、运行复制/模板 | `pytest` + 前端 build；技能端到端确认"仅分配生效" |

每个里程碑单独提交、单独验证（铁律 8），M1 完成前不碰前端画布。

---

## 12. 已确认的决策（评审结论）

| 议题 | 决策 | 落地位置 |
| --- | --- | --- |
| 导航形态 | **一个「协作」菜单 + 页内页签**（协作运行 / 协作设置），保持主导航清爽 | §7.1；M2 |
| 工单协议 | **强制 `work_order` 结构化块**，三级容错解析；提示词给标准围栏 + 示例 | §4.1；M0 已实现 |
| 并发模型 | **绝对串行**：同一时刻只推进一个步骤，杜绝共享工作目录的竞态与冲突 | §3.4；M1 |
| 审查关系 | **可选**；一旦存在 `review` 边，图校验就强制要求对应的 `rework` 回边 | §3.3、§2.5；M0 已实现 |
| 工作目录 | **一期团队共享一个目录**；文件锁 / git worktree 留作演进 | §4.3；M1 |
| 用户干预 | **一期必须支持** `waiting_user` 挂起与续跑（关键节点/出错时人能踩刹车） | §5.3、§5.4；M1/M3 |
| `sse.py` | **平移到 `app/transport/sse.py`**，成为与业务无关的通用传输设施（已落地） | §2.3、§2.5；M0 已完成 |
| JSON 容错 | 在第 1 级失败后先做**正则清理**（控制字符、智能引号、尾随逗号），再做截断补齐 | §4.1；M0 已实现 |
| 画布实现 | 四向端口（左/上入、右/下出）+ 正交避障折线；不做自动布局；前端不重复实现校验规则，一律调服务端 `validate` | §7.3；✅ M2，端口/避障已升级 |

---

## 13. 实施进度

**M0 · 契约冻结 —— 已完成。**

交付物（全部为新增文件，宿主改动共 4 处约 7 行）：

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tables.py` | 4 张表：`collab_teams` / `collab_runs` / `collab_tickets` / `collab_steps` |
| `backend/app/collab/schemas.py` | 岗位/边类型/动作/状态枚举，节点与边的结构，预算，校验结果，请求响应模型 |
| `backend/app/collab/protocol.py` | 协作信封事件（`collab.*`），与智能体协议分属两层 |
| `backend/app/collab/tickets.py` | 工单协议：三级容错解析、语义校验、输出协议说明渲染（纯函数） |
| `backend/app/collab/graph.py` | 编排图查询与校验（纯函数，角色存在性由调用方注入） |
| `backend/app/collab/store.py` | 协作组持久化、运行计数、启动恢复 |
| `backend/app/collab/api.py` | `/api/collab/teams` 的 CRUD + `validate` + `duplicate` |
| `backend/app/transport/sse.py` | 由 `api/sse.py` 平移而来的通用 SSE 设施 |
| `backend/tests/test_collab_tickets.py` | 11 项：解析容错、语义校验、协议说明 |
| `backend/tests/test_collab_graph.py` | 14 项：主链通过、缺失入口、孤立节点、审查闭环、assign 成环、岗位边不相容、角色被删… |
| `backend/tests/test_collab_api.py` | 6 项：CRUD、草稿可存但报错、`validate`、404、422、有运行记录删除返回 409 |
| `backend/tests/test_collab_architecture.py` | 7 条分层守卫（§2.5） |

验证（M0 当时的基线）：`bash scripts/test.sh` → **115 passed, 2 skipped** +
前端 `tsc --noEmit` 通过。

**M1 · 引擎 MVP —— 已完成。**

交付物（协作域新增 `engine/execution/prompt` 三个模块 + 运行 API；宿主动作仍是 §2.4 那几处）：

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/prompt.py` | `build_step_prompt()`：把「岗位说明 + 输出协议 + 工单字段 + 上游交付摘要 + 本工单历史 + 本轮要求」拼成一次输入（纯函数，**只读 `gist` 不读 `content`**） |
| `backend/app/collab/execution.py` | 全域**唯一**接触 `runtime`/`agents` 的边界：`prepare_step`（`history=[]`、`session_id=None`、按 `workspace_scope` 决定工作目录、节点级 `options_override`）、`stream_step`、`snapshot_workspace/finish_audit`、`StepRegistry`（复用 `runtime.manager`） |
| `backend/app/collab/engine.py` | `prepare_turn`（校验图 → 落用户发言 → 建/唤醒根工单）+ `consume_turn`（预算检查 → 子工单汇聚 → 取就绪工单 → 跑一步 → 解析动作 → 落库），串行、`MAX_STEPS_PER_TURN=200` |
| `backend/app/collab/api.py` | 运行相关接口：`GET/POST /runs`、`GET/DELETE /runs/{id}`、`POST /runs/{id}/messages`（SSE）、`/resume`（SSE）、`/cancel`、`POST /steps/{id}/retry` |
| `backend/app/collab/tables.py` | 工单补 `owner_key/holder_key/submitter_key`，步骤补 `gist`（交付摘要的结构性落点） |
| `backend/tests/test_collab_engine.py` | 5 项端到端：完整闭环（7 步、1 次返工、`tickets_used=2`）、下游只见工单、协议违规挂起后重试续跑、入口 `ask_user` 后应答续跑、重复启动/无目标续跑被拒 |
| `backend/tests/fakes/fake_collab_agent.py` | 替身智能体：按 prompt 里的岗位 key 取剧本，可输出字符串模拟协议违规 |
| `backend/tests/test_collab_migration.py` | 2 项：给"旧库"（缺 `gist` 的 `collab_steps`）补列后旧行不变、重复启动幂等 |
| `backend/app/collab/tables.py` 的 `ADDITIVE_COLUMNS` | 后加可选列的登记处；`db.init_db` 据此幂等补列（本地真实库已实测通过） |

验证：`bash scripts/test.sh` → **125 passed, 1 skipped**（唯一 skip 是"`execution.py` 属于执行层边界"的
守卫用例，刻意跳过）+ 前端 `tsc --noEmit` 通过。

**M2 · 编排设置 —— 已完成。**

交付物（前端 7 个文件 + 3 处小改；后端零改动）：

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/types.ts` | 追加协作域类型（节点/边/预算/校验/协作组/工单/步骤/运行/信封事件），字段名与 `schemas.py` 一致 |
| `frontend/src/lib/api.ts` | 追加 `/api/collab/**` 的 14 个方法；`streamPost` 加泛型参数（协作复用同一份 SSE 读取逻辑） |
| `frontend/src/lib/agentStream.ts` | 从 `ChatPage` 抽出的纯归约器（两页共用，行为不变） |
| `frontend/src/lib/collabStream.ts` | 协作信封事件的归约：进行中的步骤、工单、运行状态、提示 |
| `frontend/src/components/CollabCanvas.tsx` | 手写画布：拖节点、左/上输入与右/下输出端口、正交避障连线、拖放添加节点、Delete 删除 |
| `frontend/src/components/NodeInspector.tsx` | 节点/边/团队三种上下文的属性面板；节点「参数覆盖」直接复用 `DynamicForm` |
| `frontend/src/components/CollabSettingsPanel.tsx` | 协作组列表 + 画布 + 面板；防抖实时校验、整图保存、放弃改动 |
| `frontend/src/components/CollabRunsPanel.tsx` | 运行列表 + 时间线（复用 `MessageView`）+ 工单条 + 直接和入口角色说话 |
| `frontend/src/pages/CollabPage.tsx` | 页签骨架 + 4 条路由 + 共享数据加载 |
| `frontend/src/App.tsx` / `index.css` / `MessageView.tsx` | `NAV` + `Routes`；协作样式；`MessageView` 加可选 `label` 显示岗位名 |

验证：`npm run build`（`tsc -b` + vite）通过；后端按**前端调用顺序**跑了一遍冒烟脚本
（建组 → validate → PUT 整图 → 发起运行 → SSE → 详情 → 列表 → 继续 → 停止 → 重试 → 删除 → 复制），
SSE 事件统计 `run.started 1 / step.started 5 / step.event 50 / step.finished 5 / ticket.created 1 /
ticket.updated 6 / run.status 1`，且 6 步时间线与 2 张工单的字段与前端类型逐一对上。

**M3 · 防死循环收尾 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/schemas.py` | `EdgeSpec.max_rounds`（边级返工上限）；`RunRead.budget`（界面显示「已用 / 上限」） |
| `backend/app/collab/graph.py` | `rework_edge()`：取「审查者 → 打回目标」那条边，读它的 `max_rounds` |
| `backend/app/collab/engine.py` | `MAX_TICKET_DEPTH=5`、`MAX_DUPLICATE_CHILD_TITLES=2` 两个护栏；`_ticket_depth()`、`_child_count_with_title()`、`_reject_action()`（拒绝即挂起 + `run.error` + `waiting_user`）；`review/changes_requested` 分支按边级上限收紧 `ticket.max_rounds` |
| `backend/app/collab/store.py` | 运行详情/列表回传 `budget` |
| `backend/tests/test_collab_guards.py` | 4 项端到端：边级上限（第 2 轮打回被拒 + 边字段可读写）、拆解第 5 层拒绝并提示 `handoff`、第三次重名子工单被拒、`budget` 出现在运行详情与列表 |

关于提示：护栏触发的说明**统一在本次推进收尾时**由 `_finalize` 发出一次（`notice(error)` + `run_status`），
不再各自重复播报；被拒的动作不会留下半截产物。

验证：`backend/.venv/bin/python -m pytest tests -q` → **131 passed, 1 skipped**。

**M4 · 协作运行页打磨 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/TicketBoard.tsx` | 新增工单看板抽屉：左「父工单 → 子工单」树（按 `parent_ticket_id`，带状态与返工轮次），右为工单细节（`brief` 五段字段、`result.summary` 与 `artifacts`、`review_comment`、`返工 x / y`、本工单的步骤与**越权审计**）；产出物兼容「纯字符串」与后端规整后的 `{kind, path}` 两种形态，默认打开「等你 → 返工 → 待审 → 根工单」里最需要介入的那张；Esc / 点遮罩关闭 |
| `frontend/src/components/CollabRunsPanel.tsx` | 顶栏改为预算「已用 / 上限」+ 单工单返工上限 + 工单状态摘要；「📋 工单看板」入口取代横向工单条（细节归入抽屉） |
| `frontend/src/components/NodeInspector.tsx` | `rework` 边上新增「这条边的返工上限」输入（0 = 按团队默认） |
| `frontend/src/lib/types.ts` | `CollabEdge.max_rounds`、`CollabRun.budget` |
| `frontend/src/components/CollabSettingsPanel.tsx` | 载入旧图时把缺失的 `max_rounds` 补 0（前端不依赖后端一定回传新字段） |
| `frontend/src/index.css` | 抽屉样式（右侧滑出 + 两栏，窄屏降为单栏） |

验证：`npm run build`（`tsc -b` + vite）通过。另起一个临时后端（`MAGENTA_DATA_DIR` 指向临时目录，
没碰真实库）真跑了一次协作（`done`，7 步、2 张工单、`rework` 边上限 1 生效、子工单 `round=1/1`），
把 `GET /api/collab/runs/{id}` 的**真实 JSON** 直接喂给抽屉用 `react-dom/server` 渲染，11 项断言全过：
默认选中、孤儿子工单兜底、`brief` 各字段、审查意见、`返工 1 / 1 轮`、产出文件、本工单步骤与审计结论、
「挑最该介入的那张」、以及脏数据类型（`goal` 是数字、`artifacts` 混入 `{kind:'file'}` 与 `null`）不崩。
其中 `result.artifacts` 为 `{kind: 'file', path}` 这一条正是冒烟发现的坑（原先会显示成 `[object Object]`）。

**下一步（M5）**：技能物化接入协作运行（按角色分配物化技能）、用量统计与导出、运行复制/模板；
以及 M4 留白的时间线独立「工单卡 / 审查卡」（目前沿用 `MessageView` + `gist` 表达）。

**M4b · 用户决策卡 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | `AskOption` 与 `WorkOrder.options`；容错解析 `options/choices/suggestions`、字符串列表、`{label,value,description,freeform}`；`ask_user` 语义校验要求 `question` + 2~12 条选项，且至少一条可直接点选；运行时 `ensure_ask_options()` 可从正文项目符号/「A 还是 B」恢复选项并补兜底项 |
| `backend/app/collab/prompt.py` / `tickets.render_protocol_guide()` | 输出协议明确要求决策必须给 `options`，并为协调者追加一份 `ask_user` 示例 |
| `backend/app/collab/engine.py` / `tickets.resolve_order_targets()` | 把 `question/options` 落进 `collab_tickets.result`，`collab.notice(level=ask)` 同时携带同样字段；用户答复后续跑时清掉旧卡片，避免下一次挂起误显示。另把模型常写的岗位显示名（如「CTO」）在唯一匹配时归一成节点 key（如 `dev`），避免点完选项后继续推进时卡在 `assignee` 校验。`timeout_seconds` 改为按 `updated_at` 计算「距上次进展」，用户隔一段时间回来仍可续跑 |
| `frontend/src/lib/collabAsk.ts` | 两端共用的纯视图逻辑：规整选项；旧记录只有 `question` 时补「按你的默认方案来」（提到默认方案时）与「我来补充说明」兜底 |
| `frontend/src/components/CollabRunsPanel.tsx` / `frontend/src/mobile/pages/MobileCollabRunPage.tsx` | `waiting_user` 且工单带 `question` 时渲染决策卡；点普通选项等于发送一条用户消息，点 `freeform` 选项只聚焦输入框；详情刷新时同步工单，避免旧卡片残留 |
| `frontend/src/lib/useAutoRefresh.ts` / `frontend/src/pages/ChatPage.tsx` / `frontend/src/mobile/pages/MobileChatPage.tsx` / 协作运行页 | 桌面与移动端定时对齐会话/运行详情和列表；回到前台立即刷新；另一台设备运行中的对话暂时禁用本机输入 |
| `frontend/src/index.css` / `frontend/src/mobile/mobile.css` / `frontend/public/m/sw.js` | 桌面/移动决策卡样式；移动端外壳缓存名从 v2 升到 v4 |

验证：`backend/.venv/bin/python -m pytest tests/test_collab_tickets.py tests/test_collab_engine.py -q` →
19 passed；全量 `bash scripts/test.sh`（150 passed, 1 skipped）与 `cd frontend && npm run build` 通过。

**M4c · 领导者主会话 + 协作进度 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/lib/collabProgress.ts` | 从入口节点做广度遍历得到用户可理解的推进顺序；按落库步骤、SSE 临时步骤、当前实时步骤、工单持有者与运行状态计算每个岗位的「未开始 / 执行中 / 等你决定 / 需处理 / 已完成」，并保留协作连线语义 |
| `frontend/src/components/CollabProgressBoard.tsx` / `RoleConversationDrawer.tsx` | 桌面端顶栏「协作进度」按钮与全屏进度页；主线程只渲染 `role_key in (user, entry_key)`，其他角色输出只在岗位抽屉里出现 |
| `frontend/src/mobile/components/MobileCollabProgress.tsx` / `MobileRoleConversationSheet.tsx` | 移动端横向滑动进度与全屏岗位实时会话；不引用桌面画布，符合移动端独立入口约束 |
| `frontend/src/lib/collabStream.ts` | SSE 完成的步骤先进入 `completed` 临时队列；用户发送中的消息也即时回显，详情刷新后清空临时队列并去重 |
| `backend/app/collab/engine.py` | 根工单完成后续追问会重新打开并交回入口节点；`timeout_seconds` 按 `run.updated_at` 的「距上次进展」计时；`ask_user` 漏选项时补卡片 |
| `frontend/public/m/sw.js` | 移动端外壳缓存名升到 v6，确保已添加到主屏的应用能拿到新样式与交互 |

验证：`bash scripts/test.sh`（156 passed, 1 skipped，含漏选项兜底、完成后续跑、断开不打断、派发目标记录等测试）与
`cd frontend && npm run build` 通过。

**M4f · 超长单行输出不再打断运行 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `backend/app/runtime/runner.py` | 新增 `_StreamLines`：按块读取子进程输出并自行拆行，单行长度不受 `asyncio.StreamReader` 默认 64 KiB 上限约束；`_MAX_LINE_BYTES`（64 MiB）越界时丢弃该行并置位 `overflowed`，调用方产出 `log(warn)` 告知用户；stdout 与 stderr 都走这条路径，取消/超时/脱敏逻辑不变 |
| `backend/tests/test_runner.py` / `backend/tests/fakes/fake_long_line_agent.py` | 替身 CLI 输出一行 200 000 字符的 JSONL；用例覆盖「默认 `readline` 会炸」的前提、整行读出、内存上限兜底、端到端整轮运行完整落库 |

真实故障：`dev-2` 岗位（Claude Code 内核）在写大文件时，工具调用被放在一行 JSONL 里，
该行超过 64 KiB，`process.stdout.readline()` 抛 `ValueError` 把整轮运行判成
`error`，运行卡在 `waiting_user`，界面上只看到一句英文报错。

验证：`backend/.venv/bin/python -m pytest tests/test_runner.py -q` → 5 passed；
全量 `bash scripts/test.sh`（161 passed, 1 skipped）与 `cd frontend && npm run build` 通过。

**M4g · 一轮多个工单块不再判死 —— 已完成。**

真实故障：`dev-2`（码农）在 Claude Code 内核里先写了一份「草稿」工单块（同一轮的中间输出），
又在最终答复里补了一份完整工单块；`parse_work_order` 直接以「检测到多个 work_order 代码块」把整轮判成
`error`，运行挂在 `waiting_user`，用户只看到一条协议错误。

| 文件 | 内容 |
| --- | --- |
| `backend/app/collab/tickets.py` | 多个合法块改为**取最后一个**，并把偏差写进新的 `ParseResult.warning`；`render_protocol_guide` 的规则 2 补上「正文里不要粘贴草稿或示例」 |
| `backend/app/collab/engine.py` | 解析带 `warning` 时产出 `collab.notice(warn)`（带 `step_id`），与越权提醒同一条可见通道 |
| `backend/tests/test_collab_tickets.py` / `backend/tests/test_collab_engine.py` | 断言「取最后一个 + 有 warning」与「草稿块不进正文」；单块输出不得产生噪声 |

验证：`backend/.venv/bin/python -m pytest tests/test_collab_tickets.py tests/test_collab_engine.py -q` →
25 passed；全量 `bash scripts/test.sh`（162 passed, 1 skipped）与 `cd frontend && npm run build` 通过。

**M4h · 协作进度改为顶栏按钮 + 全屏页 —— 已完成。**

| 文件 | 内容 |
| --- | --- |
| `frontend/src/components/CollabRunsPanel.tsx` | 移除运行页内嵌进度条；顶栏新增「🧭 协作进度」按钮，显示已完成岗位数与实时输出指示点 |
| `frontend/src/components/CollabProgressBoard.tsx` | 全屏进度页：完成率环形摘要、推进中 / 等你决定 / 已完成 / 需处理统计、当前焦点、岗位卡片与协作流向；点岗位直接打开内部实时会话 |
| `frontend/src/index.css` | 进度按钮、全屏页背景、概览卡、岗位卡片状态色与流转向导样式 |

验证：桌面端 `tsc --noEmit`、`npm run build`、`bash scripts/test.sh` 通过；无头浏览器在 `1440×900`
实际点击按钮，确认页面铺满视口、5 个岗位卡片与流转关系正常渲染。
