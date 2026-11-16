# 任务清单：协作办公室实况拟人化 v2（ENV-1 … ENV-5）

- 上游工单：T-30834d3a
- 需求来源：`prd.md`（v1.0，路线 2）—— **唯一口径来源，不得另立口径**
- 工程口径：`docs/architecture.md` §11「协作办公室实况呈现层（拟人化 v2）」
- 签发：CTO（Atlas） · 实施：SDE（Leo） · 验收：QA（Vera）
- 交付形态：纯 CSS 零资源的前端实现 + 守卫测试 + 运行证据

## 0. 硬约束（先读这段，违反其一即打回）

1. **纯 CSS 零资源**：不新增图片 / 字体 / 视频 / Lottie / 动画或 3D 运行时依赖；
   `frontend/package.json` 的 `dependencies` 一个都不许变（含版本）。
2. **不碰后端与状态机**：不改 `backend/app/collab/**`、不改 API、不新增 `CollabAgentState`、不改数据库。
3. **不碰底图与工位契约**：不改 `frontend/src/lib/collabOffice.ts` 的 `OFFICE_DESKS` /
   `OFFICE_ROW_SIZES` / `OFFICE_CLOCK` / `OFFICE_CHAIR_OFFSET` / `OFFICE_FOCUS`，
   不改 `scripts/gen_office_bg.py`，**不需要**重新生成底图 SVG。
4. **真实状态优先**：环境行为只叠加身体姿态与位置；状态类名、`office-tone-*` 状态点颜色、
   工牌文案 / 职责、气泡 emoji、`aria-label` **一律不动**（`docs/architecture.md` §11.3 矩阵）。
5. **两端样式逐字一致**：所有新增 CSS 写进「协作办公室」共享块
   （`index.css` 横幅之后到文件结尾；`mobile.css` 同名横幅之后到 `MOBILE_OFFICE_TUNING` 之前），
   否则 `test_office_styles_are_identical_on_both_ends` 会红。
6. **不新增「随机源」**：`lib/collabEnv.ts` 内禁止 `Math.random` / `Date.now` / `performance.now`。
7. 只动 `transform` / `opacity` 做动画；禁止 `top/left/width/height/margin/box-shadow/filter` 参与动画。
8. 界面文案、注释、文档一律中文（AGENTS.md 铁律 7）。

---

## ENV-1 · 呈现层纯函数模块（新增 `frontend/src/lib/collabEnv.ts`）

- **目标**：把「个性化」与「环境行为调度」做成确定性纯函数，成为整套行为唯一的真相来源。
- **写集**：`frontend/src/lib/collabEnv.ts`（新增）。
- **依赖**：无（其余四张卡都依赖它）。
- **必须导出**（签名固定，QA 与守卫测试按此核对）：

  | 导出 | 作用 |
  | --- | --- |
  | `type EnvBehavior = 'rest' \| 'walk' \| 'discuss' \| 'call'` | 4 类环境行为 |
  | `type FidgetKind = 'blink' \| 'scratch' \| 'pen' \| 'leg' \| 'glance'` | 5 类微动作 |
  | `ENV_TICK_MS = 5000` · `ENV_CYCLE_TICKS = 24` · `ENV_AWAY_TICKS = 6` · `DISCUSS_TICKS` | 调度常量 |
  | `fnv1a32(text: string): number` | 32 位 FNV-1a（整数运算） |
  | `workerSeed(key: string): number` | 员工稳定种子 |
  | `fidgetProfile(seed: number): { kind: FidgetKind; cycleMs: number; delayMs: number }` | 微动作个性（4 档周期、负延时错相） |
  | `ENV_ALLOWED: Record<CollabAgentState, readonly EnvBehavior[]>` | 状态覆盖矩阵（`docs/architecture.md` §11.3） |
  | `envBehaviorFor(seed: number, tick: number, allowed: readonly EnvBehavior[]): EnvBehavior \| null` | 个人调度 |
  | `discussPairs(seats: OfficeSeat<T>[]): { keys: string[]; seed: number }[]` | 同排相邻两人配对 |
  | `envOffsets(chairs: OfficeDesk[]): OfficeDesk[]` | 聚点 = 椅点中点 → 每人位移（画布像素） |
  | `OFFICE_WALK_STEP: OfficeDesk` | 一格等距步长方向（由 `OFFICE_CHAIR_OFFSET` 反推，1.2～2 格） |

