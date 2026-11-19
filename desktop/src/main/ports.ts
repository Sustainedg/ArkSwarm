/**
 * 空闲端口探测：先占位再释放，把确定的端口号传给后端。
 *
 * 为什么不用 port=0：uvicorn 真实监听端口要等启动后才能从日志里抠，
 * 健康检查反而不知道往哪发。Electron 侧先选好端口再 spawn，简单可靠。
 */
import * as net from 'node:net';

/** 找一个当前空闲的 TCP 端口；失败抛错（铁律 11：不静默吞）。 */
export function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', (err) => {
      reject(new Error(`无法探测空闲端口：${(err as Error).message}`));
    });
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      if (addr === null || typeof addr === 'string') {
        server.close();
        reject(new Error('探测空闲端口时拿不到端口号'));
        return;
      }
      const { port } = addr;
      server.close(() => resolve(port));
    });
  });
}

/** 检查端口是否被占用（启动前探测残留进程用）。 */
export function isPortInUse(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once('error', () => resolve(true));
    server.once('listening', () => server.close(() => resolve(false)));
    server.listen(port, '127.0.0.1');
  });
}
