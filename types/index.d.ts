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
  usage: Usage | null
  progress: string
  lastCmd: string
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

declare module 'claude-code' {
  interface PluginState {
    'muster': {
      runs: Shaped<Run[]>
      notified: Shaped<string[]>
      folds: Shaped<Record<string, boolean>>
    }
  }
}