- **算法口径**（必须与 `docs/architecture.md` §11.2 完全一致，注释里写清楚，Python 守卫要镜像）：

  ```
  fnv1a32:  h = 0x811c9dc5; for byte b of utf8(text): h = ((h ^ b) * 0x01000193) & 0xffffffff
  envBehaviorFor(seed, tick, allowed):
      if (allowed.length === 0) return null
      phase = (tick + (seed >>> 3)) % ENV_CYCLE_TICKS
      if (phase >= ENV_AWAY_TICKS) return null
      return allowed[(seed + phase * 7) % allowed.length]
  ```

- **验收标准**：
  1. `cd frontend && npm run typecheck` 通过（`tsc --noEmit` 无错）。
  2. 模块内不出现 `Math.random` / `Date.now` / `performance.now`，不 import React / 任何组件。
  3. `ENV_ALLOWED` 与 §11.3 矩阵逐格一致：`waiting` / `error` 为 `[]`；`pending` 为 `['rest']`；
     `active` / `done` 为四类全开。
  4. 对任何 `key`，`envBehaviorFor` 在 `waiting` / `error` 下恒返回 `null`。
  5. `discussPairs` 只产出 ≥2 人的对，且每对两人来自**同一排**；单人排不产出配对。
  6. `envOffsets` 的结果使两名成员的落点**收敛到同一点**（中点），且互不重叠判定明确（残留小偏移 ≤ 6px）。
- **QA 检查点**：核对导出清单与签名；核对矩阵与调度算术（可要求 SDE 提供一张 24 格的 tick 表）。

## ENV-2 · 小人组件改造（`CollabWorker.tsx` / `WorkerAtDesk.tsx`）

- **目标**：给人加「可走位的身体层 + 姿态节点 + 微动作节点」，同时不破坏 5 种真实状态姿势。
- **写集**：`frontend/src/components/CollabWorker.tsx`、`frontend/src/components/WorkerAtDesk.tsx`。
- **依赖**：ENV-1。
- **要点**：
  - `CollabWorker` 新增**可选** props：`env?: EnvBehavior | null`、`seed?: number`（不传 = 今天的行为）。
  - DOM：`.collab-worker` 内拆成 `椅子（`.collab-worker-chair`，留在工位不动）` + `.collab-worker-body`（气泡 / 脖子 / 头 / 躯干 / 手臂 / 腿 / 杯子都进 body）。
  - `.collab-station-scene` **必须仍是 `.office-worker` 的直接子代**（守卫要看到 `.office-worker > .collab-station-scene`）。
  - 新增节点：`.collab-worker-body`、`.collab-worker-leg.left/.right`、`.collab-worker-cup`（仅 `rest` 显示）。
  - 呼吸动画从 `.collab-worker` 挪到 `.collab-worker-body`（转椅不该跟着呼吸）；`seed` 有值时用其派生 `--fidget-delay / --fidget-cycle` 内联变量，无值时保留现有 `nth-child` 兜底节拍。
  - 不传 `env` / `seed` 时（信息卡缩略图场景）渲染出的姿态与今天**等价**。
- **验收标准**：
  1. `npm run typecheck` 通过。
  2. 5 种状态姿势在双端**形态不退化**：打盹（低头闭眼）/ 敲键盘（双手在键上）/ 举手提问（右手高举）/ 出错（瞪眼、屏幕变红）/ 收工（手臂放下、光标消失）逐条可见，给出核对记录。
  3. `CollabOfficeMap.tsx` 里 `.office-worker > .collab-station-scene` 与 `officeChairPoint(` 的用法未被削掉（守卫绿）。
  4. 走位只推 `.collab-worker-body`：`walk` / `discuss` 时转椅留在工位。
- **QA 检查点**：对照 `prd.md` §3.1 与验收标准 1、3；核对「叠加不改语义」（气泡 / 状态点 / 工牌文案逐项）。

## ENV-3 · 调度与绑定（`CollabOfficeMap.tsx`）

