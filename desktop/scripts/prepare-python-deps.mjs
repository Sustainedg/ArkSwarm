/**
 * 构建前把后端 Python 依赖**预装进内嵌解释器**（方案 A），并下载离线 wheelhouse（方案 C）。
 *
 * 为什么不能直接分发 venv：
 *   `pyvenv.cfg` 的 `home` 和 Windows 的脚本垫片里写的是构建机绝对路径，
 *   换台机器就废；而依赖本身又都是平台相关二进制（.pyd / .so），
 *   只能在目标平台（CI 矩阵）上装。
 *
 * 所以直接装进 python-build-standalone 的 site-packages，运行时用同一个解释器跑后端：
 * 不建 venv、不联网、不拷贝 —— 首启即用。wheelhouse 是兜底：内置依赖指纹对不上
 * （开发模式 / 目录损坏）时，本地离线装 venv 仍然不需要联网。
 *
 * 用法：node scripts/prepare-python-deps.mjs            # 按当前平台
 *       node scripts/prepare-python-deps.mjs win-x64    # 显式指定（CI matrix 用）
 *
 * 必须先跑 fetch-python.mjs（本脚本会往它解压出来的 runtime 里装东西）。
 */
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

/** 与 Dockerfile / fetch-python.mjs 保持一致。 */
const PY_VERSION = '3.11';
/** pip 的 CPython ABI 标签：3.11 → cp311。 */
const ABI = `cp${PY_VERSION.replace('.', '')}`;

/**
 * 目标平台：arch 决定 build/python-runtime/<arch>；platform 是 pip 的 wheel tag
 * （本机原生安装时不需要，跨架构时交给 pip 选 wheel）。
 */
const TARGETS = {
  'win-x64': { arch: 'x64', platform: 'win_amd64' },
  'mac-arm64': { arch: 'arm64', platform: 'macosx_11_0_arm64' },
  'mac-x64': { arch: 'x64', platform: 'macosx_11_0_x86_64' },
  'linux-x64': { arch: 'x64', platform: 'manylinux_2_28_x86_64' },
};

const hostTarget =
  process.platform === 'win32'
    ? 'win-x64'
    : process.platform === 'darwin'
      ? process.arch === 'arm64'
        ? 'mac-arm64'
        : 'mac-x64'
      : process.platform === 'linux'
        ? 'linux-x64'
        : null;

const target = process.argv[2] ?? hostTarget;
if (!target || !(target in TARGETS)) {
  console.error('✗ 不支持的目标平台（桌面版只承诺 Windows / macOS）');
  process.exit(1);
}

const spec = TARGETS[target];
const isWin = target.startsWith('win-');
/** 目标解释器能否在本机直接执行：mac 双架构时 x64 那份要在 arm64 runner 上跨装。 */
const native = hostTarget === target;

const desktopRoot = path.resolve(import.meta.dirname, '..');
const buildDir = path.join(desktopRoot, 'build');
const requirements = path.join(desktopRoot, '..', 'backend', 'requirements.txt');
/** 解释器根目录（resources/python-runtime/python 的原型）。 */
const runtimeRoot = path.join(buildDir, 'python-runtime', spec.arch, 'python');
/** 离线 wheelhouse（resources/python-runtime/wheelhouse 的原型，兜底用）。 */
const wheelhouse = path.join(buildDir, 'python-runtime', spec.arch, 'wheelhouse');

/** 运行时里的 python 可执行文件。 */
function pythonOf(root, platformTag) {
  return platformTag.startsWith('win-')
    ? path.join(root, 'python.exe')
    : path.join(root, 'bin', `python${PY_VERSION}`);
}

/** site-packages 的落点（Windows 是 Lib，其它是 lib/pythonX.Y）。 */
const sitePackages = isWin
  ? path.join(runtimeRoot, 'Lib', 'site-packages')
  : path.join(runtimeRoot, 'lib', `python${PY_VERSION}`, 'site-packages');

const targetPython = pythonOf(runtimeRoot, target);

if (!fs.existsSync(targetPython)) {
  console.error(
    `✗ 找不到 ${targetPython}\n  先跑 node scripts/fetch-python.mjs ${target}`,
  );
  process.exit(1);
}
if (!fs.existsSync(requirements)) {
  console.error(`✗ 找不到依赖清单：${requirements}`);
  process.exit(1);
}

/**
 * 选 pip 的执行者：原生目标用目标解释器自己；跨架构时用**宿主架构的内嵌解释器**
 * （pip 版本与目标一致，也不依赖 runner 上装了什么系统 Python）。
 */
function pipRunner() {
  if (native) return targetPython;
  const hostSpec = hostTarget ? TARGETS[hostTarget] : null;
  if (hostSpec) {
    const hostRoot = path.join(buildDir, 'python-runtime', hostSpec.arch, 'python');
    const hostPython = pythonOf(hostRoot, hostTarget);
    if (fs.existsSync(hostPython)) return hostPython;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

const pipCmd = pipRunner();
/** 跨架构时需要的 pip 参数：只收 wheel，并按目标平台选包。 */
const crossArgs = native
  ? []
  : [
      '--only-binary=:all:',
      '--platform',
      spec.platform,
      '--python-version',
      PY_VERSION,
      '--implementation',
      'cp',
      '--abi',
      ABI,
    ];

function run(cmd, args) {
  console.log(`→ ${cmd} ${args.join(' ')}`);
  execFileSync(cmd, args, { stdio: 'inherit', cwd: buildDir });
}

// ---- 1. wheelhouse：离线兜底的一份 wheel ----
fs.rmSync(wheelhouse, { recursive: true, force: true });
fs.mkdirSync(wheelhouse, { recursive: true });
run(pipCmd, [
  '-m', 'pip', 'download', '-q',
  '--disable-pip-version-check',
  '--only-binary=:all:',
  '-d', wheelhouse,
  ...crossArgs,
  '-r', requirements,
]);

// ---- 2. 依赖装进内嵌解释器（方案 A）----
const installArgs = [
  '-m', 'pip', 'install', '-q',
  '--disable-pip-version-check',
  // 依赖没有 console script 需求，别为写 Scripts/ 里的垫片告警
  '--no-warn-script-location',
  '--upgrade',
  ...crossArgs,
  ...(native ? [] : ['--no-compile']), // 跨架构时宿主 Python 编译的 .pyc 版本不对，别生成
  ...(native ? [] : ['--target', sitePackages]),
  '-r', requirements,
];
if (native) {
  run(targetPython, installArgs);
} else {
  // --target 要求 --upgrade 才能真正覆盖，否则会因为目录已存在直接报错
  run(pipCmd, installArgs);
}

// ---- 3. 指纹：运行时与 wheelhouse 各写一份，启动时只认与 requirements 完全一致的那份 ----
const stamp = createHash('sha256').update(fs.readFileSync(requirements)).digest('hex');
fs.writeFileSync(path.join(runtimeRoot, '.requirements-stamp'), stamp);
fs.writeFileSync(path.join(wheelhouse, '.requirements-stamp'), stamp);

console.log(
  `✓ Python 依赖就绪（${target}${native ? '' : '，跨架构安装'}）\n` +
    `  运行时：${path.relative(desktopRoot, runtimeRoot)}\n` +
    `  wheelhouse：${path.relative(desktopRoot, wheelhouse)}（${fs.readdirSync(wheelhouse).length - 1} 个 wheel）`,
);
