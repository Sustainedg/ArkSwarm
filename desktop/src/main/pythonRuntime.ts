/**
 * Python 运行时管理：embedded 解释器 + venv（方案 C 的核心）。
 *
 * 首次启动把 python-build-standalone 解释器铺到 python/runtime/，
 * 再用 `python -m venv` 建 venv、`pip install -r requirements.txt` 装依赖。
 * venv 损坏时删掉整个 python/ 目录即可秒级重建（对话数据在 data/，不受影响）。
 *
 * 下载动作只在首启 / 显式重建时发生，日常启动零网络依赖。
 */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { app } from 'electron';
import { backendRoot, ensureDir, pythonRuntimeDir, venvPython } from './paths';
/**
 * embedded 解释器来源。打包时随 resources/ 分发（scripts/fetch-python.mjs 放进包），
 * 开发模式用系统 python3（要求 ≥ 3.11，与主项目一致）。版本与主项目 Dockerfile 对齐（python:3.11）。
 */
export function embeddedPythonPath(): string {
  const win = process.platform === 'win32';
  const bundled = path.join(
    process.resourcesPath,
    'python-runtime',
    'python',
    win ? 'python.exe' : path.join('bin', 'python3.11'),
  );
  if (app.isPackaged && fs.existsSync(bundled)) {
    return bundled;
  }
  return win ? 'python' : 'python3';
}

/** embedded 解释器落地目录（userData/python/runtime）。 */
function runtimePythonDir(): string {
  return path.join(pythonRuntimeDir(), 'runtime');
}

/** 诊断用：venv 是否就绪（外部展示「运行时状态」用，不驱动启动流程）。 */
export function venvReady(): boolean {
  return fs.existsSync(venvPython());
}

export interface Progress {
  stage: 'runtime' | 'venv' | 'deps';
  message: string;
}

/**
 * 初始化 Python 运行时（幂等：任一步已就绪就跳过）。
 * @param onProgress 进度回调（驱动启动页的进度文案）
 * @throws 任一步失败都抛错并携带可读信息（铁律 11）
 */
export async function ensurePythonRuntime(onProgress: (p: Progress) => void): Promise<void> {
  const runtimeDir = runtimePythonDir();
  const venvDir = path.join(pythonRuntimeDir(), 'venv');

  // ---- 1. 铺解释器（只在 venv 缺失时才走；venv 建好就不再动 runtime） ----
  if (!fs.existsSync(venvPython())) {
    onProgress({ stage: 'runtime', message: '准备 Python 解释器…' });
    ensureDir(pythonRuntimeDir());
    const src = embeddedPythonPath();
    // 内置解释器的**根目录**是 python-runtime/python（Windows 是可执行文件同层，
    // macOS 是 bin/ 的上一层）。不要用 path.dirname(src) 推：unix 的 src 是
    // `python/bin/python3.11`，dirname 会落在 bin/ 上，拷完 runtimeDir 里就没有
    // `bin/python3`，建 venv 直接失败（打包版 mac 起不来的经典症状）。
    const bundledRoot = path.join(process.resourcesPath ?? '', 'python-runtime', 'python');
    if (app.isPackaged && fs.existsSync(bundledRoot)) {
      copyDir(bundledRoot, runtimeDir);
    } else {
      // 开发模式：用系统 python3 直接建 venv（不拷贝；venv 就建在 userData，
      // 与 serve.sh 的 backend/.venv 互不干扰）
      if (process.platform === 'win32' ? !fs.existsSync(src) : !await commandExists(src)) {
        throw new Error(
          '未找到 python3（≥ 3.11）：开发模式需要系统 Python；打包版内置解释器，无需系统 Python',
        );
      }
    }
  }

  // ---- 2. 建 venv（只在缺失时） ----
  if (!fs.existsSync(venvPython())) {
    onProgress({ stage: 'venv', message: '创建虚拟环境…' });
    const basePython = app.isPackaged
      ? path.join(runtimeDir, process.platform === 'win32' ? 'python.exe' : path.join('bin', 'python3'))
      : embeddedPythonPath();
    await runCommand(basePython, ['-m', 'venv', venvDir]);
  }

  // ---- 3. 装依赖：requirements.txt 内容变了就必须重装 ----
  // 判据是**内容指纹**而不是「装过一次」：桌面端整包升级会换掉
  // releases/current/backend 里的 requirements.txt，旧实现只写个时间戳，
  // 新版本加了依赖也永远发现不了（后端起来直接 ImportError）。
  const requirements = path.join(backendRoot(), 'requirements.txt');
  const wanted = requirementsStamp(requirements);
  if (!wanted) {
    throw new Error(`后端依赖清单缺失或不可读：${requirements}`);
  }
  const stampFile = path.join(venvDir, '.requirements-stamp');
  const installed = fs.existsSync(stampFile) ? fs.readFileSync(stampFile, 'utf8').trim() : '';
  if (installed !== wanted) {
    onProgress({
      stage: 'deps',
      message: installed ? '后端依赖有变化，正在更新…' : '安装后端依赖（首次需要联网）…',
    });
    await runCommand(venvPython(), [
      '-m', 'pip', 'install', '-q',
      // 与 Docker entrypoint 对齐：快失败、别被 pip 自身的版本检查拖住
      '--disable-pip-version-check', '--retries', '2', '--timeout', '15',
      '-r', requirements,
    ]);
    fs.writeFileSync(stampFile, wanted);
  }
}

