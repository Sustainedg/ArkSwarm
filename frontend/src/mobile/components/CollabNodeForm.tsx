import { useState } from 'react'
import { DynamicForm } from '../../components/DynamicForm'
import { ACTIONS, DUTIES, SCOPES, sanitizeKey } from '../../lib/collabDraft'
import type {
  AgentType, CollabDuty, CollabNode, CollabTicketAction, CollabWorkspaceScope, Role,
} from '../../lib/types'


/**
 * 协作「岗位」表单（移动端）：底部弹层里的内容，本地编辑、点「保存岗位」才写回草稿。
 *
 * 只编辑这一个节点；改名会同步连线，所以 key 输入框每次按键都按后端规则规整（`sanitizeKey`）。
 * 「参数覆盖」直接复用 `DynamicForm`（按该角色绑定智能体类型的 `config_fields` 渲染）。
 */
export function CollabNodeForm({
  node,
  roles,
  agentTypes,
  isEntry,
  onSave,
  onDelete,
  onSetEntry,
}: {
  node: CollabNode
  roles: Role[]
  agentTypes: AgentType[]
  isEntry: boolean
  onSave: (next: CollabNode) => void
  onDelete: () => void
  onSetEntry: () => void
}) {
  const [form, setForm] = useState<CollabNode>(node)
  const [keyInput, setKeyInput] = useState(node.key)
  const role = roles.find((item) => item.id === form.role_id)
  const type = agentTypes.find((item) => item.id === role?.agent_type_id)

  const set = (next: Partial<CollabNode>) => setForm((prev) => ({ ...prev, ...next }))
  const toggleAction = (action: CollabTicketAction) =>
    set({
      output_contract: form.output_contract.includes(action)
        ? form.output_contract.filter((item) => item !== action)
        : [...form.output_contract, action],
    })

  return (
    <>
      <div className="field">
        <label className="field-label">角色</label>
        <select className="select" value={form.role_id} onChange={(event) => set({ role_id: event.target.value })}>
          {roles.map((item) => (
            <option key={item.id} value={item.id}>
              {item.avatar} {item.name} · {item.agent_name}
            </option>
          ))}
        </select>
        <span className="field-help">
          {role ? `智能体：${role.agent_name}（${role.agent_type_id}）` : '该角色已不存在，请重新选择'}
        </span>
      </div>

      <div className="field">
        <label className="field-label">显示名</label>
        <input className="input" value={form.label} onChange={(event) => set({ label: event.target.value })} />
      </div>

      <div className="field">
        <label className="field-label">标识（key）</label>
        <input
          className="input mono"
          value={keyInput}
          onChange={(event) => {
            const next = sanitizeKey(event.target.value)
            setKeyInput(next)
            set({ key: next })
          }}
          placeholder="小写字母/数字/-_，例如 cto"
        />
        <span className="field-help">工单里用它指代岗位；改名会同步更新所有连线。</span>
      </div>

      <div className="field">
        <label className="field-label">岗位</label>
        <select className="select" value={form.duty} onChange={(event) => set({ duty: event.target.value as CollabDuty })}>
          {DUTIES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <span className="field-help">{DUTIES.find((item) => item.value === form.duty)?.hint}</span>
      </div>

      <div className="field">
        <label className="field-label">职责说明</label>
        <textarea
          className="textarea"
          style={{ minHeight: 74 }}
          value={form.instructions}
          onChange={(event) => set({ instructions: event.target.value })}
          placeholder="追加在角色人格之后，例如：负责拆解任务与汇总"
        />
      </div>

      <div className="field">
        <label className="field-label">能力边界</label>
        <textarea
          className="textarea"
          style={{ minHeight: 60 }}
          value={form.capability_scope}
          onChange={(event) => set({ capability_scope: event.target.value })}
          placeholder="例如：不得写代码、不得访问工作目录"
        />
        <span className="field-help">会写进该节点的提示词；真正的兜底是工作目录范围与越权审计。</span>
      </div>

      <div className="field">
        <label className="field-label">工作目录范围</label>
        <select
          className="select"
          value={form.workspace_scope ?? ''}
          onChange={(event) => set({ workspace_scope: (event.target.value || null) as CollabWorkspaceScope | null })}
        >
          {SCOPES.map((item) => (
            <option key={String(item.value)} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field-label">同一工单最大返工轮次</label>
        <input
          className="input mono"
          type="number"
          min={0}
          value={form.max_rounds}
          onChange={(event) => set({ max_rounds: Math.max(0, Number(event.target.value) || 0) })}
        />
        <span className="field-help">0 表示用团队默认；与团队上限取小。</span>
      </div>

      <div className="field">
        <label className="field-label">允许发起的动作</label>
        <div className="m-chip-row">
          {ACTIONS.map((action) => (
            <label key={action.value} className={`m-chip${form.output_contract.includes(action.value) ? ' on' : ''}`}>
              <input
                type="checkbox"
                checked={form.output_contract.includes(action.value)}
                onChange={() => toggleAction(action.value)}
              />
              {action.label}
            </label>
          ))}
        </div>
        <span className="field-help">一个都不选 = 按岗位取默认动作集合。</span>
      </div>

      {type && type.config_fields.length > 0 && (
        <div className="field">
          <label className="field-label">参数覆盖（只对本次协作生效）</label>
          <DynamicForm
            fields={type.config_fields}
            value={form.options_override}
            onChange={(next) => set({ options_override: next })}
          />
        </div>
      )}

      <button className="btn sm" onClick={onSetEntry} disabled={isEntry}>
        {isEntry ? '★ 已是入口节点' : '设为入口节点（与用户对话）'}
      </button>

      <div className="row" style={{ gap: 8, marginTop: 4 }}>
        <button className="btn danger" onClick={onDelete}>
          删除岗位
        </button>
        <button className="btn primary" style={{ flex: 1 }} onClick={() => onSave(form)} disabled={!form.key}>
          保存岗位
        </button>
      </div>
    </>
  )
}
