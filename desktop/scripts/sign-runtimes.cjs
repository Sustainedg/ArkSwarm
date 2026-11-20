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
 *   - 有 Developer ID Application 身份（CSC_LINK / CSC_NAME 已配置）→ 逐个签内嵌运行时，
 *     交给 electron-builder 后续做顶层签名与公证；
 *   - **没有证书 → 对整包做 ad-hoc 签名**（`codesign --sign -`）。这一步是必须的：
 *     electron-builder 改过名/改过 Info.plist 之后，Electron 的原签名已经失效，
 *     不重签的 `.app` 在用户机器上会被 Gatekeeper 判成「已损坏」，
 *     而「已损坏」在系统设置里**没有**「仍要打开」的入口。
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

/**
 * 没有证书时的兜底：对整包做 **ad-hoc 签名**（`codesign --sign -`）。
 *
 * 为什么需要：
 *   electron-builder 打包 macOS 时会改名应用、改 Info.plist、换图标，
 *   这些操作会让 Electron 自带的签名失效。未重新签名的 `.app` 被下载后带上 quarantine，
 *   Gatekeeper 判定的是「**已损坏**」——这个状态在系统设置里**没有**「仍要打开」的入口，
 *   用户只能靠终端 `xattr` / 手动签名自救。
 *
 * ad-hoc 签名的效果：
 *   `.app` 重新拥有一份**自洽**的签名（虽然不受 Apple 信任），Gatekeeper 于是降级为
 *   「**未识别开发者**」——系统设置 → 隐私与安全性会出现「仍要打开」，双击 → 允许即可。
 *   这是不买证书时能拿到的最好体验；真正的双击无提示仍然需要 Developer ID + 公证。
 *
 * 为什么用 --deep：
 *   Electron 的 helper app / framework / 内嵌 Python·Node 的 Mach-O 都得重签，
 *   逐个列太脆；`--deep` 是 Apple 给的「整包递归签」入口（虽然官方不推荐用于正式签名，
 *   但 ad-hoc 救急正好）。签完立刻 `--verify --deep --strict` 自检，失败必须显式报错 ——
 *   否则我们会安静地发出一份仍然「已损坏」的包。
 */
function adhocSign(context) {
  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${appName}.app`);
  if (!fs.existsSync(appPath)) {
    throw new Error(`[sign-runtimes] ad-hoc 签名失败：找不到 ${appPath}`);
  }

  console.log('[sign-runtimes] 未找到 Developer ID 证书 → 对整包做 ad-hoc 签名（消除「已损坏」）');
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', appPath], { stdio: 'inherit' });
  // 自检：签名自洽才算兜底成功。失败会抛出 → 构建失败，而不是发出一份坏包。
  execFileSync('codesign', ['--verify', '--deep', '--strict', '--verbose=2', appPath], {
    stdio: 'inherit',
  });
  console.log('[sign-runtimes] ad-hoc 签名完成（用户首次打开需在「隐私与安全性」里点「仍要打开」）');
}

module.exports = async function signRuntimes(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const identity = findIdentity();
  if (!identity) {
    // 没有证书：不再直接跳过，走 ad-hoc 兜底
    adhocSign(context);
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