/** requirements.txt 的 sha256；文件缺失时返回空串（调用方据此报错）。 */
function requirementsStamp(file: string): string {
  try {
    return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch {
    return '';
  }
}

/** 跑一条子命令，stdout/stderr 逐行转发给调用方；非零退出抛错。 */
function runCommand(cmd: string, args: string[], cwd?: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let tail = '';
    const feed = (buf: Buffer): void => {
      const text = buf.toString('utf8');
      tail = (tail + text).slice(-2000); // 失败时只回显尾部，避免刷屏
      process.stdout.write(text);
    };
    child.stdout?.on('data', feed);
    child.stderr?.on('data', feed);
    child.on('error', (err) => reject(new Error(`${cmd} 无法启动：${err.message}`)));
    child.on('exit', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${cmd} ${args.join(' ')} 退出码 ${code}\n${tail}`));
    });
  });
}

/**
 * 递归拷贝目录（打包解释器 → userData）。
 *
 * 必须保留符号链接：python-build-standalone 在 macOS 上大量使用相对软链
 * （``bin/python3 → python3.11``、``lib/libpython3.11.dylib → …``）。旧实现用
 * ``copyFileSync`` 逐个跟随软链，会把它们展开成整份二进制副本，体积翻倍且
 * 动态库的 install_name 关系变味。``fs.cpSync`` 的原生实现能原样保留。
 */
function copyDir(src: string, dest: string): void {
  fs.cpSync(src, dest, { recursive: true, dereference: false, verbatimSymlinks: true });
}

/** 检查命令是否存在（unix：PATH 上有可执行文件；win：可直接 fs.existsSync 或 where）。 */
async function commandExists(cmd: string): Promise<boolean> {
  if (path.isAbsolute(cmd) || cmd.includes('/') || cmd.includes('\\')) {
    return fs.existsSync(cmd);
  }
  // PATH 查找交给 `command -v` / `where`，比手动扫 PATH 目录稳
  const probe = process.platform === 'win32' ? ['cmd', '/c', `where ${cmd}`] : ['sh', '-c', `command -v ${cmd}`];
  return new Promise((resolve) => {
    const child = spawn(probe[0], probe.slice(1), { stdio: 'ignore' });
    child.on('error', () => resolve(false));
    child.on('exit', (code) => resolve(code === 0));
  });
}
