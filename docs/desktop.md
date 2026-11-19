# 桌面版（Electron 壳）· 长期文档

> 目录 `desktop/` 是**物理隔离**的旁路模块：不 import 主项目代码、不进 `scripts/test.sh`、
> 构建 / 依赖 / CI 自成一体。`desktop/README.md` 保持自包含的入门说明；本文记录长期约定、
> CI/CD、壳更新通道、签名公证与内核安装通道的调研结论。

---

## 1. 运行形态

```
Electron 主进程（薄壳）
 ├─ 首启 / 整包升级物化：安装包内代码 → userData/releases/<版本>/ + current 软链（bootstrap.ts）
 ├─ 选端口：8010 优先，被占退随机（ports.ts）
 ├─ Python 运行时：内嵌解释器 → venv → pip 装依赖（pythonRuntime.ts；按 requirements 的
 │    sha256 指纹判断，变了才重装 —— 整包升级引入新依赖时不会漏装）
 ├─ Node 运行时：内嵌 Node + npm，全局前缀钉在 userData（runtimeEnv.ts，M4）
 ├─ spawn 后端：uvicorn --host 127.0.0.1 --app-dir releases/current/backend
 ├─ 健康检查门控 → loadURL(127.0.0.1:<port>)
 ├─ execv 看门狗：后端同 PID 换血后刷新窗口（兼容旧版 / 直接调用自更新 API）
 ├─ 壳自更新：electron-updater + 滚动 Release（updater.ts；托盘与「设置 → 关于」共用）
 └─ 托盘常驻：关窗 = 隐藏，后端继续跑（IM / 协作 / 生成不中断）
```

### 目录布局（userData，与 Docker 卷设计对齐）

| 目录 | 内容 | 删了会怎样 |
| --- | --- | --- |
| `data/` | `ARKSWARM_DATA_DIR`：SQLite、工作区、`secret.key`、内核隔离配置 | ⚠️ 数据全丢 |
| `python/` | 内嵌解释器 + venv | 下次启动自动重建（离线秒级 + 首次联网装依赖） |
| `node/global/` | npm 全局安装前缀（内核 CLI 装在这里）+ `node/cache/` | 内核需要重装，不影响对话数据 |
| `releases/` | 后端版本目录 + `current` 指针（复用主项目自更新机制） | 回滚历史丢失，不影响运行 |

> 应用图标与托盘图标由 `desktop/scripts/gen-icons.py` 生成，几何直接复用网页
> `scripts/gen_icons.py`（三节点星座 + 轨道弧）—— 桌面端与网页的图标是同一张图。
> 改了品牌图形后跑 `npm run gen:icons` 重新生成；macOS 托盘是模板图（纯黑 + alpha），
> Windows/Linux 用品牌色。

> 安装包卸载时 `deleteAppDataOnUninstall: false`：对话数据与内核安装都在 userData，卸载应用不该带走。

---

## 2. 两条版本线 · 两条更新通道（最容易踩的坑）

| | 后端 | 桌面壳 |
| --- | --- | --- |
| 版本源 | `backend/app/__init__.py` 的 `__version__`（V0.1.x） | `desktop/package.json` 的 `version`（0.1.x，**与后端同号**，发布时 CI 双向校验） |
| 产物 | `arkswarm.tar.gz` + `.sha256` | NSIS / DMG / zip + `latest*.yml` |
| 发布 tag | `v0.1.3`（普通 Release，含网页/后端源码） | 安装包**追加到同一个 `v0.1.3` Release**；另有 `desktop-latest`（**滚动**，prerelease，壳自更新读它） |
| 更新器 | `backend/app/selfupdate.py`，读 `/releases/latest` | `electron-updater`，Generic Provider 直读滚动 tag 下的 `latest*.yml` |
| 生效方式 | 下载 → `releases/<版本>/` → 切 current → `os.execv` 同 PID 重启 | 下载**完整安装包** → 重启 App（NSIS / Squirrel.Mac）；启动时把新安装包里的后端物化并切 current |
| 界面入口 | Docker / 网页部署：「设置 → 关于」 | 桌面端：「设置 → 关于」**与托盘菜单共用** electron-updater（不再走后端 tar.gz 通道） |

**为什么必须隔离**：两条通道都跑在 `Sustainedg/ArkSwarm`，而 GitHub 的 `/releases/latest`
是「全仓库最新一个非 prerelease 非 draft 的 Release」。如果壳的版本 Release 成了 latest：

