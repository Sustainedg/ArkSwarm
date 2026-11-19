/**
 * 下载 python-build-standalone 解释器（打包时用，产物进 build/python-runtime/<arch>/）。
 *
 * 版本与主项目 Dockerfile 对齐：CPython 3.11。双平台各自在对应 OS 的 CI 上下载，
 * 不在本机跨平台下载（macOS 包不塞 Windows 解释器）。
 *
 * 目录按**目标架构**分：electron-builder 的 extraResources 用 `${arch}` 取对应那份，
 * 所以 mac 一次构建 arm64 + x64 时两份解释器可以并存、各进各的安装包。
 *
 * 用法：node scripts/fetch-python.mjs            # 按当前平台
 *       node scripts/fetch-python.mjs win-x64    # 显式指定（CI matrix 用）
 */
import { createWriteStream } from 'node:fs';
import fs from 'node:fs';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { execFileSync } from 'node:child_process';

const TAG = '20250409'; // python-build-standalone 发布 tag
const VERSION = '3.11.12';

const PLATFORMS = {
  'win-x64': {
    url: `https://github.com/astral-sh/python-build-standalone/releases/download/${TAG}/cpython-${VERSION}+${TAG}-x86_64-pc-windows-msvc-install_only.tar.gz`,
    marker: 'python/python.exe',
    arch: 'x64',
  },
  'mac-arm64': {
    url: `https://github.com/astral-sh/python-build-standalone/releases/download/${TAG}/cpython-${VERSION}+${TAG}-aarch64-apple-darwin-install_only.tar.gz`,
    marker: 'python/bin/python3.11',
    arch: 'arm64',
  },
  'mac-x64': {
    url: `https://github.com/astral-sh/python-build-standalone/releases/download/${TAG}/cpython-${VERSION}+${TAG}-x86_64-apple-darwin-install_only.tar.gz`,
    marker: 'python/bin/python3.11',
    arch: 'x64',
  },
  // Linux 只用于本地验证与 AppImage 顺带构建，桌面版不作支持承诺
  'linux-x64': {
    url: `https://github.com/astral-sh/python-build-standalone/releases/download/${TAG}/cpython-${VERSION}+${TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz`,
    marker: 'python/bin/python3.11',
    arch: 'x64',
  },
};

// 优先级：显式参数 > 当前平台。?? 的优先级高于三目，必须加括号，否则显式参数会被吞掉
const platformDefault =
  process.platform === 'win32'
    ? 'win-x64'
    : process.platform === 'darwin'
      ? process.arch === 'arm64'
        ? 'mac-arm64'
        : 'mac-x64'
      : null;
const target = process.argv[2] ?? platformDefault;
if (!target || !(target in PLATFORMS)) {
  console.error('✗ 不支持的平台（桌面版只承诺 Windows / macOS）');
  process.exit(1);
}
const spec = PLATFORMS[target];
const outDir = path.resolve(import.meta.dirname, '..', 'build', 'python-runtime', spec.arch);
fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

console.log(`→ 下载 ${spec.url}`);
const resp = await fetch(spec.url);
if (!resp.ok) {
  console.error(`✗ 下载失败 HTTP ${resp.status}`);
  process.exit(1);
}
const tarPath = path.join(outDir, 'runtime.tar.gz');
await pipeline(resp.body, createWriteStream(tarPath));
console.log('→ 解压');
fs.mkdirSync(path.join(outDir, 'extract'), { recursive: true });
execFileSync('tar', ['-xzf', tarPath, '-C', path.join(outDir, 'extract')]);
fs.rmSync(tarPath);
// install_only 包解开就是 python/ 目录；把它挪到 build/python-runtime/<arch>/python
const extracted = path.join(outDir, 'extract', 'python');
if (!fs.existsSync(path.join(extracted, ...spec.marker.split('/').slice(1)))) {
  console.error(`✗ 解压结果不符合预期（找不到 ${spec.marker}）`);
  process.exit(1);
}
fs.renameSync(extracted, path.join(outDir, 'python'));
fs.rmSync(path.join(outDir, 'extract'), { recursive: true, force: true });
console.log(`✓ Python ${VERSION}（${target} → ${spec.arch}）就绪 → build/python-runtime/${spec.arch}/python`);