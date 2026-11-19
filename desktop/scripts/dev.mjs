/**
 * 开发模式入口：编译主进程 → 起 Electron，日志带颜色前缀。
 *
 * 开发模式下后端直接用仓库 backend/ 源码 + 系统 python3（venv 建在
 * <仓库>/backend/.venv_desktop，避免污染 serve.sh 的 .venv）。
 */
import { execSync, spawn } from 'node:child_process';
import path from 'node:path';

const dir = import.meta.dirname;

console.log('→ 编译主进程');
execSync('tsc -p tsconfig.main.json', { cwd: dir, stdio: 'inherit' });
execSync('tsc -p tsconfig.preload.json', { cwd: dir, stdio: 'inherit' });

console.log('→ 启动 Electron（开发模式）');
const child = spawn('npx', ['electron', '.'], {
  cwd: dir,
  stdio: 'inherit',
  env: { ...process.env, ARKSWARM_DESKTOP_DEV: '1' },
});
child.on('exit', (code) => process.exit(code ?? 0));
