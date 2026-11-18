import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronRight, MessageCircle, Save, Unplug, X } from 'lucide-react'
import { api, type AssistantChannel } from '@/lib/api'
import { toast } from '@/components/Toast'

const PLATFORM_ORDER = ['feishu', 'slack', 'dingtalk', 'wecom', 'telegram']
const FIELD_LABELS: Record<string, string> = {
  app_id: 'App ID', app_secret: 'App Secret', bot_token: 'Bot Token',
  app_token: 'App Token', signing_secret: 'Signing Secret',
  app_key: 'AppKey', bot_id: 'BotID', secret: 'Secret',
}

const icons: Record<string, string> = {
  feishu: '飞', slack: '✣', dingtalk: '钉', wecom: '企', telegram: '➤',
}

export function SettingsChannelsPanel() {
  const qc = useQueryClient()
  const channels = useQuery({ queryKey: ['assistant-channels'], queryFn: api.assistantChannels })
  const [editing, setEditing] = useState<AssistantChannel | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const save = useMutation({
    mutationFn: ({ platform, values }: { platform: string; values: Record<string, string> }) => api.saveAssistantChannel(platform, values),
    onSuccess: async (row) => {
      const probe = await api.probeAssistantChannel(row.platform)
      if (!probe.ok) toast(probe.detail, 'error')
      else toast(`${row.label} 凭据验证通过`, 'success')
      qc.invalidateQueries({ queryKey: ['assistant-channels'] })
      setEditing(null)
    },
  })
  const disconnect = useMutation({
    mutationFn: api.disconnectAssistantChannel,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['assistant-channels'] }),
  })
  const rows = useMemo(() => {
    const source = channels.data ?? []
    return PLATFORM_ORDER.map(platform => source.find(row => row.platform === platform)).filter(Boolean) as AssistantChannel[]
  }, [channels.data])

  const open = (row: AssistantChannel) => {
    const next: Record<string, string> = {}
    for (const field of row.fields) {
      // Secret fields are intentionally blank: an empty value means keep the
      // encrypted value already stored on the server.
      next[field] = row.config[field] && !row.config[field].includes('•') ? row.config[field] : ''
    }
    setValues(next); setEditing(row)
  }

  return <div className="max-w-4xl space-y-6">
    <div className="flex items-center justify-between gap-4">
      <div>
        <h2 className="text-lg font-semibold text-foreground">消息渠道</h2>
        <p className="mt-1 text-xs text-secondary">把本地 AI 助手接入团队已有的聊天工具。</p>
      </div>
      <div className="rounded-full border border-border bg-surface px-3 py-1.5 text-xs text-secondary">⌂ lsiten 工作区</div>
    </div>
    <div className="overflow-hidden rounded-xl border border-border bg-surface/40">
      {rows.map((row, index) => <div key={row.platform} className={`flex min-h-24 items-center gap-4 px-5 py-4 ${index ? 'border-t border-border' : ''}`}>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-elevated text-sm font-semibold text-foreground">{icons[row.platform] ?? <MessageCircle className="h-5 w-5" />}</div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="text-base font-medium text-foreground">{row.label}</span>
            {row.agent_names.map(name => <span key={name} className="rounded-full bg-elevated px-2.5 py-1 text-xs text-secondary">● {name}</span>)}
          </div>
          <div className="mt-1 flex items-center gap-2 text-xs text-secondary">
            {row.connected ? <><Check className="h-3.5 w-3.5 text-emerald-500" />已连接</> : row.configured ? <><ChevronRight className="h-3.5 w-3.5 text-amber-500" />已配置，等待连接</> : <><Unplug className="h-3.5 w-3.5" />已断开</>}
            <span className="text-muted">{row.description}</span>
          </div>
        </div>
        <button onClick={() => open(row)} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-sm text-foreground hover:bg-elevated">
          {row.configured ? '管理' : '连接'} <ChevronRight className="h-4 w-4" />
        </button>
      </div>)}
      {!rows.length && <div className="p-8 text-center text-sm text-secondary">加载中…</div>}
    </div>
    {editing && <div className="rounded-xl border border-border bg-surface p-5 shadow-lg">
      <div className="flex items-center justify-between">
        <div><h3 className="font-medium text-foreground">连接 {editing.label}</h3><p className="mt-1 text-xs text-secondary">凭据只保存在本机，不会回显完整密钥。</p></div>
        <button onClick={() => setEditing(null)} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground"><X className="h-4 w-4" /></button>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {editing.fields.map(field => <label key={field} className="space-y-1.5 text-xs text-secondary">
          <span>{FIELD_LABELS[field] ?? field}</span>
          <input type="password" value={values[field] ?? ''} onChange={e => setValues(prev => ({ ...prev, [field]: e.target.value }))} placeholder={editing.config[field] ? '已保存，留空保持不变' : `请输入 ${FIELD_LABELS[field] ?? field}`} className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm text-foreground outline-none focus:border-accent" />
        </label>)}
      </div>
      <div className="mt-5 flex items-center gap-2">
        <button disabled={save.isPending} onClick={() => save.mutate({ platform: editing.platform, values })} className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"><Save className="h-4 w-4" />{save.isPending ? '连接中…' : '保存并连接'}</button>
        {editing.configured && <button disabled={disconnect.isPending} onClick={() => { disconnect.mutate(editing.platform); setEditing(null) }} className="inline-flex items-center gap-2 rounded-lg border border-danger/40 px-4 py-2 text-sm text-danger hover:bg-danger/10"><Unplug className="h-4 w-4" />断开</button>}
      </div>
    </div>}
  </div>
}
