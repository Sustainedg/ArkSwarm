# ArkSwarm 桌面版（desktop/）

> 🖥️ Windows / macOS 桌面壳：方案 C —— Electron 薄壳 + Embedded Python + Embedded Node +
> 复用主项目自更新机制。
> 本目录**物理隔离**于主项目（不 import 主项目代码、不进 `scripts/test.sh` 测试主干），
> 与 IM 域、协作域同为「旁路模块」，但更进一步：连构建、依赖、CI 都自成一体。
>
> 长期约定（更新通道、CI/CD、签名公证、内核安装通道调研）见 **`docs/desktop.md`**。

## 架构一览

```
Electron 主进程（薄壳，TypeScript）
 ├─ ⓪ 首启物化：安装包内置代码 → releases/<版本>/ + current 软链（bootstrap.ts，
 │    复刻 Docker entrypoint 的托管模式布局 —— selfupdate.py 靠它识别「可一键更新」）
 ├─ ① 选端口：8010 优先，被占退随机（ports.ts）
 ├─ ② Python 运行时：embedded 解释器 → venv → pip 装依赖（pythonRuntime.ts，首启联网，之后零网络）
 ├─ ②' Node 运行时：embedded Node + npm，全局前缀 / 缓存在 userData（runtimeEnv.ts，M4）
 ├─ ③ spawn 后端：uvicorn --host 127.0.0.1 --app-dir releases/current/backend（backend.ts，只听本机）
 ├─ ④ 健康检查门控：轮询 /api/health（health.ts）
 ├─ ⑤ BrowserWindow.loadURL(127.0.0.1:<port>)（index.ts）
 ├─ ⑥ 托盘常驻 + 单实例锁（关窗 = 隐藏，后端继续跑）
 ├─ ⑦ execv 看门狗：后端界面内自更新后同 PID 换血 → 自动刷新窗口（backend.ts:watchBackendRestart）
 └─ ⑧ 壳自更新：electron-updater + 滚动 Release `desktop-latest`（updater.ts，M3）
```

**目录布局**（安装后 userData，与 Docker 卷设计对齐）：

| 目录 | 内容 | 删了会怎样 |
| --- | --- | --- |
| `userData/data/` | `ARKSWARM_DATA_DIR`：SQLite、工作区、`secret.key`、内核 HOME | ⚠️ 数据全丢（与 Docker 的 `arkswarm-data` 卷同级） |
| `userData/python/` | embedded 解释器 + venv | 下次启动自动重建（离线秒级） |
| `userData/node/` | npm 全局安装前缀（内核 CLI 装在这）+ 缓存 | 内核需要重装，对话数据不受影响 |
| `userData/releases/` | 后端版本目录（预留，接主项目自更新） | 回滚历史丢失，不影响运行 |

## 开发

```bash
cd desktop
npm install           # 装 electron / electron-builder / typescript
npm run dev           # 编译主进程 + 启动 Electron（后端用仓库源码 + 系统 python3）
```

> 开发模式要求本机 python3 ≥ 3.11（与主项目一致），并用系统 node / npm（不注入内置运行时）。
> 打包版内嵌解释器与 Node，用户不需要装 Python / Node。

## 打包

```bash
cd desktop
node scripts/fetch-python.mjs        # 下载 python-build-standalone → build/python-runtime/<arch>/
node scripts/fetch-node.mjs          # 下载官方 Node 22 LTS（校验 sha256）→ build/node-runtime/<arch>/
node scripts/prepare-backend.mjs     # 确保 frontend/dist 存在
npm run compile
npx electron-builder --publish never # 产物在 release/
```

- **Windows**：NSIS 安装器（x64）；未签名时 SmartScreen 会拦，文档引导「仍要运行」
- **macOS**：DMG + zip（arm64 + x64）；未签名时首次打开需右键 →「打开」
- 签名与公证的接线已就绪（配好证书 / Apple 凭据即自动生效），步骤见 `docs/desktop.md` §4

