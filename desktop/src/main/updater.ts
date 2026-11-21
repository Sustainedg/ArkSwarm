/**
 * 壳自身的版本更新（electron-updater）。
 *
 * 为什么壳更新要单开一条通道、不能复用后端自更新：
 *   - 壳与后端是**两条独立版本线**（package.json 的版本 vs 后端 `__version__`）；
 *   - 同一个 GitHub 仓库里，后端发的是 `coda.tar.gz`，壳发的是 NSIS / DMG / zip
 *     + `latest*.yml`。electron-updater 的 GitHub Provider 认的是仓库「最新 Release」——
 *     后端一发版，它就会去那个 Release 里找 `latest.yml`，然后一头撞上 404。
 *
 * 因此壳这边改用 **Generic Provider 指向一个滚动 Release**（tag `desktop-latest`，
 * 每次桌面构建覆盖上传）。关键约定：这个 Release 必须发布为 **prerelease**，
 * 否则 GitHub 的 `/releases/latest` 会指到它，把后端「设置 → 关于 → 检测更新」带沟里。
 *
 * 更新形态只有一种：**下载完整的桌面安装包**（Windows NSIS / macOS zip）。Windows
 * 开了差分（`.blockmap`，只下变化的块），但拼出来的仍是完整安装包；不存在「只换
 * 后端源码」的轻量通道。托盘菜单与「设置 → 关于」的更新入口共用本模块的同一套状态机。
 *
 * macOS 的自动更新由 Squirrel.Mac 执行，**要求 App 已签名**；未签名构建里
 * 自动更新不可用，这里降级为「提示用户去发布页手动下载」，绝不让检查更新把应用搞崩。
 */
import { app, BrowserWindow, dialog, ipcMain } from 'electron';
import { spawnSync } from 'node:child_process';
import { autoUpdater } from 'electron-updater';

/** 滚动发布地址：每次桌面构建把安装包与 latest*.yml 覆盖上传到这个 tag。 */
const DEFAULT_UPDATE_URL =
  'https://github.com/Sustainedg/Coda/releases/download/desktop-latest';

/** 手动下载的兜底发布页（未签名 macOS 构建、自动更新失败时引导用户去这里）。 */
export const RELEASE_PAGE = 'https://github.com/Sustainedg/Coda/releases';

/** 自动检查的延迟与周期：启动 20 秒后一次，之后每 6 小时一次。 */
const FIRST_CHECK_DELAY_MS = 20_000;
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

/** 桌面端更新状态机的阶段。渲染层（关于页）与托盘共用。 */
export type DesktopUpdatePhase =
  | 'idle' // 还没检查过
  | 'checking' // 正在检查
  | 'downloading' // 发现新版本，后台下载中
  | 'downloaded' // 下载完成，等待重启安装
  | 'up-to-date' // 远端没有更新
  | 'error' // 检查 / 下载失败
  | 'unsupported'; // 当前构建不具备自动更新能力（开发模式 / macOS 未签名 / 平台不支持）

/** 渲染层「关于」页需要的完整更新状态（IPC 直接返回，字段保持 snake_case 与前端一致）。 */
export interface DesktopUpdateState {
  /** 当前构建是否具备自动更新能力。 */
  supported: boolean;
  /** 壳自身的版本号（desktop/package.json 的 version，形如 0.1.3）。 */
  current_version: string;
  phase: DesktopUpdatePhase;
  /** 远端可用的新版本号；没发现 / 已是最新时为 null。 */
  available_version: string | null;
  /** 下载进度（0-100）；不在下载阶段时为 0。 */
  percent: number;
  /** 失败原因（phase === 'error' / 'unsupported' 时给出）。 */
  error: string | null;
}

