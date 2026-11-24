export type Task = {
  id: string
  title: string
  worker: string
  // The worker's model and reasoning effort when known, e.g. 'gpt-6-astra high'; else ''.
  model: string
  branch: string
  session: string
  status: string
  // 'reviewed', 'needs review' or 'needs fixes' for a done task with a recorded diff (review.md vs meta's
  // changes_sha256); else ''.
  review: string
  startedMs: number
  queuedMs: number
  after: string[]
  priority: number
  // `<run>/<task>` of the attempt this task reruns (meta's `retry_of`); else ''.
  retryOf: string
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
  // What a running task does once its worker has exited (the done check, saving results), and since when; else ''.
  phase: string
  phaseMs: number
  // decision.md while it waits for the user (no `Decided:` line yet); else null.
  decision: Decision | null
}
// A question Claude hands the user, from decision.md, in the user's language.
export type Decision = { question: string; context: string; options: string[]; recommend: string }
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
      reminded: Shaped<string[]>
      sent: Shaped<Record<string, string>>
      folds: Shaped<Record<string, boolean>>
    }
  }
}
