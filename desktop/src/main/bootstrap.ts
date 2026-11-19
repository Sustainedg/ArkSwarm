/**
 * 首启引导：把安装包内置的后端物化成「托管模式」布局（selfupdate.py 的契约）。
 *
 * Docker 的 entrypoint.sh 做的同款事情，桌面版翻译成 TS：
 *
 *   userData/releases/<V0.43>/   各版本目录（backend/ + frontend/dist）
 *   userData/releases/current   软链 → 正在运行的版本
 *
 * 为什么必须这样（而不是直接从 resources/app-backend 跑）：
 *   1. 安装目录（Program Files / Applications）不可写 → selfupdate.py 下载的
 *      新版本无处可放，`managed_root()` 判 None → 一键更新永久禁用；
 *   2. execv 重启后 cwd 停在旧版本目录 → 必须由「current 指针 + 重新进 backend」解决；
 *   3. releases/ 布局给了回滚能力：保留旧版本目录，切指针即回滚。
 *
 * **整包升级也要更新后端代码**：桌面端的更新通道是「下载完整安装包」（electron-updater），
 * 新安装包里自带的后端代码比 `releases/` 里现有的新时，必须物化成新版本目录并切
 * `current`，否则壳升级了、后端还跑着旧代码（两条版本线会错位）。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';
import { app } from 'electron';
import { releasesDir } from './paths';

/** selfupdate.py 的 _PAYLOAD_MARKERS：一个版本目录「完整」的标志文件。 */
const PAYLOAD_MARKERS = [
  path.join('backend', 'app', '__init__.py'),
  path.join('backend', 'app', 'main.py'),
  path.join('frontend', 'dist', 'index.html'),
];

/** 安装包内自带的后端源码包（electron-builder extraResources 铺进来的）。 */
function bundledBackendDir(): string {
  return path.join(process.resourcesPath, 'app-backend');
}

/** 读内置代码的版本号（backend/app/__init__.py 的 __version__，形如 V0.43）。 */
function bundledVersion(): string {
  const file = path.join(bundledBackendDir(), 'backend', 'app', '__init__.py');
  const content = fs.readFileSync(file, 'utf8');
  const match = content.match(/^__version__\s*=\s*"([^"]+)"/m);
  if (!match) {
    throw new Error(`安装包内代码缺版本号（${file} 里没有 __version__）`);
  }
  return match[1];
}

/** 目录是否是「完整的版本目录」（selfupdate.py 同款判据）。 */
function payloadComplete(dir: string): boolean {
  return PAYLOAD_MARKERS.every((marker) => fs.existsSync(path.join(dir, marker)));
}

/** V0.43 / V0.1.1 → [0,43] / [0,1,1]；缺段补 0 用于比较（V0.44 = V0.44.0）。 */
function versionKey(version: string): number[] {
  const parts = version.replace(/^[vV]/, '').split('.').map((p) => Number.parseInt(p, 10) || 0);
  while (parts.length > 1 && parts[parts.length - 1] === 0) parts.pop();
  return parts;
}

function versionGt(a: string, b: string): boolean {
  const ka = versionKey(a);
  const kb = versionKey(b);
  const len = Math.max(ka.length, kb.length);
  for (let i = 0; i < len; i += 1) {
    const va = ka[i] ?? 0;
    const vb = kb[i] ?? 0;
    if (va !== vb) return va > vb;
  }
  return false;
}

/** releases/ 里所有完整版本（不跟随 current 指向；降序）。 */
function installedVersions(): string[] {
  const releases = releasesDir();
  if (!fs.existsSync(releases)) return [];
  return fs
    .readdirSync(releases)
    .filter((name) => /^V[\d.]+$/.test(name) && payloadComplete(path.join(releases, name)))
    .sort((a, b) => (versionGt(a, b) ? -1 : 1));
}

/**
 * 目录链接类型：Windows 普通用户没有创建目录符号链接的权限（需要管理员 /
 * 开发者模式），junction 不需要提权且同样能被 realpath 解析。
 */