let started = false;
let busy = false;
/** 本次检查是用户主动触发的（决定「已是最新」要不要弹窗）。 */
let manualRequest = false;
/** 本次检查来自「关于」页（结果由页面自己渲染，不再弹系统对话框）。 */
let uiRequest = false;
/** 已经弹过「重启安装」的版本号，避免同一版本反复打扰。 */
let promptedVersion: string | null = null;
let interval: NodeJS.Timeout | null = null;
let firstTimer: NodeJS.Timeout | null = null;

/** 共享状态：托盘与关于页读同一份，事件里只做增量更新。 */
let state: DesktopUpdateState = {
  supported: false,
  current_version: '',
  phase: 'idle',
  available_version: null,
  percent: 0,
  error: null,
};

function log(message: string): void {
  process.stdout.write(`[updater] ${message}\n`);
}

/** 更新源地址（fork / 私有部署可用环境变量覆盖）。 */
export function updateFeedUrl(): string {
  const override = process.env.CODA_DESKTOP_UPDATE_URL?.trim();
  return override || DEFAULT_UPDATE_URL;
}

/**
 * macOS 上当前这份 App 是否带有效签名。
 * Squirrel.Mac 只接受已签名应用；未签名时自动更新必然失败，必须提前识别。
 */
function macSigned(): boolean {
  // 新版本 Electron 提供了官方 API（Electron 33 还没有，不能硬依赖）
  type SigningInfo = { existence?: boolean; status?: string };
  const probe = (app as unknown as { getCodeSigningInfo?: () => SigningInfo | null })
    .getCodeSigningInfo;
  if (typeof probe === 'function') {
    try {
      const info = probe.call(app);
      if (info && typeof info.existence === 'boolean') {
        return info.existence && info.status !== 'invalid';
      }
    } catch {
      /* 落到命令行探测 */
    }
  }
  // 兜底：问 codesign 自己。`-dv` 的详情输出在 stderr，两个流一起看。
  // 只认 Developer ID Application —— ad-hoc 签名过不了 Squirrel.Mac 的校验。
  try {
    const result = spawnSync('codesign', ['-dv', '--verbose=2', process.execPath], {
      encoding: 'utf8',
    });
    const text = `${result.stdout ?? ''}${result.stderr ?? ''}`;
    return result.status === 0 && text.includes('Authority=Developer ID Application');
  } catch {
    return false;
  }
}

/** 当前构建是否具备自动更新能力（打包版 + 平台支持 + macOS 已签名）。 */
export function autoUpdateSupported(): boolean {
  if (!app.isPackaged) return false;
  if (process.platform === 'win32') return true; // NSIS 未签名也能更新（只是 SmartScreen 会提示）
  if (process.platform === 'darwin') return macSigned();
  return false; // 桌面版只承诺 Windows / macOS
}

/** 把最新状态推给所有窗口（关于页据此实时渲染进度）。 */
function broadcastState(): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send('desktop-update-state', state);
    }
  }
}

/** 合并一次状态变更并广播。 */
function setState(patch: Partial<DesktopUpdateState>): void {
  state = { ...state, ...patch };
  broadcastState();
}

/** 当前状态快照（IPC 用；渲染层首屏拿一次，之后靠事件推送）。 */
export function getUpdateState(): DesktopUpdateState {
  return state;
}

/** 用户手点「检查更新」时，把结论以弹窗告诉他（自动检查只写日志）。 */
async function notify(message: string, detail?: string): Promise<void> {
  await dialog.showMessageBox({
    type: 'info',
    title: '桌面端更新',
    message,
    detail,
    buttons: ['知道了'],
    noLink: true,
  });
}

