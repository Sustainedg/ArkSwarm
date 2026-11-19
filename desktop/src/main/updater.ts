/**
 * 壳自身的版本更新（electron-updater）。
 *
 * 为什么壳更新要单开一条通道、不能复用后端自更新：
 *   - 壳与后端是**两条独立版本线**（package.json 的版本 vs 后端 `__version__`）；
 *   - 同一个 GitHub 仓库里，后端发的是 `arkswarm.tar.gz`，壳发的是 NSIS / DMG / zip
 *     + `latest*.yml`。electron-updater 的 GitHub Provider 认的是仓库「最新 Release」——
 *     后端一发版，它就会去那个 Release 里找 `latest.yml`，然后一头撞上 404。
 *
 * 因此壳这边改用 **Generic Provider 指向一个滚动 Release**（tag `desktop-latest`，
 * 每次桌面构建覆盖上传）。关键约定：这个 Release 必须发布为 **prerelease**，
 * 否则 GitHub 的 `/releases/latest` 会指到它，把后端「设置 → 关于 → 检测更新」带沟里。
 *
 * macOS 的自动更新由 Squirrel.Mac 执行，**要求 App 已签名**；未签名构建里
 * 自动更新不可用，这里降级为「提示用户去发布页手动下载」，绝不让检查更新把应用搞崩。
 */
import { app, dialog } from 'electron';
import { spawnSync } from 'node:child_process';
import { autoUpdater } from 'electron-updater';

/** 滚动发布地址：每次桌面构建把安装包与 latest*.yml 覆盖上传到这个 tag。 */
const DEFAULT_UPDATE_URL =
  'https://github.com/Sustainedg/ArkSwarm/releases/download/desktop-latest';

/** 手动下载的兜底发布页（未签名 macOS 构建、自动更新失败时引导用户去这里）。 */
export const RELEASE_PAGE = 'https://github.com/Sustainedg/ArkSwarm/releases';

/** 自动检查的延迟与周期：启动 20 秒后一次，之后每 6 小时一次。 */
const FIRST_CHECK_DELAY_MS = 20_000;
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

let started = false;
let busy = false;
/** 本次检查是用户手点「检查更新」触发的（决定「已是最新」要不要弹窗）。 */
let manualRequest = false;
/** 已经弹过「重启安装」的版本号，避免同一版本反复打扰。 */
let promptedVersion: string | null = null;
let interval: NodeJS.Timeout | null = null;
let firstTimer: NodeJS.Timeout | null = null;

function log(message: string): void {
  process.stdout.write(`[updater] ${message}\n`);
}

/** 更新源地址（fork / 私有部署可用环境变量覆盖）。 */
export function updateFeedUrl(): string {
  const override = process.env.ARKSWARM_DESKTOP_UPDATE_URL?.trim();
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
    message: `ArkSwarm 桌面端 ${version} 已下载完成`,
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

  if (!app.isPackaged) {
    log('开发模式：跳过壳更新检查');
    return;
  }
  if (!autoUpdateSupported()) {
    log('当前构建不支持自动更新（macOS 未签名或平台不支持），仅保留手动下载入口');
    return;
  }

  try {
    autoUpdater.setFeedURL({ provider: 'generic', url: updateFeedUrl() });
  } catch (err) {
    // 更新源配置坏掉也不该拖垮启动：壳照常用，只是没有自动更新
    log(`更新源配置失败，跳过自动更新：${String(err)}`);
    return;
  }
  // 后台自动下载，下载完再问用户何时重启 —— 不阻塞启动，也不打断正在跑的任务
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('checking-for-update', () => log('检查更新中…'));
  autoUpdater.on('update-available', (info) => {
    log(`发现新版本 ${info.version}，开始后台下载`);
  });
  autoUpdater.on('update-not-available', () => {
    log('已是最新版本');
    if (manualRequest) {
      manualRequest = false;
      void notify(`已是最新版本（${app.getVersion()}）`);
    }
  });
  let reportedPercent = -10;
  autoUpdater.on('download-progress', (progress) => {
    // 只按 10% 打点一次（progress 事件很密），避免刷屏
    const bucket = Math.floor(progress.percent / 10) * 10;
    if (bucket > reportedPercent) {
      reportedPercent = bucket;
      log(`下载进度 ${bucket}%（${Math.round(progress.bytesPerSecond / 1024)} KB/s）`);
    }
  });
  autoUpdater.on('update-downloaded', (info) => {
    log(`新版本 ${info.version} 下载完成`);
    if (manualRequest) manualRequest = false;
    void promptInstall(info.version);
  });
  autoUpdater.on('error', (err) => {
    const reason = (err as Error)?.message ?? String(err);
    log(`更新失败：${reason}`);
    if (manualRequest) {
      manualRequest = false;
      void notify('检查更新失败', `${reason}\n\n也可以到发布页手动下载：${RELEASE_PAGE}`);
    }
  });

  firstTimer = setTimeout(() => {
    void autoUpdater.checkForUpdates().catch((err: unknown) => log(`自动检查失败：${String(err)}`));
  }, FIRST_CHECK_DELAY_MS);
  firstTimer.unref?.();

  interval = setInterval(() => {
    void autoUpdater.checkForUpdates().catch((err: unknown) => log(`定时检查失败：${String(err)}`));
  }, CHECK_INTERVAL_MS);
  interval.unref?.();
}

/** 托盘菜单「检查更新…」的入口。 */
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
  busy = true;
  manualRequest = true;
  try {
    await autoUpdater.checkForUpdates();
  } catch (err) {
    manualRequest = false;
    log(`手动检查失败：${String(err)}`);
    await notify('检查更新失败', `${String(err)}\n\n手动下载：${RELEASE_PAGE}`);
  } finally {
    busy = false;
  }
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