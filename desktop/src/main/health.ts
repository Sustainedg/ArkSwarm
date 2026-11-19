/**
 * 健康检查轮询：等本地 uvicorn 就绪再加载页面（M1 的就绪门控）。
 *
 * 后端 `/api/health` 免鉴权（system.py 里无 current_user 依赖），适合做探活。
 */
import * as http from 'node:http';

export interface HealthInfo {
  version: string;
}

/** 单次探测 `/api/health`；成功返回版本信息，失败返回 null（不抛错，交给轮询重试）。 */
function probe(port: number, timeoutMs: number): Promise<HealthInfo | null> {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/api/health', timeout: timeoutMs },
      (res) => {
        // 只要 200 才算就绪；其他状态码（含被代理劫持的响应）一律视为未就绪
        if (res.statusCode !== 200) {
          res.resume();
          resolve(null);
          return;
        }
        const chunks: Buffer[] = [];
        res.on('data', (c: Buffer) => chunks.push(c));
        res.on('end', () => {
          try {
            const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            resolve({ version: String(body.version ?? '') });
          } catch {
            resolve(null);
          }
        });
      },
    );
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
    req.on('error', () => resolve(null));
  });
}

export interface WaitOptions {
  /** 轮询间隔（毫秒），默认 500。 */
  intervalMs?: number;
  /** 最长等待（毫秒），默认 60_000。 */
  timeoutMs?: number;
  /** 每次探测的超时（毫秒），默认 2_000。 */
  probeTimeoutMs?: number;
  /** 可选：拿到版本号时回调一次（用于日志/启动页展示）。 */
  onVersion?: (info: HealthInfo) => void;
}

/**
 * 轮询直到 `/api/health` 返回 200。
 * @throws Error 超时（默认 60 秒）仍不就绪时抛出，由调用方显式展示给用户（铁律 11）
 */
export async function waitForHealth(port: number, opts: WaitOptions = {}): Promise<HealthInfo> {
  const interval = opts.intervalMs ?? 500;
  const deadline = Date.now() + (opts.timeoutMs ?? 60_000);
  while (Date.now() < deadline) {
    const info = await probe(port, opts.probeTimeoutMs ?? 2_000);
    if (info) {
      opts.onVersion?.(info);
      return info;
    }
    await sleep(interval);
  }
  throw new Error(`后端 ${deadline / 1000}s 内未就绪（127.0.0.1:${port}/api/health）`);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