- 后端更新器会去那个 Release 里找 `arkswarm.tar.gz` → 落空（「没有可安装的版本」）；
- 壳更新器（GitHub Provider 模式）会去后端 Release 里找 `latest.yml` → 404。

因此约定：

1. **壳的所有 Release 都是 prerelease**（`gh release create --prerelease`），GitHub 的
   `/releases/latest` 永远指向最后发的后端版本；
2. 壳更新用 **Generic Provider**（`electron-builder.yml` 的 `publish`），地址固定为
   `https://github.com/Sustainedg/ArkSwarm/releases/download/desktop-latest/`，与 latest 语义无关；
3. 滚动 tag `desktop-latest` 的资产由 CI **覆盖上传**（`gh release upload --clobber`）；
4. fork / 私有部署可用 `ARKSWARM_DESKTOP_UPDATE_URL` 覆盖更新源。

**桌面端只有一条更新通道**：托盘「检查桌面端更新…」与「设置 → 关于 → 检测更新」共用
`updater.ts` 的同一套状态机，下载的都是**完整安装包**（Windows 走 `.blockmap` 差分，
只传变化的数据块，但拼出来的仍是完整安装包）。新安装包自带的后端代码由 `bootstrap.ts`
在下次启动时物化成 `releases/<新版本>` 并切 `current`；若 `requirements.txt` 内容变了
（sha256 指纹不同），`pythonRuntime.ts` 会重装 venv 依赖。后端的「一键更新」
（`arkswarm.tar.gz` + `os.execv`）只服务 Docker / 网页部署，桌面端界面不再暴露。

**macOS 前提**：Squirrel.Mac 只接受已签名应用。未签名构建里 `updater.ts` 会识别
（优先 `app.getCodeSigningInfo()`，Electron 33 没有该 API 时用 `codesign -dv` 探测
`Authority=Developer ID Application`）并降级为「请到发布页手动下载」，不会弹错误或崩溃。
Windows 的 NSIS 未签名也能更新（仅 SmartScreen 提示）。

---

## 3. CI/CD（`.github/workflows/desktop.yml`）

| 触发 | 行为 |
| --- | --- |
| PR / push（`desktop/**`、`backend/**`、`frontend/**` 有改动） | 双平台构建 + 打包版冒烟，不发布 |
| push tag `v*`（后端/网页发版） | 构建 + 冒烟，安装包追加到同一个 `v<版本>` Release，并刷新 `desktop-latest` |
| `workflow_dispatch` + 勾选 publish | 同上（不发 tag 也能发布） |

矩阵：

- `windows-latest`：`--win --x64`，下载 `win-x64` 的 Python / Node；
- `macos-latest`（arm64）：`--mac --arm64 --x64` 一次构建双架构。**必须放在同一次
  electron-builder 调用里**：`latest-mac.yml` 只有在同一次构建内才会合并两个架构的 zip
  条目（app-builder-lib 按同名 update-info 文件合并 `files`）；拆开调用会互相覆盖。
  因此 mac 任务要下载 `mac-arm64` 与 `mac-x64` 两份 Python / Node。

### 发版（与网页/后端同一个版本号、同一个 Release）

```bash
# 1) 改 backend/app/__init__.py 的 __version__（唯一版本源）与
#    desktop/package.json 的 version（去掉 V，例如 V0.1.3 ↔ 0.1.3）
# 2) 提交后跑 release.sh —— 它会校验两个版本号一致，并创建 v<版本> Release
bash scripts/release.sh
# 3) 推送 tag；桌面端 CI 会自动构建，把 Win/mac 安装包追加到同一个 Release，
#    并刷新滚动 Release desktop-latest（壳自更新用）
git push origin v0.1.3
```

### 冒烟（smoke）

打包产物以 `ARKSWARM_DESKTOP_SMOKE=1` 启动：主进程只跑「运行时 → 后端 → `/api/health`」，
不建窗口 / 托盘，就绪后打印 `[desktop] smoke ok version=… port=…` 并退出 0；失败退出 1，
10 分钟总看门狗退出 2。它验证的是**真实安装包**里的整条链路（内嵌 Python 建 venv、
装依赖、起 uvicorn），而不是源码模式。

### 本地打包

```bash
cd desktop
node scripts/fetch-python.mjs   # 按当前平台下载（产物进 build/python-runtime/<arch>/）
node scripts/fetch-node.mjs     # 同上（build/node-runtime/<arch>/）
node scripts/prepare-backend.mjs
npm run compile && npx electron-builder --publish never
```