async function promptInstall(version: string): Promise<void> {
  if (promptedVersion === version) return;
  promptedVersion = version;
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: '发现新版本',
    message: `Coda 桌面端 ${version} 已下载完成`,
    detail: '重启应用即可完成更新。选择「稍后」也没关系：退出应用时同样会自动安装。',
    buttons: ['立即重启安装', '稍后'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  if (response === 0) {
    log('用户选择立即重启安装');
    autoUpdater.quitAndInstall();
  }
}

/** 初始化自动更新（幂等；开发模式与不支持的平台只写一行日志）。 */
export function setupAutoUpdate(): void {
  if (started) return;
  started = true;

  state = {
    ...state,
    supported: autoUpdateSupported(),
    current_version: app.getVersion(),
  };

  if (!app.isPackaged) {
    log('开发模式：跳过壳更新检查');
    setState({ phase: 'unsupported', error: '开发模式不检查桌面端更新，打包后的版本才连接更新源' });
    return;
  }
  if (!autoUpdateSupported()) {
    const reason =
      process.platform === 'darwin'
        ? 'macOS 自动更新需要已签名的应用；请到发布页手动下载新版本'
        : '当前平台不支持自动更新';
    log(`当前构建不支持自动更新（${reason}），仅保留手动下载入口`);
    setState({ phase: 'unsupported', error: reason });
    return;
  }

  try {
    autoUpdater.setFeedURL({ provider: 'generic', url: updateFeedUrl() });
  } catch (err) {
    // 更新源配置坏掉也不该拖垮启动：壳照常用，只是没有自动更新
    log(`更新源配置失败，跳过自动更新：${String(err)}`);
    setState({ phase: 'error', error: `更新源配置失败：${String(err)}` });
    return;
  }
  // 后台自动下载，下载完再问用户何时重启 —— 不阻塞启动，也不打断正在跑的任务
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('checking-for-update', () => {
    log('检查更新中…');
    setState({ phase: 'checking', error: null });
  });
  autoUpdater.on('update-available', (info) => {
    log(`发现新版本 ${info.version}，开始后台下载`);
    setState({ phase: 'downloading', available_version: info.version, percent: 0, error: null });
  });
  autoUpdater.on('update-not-available', () => {
    log('已是最新版本');
    setState({ phase: 'up-to-date', available_version: null, percent: 0 });
    if (manualRequest && !uiRequest) {
      manualRequest = false;
      void notify(`已是最新版本（${app.getVersion()}）`);
    }
    manualRequest = false;
    uiRequest = false;
  });
  let reportedPercent = -10;
  autoUpdater.on('download-progress', (progress) => {
    setState({ phase: 'downloading', percent: clampPercent(progress.percent) });
    // 只按 10% 打点一次（progress 事件很密），避免刷屏
    const bucket = Math.floor(progress.percent / 10) * 10;
    if (bucket > reportedPercent) {
      reportedPercent = bucket;
      log(`下载进度 ${bucket}%（${Math.round(progress.bytesPerSecond / 1024)} KB/s）`);
    }
  });
  autoUpdater.on('update-downloaded', (info) => {
    log(`新版本 ${info.version} 下载完成`);
    setState({
      phase: 'downloaded',
      available_version: info.version,
      percent: 100,
      error: null,
    });
    // 关于页会自己显示「重启安装」按钮，不再叠一层系统弹窗；
    // 自动检查（后台）与托盘手动检查仍用弹窗提示。
    if (uiRequest) {
      uiRequest = false;
      manualRequest = false;
      return;
    }
    manualRequest = false;
    void promptInstall(info.version);
  });
  autoUpdater.on('error', (err) => {
    const reason = (err as Error)?.message ?? String(err);
    log(`更新失败：${reason}`);
    setState({ phase: 'error', error: reason });
    if (manualRequest && !uiRequest) {
      void notify('检查更新失败', `${reason}\n\n也可以到发布页手动下载：${RELEASE_PAGE}`);
    }
    manualRequest = false;
    uiRequest = false;
  });

  /** 后台定时检查：正在下载 / 已下载时不重复触发（避免打断当前进度）。 */
  const autoCheck = (): void => {
    if (busy) return;
    if (state.phase === 'downloading' || state.phase === 'downloaded') return;
    void autoUpdater.checkForUpdates().catch((err: unknown) => log(`自动检查失败：${String(err)}`));
  };

  firstTimer = setTimeout(autoCheck, FIRST_CHECK_DELAY_MS);
  firstTimer.unref?.();

  interval = setInterval(autoCheck, CHECK_INTERVAL_MS);
  interval.unref?.();
}

/** 0-100 取整并夹紧（electron-updater 偶有浮点越界）。 */
function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

/**
 * 托盘菜单「检查桌面端更新…」的入口：结果用系统弹窗告知（窗口可能是隐藏的）。
 */
export async function checkForUpdatesManually(): Promise<void> {
  if (!app.isPackaged) {
    await notify('开发模式不检查桌面端更新', '打包后的版本才会连接更新源。');
    return;
  }
  if (!autoUpdateSupported()) {
    await notify(
      '当前构建无法自动更新',
      (process.platform === 'darwin'
        ? 'macOS 自动更新需要已签名的应用。'
        : '当前平台不支持自动更新。') + `\n请到发布页手动下载新版本：${RELEASE_PAGE}`,
    );
    return;
  }
  if (busy) return; // 上一轮还没结束，重复点击直接忽略
  if (state.phase === 'downloading') return; // 正在下载：不重复触发
  if (state.phase === 'downloaded' && state.available_version) {
    // 已经下好了：直接把「重启安装」弹窗再给出来（promptInstall 会去重）
    void promptInstall(state.available_version);
    return;
  }
  busy = true;
  manualRequest = true;
  uiRequest = false;
  try {
    await autoUpdater.checkForUpdates();
  } catch (err) {
    manualRequest = false;
    log(`手动检查失败：${String(err)}`);
    setState({ phase: 'error', error: String(err) });
    await notify('检查更新失败', `${String(err)}\n\n手动下载：${RELEASE_PAGE}`);
  } finally {
    busy = false;
  }
}

/**
 * 「设置 → 关于」的检查入口：与托盘共用同一个 electron-updater 通道，
 * 区别只在结果展示 —— 页面读状态、不弹系统对话框。
 */
export async function checkForUpdatesFromUi(): Promise<DesktopUpdateState> {
  if (!app.isPackaged || !autoUpdateSupported()) {
    // setupAutoUpdate 已经把「为什么不支持」写进 state.error，这里原样返回。
    return state;
  }
  if (busy) return state; // 后台检查进行中：直接回报当前状态
  // 正在下载 / 已经下好：不再触发新检查，页面照常显示进度或「重启安装」
  if (state.phase === 'downloading' || state.phase === 'downloaded') return state;
  busy = true;
  manualRequest = true;
  uiRequest = true;
  try {
    await autoUpdater.checkForUpdates();
  } catch (err) {
    log(`关于页检查失败：${String(err)}`);
    setState({ phase: 'error', error: String(err) });
    manualRequest = false;
    uiRequest = false;
  } finally {
    busy = false;
  }
  return state;
}

/** 「关于」页点「重启安装」：与托盘弹窗里的「立即重启安装」是同一个动作。 */
export function installDownloadedUpdate(): void {
  if (state.phase !== 'downloaded') return;
  log('关于页请求重启安装');
  autoUpdater.quitAndInstall();
}

/** 注册更新相关 IPC（渲染层只能调这三个白名单通道）。 */
export function registerUpdateIpc(): void {
  state = { ...state, current_version: app.getVersion(), supported: autoUpdateSupported() };
  ipcMain.handle('desktop-update:status', () => getUpdateState());
  ipcMain.handle('desktop-update:check', () => checkForUpdatesFromUi());
  ipcMain.handle('desktop-update:install', () => {
    installDownloadedUpdate();
  });
}

/** 退出时清掉定时器（避免句柄挂住进程）。 */
export function disposeAutoUpdate(): void {
  if (firstTimer) {
    clearTimeout(firstTimer);
    firstTimer = null;
  }
  if (interval) {
    clearInterval(interval);
    interval = null;
  }
}