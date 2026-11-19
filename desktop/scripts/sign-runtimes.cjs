/**
 * 给内嵌运行时（Python / Node）里的 Mach-O 逐个签名 —— electron-builder 的 `afterPack` 钩子。
 *
 * 为什么必须自己签：
 *   electron-builder 只会签它认识的 Electron 框架与顶层 App；`extraResources` 里铺进去的
 *   python-build-standalone 与 Node 发行包（各自带几十上百个 .dylib / .so）不在它的扫描范围。
 *   而 Apple 的公证服务要求**包里每一个 Mach-O 都带有效签名**，漏一个就是
 *   "The signature of the binary ... is invalid" —— 公证直接失败。
 *
 * 什么时候生效：
 *   只有 macOS + 证书环境（CSC_LINK / CSC_NAME 已配置、钥匙串里能找到
 *   "Developer ID Application" 身份）时才动手；本地开发与 unsigned 构建会直接跳过，
 *   不影响打包。
 *
 * 为什么逐个 `file` 判断再签：
 *   `codesign` 对非 Mach-O 文件（.py、.js、.json）会报错；只对 `file` 识别为
 *   Mach-O 的普通文件动手，目录（如 .framework 由 electron-builder 自家处理）跳过。
 *   签名从最深层往上层走（Apple 的规矩：先内后外）。
 */
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

/** 这些运行时目录里的东西需要自己签名（相对 Contents/Resources）。 */
const RUNTIME_DIRS = ['python-runtime', 'node-runtime'];

function findIdentity() {
  const explicit = process.env.CSC_NAME?.trim();
  if (explicit) return explicit;
  try {
    const out = execFileSync('security', ['find-identity', '-v', '-p', 'codesigning'], {
      encoding: 'utf8',
    });
    const match = out.match(/"(Developer ID Application:[^"]+)"/);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}

/** 递归收集需要签名的 Mach-O 文件（深度降序，先签里层）。 */
function collectMachO(root) {
  if (!fs.existsSync(root)) return [];
  const found = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!entry.isFile() || entry.isSymbolicLink()) continue;
      if (!shouldCheck(entry.name)) continue;
      found.push(full);
    }
  };
  walk(root);
  return found.sort((a, b) => b.split(path.sep).length - a.split(path.sep).length);
}

/** 只对「可能是 Mach-O」的名字做 file 探测，避免在上百个 .py/.json 上起子进程。 */
function shouldCheck(name) {
  if (/\.(dylib|so|node)$/i.test(name)) return true;
  if (!name.includes('.')) return true; // bin/node、bin/python3 这类无扩展名的可执行文件
  return /^(python|node)[\d.]*$/i.test(name); // bin/python3.11
}

function isMachO(file) {
  try {
    return execFileSync('file', ['-b', file], { encoding: 'utf8' }).includes('Mach-O');
  } catch {
    return false;
  }
}

module.exports = async function signRuntimes(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const identity = findIdentity();
  if (!identity) {
    console.log('[sign-runtimes] 没有可用的 Developer ID Application 身份，跳过内嵌运行时签名');
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const resources = path.join(context.appOutDir, `${appName}.app`, 'Contents', 'Resources');
  const entitlements = path.join(__dirname, '..', 'build', 'entitlements.mac.plist');

  let signed = 0;
  for (const dirName of RUNTIME_DIRS) {
    for (const file of collectMachO(path.join(resources, dirName))) {
      if (!isMachO(file)) continue;
      execFileSync(
        'codesign',
        [
          '--force',
          '--timestamp',
          '--options',
          'runtime',
          '--entitlements',
          entitlements,
          '--sign',
          identity,
          file,
        ],
        { stdio: 'inherit' },
      );
      signed += 1;
    }
  }
  console.log(`[sign-runtimes] 已签名 ${signed} 个内嵌运行时二进制（${identity}）`);
};