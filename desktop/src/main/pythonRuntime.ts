/**
 * Python 运行时管理：优先用「依赖已预装」的内置解释器，兜底才建 venv。
 *
 * 三级策略（按顺序选第一条可用的）：
 *   A. 构建期已把后端依赖装进 python-build-standalone（prepare-python-deps.mjs），
 *      运行时指纹一致就直接用这份解释器 —— 不拷贝、不建 venv、不跑 pip、不联网；
 *   C. 指纹对不上（开发模式 / 资源损坏）→ 用随包的离线 wheelhouse 在 userData 里
 *      建 venv 并离线安装（`--no-index --find-links`）；
 *   最后才是在线 pip（保留镜像 / 安装期代理 / 绕过死代理重试）。
 *
 * 这三级都失败才会弹「启动失败」；日志与完整 pip 输出落在 userData/install.log。
 */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { app } from 'electron';
import { ensureDir, installLogPath, pythonRuntimeDir, venvPython } from './paths';

/**
 * 安装期网络配置（与 Docker entrypoint 同名同义，部署侧 / 高级用户按需设置）：
 *
 * - `CODA_PIP_INDEX_URL`：改用镜像源（国内直连 PyPI 实测只有 ~15KB/s）；
 * - `CODA_INSTALL_PROXY`：本次装依赖专用的代理（不会写进进程环境，也不影响
 *   之后智能体运行时的出口）。
 */
const PIP_INDEX_ENV = 'CODA_PIP_INDEX_URL';
const INSTALL_PROXY_ENV = 'CODA_INSTALL_PROXY';
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

/** 安装包内置的解释器根目录（resources/python-runtime/python，方案 A 直接跑它）。 */
function bundledRuntimeRoot(): string {
  return path.join(process.resourcesPath ?? '', 'python-runtime', 'python');
}

/** 离线 wheelhouse（构建期预下载；兜底建 venv 时不用联网）。 */
function wheelhouseDir(): string {
  return path.join(process.resourcesPath ?? '', 'python-runtime', 'wheelhouse');
}

/** embedded 解释器落地目录（userData/python/runtime，仅兜底路径使用）。 */
function runtimePythonDir(): string {
  return path.join(pythonRuntimeDir(), 'runtime');
}

/** 诊断用：venv 是否就绪（外部展示「运行时状态」用，不驱动启动流程）。 */
export function venvReady(): boolean {
  return fs.existsSync(venvPython());
}

/** 读指纹文件；不存在 / 不可读一律视为「未预装」（空串不会与真实 sha256 相等）。 */
function readStamp(file: string): string {
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch {
    return '';
  }
}

export interface Progress {
  stage: 'runtime' | 'venv' | 'deps';
  message: string;
}

/** 后端用哪个解释器跑：内置（依赖已预装）还是 userData 里的 venv。 */
export interface RuntimeHandle {
  python: string;
  mode: 'bundled' | 'venv';
}

/**
 * 初始化 Python 运行时（幂等：任一步已就绪就跳过）。
 * @param runDir 正在运行的后端版本目录（`releases/<版本>` 或开发模式的仓库根）。
 *   依赖清单从这里取，**不要**再走 `releases/current`：链接在少数 Windows 环境里
 *   建不出来（见 bootstrap.ts 的降级策略），但应用仍应能启动。
 * @param onProgress 进度回调（驱动启动页的进度文案）
 * @throws 任一步失败都抛错并携带可读信息（铁律 11）
 */