发版只在 CI 做（覆盖上传滚动 Release），不要本地 `--publish always`。

---

## 4. 签名与公证

> 当前公开构建仍是 **unsigned**：Windows SmartScreen 会拦（README 引导「仍要运行」），
> macOS 首次打开需右键「打开」。下面的接线在**配置证书后自动生效**，不需要改代码。

### 4.1 CI Secrets 清单

| Secret | 平台 | 用途 |
| --- | --- | --- |
| `CSC_LINK` | Win + mac | 证书文件（`.pfx` / `.p12` 的 base64，或可访问的路径）。electron-builder 通用约定 |
| `CSC_KEY_PASSWORD` | Win + mac | 证书密码 |
| `APPLE_ID` | mac | Apple 开发者账号 |
| `APPLE_APP_SPECIFIC_PASSWORD` | mac | App 专用密码（不是登录密码） |
| `APPLE_TEAM_ID` | mac | 团队 ID |

另可用 `CSC_NAME` 显式指定钥匙串里的签名身份（默认取第一个 `Developer ID Application`）。

### 4.2 Windows

拿到代码签名证书（OV/EV）→ 导出 `.pfx` → base64 后放进 `CSC_LINK`，密码进
`CSC_KEY_PASSWORD`。electron-builder 自动签 `ArkSwarm-<版本>-win-x64.exe`。
Azure Trusted Signing 属于另一条路（`win.azureSignOptions`），本文不展开。

> NSIS 保持 `perMachine: false`：electron-updater 的静默更新要求安装目录对当前用户可写，
> 装到 `Program Files`（machine 级）会需要提权、静默更新直接失败。

### 4.3 macOS

1. Apple Developer Program → 创建 **Developer ID Application** 证书 → 导出 `.p12`；
2. `.p12` base64 → `CSC_LINK`，密码 → `CSC_KEY_PASSWORD`；再配 `APPLE_ID` /
   `APPLE_APP_SPECIFIC_PASSWORD` / `APPLE_TEAM_ID`；
3. 构建时的顺序（配置见 `electron-builder.yml`）：
   - `hardenedRuntime: true` + `build/entitlements.mac.plist`（JIT / 可执行内存 /
     禁用库校验 / 本机网络服务 —— 内嵌 Python、Node 作为子进程运行的既定组合）；
   - `afterPack: scripts/sign-runtimes.cjs`：**逐个签名内嵌 Python / Node 里的 Mach-O**。
     electron-builder 虽然会扫包内二进制，但按「内容像不像二进制」启发式判定（会把 `.pyc`
     之类也当二进制丢给 `codesign`），所以用 `mac.signIgnore` 把两份运行时交给专用脚本：
     只签 `file` 认定的 Mach-O，并统一套 runtime entitlements。漏签一个 dylib，公证就整包失败；
   - 正常签名（electron-builder，已跳过上面两个目录）；
   - `afterSign: scripts/notarize.cjs`：提交 Apple 公证（未配置凭据时直接跳过）。
4. 验证：

```bash
codesign --verify --deep --strict --verbose=2 "ArkSwarm.app"
spctl --assess --type execute --verbose "ArkSwarm.app"   # 应显示 accepted / notarized
xcrun stapler validate "ArkSwarm.app"                    # 确认已装订
```

签名生效后，macOS 自动更新随之可用（`updater.ts` 会自动识别签名状态）。

---

## 5. 内核安装通道（M4 调研结论）

### 5.1 问题

「设置 → 内核管理」安装 CLI 内核时跑的是 `npm install -g`，内核本身也是 Node 程序。
桌面用户机器上**可能没有 Node**；而且：

- macOS 上全局 npm 前缀默认是 `/usr/local` / 只读的 `.app` 内部 → `EACCES`；
- Windows 上没有 `sh`，原来的包装命令 `["sh", "-c", …]` 直接「未找到命令 sh」；
- Windows 上 npm 是 `npm.cmd`，`CreateProcess` 不认识 `.cmd`，直连也会失败；
- 裸 `claude` / `codex` 在 Windows 上是 `.cmd` 垫片，执行层 `create_subprocess_exec` 同样跑不起来。

### 5.2 原生二进制通道调研（2026-11 实测 GitHub Releases）