## CI/CD

`.github/workflows/desktop.yml`：

- PR / push 改动 `desktop/**`、`backend/**`、`frontend/**` → Windows + macOS 双平台构建，
  并对**打包产物**跑冒烟启动（`ARKSWARM_DESKTOP_SMOKE=1`：内嵌 Python 建 venv → 起后端 →
  健康检查 → 退出 0）；
- 打 tag `desktop-v*` → 构建产物与 `latest*.yml` 上传到滚动 Release `desktop-latest`
  （**prerelease**，避免抢走后端更新器的 `/releases/latest`）。

## 壳更新通道（要点）

壳与后端是两条版本线、共用同一个 GitHub 仓库。因此壳更新用 **Generic Provider** 直读
滚动 tag `desktop-latest`，与 GitHub「最新 Release」语义无关；所有桌面 Release 一律
prerelease。细节与设计原因见 `docs/desktop.md` §2。

## 与主项目的边界（重要）

| 事项 | 约定 |
| --- | --- |
| 依赖方向 | 桌面壳**只调用**后端 HTTP API（`/api/health` 等），不 import 主项目任何 Python/TS 代码 |
| 后端改动 | 桌面版需要的后端适配（若有）走主项目正常 PR 流程，桌面壳只读仓库 `backend/` 源码 |
| 测试 | `desktop/` 不进 `scripts/test.sh`；本目录 `npm run typecheck` 自证，CI 另跑打包版冒烟 |
| 文档 | 长期文档进 `docs/`（`docs/desktop.md`）；`desktop/README.md` 保持自包含 |
| 版本 | 壳版本（package.json `version`）与后端 `__version__` 独立演进；后端更新走主项目 releases 机制（界面内一键升级） |

## 里程碑

- [x] M1 最小壳：spawn 后端 + 健康检查 + 加载页面
- [x] M2 托盘常驻 + 单实例锁 + 托管模式布局（releases/ + current，一键更新可用）
      + execv 看门狗 + Windows 进程树清理
- [x] M3 CI/CD：GitHub Actions 双平台构建 + 打包版冒烟；electron-updater 壳自身更新
      （Generic Provider + 滚动 prerelease Release）
- [x] M4 内核 provision 的 Node 依赖：内嵌 Node + npm、App 私有全局前缀（调研结论见
      `docs/desktop.md` §5）；npm 命令跨平台化（Windows 无 `sh`、`.cmd` 垫片）；
      签名 / 公证接线（afterPack 逐签内嵌运行时 + afterSign 公证，配证书即生效）

## 已验证项

| 验证 | 结果 |
| --- | --- |
| `npm run typecheck`（strict）+ `npm run compile` | 通过（M3/M4 后复跑） |
| venv 构建链路（python 3.11 + requirements.txt） | ✅ 真实执行通过，fastapi/uvicorn/cryptography 全可导入 |
| 后端桌面形态启动（`ARKSWARM_DATA_DIR` + 127.0.0.1 + `--app-dir`） | ✅ `/api/health` 200，数据目录布局与 Docker 卷一致 |
| 优雅停止（SIGTERM → 端口释放） | ✅ 杀掉后端口无响应，无孤儿进程 |
| electron-builder `--dir` 打包 | ✅ 资源布局正确（app-backend 同构 tar.gz） |
| 内置 Node 链路（M4） | ✅ 下载校验 sha256 → node v22.23.5 / npm 10.9.9 → 私有 prefix `npm install -g` → 垫片在 PATH 可执行 |
| npm 包装命令（Node 实现） | ✅ `pytest tests/test_runtimes.py` 真实跑通（挪占位 / 失败回滚 / 不误伤自有链接） |
| Windows `.cmd` 启动形态 | ✅ `pytest tests/test_runner.py`（`_spawn_argv` 的 Windows 分支单测） |
| 打包版冒烟（真实安装包） | CI 矩阵（Windows / macOS）各跑一次 —— 本机无图形栈，按 M2 的约定排进 CI |