export async function ensurePythonRuntime(
  runDir: string,
  onProgress: (p: Progress) => void,
): Promise<RuntimeHandle> {
  const runtimeDir = runtimePythonDir();
  const venvDir = path.join(pythonRuntimeDir(), 'venv');

  const requirements = path.join(runDir, 'backend', 'requirements.txt');
  const wanted = requirementsStamp(requirements);
  if (!wanted) {
    throw new Error(`后端依赖清单缺失或不可读：${requirements}`);
  }

  // ---- 0. 方案 A：内置解释器已预装同指纹依赖 → 零拷贝、零网络、零 pip ----
  // 依赖在构建期装进 python-build-standalone 的 site-packages（prepare-python-deps.mjs），
  // 指纹一致就直接用这份解释器跑后端。首启只剩「起进程 + 健康检查」，秒级可用。
  const bundledRoot = bundledRuntimeRoot();
  if (
    app.isPackaged
    && fs.existsSync(bundledRoot)
    && readStamp(path.join(bundledRoot, '.requirements-stamp')) === wanted
  ) {
    onProgress({ stage: 'runtime', message: '使用内置运行时…' });
    return { python: embeddedPythonPath(), mode: 'bundled' };
  }

  // ---- 1. 铺解释器（兜底路径：只在 venv 缺失时才走） ----
  if (!fs.existsSync(venvPython())) {
    onProgress({ stage: 'runtime', message: '准备 Python 解释器…' });
    ensureDir(pythonRuntimeDir());
    const src = embeddedPythonPath();
    // 内置解释器的**根目录**是 python-runtime/python（Windows 是可执行文件同层，
    // macOS 是 bin/ 的上一层）。不要用 path.dirname(src) 推：unix 的 src 是
    // `python/bin/python3.11`，dirname 会落在 bin/ 上，拷完 runtimeDir 里就没有
    // `bin/python3`，建 venv 直接失败（打包版 mac 起不来的经典症状）。
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
  // 判据是**内容指纹**而不是「装过一次」：桌面端整包升级会换掉运行目录里的
  // requirements.txt，旧实现只写个时间戳，新版本加了依赖也永远发现不了
  // （后端起来直接 ImportError）。
  const stampFile = path.join(venvDir, '.requirements-stamp');
  const installed = readStamp(stampFile);
  if (installed !== wanted) {
    onProgress({
      stage: 'deps',
      message: installed ? '后端依赖有变化，正在更新…' : '安装后端依赖…',
    });
    await installDependencies(requirements, wanted, onProgress);
    fs.writeFileSync(stampFile, wanted);
  }
  return { python: venvPython(), mode: 'venv' };
}

/**
 * 装依赖：先按当前环境装；若失败原因指向「连不上代理」，绕过系统代理再试一次。
 *
 * 为什么要这一层：Windows 上 Clash / V2Ray 之类退出后，注册表里的系统代理设置
 * 往往还留着，pip 会读到它并硬连一个已经关掉的端口 —— 报错形如
 * `ProxyError('Cannot connect to proxy.', ConnectionAbortedError(10053, ...))`。
 * 这时候直连（或镜像）反而是通的，没必要让用户先学会改注册表。
 *
 * 显式配了 `CODA_INSTALL_PROXY` 时不自动绕过：那说明用户确实需要代理，
 * 失败就按真实原因报，避免把「公司代理暂时不通」误判成代理配置错误。
 */
async function installDependencies(
  requirements: string,
  wanted: string,
  onProgress: (p: Progress) => void,
): Promise<void> {
  // ---- 方案 C：优先用随包分发的离线 wheelhouse（同指纹才可信）----
  const wheels = wheelhouseDir();
  if (readStamp(path.join(wheels, '.requirements-stamp')) === wanted) {
    onProgress({ stage: 'deps', message: '从离线依赖包安装…' });
    try {
      await runCommand(venvPython(), [
        '-m', 'pip', 'install', '-q',
        '--disable-pip-version-check',
        '--no-index', '--find-links', wheels,
        '-r', requirements,
      ], { env: pipEnv() });
      return;
    } catch (err) {
      // 离线包损坏 / 依赖与 wheel 不匹配：不把用户卡死，退到在线安装（保留代理重试）。
      process.stdout.write(`[deps] 离线依赖包安装失败，改为在线安装：${(err as Error).message}\n`);
    }
  }

  try {
    await runCommand(venvPython(), pipInstallArgs(requirements), { env: pipEnv() });
    return;
  } catch (err) {
    const detail = (err as Error).message;
    if (process.env[INSTALL_PROXY_ENV]?.trim() || !isProxyFailure(detail)) {
      throw pipFailure(detail);
    }
    onProgress({ stage: 'deps', message: '系统代理连不上，正在绕过代理重试…' });
  }
  try {
    // `--proxy ""` 同时压掉 pip.ini 里配的代理；NO_PROXY=* 压掉环境变量与 Windows 注册表代理。
    await runCommand(venvPython(), pipInstallArgs(requirements, { bypassProxy: true }), {
      env: pipEnv({ NO_PROXY: '*', no_proxy: '*' }),
    });
  } catch (err) {
    throw pipFailure((err as Error).message);
  }
}

/**
 * pip 安装参数：镜像源可选，重试 / 超时写死成「快速失败」（与 Docker entrypoint 对齐）。
 * @param bypassProxy 重试时显式关掉代理（pip 的 `--proxy ""` 优先级高于 pip.ini）。
 */
function pipInstallArgs(
  requirements: string,
  opts: { bypassProxy?: boolean } = {},
): string[] {
  const args = [
    '-m', 'pip', 'install', '-q',
    '--disable-pip-version-check', '--retries', '2', '--timeout', '15',
  ];
  if (opts.bypassProxy) args.push('--proxy', '');
  const index = process.env[PIP_INDEX_ENV]?.trim();
  if (index) {
    args.push('-i', index);
    // 镜像站一般是单一 https 源；只给它的 host 放宽证书校验（自签 / 代理场景）。
    const host = indexHost(index);
    if (host) args.push('--trusted-host', host);
  }
  args.push('-r', requirements);
  return args;
}

/** 从索引 URL 取 host（取不到就让 pip 自己报 URL 错误，不在这里吞掉）。 */
function indexHost(index: string): string {
  try {
    return new URL(index).host;
  } catch {
    return '';
  }
}

/**
 * pip 子进程环境。
 *
 * - 强制 Python 子进程用 UTF-8 输出：Windows 控制台默认 GBK，中文报错经 Node 按
 *   UTF-8 解出来就是一片 `@eeee…` 乱码，用户根本看不懂；
 * - `CODA_INSTALL_PROXY` 只注入给 pip 这一个子进程（不污染后端与智能体出口）。
 */
function pipEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    ...extra,
  };
  const proxy = process.env[INSTALL_PROXY_ENV]?.trim();
  if (proxy) {
    env.HTTP_PROXY = proxy;
    env.HTTPS_PROXY = proxy;
    env.http_proxy = proxy;
    env.https_proxy = proxy;
  }
  return env;
}

