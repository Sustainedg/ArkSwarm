/**
 * 主进程入口：应用生命周期 + 窗口/托盘 + 后端编排。
 *
 * 流程（方案 C）：
 *   ready → 选端口 → 初始化 Python 运行时 → spawn 后端 → 健康检查 → 加载页面
 *
 * 窗口策略（桌面版价值点）：关窗 = 隐藏到托盘，后端继续跑
 * → IM 在线、工单协作、生成中的回复都不中断。
 */
import { app, BrowserWindow, Tray, Menu, dialog, shell } from 'electron';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { findFreePort, isPortInUse } from './ports';
import { waitForHealth } from './health';
import { spawnBackend, watchBackendRestart, type BackendHandle } from './backend';
import { ensurePythonRuntime } from './pythonRuntime';
import { bootstrapBundledBackend } from './bootstrap';
import { ensureLayout, dataDir } from './paths';
import { checkForUpdatesManually, disposeAutoUpdate, setupAutoUpdate } from './updater';

/**
 * CI 冒烟模式（M3）：只跑「运行时 → 后端 → 健康检查」这条链路，不建窗口、不建托盘，
 * 就绪后打印一行标记并按退出码收工。npm 侧（Windows/macOS runner）用它验证安装包，
 * 因此这里刻意不碰任何 GUI 设施，避免无头环境里卡在窗口初始化上。
 */
const smokeMode = process.env.ARKSWARM_DESKTOP_SMOKE === '1';

/** 冒烟模式的总看门狗：卡住也要以非零退出，绝不让 CI 任务挂到超时。 */
const SMOKE_TIMEOUT_MS = 10 * 60 * 1000;

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let backend: BackendHandle | null = null;
let backendPort: number | null = null;
/** 启动失败时显示错误，不再自动重启循环（避免用户面对无限弹窗）。 */
let startupError: string | null = null;
/** execv 自更新看门狗（退出时清掉，防止句柄挂住进程）。 */
let restartWatcher: NodeJS.Timeout | null = null;

// ---- 单实例锁：两个后端抢同一个 SQLite 文件是不可接受的 ----
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 有人想开第二个实例 → 把现有窗口唤到前台
    showMainWindow();
  });
  void main();
}

async function main(): Promise<void> {
  await app.whenReady();
  ensureLayout();

  if (smokeMode) {
    // 不建窗口；只给整条链路一个总时限（依赖安装 + 首启可能花几分钟）
    setTimeout(() => {
      process.stderr.write('[desktop] smoke 超时：链路没有在时限内走完\n');
      app.exit(2);
    }, SMOKE_TIMEOUT_MS).unref?.();
  } else {
    createWindow();
  }

  try {
    // ⓪ 首启物化：安装包内置代码 → releases/<版本>/（托管模式布局，自更新的地基）
    const { ranDir } = bootstrapBundledBackend();
    // ① 端口：首选 8010（和其他宿主一致），被占则退到随机空闲端口
    backendPort = await pickPort();
    // ② Python 运行时（首启建 venv + 装依赖，之后零网络）
    await ensurePythonRuntime((p) => sendProgress(p.message));
    // ③ 起后端（cwd 留在 ranDir，保持与 entrypoint.sh 的 exec 行为一致）
    sendProgress('启动后端服务…');
    backend = spawnBackend({ port: backendPort, backendRoot: path.join(ranDir, 'backend') });
    // ④ 健康检查门控
    sendProgress('等待服务就绪…');
    const { version } = await waitForHealth(backendPort, { timeoutMs: 60_000 });
    sendProgress('就绪');
    if (smokeMode) {
      // 链路全部走通：打印机器可读的一行，CI 用退出码判定成败
      process.stdout.write(
        `[desktop] smoke ok version=${version} port=${backendPort} pid=${process.pid}\n`,
      );
      await backend?.stop();
      app.exit(0);
      return;
    }
    // ⑤ 托盘（关窗=隐藏到托盘的前提；图标缺失时降级跳过）
    createTray();
    // ⑥ 加载主界面
    if (mainWindow && !mainWindow.isDestroyed()) {
      await mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
    }
    // ⑦ execv 看门狗：界面里点「安装更新」后，后端同 PID 换血，这里负责刷新页面
    restartWatcher = watchBackendRestart(backendPort, version, () => {
      process.stdout.write(`[desktop] 后端已自更新 → 刷新窗口\n`);
      if (mainWindow && !mainWindow.isDestroyed()) {
        void mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
      }
    });
    // ⑧ 壳自身更新（electron-updater）：启动 20 秒后自动检查一次，之后每 6 小时一次
    setupAutoUpdate();
  } catch (err) {
    startupError = `${(err as Error).message ?? err}`;
    if (smokeMode) {
      process.stderr.write(`[desktop] smoke failed: ${startupError}\n`);
      await backend?.stop();
      app.exit(1);
      return;
    }
    showFatal(`启动失败：${startupError}`);
  }
}

