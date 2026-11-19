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
 * 首启只做一次拷贝（安装包内代码 → releases/<内置版本>），之后完全由
 * 界面里的「检测更新」接管（selfupdate.py 下载 → releases/<新版本> → execv）。
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
  const versions = fs
    .readdirSync(releases)
    .filter((name) => /^V[\d.]+$/.test(name) && payloadComplete(path.join(releases, name)))
    .sort((a, b) => (versionGt(a, b) ? -1 : 1));
  if (versions.length === 0) {
    throw new Error('releases/ 里没有任何可用版本（内置代码物化失败？）');
  }
  fs.symlinkSync(versions[0], current, 'dir');
  return fs.realpathSync(current);
}

/**
 * 首启物化：安装包内置代码 → releases/<内置版本>/（幂等，只做一次）。
 * 返回「是否发生了物化」（首次 vs 已有版本）。
 */
export function bootstrapBundledBackend(): { ranDir: string; firstBoot: boolean } {
  const releases = releasesDir();
  fs.mkdirSync(releases, { recursive: true });

  // 已有完整版本就不动（升级走 selfupdate.py；重装应用时也保留用户已有版本）
  const existing = fs
    .readdirSync(releases)
    .filter((name) => /^V[\d.]+$/.test(name) && payloadComplete(path.join(releases, name)));
  let firstBoot = existing.length === 0;
  if (existing.length > 0) {
    const ranDir = resolveRunDir();
    return { ranDir, firstBoot: false };
  }

  // 首启：把内置代码拷贝成第一个版本目录
  const version = bundledVersion();
  if (!app.isPackaged) {
    // 开发模式不物化：直接用仓库源码跑（源码模式），一键更新按设计禁用
    const devBackend = path.join(app.getAppPath(), '..', 'backend');
    return { ranDir: path.dirname(devBackend), firstBoot: true };
  }
  const target = path.join(releases, version);
  if (!payloadComplete(bundledBackendDir())) {
    throw new Error('安装包内自带代码不完整（缺少标志文件），请重新下载安装包');
  }
  fs.rmSync(target, { recursive: true, force: true });
  copyDir(bundledBackendDir(), target);
  const ranDir = resolveRunDir();
  return { ranDir, firstBoot };
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
