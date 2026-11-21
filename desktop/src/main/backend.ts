/**
 * 后端子进程管理：spawn uvicorn、追踪、优雅退出、Windows 进程树清理。
 *
 * 铁律 10：子进程要能优雅退出（SIGTERM + 超时强杀）；
 * Windows 上 uvicorn 不会自己杀孙子进程（CLI 内核），必须杀整棵树。
 *
 * ⚠ execv 陷阱（selfupdate.py 的 schedule_restart 用 os.execv 原地重启）：
 * execv 保持同一个 PID —— 后端进程**不会退出**，Electron 看不到 exit 事件。
 * 因此本模块还提供 `watchRestart`：让主进程能感知「后端自己换了」
 * （靠轮询版本号变化，而不是等 exit 事件，那个永远不会来）。
 */
import { spawn, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { dataDir, venvPython } from './paths';
import { desktopRuntimeEnv } from './runtimeEnv';

export interface SpawnOptions {
  port: number;
  /** 后端应用根（releases/current 的 backend；源码模式 = 仓库 backend/）。 */
  backendRoot: string;
  /**
   * 跑后端的解释器：内置运行时（依赖已预装）或 userData 里的 venv python。
   * 由 pythonRuntime.ensurePythonRuntime 决定，别在这里自己猜。
   */
  python: string;
}

export interface BackendHandle {
  child: ChildProcess;
  /** 优雅停止：SIGTERM/杀树 + 宽限期后强杀。 */
  stop: (graceMs?: number) => Promise<void>;
}

/**
 * 感知后端 execv 自更新（界面里点「安装更新」后发生）。
 *
 * execv 保持同一个 PID：进程不退出，Electron 永远等不到 exit 事件。
 * 所以改用「版本号轮询」：/api/health 的 version 变了 = 后端换了新版本，
 * 此时应重新加载窗口页面（前端静态资源已换血）。
 */
export function watchBackendRestart(
  port: number,
  initialVersion: string,
  onRestarted: () => void,
): NodeJS.Timeout {
  let lastVersion = initialVersion;
  // 10 秒一拍：execv 后服务最多几秒恢复，页面刷新晚几秒无伤大雅
  return setInterval(() => {
    void fetch(`http://127.0.0.1:${port}/api/health`)
      .then((res) => (res.ok ? (res.json() as Promise<{ version?: string }>) : null))
      .then((body) => {
        const version = body?.version;
        if (version && version !== lastVersion) {
          lastVersion = version;
          onRestarted();
        }
      })
      .catch(() => undefined); // 服务重启窗口期探测失败很正常，静默重试
  }, 10_000);
}

/** Windows 下杀整棵进程树（taskkill /T /F），否则会留孤儿 uvicorn。 */
function killTree(pid: number, force: boolean): void {
  if (process.platform === 'win32') {
    spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
    return;
  }
  try {
    // detached:true 时负 pid = 整个进程组，一锅端；进程不在组里再退化为直杀
    process.kill(-pid, force ? 'SIGKILL' : 'SIGTERM');
  } catch {
    try {
      process.kill(pid, force ? 'SIGKILL' : 'SIGTERM');
    } catch {
      /* 已退出：无事可做 */
    }
  }
}

/** 拉起后端（uvicorn）。要求 venv 已就绪（pythonRuntime.ts 负责）。 */
export function spawnBackend(opts: SpawnOptions): BackendHandle {
  const child = spawn(
    opts.python,
    [
      '-m', 'uvicorn', 'app.main:app',
      '--host', '127.0.0.1',
      '--port', String(opts.port),
      '--app-dir', opts.backendRoot,
    ],
    {
      // detached + unix 进程组：退出时 kill(-pid) 一次带走全组
      detached: process.platform !== 'win32',
      windowsHide: true,
      env: {
        ...process.env,
        // 内置 Node / npm 全局前缀 / PATH 前置（M4：内核安装与运行不依赖系统 Node）。
        // 后端进程继承这份环境，`runtime/provisioning` 起 npm、执行层 spawn 内核时都用它。
        ...desktopRuntimeEnv(),
        CODA_PORT: String(opts.port),
        CODA_DATA_DIR: dataDir(),
        // 桌面单机形态：后端只听本机，绝不对局域网暴露
        CODA_PUBLIC_BASE_URL: `http://127.0.0.1:${opts.port}`,
        PYTHONUNBUFFERED: '1',
        // 继承用户 PATH：内核（claude / codex / gemini）与 node 要能被 provisioning 找到
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  if (!child.stdout || !child.stderr) {
    // stdio 配置决定了这两个流必在；防御性检查
    throw new Error('后端子进程的 stdio 流缺失（内部错误）');
  }
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  const log = (line: string): void => {
    process.stdout.write(`[backend] ${line}`);
  };
  child.stdout.on('data', log);
  child.stderr.on('data', log);
  child.on('exit', (code, signal) => {
    log(`后端进程退出 code=${code} signal=${signal}\n`);
  });

  const stop = async (graceMs = 5_000): Promise<void> => {
    if (child.exitCode !== null || child.signalCode !== null) return; // 已退出
    const pid = child.pid;
    if (pid === undefined) return;
    if (process.platform === 'win32') {
      killTree(pid, true); // taskkill /T 连树带杀（/F 无优雅概念）
      return;
    }
    killTree(pid, false);
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        killTree(pid, true);
        resolve();
      }, graceMs);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  };
  return { child, stop };
}

/** venv 是否就绪（python 可执行存在）——启动流程用。 */
export function venvReady(): boolean {
  return fs.existsSync(venvPython());
}

/** 辅助：日志文件路径（backend.log 放 userData）。 */
export function backendLogPath(): string {
  return path.join(dataDir(), '..', 'backend.log');
}
