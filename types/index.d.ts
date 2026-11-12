export type Task = {
  id: string
  title: string
  worker: string
  branch: string
  session: string
  status: string
  startedMs: number
  queuedMs: number
  after: string[]
  priority: number
  finishedMs: number
  summary: string
  reason: string
  logPath: string
  usage: Usage | null
  progress: string
  lastCmd: string
  // Progress the worker printed as it went ("12/40 (30%)") and an ETA from its rate, both best effort.
  pct: number | null
  pctText: string
  etaMs: number
  logMs: number
}
export type Usage = {
  model: string
  input: number
  cached: number
  output: number
  usd: number | null
  billing: 'subscription' | 'metered' | 'unknown'
  peak?: boolean
}
export type Run = { name: string; tasks: Task[] }

// One subagent the session spawned (the Agent tool), as `$.agent.list()` reports it.
export type Agent = {
  id: string
  description: string
  type: string
  status: string
  parentId?: string
  spawnedBy?: string
  name?: string
}

declare module 'claude-code' {
  interface PluginState {
    'muster': {
      runs: Shaped<Run[]>
      agents: Shaped<Agent[]>
      notified: Shaped<string[]>
      folds: Shaped<Record<string, boolean>>
    }
  }
}