| 内核 | 存在的原生资产 | 体积（单平台） | Windows 覆盖 |
| --- | --- | --- | --- |
| Claude Code | `claude-win32-x64.zip`、`claude-darwin-arm64.tar.gz`、`claude-darwin-x64.tar.gz`、linux 四份 | 158–173 MB | ✅ |
| Codex | `codex-x86_64-pc-windows-msvc.exe.zip`、`codex-*-apple-darwin.tar.gz`、linux musl | 115–215 MB | ✅ |
| Gemini CLI | 只有 `gemini-darwin-{arm64,x64}-unsigned.zip` + `gemini-cli-bundle.zip` | 21–38 MB | ❌ 没有 Windows 版 |

结论：

- 三个内核的 npm 包里装的**就是同一份原生二进制**（体积大头一致），直接走原生通道不省空间；
- Gemini CLI 缺 Windows 原生资产，原生通道无法覆盖全部内核；
- 官方自更新口径仍是 `npm i -g` / 内核自己的 update 命令，绕开 npm 会丢这条升级路径。

**因此选型：内嵌 Node + npm**（与内嵌 Python 同一思路），既有 provisioner 声明原样工作，
一个通道覆盖三个内核；原生二进制通道留作后续（若将来 Gemini 补齐 Windows 资产，可在
适配器里为个别内核声明下载源，框架侧只执行声明）。

### 5.3 落地内容

- `scripts/fetch-node.mjs`：下载官方 Node 22 LTS（与 `docker/Dockerfile` 同版本）、
  校验 `SHASUMS256.txt`、按架构落到 `build/node-runtime/<arch>/`；
- `src/main/runtimeEnv.ts`：把「内置 Node 目录 + 托管 npm 全局 bin」前置进 PATH，
  并把 `npm_config_prefix` / `npm_config_cache` 钉到 userData；后端进程继承这份环境，
  provisioning 与内核执行自动生效；
- `backend/app/agents/builtin/util.py`：npm 包装脚本改用 **Node 内联脚本**（三平台同一套
  逻辑）；`npm_cli()` 负责卸载 / 查询版本（Windows 经 `cmd.exe /c npm`）；
- `backend/app/runtime/runner.py`：Windows 上把 `.cmd` / `.bat` 垫片交给 `cmd.exe /d /s /c`
  启动，真正的 `.exe` 换成解析后的绝对路径；
- `backend/app/runtime/processes.py`：结束内核统一走这里 —— Windows 上用
  `taskkill /T /F` 连进程树一起杀（`.cmd` 垫片的 pid 是 cmd.exe，只杀它会留下孤儿 node）；
- 桌面版 npm 全局前缀是**App 私有的**（`userData/node/global`），不与用户自己
  `%APPDATA%\npm` / `/usr/local` 里的同名命令抢位置。

> Hermes Agent 走官方 `curl | bash` 安装脚本，Windows 天然不可用（上游也不提供 Windows
> 安装方式）；在 Windows 上点安装会明确报「未找到命令 sh」，不会静默失败。

---

## 6. 已知限制

- **未签名构建**：Windows SmartScreen 拦截、macOS 需右键打开，macOS 自动更新不可用；
- **mac Intel**：构建双架构，但 CI 冒烟只跑 arm64（`macos-latest` 是 arm64 runner）；
- **Windows npm 内核**：`.cmd` 垫片已处理；若某个内核的 Windows 包不是 Node 程序
  （未来变化），需要按新通道重新评估；
- **内嵌运行时签名**依赖 cert；`afterPack` 钩子在没有证书时会跳过，行为等同 unsigned；
- 桌面版**不承诺 Linux**（electron-builder 的 AppImage 只是顺带产物）。

## 7. 排障

| 症状 | 先看 |
| --- | --- |
| 启动卡在「安装后端依赖」 | 网络 / 代理；`userData/python/venv` 可整目录删掉重建（对话数据不受影响） |
| 内核装了但启动报「未安装或不在 PATH」 | 是否用的是打包版（`resources/node-runtime` 是否存在）；`userData/node/global/bin` 是否在 PATH |
| 壳更新提示 404 / 找不到 latest.yml | 滚动 Release `desktop-latest` 是否还在、是否被误设成非 prerelease；`ARKSWARM_DESKTOP_UPDATE_URL` 是否被覆盖 |
| macOS 公证失败 | 日志里搜 `sign-runtimes`；确认内嵌运行时里没有漏签的 Mach-O；检查 entitlements |