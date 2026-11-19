/**
 * macOS 公证（notarization）：electron-builder 的 afterSign 钩子。
 *
 * 三种凭据缺一不可（CI 里放在仓库 Secrets，本地放在环境里）：
 *   APPLE_ID                     Apple 开发者账号
 *   APPLE_APP_SPECIFIC_PASSWORD  该账号的 App 专用密码（不是登录密码）
 *   APPLE_TEAM_ID                团队 ID（开发者后台 Membership 页）
 *
 * 没有配置时**不报错、直接跳过**：M4 之前 / 未购证书的构建照常产出 unsigned 包，
 * 只是用户首次打开要右键「打开」（README 有引导）。签名本身由 electron-builder
 * 按 CSC_LINK + CSC_KEY_PASSWORD 自动完成，不需要脚本参与。
 */
const path = require('node:path');

module.exports = async function notarizeAfterSign(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const { APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID } = process.env;
  if (!APPLE_ID || !APPLE_APP_SPECIFIC_PASSWORD || !APPLE_TEAM_ID) {
    console.log(
      '[notarize] 未配置 APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID，跳过公证（unsigned 构建）',
    );
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${appName}.app`);
  // 延迟 require：只有真正要公证时才加载，未配置凭据的构建不需要这个依赖
  const { notarize } = require('@electron/notarize');

  console.log(`[notarize] 提交公证：${appPath}`);
  await notarize({
    appPath,
    appleId: APPLE_ID,
    appleIdPassword: APPLE_APP_SPECIFIC_PASSWORD,
    teamId: APPLE_TEAM_ID,
  });
  console.log('[notarize] 公证完成，可以装订（staple）分发');
};