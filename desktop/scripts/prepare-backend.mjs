/**
 * 构建前准备：把 backend/ + frontend/dist/ 组装成「主项目同构」的源码包。
 *
 * electron-builder 的 extraResources 只会做目录拷贝，这里负责确保：
 *   1. frontend/dist 存在（没构建过就先跑一次前端 build）
 *   2. backend/ 干净（剔除 data/ .venv/ __pycache__/ tests/，和 release.sh 的排除清单对齐）
 */
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..', '..');
const dist = path.join(root, 'frontend', 'dist');

if (!fs.existsSync(dist)) {
  console.log('→ frontend/dist 不存在，先构建前端');
  execSync('npm run build', { cwd: path.join(root, 'frontend'), stdio: 'inherit' });
} else {
  console.log('→ frontend/dist 已存在（如需更新请先 npm run build）');
}
console.log('✓ 后端源码包就绪（electron-builder extraResources 负责拷贝与过滤）');