- **目标**：在容器层算 tick + 媒体查询，把 `env-*` / `fidget-*` 与 `--env-x/--env-y` 绑到每个工位。
- **写集**：`frontend/src/components/CollabOfficeMap.tsx`。
- **依赖**：ENV-1、ENV-2。
- **要点**：
  - 一个 `setInterval(ENV_TICK_MS)` 维护 `tick`；`useMemo` 按 `seat.node.key` 派生
    「key → env 行为」；**派生结果与上一格完全相同时不 `setState`**。
  - `.office-worker` 上写 `--seed`、`--env-x`、`--env-y`（画布像素）、`data-seat-key`；
    `.collab-station-scene` 的类名追加 `env-<name>` 与 `fidget-<kind>`。
  - 读 `matchMedia('(prefers-reduced-motion: reduce)')` 并监听 `change`：该模式下**屏蔽 `walk` / `discuss`**，保留 `rest` / `call`。
  - 工牌（`.office-pin`）位置、信息卡弹层逻辑**不动**（仍用 `officeChairPoint`）。
- **验收标准**：
  1. `npm run typecheck` 通过；`state-*` / `live` 类名与状态点 `tone-*` 逻辑未被改动。
  2. 同一画面里不同员工在同一 tick 的行为不为全场相同（给出某一 tick 的 10 个工位行为表作为证据）。
  3. reduced-motion 下不会出现跨屏瞬移：`walk` / `discuss` 不再出现（给出两次 tick 的对比记录）。
  4. 定时器在组件卸载时被清理（`clearInterval`），不出现内存泄漏式定时器。
- **QA 检查点**：核对 `prd.md` P0-2（差异化）与 P1-3（reduced-motion）；核对信息卡不参与环境行为。

## ENV-4 · 双端样式表 + PWA 外壳（一次改三份，避免两人改同一个共享段）

- **目标**：4 类环境行为各有可辨识形态、5 类微动作、双端逐字一致、无障碍降级。
- **写集**：`frontend/src/index.css`、`frontend/src/mobile/mobile.css`、`frontend/public/m/sw.js`。
- **依赖**：ENV-2、ENV-3。
- **要点**：
  - 新增 CSS **全部落在「协作办公室」共享块内**（在状态姿势段之后 → 顺序 + 权重双保险）：
    - `rest`：靠椅背（身体后仰）+ 手端 `.collab-worker-cup`；
    - `walk`：`.collab-worker-body` 沿 `--env-x/--env-y` 平移到本排过道并回到工位（一个窗口一来回）；
    - `discuss`：两人位移到中点、相向呈交谈姿态（小幅点头 / 手比划）；
    - `call`：右臂抬到耳边 + 头部侧倾；
    - 微动作：`blink` / `scratch` / `pen` / `leg` / `glance` 各一条关键帧。
  - 关键帧名统一前缀 `office-env-*` / `office-fidget-*`，只动 `transform` / `opacity`。
  - 选择器统一带 `.office-worker` 前缀（`.office-worker .collab-station-scene.env-rest …`），
    稳定压过 `.collab-station-scene.state-* …`；**不许用 `!important`**（reduced-motion 段除外）。
  - `--office-scale: 0.72` 从 `.office-worker > .collab-station-scene` 的 `scale()` 里提出来，
    只此一处定义；`.collab-worker-body` 的位移按 `calc(var(--env-x) / var(--office-scale))` 换算。
  - 共享块内新增 reduced-motion 覆盖：新增关键帧全部 `animation: none`，静态姿态保留。
  - `sw.js` 缓存名版本 +1（`arkswarm-m-shell-v78` → `v79`、`arkswarm-m-assets-v78` → `v79`）。
- **验收标准**：
  1. `python3 -m pytest backend/tests/test_mobile_web.py -q` 全绿（含双端共享段逐字一致）。
  2. 4 类环境行为形态可辨识（给出每类的观察描述 + 复现步骤：什么状态、什么 tick、看到什么）。
  3. 5 类微动作都在，且同屏节拍不同（不同员工的 `--fidget-delay / --fidget-cycle` 不同）。
  4. reduced-motion 下页面仍可读（姿势保留、无位移动画、无瞬移）。
  5. `index.css` / `mobile.css` 的「协作办公室」共享段逐字一致（守卫会强制）。
