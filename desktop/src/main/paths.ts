/**
 * 路径与目录布局约定（桌面版唯一权威来源，其他模块只准 import 这里）。
 *
 * 目录布局（userData 即系统标准应用数据目录）：
 *
 *   userData/
 *   ├── data/          ← ARKSWARM_DATA_DIR：SQLite、工作区、secret.key、内核 HOME
 *   │                     （对话数据全在这，卸载应用不清空，升级原样保留）
 *   ├── python/        ← embedded 解释器 + venv（首启联网装依赖，可随时删掉重建）
 *   ├── node/          ← npm 全局安装前缀 + 缓存（内核 CLI 装在这里，不碰系统 Node）
 *   └── releases/      ← 后端版本目录 + current 指针（复用主项目自更新机制）
 */
import { app } from 'electron';
import path from 'node:path';
import fs from 'node:fs';

/** userData 根目录。 */
export function userDataRoot(): string {
  return app.getPath('userData');
}

/** 数据目录（ARKSWARM_DATA_DIR）：对话、密钥、工作区都在这。 */
export function dataDir(): string {
  return path.join(userDataRoot(), 'data');
}

/** Python 运行时目录（embedded 解释器 + venv）。 */
export function pythonRuntimeDir(): string {
  return path.join(userDataRoot(), 'python');
}

/** venv 的 python 可执行文件路径（Windows 是 python.exe，unix 是 bin/python）。 */
export function venvPython(): string {
  const dir = path.join(pythonRuntimeDir(), 'venv');
  return process.platform === 'win32'
    ? path.join(dir, 'Scripts', 'python.exe')
    : path.join(dir, 'bin', 'python');
}

/** 后端 releases 目录（自更新机制用，M1 阶段先放源码形态）。 */
export function releasesDir(): string {
  return path.join(userDataRoot(), 'releases');
}

/**
 * npm 的全局安装前缀（桌面版自己的一份，绝不写进系统 Node 的目录）。
 *
 * 为什么必须隔离：打包版的 Node 运行时在 `resources/` 里（macOS 上位于 .app 内部，
 * 签名后只读），`npm install -g` 想写全局目录会直接 EACCES；Windows 上默认写到
 * `%APPDATA%\npm`，又会和用户自己的 Node 抢同名垫片。把前缀钉在 userData，
 * 既解决了权限问题，也让「内核装在哪」可预期、可随数据目录一起备份 / 迁移。
 */
export function nodeGlobalDir(): string {
  return path.join(userDataRoot(), 'node', 'global');
}

/** npm 缓存目录（同样放 userData，卸载 / 重建不影响系统缓存）。 */
export function nodeCacheDir(): string {
  return path.join(userDataRoot(), 'node', 'cache');
}

/** 已安装内核命令所在目录：POSIX 是 `<prefix>/bin`，Windows 是 `<prefix>` 本身。 */
export function nodeGlobalBinDir(): string {
  const prefix = nodeGlobalDir();
  return process.platform === 'win32' ? prefix : path.join(prefix, 'bin');
}

/**
 * 后端应用根的**默认值**：仓库 backend/（开发模式）。
 *
 * 打包模式实际跑 releases/current/backend（bootstrap.ts 物化的托管布局，
 * selfupdate.py 靠它识别托管模式），这里保留 dev 模式的落点；
 * 打包流程请用 `resolveRunDir()` 的结果，不要直接用本函数。
 */
export function backendRoot(): string {
  if (app.isPackaged) {
    return path.join(userDataRoot(), 'releases', 'current', 'backend');
  }
  return path.join(app.getAppPath(), '..', 'backend');
}

/** 确保目录存在（递归创建，幂等）。 */
export function ensureDir(dir: string): void {
  fs.mkdirSync(dir, { recursive: true });
}

/** 初始化全部目录布局（app.ready 之后、起后端之前调用一次）。 */
export function ensureLayout(): void {
  for (const dir of [
    dataDir(),
    pythonRuntimeDir(),
    releasesDir(),
    nodeGlobalBinDir(),
    nodeCacheDir(),
  ]) {
    ensureDir(dir);
  }
}
