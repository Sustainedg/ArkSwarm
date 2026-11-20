/**
 * 下载官方 Node.js 发行包（M4：内核 provision 的 Node 依赖）。
 *
 * 为什么不再靠 npm 的「原生二进制通道」做替代：
 *   Claude Code / Codex / Gemini CLI 三个内核确实在 GitHub Releases 上发布了按平台的
 *   原生可执行文件，但 ① Gemini CLI 没有 Windows 版（只有 darwin / bundle 包）；
 *   ② 三个内核的 npm 包里带的正是同一份原生二进制，直接下载并不更省空间
 *     （claude 单平台包 ~165 MB、codex ~120 MB 起）；③ 官方自更新的口径仍是
 *   `npm i -g` / 内核自己的 update 命令。经调研，桌面端最稳的是**内嵌 Node + npm**，
 *   让既有 provisioner 声明原样工作（见 docs/desktop.md「内核安装通道」一节）。
 *
 * 版本与 docker/Dockerfile 对齐（Node 24 LTS：DeepSeek Harness 要求 >= 24.9，
 * Claude Code 要求 >= 22，Gemini >= 20）。
 * 同时校验官方 SHASUMS256.txt，不拿未经校验的二进制进安装包。
 *
 * 用法：node scripts/fetch-node.mjs            # 按当前平台
 *       node scripts/fetch-node.mjs mac-arm64  # 显式指定（CI matrix 用）
 */
import { createWriteStream } from 'node:fs';
import fs from 'node:fs';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';

const VERSION = 'v24.22.3'; // 与 docker/Dockerfile 的 node_version 保持一致
const DIST = `https://nodejs.org/dist/${VERSION}`;

const PLATFORMS = {
  'win-x64': { file: `node-${VERSION}-win-x64.zip`, kind: 'zip', arch: 'x64', marker: 'node.exe' },
  'mac-arm64': {
    file: `node-${VERSION}-darwin-arm64.tar.gz`,
    kind: 'tar',
    arch: 'arm64',
    marker: 'bin/node',
  },
  'mac-x64': {
    file: `node-${VERSION}-darwin-x64.tar.gz`,
    kind: 'tar',
    arch: 'x64',
    marker: 'bin/node',
  },
  // Linux 只用于本地验证与 AppImage 顺带构建，桌面版不作支持承诺
  'linux-x64': {
    file: `node-${VERSION}-linux-x64.tar.gz`,
    kind: 'tar',
    arch: 'x64',
    marker: 'bin/node',
  },
};

const platformDefault =
  process.platform === 'win32'
    ? 'win-x64'
    : process.platform === 'darwin'
      ? process.arch === 'arm64'
        ? 'mac-arm64'
        : 'mac-x64'
      : process.arch === 'x64'
        ? 'linux-x64'
        : null;
const target = process.argv[2] ?? platformDefault;
if (!target || !(target in PLATFORMS)) {
  console.error(`✗ 不支持的平台：${target ?? process.platform}（桌面版只承诺 Windows / macOS）`);
  process.exit(1);
}
const spec = PLATFORMS[target];

const outRoot = path.resolve(import.meta.dirname, '..', 'build', 'node-runtime');
const outDir = path.join(outRoot, spec.arch);
fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

console.log(`→ 下载校验清单`);
const sumsResp = await fetch(`${DIST}/SHASUMS256.txt`);
if (!sumsResp.ok) {
  console.error(`✗ 无法获取 SHASUMS256.txt（HTTP ${sumsResp.status}）`);
  process.exit(1);
}
const sums = await sumsResp.text();
const expected = sums
  .split('\n')
  .map((line) => line.trim().split(/\s+/))
  .find((parts) => parts.length === 2 && parts[1] === spec.file)?.[0];
if (!expected) {
  console.error(`✗ SHASUMS256.txt 里找不到 ${spec.file}`);
  process.exit(1);
}

const archive = path.join(outDir, spec.file);
console.log(`→ 下载 ${DIST}/${spec.file}`);
const resp = await fetch(`${DIST}/${spec.file}`);
if (!resp.ok) {
  console.error(`✗ 下载失败 HTTP ${resp.status}`);
  process.exit(1);
}
await pipeline(resp.body, createWriteStream(archive));

console.log('→ 校验 sha256');
const actual = crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
if (actual !== expected) {
  console.error(`✗ sha256 不匹配：期望 ${expected}，实际 ${actual}`);
  process.exit(1);
}

console.log('→ 解压');
const extractDir = path.join(outDir, 'extract');
fs.mkdirSync(extractDir, { recursive: true });
// 相对路径 + cwd：Windows 的 GNU tar 会把 `D:\...` 当成远程主机（Cannot connect to D:）
if (spec.kind === 'tar') {
  execFileSync('tar', ['-xzf', spec.file, '-C', 'extract'], { cwd: outDir });
} else {
  // Windows 包是 zip：用 PowerShell 解（GitHub windows runner 上可用）
  execFileSync(
    'powershell',
    ['-NoProfile', '-Command', `Expand-Archive -LiteralPath '${spec.file}' -DestinationPath 'extract'`],
    { cwd: outDir, stdio: 'inherit' },
  );
}
fs.rmSync(archive);

// 两边发行包都是单一顶层目录（node-vX-...），把它整个挪成 outDir 的内容
const entries = fs.readdirSync(extractDir, { withFileTypes: true });
if (entries.length !== 1 || !entries[0].isDirectory()) {
  console.error('✗ 发行包结构不符合预期（顶层不是单一目录）');
  process.exit(1);
}
const unpacked = path.join(extractDir, entries[0].name);
if (!fs.existsSync(path.join(unpacked, ...spec.marker.split('/')))) {
  console.error(`✗ 解压结果不符合预期（找不到 ${spec.marker}）`);
  process.exit(1);
}
for (const entry of fs.readdirSync(unpacked)) {
  fs.renameSync(path.join(unpacked, entry), path.join(outDir, entry));
}
fs.rmSync(extractDir, { recursive: true, force: true });
// runtimeEnv.ts 用这个文件展示「内置 Node 版本」，也作为完整性标记
fs.writeFileSync(path.join(outDir, 'VERSION'), `${VERSION}\n`);

console.log(`✓ Node ${VERSION}（${target} → ${spec.arch}）就绪 → build/node-runtime/${spec.arch}`);