- **QA 检查点**：`prd.md` 验收 1、2、3、5、6；核对无 `!important` 滥用、关键帧属性白名单。

## ENV-5 · 守卫测试（新增 `backend/tests/test_office_env.py`）

- **目标**：把 v2 的约定固化成机器可判的检查，让「刷新后个性一致」「凑一起」「状态保真」不靠肉眼。
- **写集**：`backend/tests/test_office_env.py`（新增）。
- **依赖**：ENV-1 … ENV-4。
- **必须覆盖的断言**（`docs/architecture.md` §11.7 第 1–7 条）：
  1. `frontend/package.json` 的 `dependencies` == 冻结集合（零新依赖）。
  2. `lib/collabEnv.ts` 无 `Math.random` / `Date.now` / `performance.now`；seed 由 key 派生。
  3. **种子冻结表**：Python 用同一 FNV-1a 重算 `workerSeed(key)`，与测试内冻结表比对
     （至少 5 个 key：`ceo`、`cto`、`sde`、`qa`、`user`）。
  4. **调度冻结表**：Python 镜像 `envBehaviorFor`；断言 `waiting` / `error` 恒 `null`；
     断言出现 `discuss` 的 tick 上**配对两人同时**为 `discuss`；断言矩阵与 §11.3 一致。
  5. CSS 文本：4 个 `.env-*` + 5 个 `.fidget-*` 选择器与新增关键帧都落在**两端共享块内**；
     `state-error` / `state-waiting` 不出现在任何 `.env-*` 组合选择器里。
  6. 共享块内 reduced-motion 段覆盖所有新增 `@keyframes`（抽名逐个核对）。
  7. 新增关键帧只出现 `transform` / `opacity`（属性白名单）。
- **验收标准**：
  1. `bash scripts/test.sh` 全绿（pytest 全部 + 前端 `tsc --noEmit`）。
  2. 新增守卫在上述任一条被破坏时**真的会红**（SDE 需演示至少 1 处「故意改坏 → 红 → 改回 → 绿」的负向验证）。
- **QA 检查点**：`prd.md` 验收 4、7；核验守卫不是「空断言」（负向验证证据）。

---

## 交付与证据（每张卡都要有）

1. `bash scripts/test.sh` 全绿输出摘要。
2. `cd frontend && npm run build` 成功（含 `dist` 产物时间戳）。
3. 按 AGENTS.md 铁律 8：前端改动**重启服务**后自测页面，
   并把「视口 / 主题（深色 · 浅色）/ 状态 / tick → 观察到的形态」写进工单。
4. 一份逐条对照 `prd.md` §3 功能范围与 §6 验收标准 1–7 的自查表（每条写：证据 + 判定）。
5. **不得**往仓库提交任何图片 / 截图 / 二进制产物 —— 证据一律用文字 + 测试输出。

## 依赖关系（不可颠倒）

```
ENV-1 ──→ ENV-2 ──→ ENV-3 ──→ ENV-4 ──→ ENV-5
 （纯函数）  （组件层） （调度绑定） （双端样式+PWA）（守卫测试）
```

ENV-4 之所以把两份样式表与 `sw.js` 收在一张卡里：三者互相约束（共享段逐字一致 + 外壳版本号），
拆开派单必然产生「一半改了、守卫红着」的中间态。

## 风险与退路（CTO 预留）

| 风险 | 退路 |
| --- | --- |
| `pending + rest` 让「闲置」读起来像「悠闲」 | `ENV_ALLOWED.pending = []`（一行改动），矩阵其余不动 |
| 环境行为抢状态姿势的手臂（`active` 敲键盘 / `waiting` 举手） | 选择器加 `.office-worker` 前缀（5 类名级）压过状态规则；仍冲突则该状态不开放对应行为 |
| 走位在 `rotateY(-11deg)` 下横向略缩水（约 2%） | 判定粒度只看「人离开了工位、落在走道上」，不要求像素级对齐 |
| 5 秒 tick 带来的重渲染 | 派生结果不变不 `setState`；`will-change` 只加在被驱动的层 |
| 底图 / 坐标守卫被误动 | 见 §0 硬约束 3；任何改 `OFFICE_DESKS` 的提交一律打回 |
