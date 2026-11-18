/**
 * MCP 目录项的展示辅助（桌面端与移动端共用）。
 *
 * 只放与界面无关的纯函数：同一条服务器在两端必须显示同一种状态，
 * 否则「待填参数」在桌面端看得见、手机上却看不见。
 */
import type { McpServer } from './types'

/**
 * 内置服务器还有必填参数没填（平台托管的不算），界面应显示「待填参数」。
 *
 * 「有值」的判定与表单一致：已保存的值只回掩码，留空提交表示保持原值。
 */
export function hasMissingBuiltinParams(server: McpServer): boolean {
  return (server.builtin_params ?? []).some(
    (param) =>
      param.required &&
      !param.managed &&
      !(server.env_preview[param.key] ?? param.masked),
  )
}