/** 失败信息是否指向代理层（ProxyError / Windows 10053/10060/10061）。 */
function isProxyFailure(detail: string): boolean {
  return /ProxyError|Cannot connect to proxy|10053|10060|10061/i.test(detail);
}

/** 给 pip 失败补上可照做的中文说明（错误必须显式暴露且能照着修）。 */
function pipFailure(detail: string): Error {
  const retried = isProxyFailure(detail) ? '（已自动绕过代理重试一次，仍然失败）' : '';
  return new Error(
    [
      `后端依赖安装失败${retried}。`,
      '1) 若开着代理软件（Clash / V2Ray / 公司代理），确认它正在运行；',
      '   或关闭 Windows「设置 → 网络和 Internet → 代理」里的系统代理后重启应用重试。',
      `2) 国内网络可改走镜像：设置环境变量 ${PIP_INDEX_ENV}=https://pypi.tuna.tsinghua.edu.cn/simple，`,
      '   或在 %APPDATA%\\pip\\pip.ini 写 [global] index-url = https://pypi.tuna.tsinghua.edu.cn/simple；',
      `3) 必须走代理时设置 ${INSTALL_PROXY_ENV}=http://127.0.0.1:7890（只影响装依赖）。`,
      `完整日志：${installLogPath()}`,
      '',
      detail,
    ].join('\n'),
  );
}

/** requirements.txt 的 sha256；文件缺失时返回空串（调用方据此报错）。 */
function requirementsStamp(file: string): string {
  try {
    return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch {
    return '';
  }
}

interface RunOptions {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
}

/** 跑一条子命令，stdout/stderr 逐行转发；非零退出抛错，并把完整输出落到安装日志。 */
function runCommand(cmd: string, args: string[], opts: RunOptions = {}): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, {
      cwd: opts.cwd,
      env: opts.env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let tail = '';
    let output = '';
    const feed = (buf: Buffer): void => {
      const text = buf.toString('utf8');
      output = (output + text).slice(-64_000); // 完整日志留一份，但有上限
      tail = (tail + text).slice(-4000); // 弹窗只回显尾部，避免刷屏
      process.stdout.write(text);
    };
    child.stdout?.on('data', feed);
    child.stderr?.on('data', feed);
    child.on('error', (err) => reject(new Error(`${cmd} 无法启动：${err.message}`)));
    child.on('exit', (code) => {
      if (code === 0) {
        resolve();
        return;
      }
      writeInstallLog(cmd, args, output);
      reject(new Error(`${cmd} ${args.join(' ')} 退出码 ${code}\n${tail}`));
    });
  });
}

/**
 * 把失败命令的完整输出写进安装日志（GUI 应用没有控制台，弹窗只能看到尾部）。
 * 写不进去也不能盖住真正的错误，这里只做尽力而为。
 */
function writeInstallLog(cmd: string, args: string[], output: string): void {
  try {
    fs.writeFileSync(installLogPath(), `$ ${cmd} ${args.join(' ')}\n${output}\n`);
  } catch {
    /* 日志失败不影响报错本身 */
  }
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
