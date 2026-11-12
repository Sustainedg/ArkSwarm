/**
 * 编排编辑器的右侧属性面板：节点 / 边 / 团队三种上下文共用一个面板。
 *
 * 节点上的「参数覆盖」直接复用 `DynamicForm`（读该智能体类型的 `config_fields`），
 * 因此新增一个可配置项不需要动协作页（与 AGENTS.md §4.4 一致）。
 */
import type { ReactNode } from 'react'
import { DynamicForm } from './DynamicForm'
import type { CollabSelection } from './CollabCanvas'
import { ACTIONS, DUTIES, EDGE_KINDS, SCOPES, isWarningIssue, type TeamDraft } from '../lib/collabDraft'
import type {
  AgentType, CollabBudget, CollabEdge, CollabEdgeKind, CollabIssue, CollabNode,
  CollabWorkspaceScope, CollabDuty as Duty, AgentProfile,
} from '../lib/types'

interface Props {
  draft: TeamDraft
  selection: CollabSelection | null
  agents: AgentProfile[]
  agentTypes: AgentType[]
  issues: CollabIssue[]
  onPatchTeam: (patch: Partial<TeamDraft>) => void
  onPatchNode: (key: string, patch: Partial<CollabNode>) => void
  onPatchEdge: (id: string, patch: Partial<CollabEdge>) => void
  onRenameNode: (oldKey: string, newKey: string) => void
  onDeleteNode: (key: string) => void
  onDeleteEdge: (id: string) => void
}

export function NodeInspector({
  draft, selection, agents, agentTypes, issues,
  onPatchTeam, onPatchNode, onPatchEdge, onRenameNode, onDeleteNode, onDeleteEdge,
}: Props) {
  const node = selection?.kind === 'node' ? draft.nodes.find((item) => item.key === selection.id) : undefined
  const edge = selection?.kind === 'edge' ? draft.edges.find((item) => item.id === selection.id) : undefined

  return (
    <aside className="collab-inspector">
      {node ? (
        <NodePanel
          node={node}
          nodes={draft.nodes}
          entryKey={draft.entry_key}
          agents={agents}
          agentTypes={agentTypes}
          issues={issues}
          onPatch={(patch) => onPatchNode(node.key, patch)}
          onRename={(next) => onRenameNode(node.key, next)}
          onDelete={() => onDeleteNode(node.key)}
          onSetEntry={() => onPatchTeam({ entry_key: node.key })}
        />
      ) : edge ? (
        <EdgePanel
          edge={edge}
          nodes={draft.nodes}
          onPatch={(patch) => onPatchEdge(edge.id, patch)}
          onDelete={() => onDeleteEdge(edge.id)}
        />
      ) : (
        <TeamPanel draft={draft} issues={issues} onPatchTeam={onPatchTeam} />
      )}
    </aside>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="collab-section">
      <h3 className="collab-section-title">{title}</h3>
      {children}
    </section>
  )
}