const DIR_LINK_TYPE = process.platform === 'win32' ? 'junction' : 'dir';

/** 把 current 原子地指到目标版本目录（临时链接 + rename）。 */
function switchCurrent(targetDir: string): void {
  const releases = releasesDir();
  const current = path.join(releases, 'current');
  const tmp = path.join(releases, '.current.tmp');
  fs.rmSync(tmp, { force: true });
  fs.symlinkSync(targetDir, tmp, DIR_LINK_TYPE);
  try {
    fs.renameSync(tmp, current);
  } catch {
    // Windows / 某些文件系统上目标已存在时 rename 会失败：先删再改名。
    fs.rmSync(current, { force: true });
    fs.renameSync(tmp, current);
  }
}

/**
 * 选择要运行的版本目录：current 完整就直接用；否则挑最高完整版本并建链。
 * 返回 current 最终指向的版本目录（绝对路径）。
 */
export function resolveRunDir(): string {
  const releases = releasesDir();
  fs.mkdirSync(releases, { recursive: true });

  const current = path.join(releases, 'current');
  // current 指向的版本必须完整（半截解包/被删文件时重新选择）
  if (fs.existsSync(current) && payloadComplete(current)) {
    return fs.realpathSync(current);
  }
  fs.rmSync(current, { force: true });

  // 挑 releases/ 里最高的完整版本
  const versions = installedVersions();
  if (versions.length === 0) {
    throw new Error('releases/ 里没有任何可用版本（内置代码物化失败？）');
  }
  switchCurrent(path.join(releases, versions[0]));
  return fs.realpathSync(current);
}

/**
 * 首启 / 整包升级后的物化：安装包内置代码 → releases/<内置版本>/。
 *
 * - 首次安装：物化内置版本并建 current；
 * - 整包升级（新安装包覆盖旧安装）：内置版本比 releases/ 里的都新时，物化新版本
 *   并切 current —— 否则壳更新了、后端还跑旧代码；
 * - 重装旧安装包：releases/ 里已有更新版本时不动，避免把用户降级。
 */
export function bootstrapBundledBackend(): { ranDir: string; firstBoot: boolean } {
  const releases = releasesDir();
  fs.mkdirSync(releases, { recursive: true });

  if (!app.isPackaged) {
    // 开发模式不物化：直接用仓库源码跑（源码模式），一键更新按设计禁用
    const devBackend = path.join(app.getAppPath(), '..', 'backend');
    return { ranDir: path.dirname(devBackend), firstBoot: true };
  }

  const existing = installedVersions();
  const firstBoot = existing.length === 0;
  const version = bundledVersion();

  // 内置版本已完整落地：不重复拷贝。只要内置版本是（并列）最高版本，它就是
  // 安装包的权威代码 —— current 若停在别处（例如旧版手动切过指针），指回来。
  // 仅当用户已有更高的历史版本（旧版后端自更新留下的）时才保持现状，不降级。
  const target = path.join(releases, version);
  if (payloadComplete(target)) {
    if (!existing[0] || !versionGt(existing[0], version)) {
      const current = path.join(releases, 'current');
      const running =
        fs.existsSync(current) && payloadComplete(current)
          ? path.basename(fs.realpathSync(current))
          : null;
      if (running !== version) switchCurrent(target);
    }
    return { ranDir: resolveRunDir(), firstBoot };
  }

  // 内置版本比用户已有版本旧：不降级（用户可能通过后端自更新昇过级）
  if (existing.length > 0 && !versionGt(version, existing[0])) {
    return { ranDir: resolveRunDir(), firstBoot: false };
  }

  // 物化内置代码成新版本目录
  if (!payloadComplete(bundledBackendDir())) {
    throw new Error('安装包内自带代码不完整（缺少标志文件），请重新下载安装包');
  }
  fs.rmSync(target, { recursive: true, force: true });
  copyDir(bundledBackendDir(), target);
  switchCurrent(target);
  return { ranDir: resolveRunDir(), firstBoot };
}

/** 递归拷贝目录。 */
function copyDir(src: string, dest: string): void {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}