/** 选端口：8010 被占（很可能是残留进程）则随机；两者都失败则报错。 */
async function pickPort(): Promise<number> {
  const defaultPort = 8010;
  if (!(await isPortInUse(defaultPort))) return defaultPort;
  process.stdout.write('[desktop] 8010 被占用（残留进程或并行实例），改用随机端口\n');
  return findFreePort();
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: '#0f1117',
    title: 'ArkSwarm',
    autoHideMenuBar: true,
    webPreferences: {
      // 壳内页面就是普通 Web 页面：不开 nodeIntegration、不关 webSecurity
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: path.join(__dirname, '..', 'dist-preload', 'index.js'),
    },
  });
  // 就绪前先显示启动页（内联 HTML，无网络依赖）
  void loadSplash();
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
  });
  // 关窗 = 隐藏到托盘（后端继续跑）
  mainWindow.on('close', (event) => {
    if (isQuitting) return; // 真退出走 will-quit
    event.preventDefault();
    mainWindow?.hide();
  });
}

/** 启动页（等待后端就绪时显示）。 */
async function loadSplash(): Promise<void> {
  const html = `<html><body style="margin:0;background:#0f1117;color:#e8eaf0;font-family:system-ui;
    display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px">
    <div style="font-size:42px">🐝</div>
    <div id="msg" style="font-size:14px;color:#9aa0b0">正在初始化…</div>
    </body></html>`;
  await mainWindow?.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
}

/** 启动进度文案推给启动页。 */
function sendProgress(message: string): void {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents) {
    void mainWindow.webContents
      .executeJavaScript(
        `document.getElementById('msg')?.textContent = ${JSON.stringify(message)}; undefined`,
      )
      .catch(() => undefined);
  }
}

function showMainWindow(): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
  }
}

/** 致命错误：弹窗告知并保留窗口可看日志（铁律 11：显式暴露）。 */
function showFatal(message: string): void {
  void dialog.showMessageBox({
    type: 'error',
    title: 'ArkSwarm 启动失败',
    message,
    detail: `数据目录：${dataDir()}\n可尝试删除运行时目录后重试（对话数据不受影响）。`,
  });
}

// ---- 托盘 ----
function createTray(): void {
  // M1 用生成的占位图（resources/icons/trayTemplate.png）；正式版换多分辨率正式图标
  const iconPath = path.join(__dirname, '..', 'resources', 'icons', 'trayTemplate.png');
  if (!fs.existsSync(iconPath)) {
    // 图标缺失时宁可不出托盘也不能崩（后面还有 Dock/任务栏兜底）；但必须留下日志线索
    process.stdout.write('[desktop] 托盘图标缺失，跳过托盘创建\n');
    return;
  }
  tray = new Tray(iconPath);
  tray.setToolTip(`ArkSwarm 正在运行（桌面端 ${app.getVersion()}）`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: `ArkSwarm ${app.getVersion()}`, enabled: false },
      { label: '打开主界面', click: () => showMainWindow() },
      { type: 'separator' },
      { label: '检查桌面端更新…', click: () => void checkForUpdatesManually() },
      { type: 'separator' },
      {
        label: '退出',
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on('click', () => showMainWindow());
}

let isQuitting = false;
app.on('before-quit', () => {
  isQuitting = true;
});

// ---- 退出清理：优雅停后端（只拦一次，stop 完成后放行） ----
let quitPending = false;
app.on('will-quit', (event) => {
  disposeAutoUpdate();
  if (restartWatcher) {
    clearInterval(restartWatcher);
    restartWatcher = null;
  }
  if (!backend || quitPending) return;
  quitPending = true;
  event.preventDefault();
  const handle = backend;
  backend = null; // 防重入：第二次 will-quit 直接放行
  void handle.stop().finally(() => app.quit());
});

// ---- macOS 惯例 ----
app.on('activate', () => {
  showMainWindow();
});

// ---- 开发快捷键 ----
app.on('web-contents-created', (_e, contents) => {
  contents.on('before-input-event', (event, input) => {
    // 打包版禁用 DevTools 快捷键；开发模式保留（isPackaged 判断）
    if (app.isPackaged && input.key === 'F12') event.preventDefault();
  });
});

// 外部链接交给系统浏览器（不在壳内导航走）
app.on('web-contents-created', (_e, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://127.0.0.1')) return { action: 'allow' };
    void shell.openExternal(url);
    return { action: 'deny' };
  });
});