function NodePanel({
  node, nodes, entryKey, agents, agentTypes, issues, onPatch, onRename, onDelete, onSetEntry,
}: {
  node: CollabNode
  nodes: CollabNode[]
  entryKey: string | null
  agents: AgentProfile[]
  agentTypes: AgentType[]
  issues: CollabIssue[]
  onPatch: (patch: Partial<CollabNode>) => void
  onRename: (next: string) => void
  onDelete: () => void
  onSetEntry: () => void
}) {
  const agent = agents.find((item) => item.id === node.agent_profile_id)
  const type = agentTypes.find((item) => item.id === agent?.type_id)
  const nodeIssues = issues.filter((issue) => issue.node_key === node.key)
  const isEntry = entryKey === node.key
  // 历史数据可能给非入口节点勾过 ask_user、给入口节点勾过 escalate；这里统一规整，
  // 任何一次改动都会把它清掉（运行时本来就会忽略，界面别显示成「生效了」）。
  const contract = node.output_contract.filter(
    (item) => (item !== 'ask_user' || isEntry) && (item !== 'escalate' || !isEntry),
  )

  return (
    <>
      <div className="collab-inspector-head">
        <span className="collab-node-avatar">{agent?.avatar ?? '🤖'}</span>
        <div style={{ minWidth: 0 }}>
          <div className="collab-inspector-title">{node.label || agent?.name || node.key}</div>
          <div className="dim small mono-sm">{node.key}</div>
        </div>
      </div>

      {nodeIssues.length > 0 && (
        <div className="stack" style={{ gap: 6 }}>
          {nodeIssues.map((issue, index) => (
            <div key={`${issue.code}-${index}`} className={`banner ${isWarningIssue(issue) ? 'info' : 'err'}`}>
              {issue.message}
            </div>
          ))}
        </div>
      )}

      <Section title="这个节点是谁">
        <label className="field">
          <span className="field-label">智能体</span>
          <select className="select" value={node.agent_profile_id} onChange={(e) => onPatch({ agent_profile_id: e.target.value })}>
            {agents.map((item) => (
              <option key={item.id} value={item.id}>
                {item.avatar} {item.name} · {item.type_name ?? item.type_id}
              </option>
            ))}
          </select>
          <span className="field-help">
            {agent ? `内核：${agent.type_name ?? agent.type_id} · 模型：${agent.model ?? '默认'}` : '该智能体已不存在，请重新选择'}
          </span>
        </label>
        <label className="field">
          <span className="field-label">显示名</span>
          <input className="input" value={node.label} onChange={(e) => onPatch({ label: e.target.value })} placeholder="显示在画布与时间线上" />
        </label>
        <label className="field">
          <span className="field-label">标识（key）</span>
          <input
            className="input mono"
            value={node.key}
            onChange={(e) => onRename(e.target.value)}
            placeholder="小写字母/数字/-_，例如 cto"
          />
          <span className="field-help">工单里用它指代岗位；改名会同步更新所有连线。</span>
        </label>
      </Section>

      <Section title="在这个协作里干什么">
        <label className="field">
          <span className="field-label">岗位</span>
          <select className="select" value={node.duty} onChange={(e) => onPatch({ duty: e.target.value as Duty })}>
            {DUTIES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <span className="field-help">{DUTIES.find((item) => item.value === node.duty)?.hint}</span>
        </label>
        <label className="field">
          <span className="field-label">职责说明</span>
          <textarea
            className="textarea"
            style={{ minHeight: 74 }}
            value={node.instructions}
            onChange={(e) => onPatch({ instructions: e.target.value })}
            placeholder="追加在智能体人格之后，例如：负责拆解任务与汇总"
          />
        </label>
        <label className="field">
          <span className="field-label">能力边界</span>
          <textarea
            className="textarea"
            style={{ minHeight: 60 }}
            value={node.capability_scope}
            onChange={(e) => onPatch({ capability_scope: e.target.value })}
            placeholder="例如：不得写代码、不得访问工作目录"
          />
          <span className="field-help">会写进该节点的提示词；真正的兜底是工作目录范围与越权审计。</span>
        </label>
        <label className="field">
          <span className="field-label">工作目录范围</span>
          <select
            className="select"
            value={node.workspace_scope ?? ''}
            onChange={(e) => onPatch({ workspace_scope: (e.target.value || null) as CollabWorkspaceScope | null })}
          >
            {SCOPES.map((item) => (
              <option key={String(item.value)} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">同一工单最大返工轮次</span>
          <input
            className="input mono"
            type="number"
            min={0}
            value={node.max_rounds}
            onChange={(e) => onPatch({ max_rounds: Math.max(0, Number(e.target.value) || 0) })}
          />
          <span className="field-help">0 表示用团队默认；与团队上限取小。</span>
        </label>
        <div className="field">
          <span className="field-label">允许发起的动作</span>
          <div className="chips">
            {ACTIONS.map((action) => {
              const checked = contract.includes(action.value)
              const lockHint =
                action.value === 'ask_user' && !isEntry
                  ? '只有入口节点能向用户提问'
                  : action.value === 'escalate'
                    ? isEntry
                      ? '入口节点是最顶端，没有更上级可以上报（由 ask_user 收口）'
                      : '上报是保底动作：任何岗位都保留，不用勾也生效'
                    : undefined
              return (
                <label
                  key={action.value}
                  className={`collab-toggle${checked ? ' on' : ''}${lockHint ? ' disabled' : ''}`}
                  title={lockHint}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={Boolean(lockHint)}
                    onChange={() =>
                      onPatch({
                        output_contract: checked
                          ? contract.filter((item) => item !== action.value)
                          : [...contract, action.value],
                      })
                    }
                  />
                  {action.label}
                </label>
              )
            })}
          </div>
          <span className="field-help">
            一个都不选 = 按岗位取默认动作集合。ask_user 只有入口节点可用（只有它直接面对用户）；
            escalate 是保底动作，非入口节点始终可用。
          </span>
        </div>
        <button className="btn sm" onClick={onSetEntry} disabled={entryKey === node.key}>
          {entryKey === node.key ? '★ 已是入口节点' : '设为入口节点（与用户对话）'}
        </button>
      </Section>

      {type && type.config_fields.length > 0 && (
        <Section title="参数覆盖（只对本次协作生效）">
          <DynamicForm
            fields={type.config_fields}
            value={node.options_override}
            onChange={(next) => onPatch({ options_override: next })}
          />
        </Section>
      )}

      <div className="collab-inspector-foot">
        <button className="btn sm danger" onClick={onDelete}>
          删除这个节点
        </button>
        <span className="dim small">共 {nodes.length} 个节点</span>
      </div>
    </>
  )
}

function EdgePanel({
  edge, nodes, onPatch, onDelete,
}: {
  edge: CollabEdge
  nodes: CollabNode[]
  onPatch: (patch: Partial<CollabEdge>) => void
  onDelete: () => void
}) {
  const label = (key: string) => {
    const node = nodes.find((item) => item.key === key)
    return node ? `${node.label || node.key}（${key}）` : key
  }
  return (
    <>
      <div className="collab-inspector-head">
        <span className="collab-node-avatar">↦</span>
        <div>
          <div className="collab-inspector-title">连线</div>
          <div className="dim small">
            {label(edge.from_key)} → {label(edge.to_key)}
          </div>
        </div>
      </div>
      <Section title="这条连线允许什么">
        <label className="field">
          <span className="field-label">类型</span>
          <select className="select" value={edge.kind} onChange={(e) => onPatch({ kind: e.target.value as CollabEdgeKind })}>
            {EDGE_KINDS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <span className="field-help">{EDGE_KINDS.find((item) => item.value === edge.kind)?.hint}</span>
        </label>
        {edge.kind === 'rework' && (
          <div className="field-help">
            这条线本身已经指定打回目标，运行时不需要额外的 rework_to。只有当同一审查者画了多条
            rework 出边时，审查者才需要在工单里写明 rework_to 来选择退路。
          </div>
        )}
        {edge.kind === 'rework' && (
          <label className="field">
            <span className="field-label">这条边的返工上限</span>
            <input
              className="input mono"
              type="number"
              min={0}
              value={edge.max_rounds}
              onChange={(e) => onPatch({ max_rounds: Math.max(0, Number(e.target.value) || 0) })}
            />
            <span className="field-help">
              0 表示按团队默认；填了就取「边 / 节点 / 团队」三者最小，超过即挂起等你介入。
            </span>
          </label>
        )}
        <label className="field">
          <span className="field-label">备注（可选）</span>
          <input className="input" value={edge.label} onChange={(e) => onPatch({ label: e.target.value })} placeholder="画在连线上的一小段文字" />
        </label>
      </Section>
      <div className="collab-inspector-foot">
        <button className="btn sm danger" onClick={onDelete}>
          删除这条连线
        </button>
      </div>
    </>
  )
}

function TeamPanel({
  draft, issues, onPatchTeam,
}: {
  draft: TeamDraft
  issues: CollabIssue[]
  onPatchTeam: (patch: Partial<TeamDraft>) => void
}) {
  const setBudget = (patch: Partial<CollabBudget>) => onPatchTeam({ budget: { ...draft.budget, ...patch } })
  return (
    <>
      <div className="collab-inspector-head">
        <span className="collab-node-avatar">🤝</span>
        <div>
          <div className="collab-inspector-title">协作组设置</div>
          <div className="dim small">点画布空白处可以回到这里</div>
        </div>
      </div>

      {issues.length > 0 && (
        <div className="stack" style={{ gap: 6 }}>
          {issues.slice(0, 6).map((issue, index) => (
            <div key={`${issue.code}-${index}`} className={`banner ${isWarningIssue(issue) ? 'info' : 'err'}`}>
              {issue.message}
            </div>
          ))}
        </div>
      )}

      <Section title="基本信息">
        <label className="field">
          <span className="field-label">名称</span>
          <input className="input" value={draft.name} onChange={(e) => onPatchTeam({ name: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-label">说明</span>
          <textarea
            className="textarea"
            style={{ minHeight: 60 }}
            value={draft.description}
            onChange={(e) => onPatchTeam({ description: e.target.value })}
            placeholder="这个协作组是干什么的"
          />
        </label>
        <label className="field">
          <span className="field-label">入口节点</span>
          <select
            className="select"
            value={draft.entry_key ?? ''}
            onChange={(e) => onPatchTeam({ entry_key: e.target.value || null })}
          >
            <option value="">（未指定）</option>
            {draft.nodes.map((node) => (
              <option key={node.key} value={node.key}>
                {node.label || node.key}（{node.key}）
              </option>
            ))}
          </select>
          <span className="field-help">唯一能与用户对话的节点，必须是协调者。</span>
        </label>
        <label className="field">
          <span className="field-label">工作路径</span>
          <input
            className="input mono"
            value={draft.workspace_dir}
            onChange={(e) => onPatchTeam({ workspace_dir: e.target.value })}
            placeholder="例如 /vol1/projects/my-app；留空自动分配"
          />
          <span className="field-help">
            作为这个团队的默认工作路径；新建协作任务时可以覆盖。留空则每次运行自动分配。
          </span>
        </label>
      </Section>

      <Section title="预算与上限">
        <div className="collab-budget-grid">
          <NumberField label="最大步数" value={draft.budget.max_steps} onChange={(v) => setBudget({ max_steps: v })} />
          <NumberField label="最大工单数" value={draft.budget.max_tickets} onChange={(v) => setBudget({ max_tickets: v })} />
          <NumberField label="单工单返工上限" value={draft.budget.max_rounds_per_ticket} onChange={(v) => setBudget({ max_rounds_per_ticket: v })} />
          <NumberField label="token 上限（0=不限）" value={draft.budget.max_tokens} onChange={(v) => setBudget({ max_tokens: v })} />
          <NumberField label="运行时限（秒）" value={draft.budget.timeout_seconds} onChange={(v) => setBudget({ timeout_seconds: v })} />
        </div>
        <span className="field-help">任何一项超限都会挂起并明确提示，不会静默烧钱。</span>
      </Section>
    </>
  )
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (next: number) => void }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <input
        className="input mono"
        type="number"
        min={0}
        value={value}
        onChange={(e) => onChange(Math.max(0, Number(e.target.value) || 0))}
      />
    </label>
  )
}
