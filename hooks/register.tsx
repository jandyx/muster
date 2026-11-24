import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Agent, Decision, Run, Task, Usage } from '../types'

const PANE = 'muster'
const TITLE = 'Muster'
const POLL_MS = 5000

// Change a tag when its value's fields change, so a reload declines old values.
const runsAtom = atom({ plugin: 'muster', key: 'runs' } as const, [], { shape: 'runs-15' })
// The session's own subagents (`$.agent.list()`), refreshed every poll. They are not Muster runs: no worker,
// no cost; the list is already bound to this session, so it needs no session filter.
const agentsAtom = atom({ plugin: 'muster', key: 'agents' } as const, [] as Agent[], { shape: 'agents-1' })
// Fold the person chose, per run (`<run>`) and per status section (`<run>/<label>`). A run without an entry
// folds once nothing in it runs; a section without one stays open.
const foldsAtom = atom({ plugin: 'muster', key: 'folds' } as const, {}, { shape: 'folds-1' })
const notifiedAtom = atom({ plugin: 'muster', key: 'notified' } as const, [], { shape: 'notified-1' })
// Tasks the dispatching session was already sent back for, so each is reminded once.
const remindedAtom = atom({ plugin: 'muster', key: 'reminded' } as const, [], { shape: 'reminded-1' })
// Inbox choices already sent to Claude, by item key, with what was sent; the row shows that instead of its buttons.
const sentAtom = atom({ plugin: 'muster', key: 'sent' } as const, {}, { shape: 'sent-1' })

// Cells in the band's overall progress bar.
const BAND_BAR = 8

const isRunning = (t: Task) => t.status === 'running'
const isFailed = (t: Task) => t.status.startsWith('failed')
const isCanceled = (t: Task) => t.status.startsWith('canceled')
// The runner is through with the task. `done` on disk means the worker and its check succeeded, not that the work
// is accepted: isDone is that.
const isFinished = (t: Task) => t.status === 'done' || isFailed(t) || isCanceled(t)
const needsReview = (t: Task) => t.review === 'needs review'
const needsFixes = (t: Task) => t.review === 'needs fixes'
const needsAttention = (t: Task) => needsReview(t) || needsFixes(t)
// Reviewed, or from before muster recorded diffs (no review state).
const isDone = (t: Task) => t.status === 'done' && !needsAttention(t)
const shownStatus = (t: Task) => (needsAttention(t) ? t.review : t.status)
// A task row leaves out what its section heading already says.
const rowStatus = (t: Task) => (isRunning(t) || isDone(t) ? '' : shownStatus(t))
// Queued by the runner (waiting for a slot or a dependency), or `pending`: the folder exists, no runner yet.
const isQueued = (t: Task) => !isRunning(t) && !isFinished(t)
const isActive = (t: Task) => isRunning(t) || isQueued(t)
// Queued tasks in the order they will start: ready before waiting on a dependency, then higher priority,
// then first queued; tasks without a runner yet go last.
const startOrder = (a: Task, b: Task) => {
  const rank = (t: Task) => [t.status === 'pending' ? 2 : t.status.includes('waiting for') ? 1 : 0, -t.priority, t.queuedMs]
  const [x, y] = [rank(a), rank(b)]
  return x[0]! - y[0]! || x[1]! - y[1]! || x[2]! - y[2]!
}
// A rerun is the same work again. As in a CI retry, the newest attempt decides how that work stands and the earlier
// ones are history. The runner records the link (`--retry-of`), across names and runs; tasks from before it was
// recorded fall back to the `x`, `x-r2`, `x-r3` naming inside one run.
const attemptOf = (id: string) => {
  const m = /^(.+)-r(\d+)$/.exec(id)
  return { work: m ? m[1]! : id, round: m ? Number(m[2]) : 1 }
}
const lineage = (runs: Run[]) => {
  const byKey = new Map(runs.flatMap(r => r.tasks.map(t => [`${r.name}/${t.id}`, t] as const)))
  const runOf = new Map(runs.flatMap(r => r.tasks.map(t => [t, r.name] as const)))
  // `seen` stops at a link that loops back, which hand-edited meta could hold.
  const trace = (t: Task, seen: Set<Task>): { work: string; round: number } => {
    const parent = byKey.get(t.retryOf)
    if (parent && !seen.has(parent)) {
      const up = trace(parent, seen.add(parent))
      return { work: up.work, round: up.round + 1 }
    }
    const { work, round } = attemptOf(t.id)
    return { work: `${runOf.get(t)}/${work}`, round }
  }
  const workOf = new Map<Task, string>()
  const newest = new Map<string, { task: Task; round: number }>()
  for (const t of runs.flatMap(r => r.tasks)) {
    const { work, round } = trace(t, new Set([t]))
    workOf.set(t, work)
    const seen = newest.get(work)
    // A canceled attempt never replaces one that ran: canceling a rerun leaves the last real result standing.
    const isNewer = !seen
      || (isCanceled(seen.task) !== isCanceled(t) ? isCanceled(seen.task)
        : round !== seen.round ? round > seen.round
        : (t.startedMs || t.queuedMs) > (seen.task.startedMs || seen.task.queuedMs))
    if (isNewer) newest.set(work, { task: t, round })
  }
  const latest = new Set([...newest.values()].map(n => n.task))
  return { latest, newestOf: (t: Task) => newest.get(workOf.get(t)!)!.task }
}
type Lineage = ReturnType<typeof lineage>
const retriedAttempts = (tasks: Task[], attempts: Lineage) => tasks.filter(t => !attempts.latest.has(t) && isFinished(t))
// Something in the run still runs, or its newest work waits for a review or fixes.
const isOpen = (run: Run, attempts: Lineage) => run.tasks.some(t => isActive(t) || (attempts.latest.has(t) && needsAttention(t)))
// decision.md, written by Claude when only the user can decide: `Question:`, `Context:` (may run on several
// lines), `Options:` with one `- ` or `1. ` line each, `Recommend:`. A `Decided:` line closes it.
const parseDecision = (text: string): Decision | null => {
  if (text.trim() === '' || /^\W*Decided\W*:/im.test(text)) return null
  const d: Decision = { question: '', context: '', options: [], recommend: '' }
  let field = ''
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    const head = /^\W*(Question|Context|Options|Recommend)\W*:\s*(.*)$/i.exec(line)
    if (head) {
      field = head[1]!.toLowerCase()
      if (field !== 'options') d[field as 'question' | 'context' | 'recommend'] = head[2]!
      continue
    }
    if (line === '') continue
    if (field === 'options') d.options.push(line.replace(/^(?:[-*•]|\d+[.)])\s*/, ''))
    // Lines of CJK text join without a space, as they read in the file.
    else if (field === 'question' || field === 'context' || field === 'recommend')
      d[field] = d[field] === '' ? line : cells(d[field].at(-1)!) === 2 || cells(line[0]!) === 2 ? d[field] + line : `${d[field]} ${line}`
  }
  return d.question === '' ? null : d
}
type InboxItem = { key: string; task: Task; decision: Decision }
// What only the user can settle, newest attempt only: a question Claude handed over. Reviewed work is merged by
// Claude, not offered here. A rerun of the task replaces it, so an answered question leaves once Claude acts on it.
// Items the user hid (Hide) stay out, by the same id the sent record uses.
const itemId = (i: InboxItem) => `${i.key}|${i.decision.question}`
const HIDDEN = 'hidden'
const inboxOf = (runs: Run[], attempts: Lineage, sent: Record<string, string>): InboxItem[] =>
  runs
    .flatMap(r => r.tasks.filter(t => attempts.latest.has(t)).flatMap(t => (t.decision ? [{ key: `${r.name}/${t.id}`, task: t, decision: t.decision }] : [])))
    .filter(i => sent[itemId(i)] !== HIDDEN)
// Pane sections inside a run, in display order; a task falls into the first whose test it passes.
const SECTIONS: [string, string | undefined, (t: Task) => boolean][] = [
  ['Running', 'yellow', isRunning],
  ['Queued', 'cyan', isQueued],
  ['Failed', 'red', isFailed],
  ['Needs fixes', 'error', needsFixes],
  ['Needs review', 'warning', needsReview],
  ['Canceled', undefined, isCanceled],
  ['Done', 'green', () => true],
  // Filled from retriedAttempts, which needs the whole run; folded until opened.
  ['Retried', undefined, () => false],
]
const mark = (t: Task) => (needsFixes(t) ? '!' : needsReview(t) ? '◇' : t.status === 'done' ? '✓' : isRunning(t) ? '…' : isFailed(t) ? '✗' : isCanceled(t) ? '⊘' : '○')
const markColor = (t: Task) => (needsFixes(t) ? 'error' : needsReview(t) ? 'warning' : t.status === 'done' ? 'green' : isRunning(t) ? 'yellow' : isFailed(t) ? 'red' : isQueued(t) ? 'cyan' : undefined)

// A subagent's lifecycle: the engine reports `running`, `completed`, `failed`, `killed`, and its own
// waiting states. A waiting one is still active, so it stays in the pane.
const isAgentRunning = (a: Agent) => a.status === 'running'
const isAgentFinished = (a: Agent) => a.status === 'completed' || a.status === 'failed' || a.status === 'killed'
const isAgentActive = (a: Agent) => !isAgentFinished(a)
const agentMark = (a: Agent) => (a.status === 'completed' ? '✓' : isAgentRunning(a) ? '…' : a.status === 'failed' ? '✗' : a.status === 'killed' ? '⊘' : '○')
const agentColor = (a: Agent) =>
  a.status === 'completed' ? 'green' : isAgentRunning(a) ? 'yellow' : a.status === 'failed' ? 'red' : isAgentActive(a) ? 'cyan' : undefined
const countAgents = (as: Agent[]) => {
  const n = (test: (a: Agent) => boolean) => as.filter(test).length
  const extra = [[n(a => isAgentActive(a) && !isAgentRunning(a)), 'waiting'], [n(a => a.status === 'killed'), 'stopped']]
    .filter(([k]) => k)
    .map(([k, l]) => ` · ${k} ${l}`)
    .join('')
  return `${n(isAgentRunning)} running · ${n(a => a.status === 'completed')} done · ${n(a => a.status === 'failed')} failed${extra}`
}

// meta holds local "YYYY-MM-DD HH:MM:SS" from run-worker.sh
const parseTime = (s: string | undefined) => {
  if (!s) return 0
  const ms = new Date(s.trim().replace(' ', 'T')).getTime()
  return Number.isNaN(ms) ? 0 : ms
}

// Worker colors are theme keys, so they follow the person's light/dark theme.
// Each worker name hashes to one theme key, so a worker keeps its color across runs and sessions.
const WORKER_COLORS = ['merged', 'suggestion', 'claude', 'planMode', 'autoAccept', 'ide', 'remember', 'permission']
const workerColor = (w: string) => {
  let h = 0
  for (const ch of w) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return WORKER_COLORS[h % WORKER_COLORS.length]!
}
const usdColor = (billing: string) => (billing === 'metered' ? 'success' : 'permission')

const fmtTokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`)
// "$" is billed money (DeepSeek); "≈$" is the API-equivalent of a ChatGPT-subscription Codex run.
const fmtUsd = (usd: number, billing: string) => `${billing === 'metered' ? '$' : '≈$'}${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`
const sumUsage = (tasks: Task[]) => {
  const us = tasks.map(t => t.usage).filter((u): u is Usage => u !== null)
  const tokens = us.reduce((a, u) => a + u.input + u.output, 0)
  const billed = us.filter(u => u.billing === 'metered').reduce((a, u) => a + (u.usd ?? 0), 0)
  const equiv = us.filter(u => u.billing !== 'metered').reduce((a, u) => a + (u.usd ?? 0), 0)
  return { count: us.length, tokens, billed, equiv }
}

// Live progress of a running codex-type task, from its log: the agent's last "codex" note and last "exec" command.
const parseProgress = (log: string) => {
  const lines = log.split('\n')
  let progress = ''
  let lastCmd = ''
  for (let i = lines.length - 1; i >= 0 && (progress === '' || lastCmd === ''); i--) {
    const line = lines[i]!.trim()
    if (progress === '' && line === 'codex') {
      const note: string[] = []
      for (let j = i + 1; j < lines.length && lines[j]!.trim() !== '' && !/^(hook|exec|codex)\b/.test(lines[j]!.trim()); j++) note.push(lines[j]!.trim())
      progress = note.join(' ')
    }
    if (lastCmd === '' && line === 'exec' && lines[i + 1]) {
      lastCmd = lines[i + 1]!
        .replace(/ in \/\S+$/, '')
        .replace(/^\/bin\/(z|ba)?sh -lc (['"])(.*?)\2?$/, '$3')
        .trim()
    }
  }
  return { progress: progress.replace(/[`*]/g, '').replace(/\s+/g, ' ').slice(0, 300), lastCmd: lastCmd.slice(0, 200) }
}

// Live progress of a running claude-type task, from its stream-json events: last assistant text and last tool call.
const parseStream = (stream: string) => {
  let progress = ''
  let lastCmd = ''
  const lines = stream.split('\n')
  for (let i = lines.length - 1; i >= 0 && (progress === '' || lastCmd === ''); i--) {
    if (!/^\{\s*"type"\s*:\s*"assistant"/.test(lines[i]!)) continue
    let ev: { message?: { content?: { type: string; text?: string; name?: string; input?: Record<string, unknown> }[] } }
    try {
      ev = JSON.parse(lines[i]!)
    } catch {
      continue
    }
    for (const c of [...(ev.message?.content ?? [])].reverse()) {
      if (progress === '' && c.type === 'text' && c.text) progress = c.text
      if (lastCmd === '' && c.type === 'tool_use') {
        const input = c.input ?? {}
        const arg = input.command ?? input.file_path ?? input.pattern ?? input.description ?? input.prompt ?? ''
        lastCmd = c.name === 'Bash' ? String(arg) : `${c.name} ${String(arg)}`.trim()
      }
    }
  }
  return { progress: progress.replace(/[`*#]/g, '').replace(/\s+/g, ' ').slice(0, 300), lastCmd: lastCmd.replace(/\s+/g, ' ').slice(0, 200) }
}

// The latest tool call of a subagent as the pane shows it: a Bash command alone, anything else with its tool
// name and most telling argument. A tool with no such argument still shows its name.
const callText = (e: Record<string, unknown>) => {
  const arg = [e.command, e.file_path, e.pattern, e.description, e.prompt, e.url, e.query].find(v => typeof v === 'string' && v !== '')
  const tool = typeof e.tool === 'string' ? e.tool : ''
  const text = typeof arg !== 'string' ? tool : tool === 'Bash' ? arg : `${tool} ${arg}`
  return text.replace(/\s+/g, ' ').trim().slice(0, 200)
}

// What a claude-type worker itself wrote, one text block after another. Tool results stay out: a file or command
// output that says "Progress: 99%" is not the worker's progress.
const streamText = (text: string) =>
  text.split('\n').slice(-400).flatMap(line => {
    try {
      const event = JSON.parse(line) as { type?: string; message?: { content?: { type?: string; text?: string }[] } }
      return event.type === 'assistant' ? (event.message?.content ?? []).filter(c => c.type === 'text' && c.text).map(c => c.text!) : []
    } catch {
      return []
    }
  }).join('\n')

// Progress the worker declared on its own line: "Progress: 45%" or "Progress: 12/40". Any other number in a log
// belongs to a command's output (`df` printed 1% for a task half done), so it is not read as the task's progress.
const readPct = (text: string): { pct: number; text: string } | null => {
  const lines = text.split('\n').slice(-400)
  for (let i = lines.length - 1; i >= 0; i--) {
    const at = /^\s*progress:\s*(?:(\d{1,3})%|(\d{1,6})\s*\/\s*(\d{1,6}))\s*$/i.exec(lines[i]!)
    if (!at) continue
    if (at[1] !== undefined && Number(at[1]) <= 100) return { pct: Number(at[1]), text: `${Number(at[1])}%` }
    const [done, total] = [Number(at[2]), Number(at[3])]
    if (done > 0 && done <= total) return { pct: Math.round((done / total) * 100), text: `${done}/${total} (${Math.round((done / total) * 100)}%)` }
  }
  return null
}

const fmtEta = (ms: number) => {
  const m = Math.ceil(ms / 60000)
  return m < 1 ? '~<1m' : m < 60 ? `~${m}m` : `~${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

// Why a failed task died: the log's last line that looks like an error. A failed codex task leaves
// result.md empty, so this is the only reason there is.
const failureLine = (log: string) =>
  (log
    .split('\n')
    .map(l => l.trim())
    .filter(l => l !== '' && !l.startsWith('hook:'))
    .reverse()
    .find(l => /error|fail|panic/i.test(l)) ?? ''
  )
    .replace(/^\d{4}-\d\d-\d\dT[\d:.]+Z\s+/, '')
    .replace(/[`*]/g, '')
    .replace(/\s+/g, ' ')
    .slice(0, 300)

// Detail rows wrap to a few lines instead of one: enough to read, not enough for one task to fill the pane.
const MAX_LINES = { progress: 3, command: 2, summary: 2 }
// Terminal cells a character takes: East Asian wide and full-width forms take two.
const cells = (ch: string) => {
  const c = ch.codePointAt(0) ?? 0
  return (c >= 0x1100 && c <= 0x115f) || (c >= 0x2e80 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3) ||
    (c >= 0xf900 && c <= 0xfaff) || (c >= 0xfe30 && c <= 0xfe4f) || (c >= 0xff00 && c <= 0xff60) ||
    (c >= 0xffe0 && c <= 0xffe6) || (c >= 0x1f300 && c <= 0x1faff) || (c >= 0x20000 && c <= 0x3fffd)
    ? 2
    : 1
}
const NO_LINE_START = '\u3002\uff0c\u3001\uff1b\uff1a\uff01\uff1f\uff09\u300d\u300f\u300b\u3011,.;:!?)'
const width = (text: string) => [...text].reduce((a, c) => a + cells(c), 0)
// Splits text into lines of `width` cells, breaking at the last space when there is one; past `max` lines it
// keeps `max` and ends the last with "…".
const wrapCells = (text: string, cols: number, max: number) => {
  const w = Math.max(10, cols)
  const lines: string[] = []
  let line = ''
  for (const ch of text.trim()) {
    if (width(line) + cells(ch) > w) {
      // Break at a space only near the line's end; CJK text has few spaces, and an early one wastes the row.
      const space = line.lastIndexOf(' ')
      if (space > 0 && ch !== ' ' && width(line.slice(0, space)) >= w * 0.6) {
        lines.push(line.slice(0, space))
        line = line.slice(space + 1)
      } else if (NO_LINE_START.includes(ch) && [...line].length > 1) {
        // Closing punctuation never starts a line: the character before it moves down with it.
        lines.push([...line].slice(0, -1).join(''))
        line = [...line].at(-1)!
      } else {
        lines.push(line)
        line = ''
      }
      if (ch === ' ') continue
    }
    line += ch
  }
  if (line !== '') lines.push(line)
  if (lines.length <= max) return lines
  const kept = lines.slice(0, max)
  let last = kept[max - 1]!
  while (last !== '' && width(last) + 1 > w) last = [...last].slice(0, -1).join('')
  kept[max - 1] = `${last}…`
  return kept
}

// A codex log's header: the 8192 characters from its banner (MCP errors can precede it). No banner, no header,
// since the log also echoes the prompt.
const codexHeader = (text: string) => {
  const i = text.search(/^OpenAI Codex/m)
  return i < 0 ? '' : text.slice(i, i + 8192)
}

// The worker's model from its log while it runs: codex prints `model:` in its header, a claude worker names it
// in the stream's init event.
const liveModel = (text: string, isStream: boolean) => {
  if (!isStream) return /^model: (\S+)/m.exec(codexHeader(text))?.[1] ?? ''
  for (const line of text.split('\n', 200)) {
    if (!line.includes('"init"')) continue
    try {
      const o = JSON.parse(line) as { type?: string; subtype?: string; model?: unknown }
      if (o.type === 'system' && o.subtype === 'init' && typeof o.model === 'string') return o.model
    } catch {
      // not a JSON line
    }
  }
  return ''
}
const liveEffort = (text: string) => /^reasoning effort: (\S+)/m.exec(codexHeader(text))?.[1] ?? ''

const fmtElapsed = (ms: number) => {
  if (ms <= 0) return ''
  const s = Math.floor(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

// Pane title: the prompt's first line, minus heading marks and a leading "Task:" label (full-width colon too).
// Prompts should open with `# <short title>`; older ones drop straight into the rules block, so fall back to their
// first prose line, then to the task id (which is what those runs used to show alone).
const titleOf = (prompt: string, id: string) => {
  const lines = prompt.split('\n').filter(l => l.trim() !== '')
  const rules = lines.findIndex(l => /^Rules for this task\s*[:\uFF1A]\s*$/i.test(l))
  const first = (rules < 0 ? lines : lines.slice(0, rules))[0] ?? ''
  const title = first.replace(/^#+\s*/, '').replace(/^Task\s*[:\uFF1A]\s*/i, '').replace(/`/g, '').trim()
  return title.slice(0, 60) || id
}

export const register: Register = on => {
  let primed = false
  const opened = new Set<string>()
  // Each subagent's latest tool call, by `agentId`, for the pane. In memory: a reload starts them blank until
  // the agent calls a tool again. Never stored, so a busy fleet costs no state writes.
  const agentActs = new Map<string, string>()

  // runsAtom lags by up to POLL_MS; the Stop hook refreshes it so a review.md written just before the turn ended counts.
  let refreshRuns = async () => {}

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'muster-panel', description: 'Open the Muster pane' })
    const home = (await $.env.get('HOME')) ?? ''
    // runsDir comes from ~/.muster/config.json (MUSTER_CONFIG overrides the path); default ~/.muster/runs.
    const expand = (p: string) => p.replace(/^~(?=\/|$)/, home)
    const configPath = expand((await $.env.get('MUSTER_CONFIG')) ?? '~/.muster/config.json')
    const config = await $.fs
      .read(configPath)
      // `//` comments allowed, stripped outside strings: same pattern as JSON_COMMENT in muster.py.
      .then(
        t =>
          JSON.parse((typeof t === 'string' ? t : '{}').replace(/"(?:\\.|[^"\\\n])*"|\/\/[^\n]*/g, m => (m[0] === '"' ? m : ''))) as {
            runsDir?: string
          },
        () => ({}) as { runsDir?: string },
      )
      .catch(() => ({}) as { runsDir?: string })
    const root = expand(config.runsDir ?? '~/.muster/runs')
    // Runs are bound to the session that dispatched them (muster.py records it).
    // Unknown session id: show everything, as before.
    let sessionId = ''
    // shortcut: a run belongs to the session that owns its first task's meta, so a task another session launched into it is not reminded there; upgrade if sessions start sharing runs.
    const isMine = (run: Run) => !sessionId || run.tasks.some(t => t.session === sessionId)

    const readText = (path: string) => $.fs.read(path).then(t => (typeof t === 'string' ? t : ''), () => '')

    // Branch of a worktree or repo, read from .git without running git.
    const readBranch = async (cwd: string | undefined) => {
      if (!cwd) return ''
      const dotGit = await readText(`${cwd}/.git`)
      const gitDir = dotGit.startsWith('gitdir:') ? dotGit.slice(7).trim() : `${cwd}/.git`
      const head = (await readText(`${gitDir}/HEAD`)).trim()
      return head.startsWith('ref: refs/heads/') ? head.slice(16) : head.slice(0, 7)
    }

    // ETA from the progress rate: a reading has to move for 10s before it estimates, and a drop starts a new
    // phase. In memory only, so a reload just loses the estimate until progress moves again.
    const rates = new Map<string, { pct: number; at: number }>()
    const etaOf = (key: string, pct: number, at: number) => {
      const prev = rates.get(key)
      if (!prev || pct < prev.pct) {
        rates.set(key, { pct, at })
        return 0
      }
      const dt = at - prev.at
      if (pct === 100 || dt < 10000 || pct === prev.pct) return 0
      rates.set(key, { pct, at })
      return (100 - pct) / ((pct - prev.pct) / dt)
    }

    const loadTasks = async (name: string): Promise<Task[]> => {
      const dirs = (await $.fs.list(`${root}/${name}`)).filter(d => d.kind === 'dir').map(d => d.name).sort()
      return Promise.all(
        dirs.map(async (id): Promise<Task> => {
          const dir = `${root}/${name}/${id}`
          const [status, meta, result, prompt, usageText, reviewText, decisionText] = await Promise.all([
            readText(`${dir}/status`),
            readText(`${dir}/meta`),
            readText(`${dir}/result.md`),
            readText(`${dir}/prompt.md`),
            readText(`${dir}/usage`),
            readText(`${dir}/review.md`),
            readText(`${dir}/decision.md`),
          ])
          let usage: Usage | null = null
          try {
            usage = usageText ? (JSON.parse(usageText) as Usage) : null
          } catch {
            usage = null
          }
          const kv = Object.fromEntries(
            meta.split('\n').filter(l => l.includes('=')).map(l => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]),
          )
          // The report opens with `Status: DONE` by rule, which says nothing; other statuses (concerns, blocked) stay.
          const first = result.split('\n').filter(l => !/^\s*#/.test(l) && !/^\W*Status\W*:\W*DONE\W*$/i.test(l)).map(l => l.replace(/^[>*\-\s]+/, '').trim()).find(Boolean) ?? ''
          const tidy = first
            .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1') // [report.md](/long/path) -> report.md
            .replace(/`?(\/[^\s`]+\/)([^\s`/]+)`?/g, '$2') // /abs/path/report.md -> report.md
            .replace(/[`*]/g, '') // underscores stay: DONE_WITH_CONCERNS is a word, not emphasis
            .replace(/\s+/g, ' ')
            .trim()
          const branch = await readBranch(kv.cwd)
          // A runner that died without writing its final status (killed, crashed, machine slept) leaves `running`
          // behind; muster.py records its pid, so a dead pid means the task is lost, not running.
          const pid = Number(kv.pid ?? 0)
          const isOrphan =
            /^(running|queued)/.test(status.trim()) &&
            pid > 0 &&
            (await $.process.run(['kill', '-0', String(pid)], { timeoutMs: 2000 }).then(r => r.exitCode !== 0, () => false))
          const isLive = status.trim() === 'running' && !isOrphan
          // shortcut: reads the whole log each poll while running; switch to a tail read if logs grow past a few MB.
          // meta's `type` is the worker kind muster.py ran: claude writes stream.jsonl, codex a plain log.
          const isStream = kv.type === 'claude'
          const liveFile = `${dir}/${isStream ? 'stream.jsonl' : 'log.txt'}`
          const liveText = isLive ? await readText(liveFile) : ''
          const live = !isLive
            ? { progress: '', lastCmd: '' }
            : isStream
              ? parseStream(liveText)
              : parseProgress(liveText)
          const measure = isLive ? readPct(isStream ? streamText(liveText) : liveText) : null
          const etaMs = measure === null ? 0 : etaOf(`${name}/${id}`, measure.pct, Date.now())
          const logMs = isLive ? ((await $.fs.stat(liveFile).catch(() => undefined))?.mtimeMs ?? 0) : 0
          // muster.py records why in meta (`last_error`); older runs: result.md says why, or the log's error line.
          const failed = isOrphan || status.trim().startsWith('failed')
          const reason = !failed ? '' : (kv.last_error ?? '') || (tidy === '' ? failureLine(await readText(`${dir}/log.txt`)) : '')
          // A done task whose diff muster recorded counts as reviewed once review.md approves that same diff: its
          // `Verdict:` line and meta's `changes_sha256`, so a review of an earlier diff does not count. Older tasks
          // (no `changes` in meta) show no review state.
          // The last `Verdict:` line counts, so a quoted or earlier approval cannot outvote a later "Needs fixes".
          const verdict = ([...reviewText.matchAll(/^\W*Verdict\W*:\W*(.*?)\W*$/gim)].at(-1)?.[1] ?? '').toLowerCase()
          const coversDiff = !kv.changes_sha256 || reviewText.includes(kv.changes_sha256)
          const approved = (verdict === 'approved' || verdict === 'user override') && coversDiff
          const fixes = verdict === 'needs fixes' && coversDiff
          // A task that changed nothing was a read-only review: its verdict is about what it read, and the review is done.
          const readOnly = kv.changes?.startsWith('0 files') ?? false
          const review = status.trim() !== 'done' || kv.changes === undefined ? '' : approved || (fixes && readOnly) ? 'reviewed' : fixes ? 'needs fixes' : 'needs review'
          // Claude's one-line account of the change in review.md (`Summary:`), in the user's language.
          const reviewSummary = /^\W*Summary\W*:\s*(.+)$/im.exec(reviewText)?.[1]?.trim() ?? ''
          // After the worker exits its log goes quiet on purpose; meta says what the runner is doing instead.
          const phase = !isLive ? '' : kv.check_started ? 'running done check' : kv.check_waiting ? 'waiting for done check' : kv.worker_exited ? 'saving results' : ''
          return {
            id,
            review,
            branch,
            session: kv.session ?? '',
            title: titleOf(prompt, id),
            // A queued task has no `worker` yet, only the `workers` it may start on.
            worker: kv.worker ?? (kv.workers ? kv.workers.split(',').join('|') : '?'),
            // meta once the worker is done (muster.py reads it from the log head); while it runs, the log itself.
            model: [kv.model ?? usage?.model ?? liveModel(liveText, isStream), kv.effort ?? liveEffort(liveText)].filter(Boolean).join(' '),
            status: isOrphan ? 'failed: lost (runner exited without a final status)' : status.trim() || 'pending',
            startedMs: parseTime(kv.started),
            queuedMs: parseTime(kv.queued),
            after: (kv.after ?? '').split(',').filter(Boolean),
            priority: Number(kv.priority ?? 0) || 0,
            retryOf: kv.retry_of ?? '',
            finishedMs: parseTime(kv.finished),
            summary: (reviewSummary || tidy).slice(0, 400),
            decision: parseDecision(decisionText),
            reason,
            logPath: failed ? (kv.log_path ?? '') : '',
            usage,
            progress: live.progress,
            lastCmd: live.lastCmd,
            pct: measure?.pct ?? null,
            pctText: measure?.text ?? '',
            etaMs,
            logMs,
            phase,
            phaseMs: phase === '' ? 0 : parseTime(kv.check_started ?? kv.check_waiting ?? kv.worker_exited),
          }
        }),
      )
    }

    // A run's session never changes, so its owner is read once per run and kept.
    const ownerOf = new Map<string, string>()
    const runOwner = async (name: string) => {
      const cached = ownerOf.get(name)
      if (cached !== undefined) return cached
      const dirs = (await $.fs.list(`${root}/${name}`).catch(() => [])).filter(d => d.kind === 'dir').map(d => d.name).sort()
      // The runner makes a task dir before it writes meta; cache only once a meta has been read.
      for (const dir of dirs) {
        const meta = await readText(`${root}/${name}/${dir}/meta`)
        if (!meta) continue
        const owner = /^session=(.*)$/m.exec(meta)?.[1] ?? ''
        ownerOf.set(name, owner)
        return owner
      }
      return ''
    }
    // The pane keeps every run of this session, newest first, up to KEEP.
    const KEEP = 30
    const loadRuns = async (): Promise<Run[]> => {
      if (!(await $.fs.exists(root))) return []
      const names = (await $.fs.list(root)).filter(r => r.kind === 'dir').map(r => r.name).sort().reverse()
      const mine: string[] = []
      for (const name of names) {
        if (mine.length >= KEEP) break
        if (!sessionId || (await runOwner(name)) === sessionId) mine.push(name)
      }
      return Promise.all(mine.reverse().map(async name => ({ name, tasks: await loadTasks(name).catch(() => []) })))
    }

    const poll = async () => {
      // Re-read each poll: /clear and resume move the session to a new id.
      sessionId = await $.session.id().catch(() => '')
      const all = (await loadRuns().catch(() => [] as Run[])).filter(isMine)
      const shown = [...all].reverse()
      await update($, runsAtom, () => shown)

      // The session's own subagents, which the engine scopes to this session already.
      const agents = await $.agent.list().catch(() => [] as Agent[])
      await update($, agentsAtom, () => agents)
      // Drop calls of agents this session no longer lists, so a long session's map stays bounded.
      const listed = new Set(agents.map(a => a.id))
      for (const id of agentActs.keys()) if (!listed.has(id)) agentActs.delete(id)

      const key = (r: Run, t: Task) => `${r.name}/${t.id}`
      const finished = all.flatMap(r => r.tasks.filter(isFinished).map(t => key(r, t)))
      if (!primed) {
        // Tasks already finished before this session started do not toast.
        primed = true
        await update($, notifiedAtom, old => [...new Set([...old, ...finished])].slice(-500))
      } else {
        const seen = new Set(await read($, notifiedAtom))
        const fresh = all.flatMap(r => r.tasks.filter(t => isFinished(t) && !seen.has(key(r, t))).map(t => ({ r, t })))
        for (const { r, t } of fresh) $.ui.toast(`muster ${t.title} (${t.worker}): ${shownStatus(t)}`)
        if (fresh.length) await update($, notifiedAtom, old => [...old, ...fresh.map(({ r, t }) => key(r, t))].slice(-500))
      }

      const active = shown.filter(r => r.tasks.some(isActive))
      const unseen = active.filter(r => !opened.has(r.name))
      if (unseen.length) {
        for (const r of unseen) opened.add(r.name)
        void $.ui.open({ id: PANE, title: TITLE })
      }
    }
    refreshRuns = poll

    // The band above the prompt replaced the status line; clear what an older version pinned.
    $.ui.status(undefined)
    $.clock.every(POLL_MS, () => void poll())
    void poll()

    return next(e)
  })

  // Reviewing is the dispatching session's job, and nothing else wakes it for that. Send it back once per task, only
  // when its run has stopped working, so a session that chooses to leave a task open is not nagged every turn.
  on('classic.Stop', async ($, e, next) => {
    if (e.stop_hook_active) return next(e)
    // Subagents or workflows still in flight will wake the session again; keep the one reminder for then.
    if (e.background_tasks?.some(t => t.type === 'subagent' || t.type === 'workflow')) return next(e)
    await refreshRuns()
    const runs = await read($, runsAtom)
    const attempts = lineage(runs)
    const seen = new Set(await read($, remindedAtom))
    const waiting = runs
      .filter(r => !r.tasks.some(t => isActive(t) && t.status !== 'pending'))
      .flatMap(r => r.tasks.filter(t => t.session === e.session_id && attempts.latest.has(t) && needsAttention(t)).map(t => ({ key: `${r.name}/${t.id}`, review: t.review })))
      .filter(w => !seen.has(w.key))
    if (waiting.length === 0) return next(e)
    const result = await next(e)
    if (result.preventContinuation) return result
    await update($, remindedAtom, old => [...old, ...waiting.map(w => w.key)].slice(-500))
    const list = waiting.map(w => `- ${w.key}: ${w.review}`).join('\n')
    const text = `Muster: finished tasks from this session still wait for you:\n${list}\nFollow step 6 of the muster skill now: review each one and record review.md, or launch its fix. If one needs the user's decision, write its decision.md as step 6 says. Answer in the language the user writes in, not the language of this reminder. This reminder comes once per task.`
    // Our block would replace a lower hook's, so join them.
    return { ...result, block: result.block ? `${result.block}\n\n${text}` : text }
  })

  // Starting a batch ("/muster …", "muster: …") opens the pane too. Opened from the person's own prompt,
  // it is placed at any terminal width; without focus, so typing goes on in the prompt.
  on('prompt.submit', async ($, e, next) => {
    if (/^\s*(\/muster(:muster)?\b|muster\s*:)/i.test(e.text ?? '')) void $.ui.open({ id: PANE, title: TITLE })
    return next(e)
  })

  // Remember each subagent's latest tool call for the pane; the main loop carries no agentId, so its calls stay
  // out. Best effort: bookkeeping must never stand in a tool call's way.
  on('tool.call', ($, e, next) => {
    if (e.agentId) {
      try {
        const text = callText(e as unknown as Record<string, unknown>)
        if (text !== '') agentActs.set(e.agentId, text)
      } catch {
        // A pane detail is not worth failing a call over.
      }
    }
    return next(e)
  })

  on('command.run', { command: 'muster-panel' }, async $ => {
    // The command hands the pane the keyboard: ↑↓ scroll, Enter folds, Esc back to the prompt.
    await $.ui.open({ id: PANE, title: TITLE, focus: true })
    // Same rows as text, so a session that cannot show the pane still gets the state in its own context.
    const runs = await read($, runsAtom)
    const agents = (await read($, agentsAtom)).filter(isAgentActive)
    const waiting = inboxOf(runs, lineage(runs), await read($, sentAtom))
    const rows = [
      ...waiting.map(i => `to decide · ${i.decision.question} · ${i.key}`),
      ...runs.flatMap(r =>
        r.tasks.filter(isActive).map(t => `${t.id} · ${t.worker} · ${t.status}${t.pctText ? ` · ${t.pctText}` : ''} · ${t.title}`),
      ),
      ...agents.map(a => `${a.type} · ${a.status} · ${a.description}${agentActs.has(a.id) ? ` · ${agentActs.get(a.id)}` : ''}`),
    ]
    return {
      text: rows.length === 0 ? 'Muster pane focused. Nothing runs right now.' : `Muster pane focused (Esc returns).\n${rows.join('\n')}`,
    }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const runs = await read($, runsAtom)
    const agents = await read($, agentsAtom)
    if (runs.length === 0 && agents.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No Muster runs yet.</Text>
          <Text dimColor>Tasks appear here when Claude dispatches work to your workers.</Text>
        </Box>
      )
    }
    const folds = await read($, foldsAtom)
    const attempts = lineage(runs)
    const isFolded = (run: Run) => folds[run.name] ?? !isOpen(run, attempts)
    const isAgentsFolded = folds.agents ?? !agents.some(isAgentActive)
    const isFinishedAgentsFolded = folds['agents/finished'] ?? true
    const finishedAgents = agents.filter(isAgentFinished)
    const keys = [
      ...runs.flatMap(r => [r.name, ...SECTIONS.map(([label]) => `${r.name}/${label}`)]),
      ...(agents.length > 0 ? ['agents', 'agents/finished'] : []),
    ]
    const setAll = (folded: boolean) => () => void update($, foldsAtom, old => ({ ...old, ...Object.fromEntries(keys.map(k => [k, folded])) }))
    const toggle = (run: Run) => () => void update($, foldsAtom, old => ({ ...old, [run.name]: !isFolded(run) }))
    const now = await $.clock.now()
    // Room for a detail line: the body less its 4-cell indent and the 2-cell marker.
    const detailWidth = (e.props.bodyColumns ?? 80) - 6
    const Worker = (w: string) => <Text bold color={workerColor(w)}>{w}</Text>
    const Tok = (n: number) => <Text bold>{fmtTokens(n)}</Text>
    const Usd = (usd: number, billing: string) => <Text bold color={usdColor(billing)}>{fmtUsd(usd, billing)}</Text>
    const SumLine = (tasks: Task[], lead: string) => {
      const s = sumUsage(tasks)
      if (s.count === 0) return null
      return (
        <Text dimColor>
          {lead}{Tok(s.tokens)} tokens
          {s.billed > 0 && <Text> · {Usd(s.billed, 'metered')} billed</Text>}
          {s.equiv > 0 && <Text> · {Usd(s.equiv, 'subscription')} sub</Text>}
        </Text>
      )
    }
    const count = (run: Run) => {
      const latest = run.tasks.filter(t => attempts.latest.has(t))
      const n = (test: (t: Task) => boolean) => latest.filter(test).length
      const extra = [[n(needsFixes), 'needs fixes'], [n(needsReview), 'needs review'], [n(isQueued), 'queued'], [n(isCanceled), 'canceled'], [retriedAttempts(run.tasks, attempts).length, 'retried']].filter(([k]) => k).map(([k, l]) => ` · ${k} ${l}`).join('')
      return `${run.tasks.filter(isRunning).length} running · ${n(isDone)} done · ${n(isFailed)} failed${extra}`
    }
    // The inbox: what only the user can settle, at the top. A choice goes to Claude as the user's own message; Other only
    // fills the prompt box for the user's own answer.
    const sent = await read($, sentAtom)
    const inbox = inboxOf(runs, attempts, sent)
    const record = (id: string, label: string | undefined) =>
      update($, sentAtom, old => Object.fromEntries([...Object.entries(old).filter(([k]) => k !== id), ...(label === undefined ? [] : [[id, label]])].slice(-50)))
    // Marked before sending: submit resolves only once the prompt has gone through every UserPromptSubmit hook, which
    // can outlast a press's time budget. A prompt that does not enter takes the mark back.
    const choose = (id: string, label: string, text: string) => async () => {
      await record(id, label)
      void $.prompt.submit({ text, asUser: true }).then(
        async r => {
          if (r.drop !== undefined) await record(id, undefined)
        },
        () => record(id, undefined),
      )
    }
    const fill = (text: string) => async () => {
      if (!(await $.prompt.fill({ text })).isFilled) $.ui.toast('muster: the prompt box is busy; close the dialog and try again')
    }
    const InboxRow = (item: InboxItem) => {
      const { key, task: t, decision: d } = item
      const id = itemId(item)
      const said = sent[id]
      const lines = (text: string, max: number, lead = '     ') => wrapCells(text, detailWidth - 1, max).map(line => <Text>{lead}{line}</Text>)
      // Self-contained, so a session that has not loaded the muster skill still knows where the answer goes.
      const ask = `Muster decision for ${key}, "${d.question}"`
      const after = (n: string) => ` (Append "Decided: ${n}" to that task's decision.md in the muster runsDir, then carry it out as step 6 of the muster skill says.)`
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text bold color="warning">{'  '}⚖️ {d.question}</Text>
          {lines(d.context, 4)}
          {d.options.map((o, i) => wrapCells(`${i + 1}. ${o}`, detailWidth - 1, 2).map((line, j) => <Text>{'     '}{j === 0 ? '' : '   '}{line}</Text>))}
          {d.recommend !== '' && lines(`→ ${d.recommend}`, 2)}
          {said !== undefined ? (
            <Text color="success">{'     '}✓ sent: {said} · waiting for Claude</Text>
          ) : (
            <Box flexDirection="row" gap={2}>
              <Text>{'    '}</Text>
              {d.options.map((o, i) => (
                <Button key={`decide:${key}:${i + 1}`} onPress={choose(id, `option ${i + 1}`, `${ask}: option ${i + 1}, ${o}.${after(`option ${i + 1}`)}`)}>Option {i + 1}</Button>
              ))}
              <Button key={`other:${key}`} onPress={fill(`${ask}: `)}>Other…</Button>
              <Button key={`hide:${key}`} onPress={() => void record(id, HIDDEN)}>Hide</Button>
            </Box>
          )}
          <Text dimColor>{'     '}{key}{t.branch ? ` · ⎇ ${t.branch}` : ''}</Text>
        </Box>
      )
    }
    // Subagent rows: running first, then waiting; finished ones sit under their own fold.
    const agentRows = (list: Agent[]) =>
      [...list]
        .sort((a, b) => Number(isAgentRunning(b)) - Number(isAgentRunning(a)))
        .map(a => {
          // Only a live agent's latest call says where it is now; a finished one's is history.
          const act = isAgentActive(a) ? (agentActs.get(a.id) ?? '') : ''
          return (
            <Box flexDirection="column" marginTop={1}>
              <Text color={agentColor(a)}>
                {'  '}{agentMark(a)} {a.description}
              </Text>
              <Text dimColor>
                {'    '}{Worker(a.type)} · {a.status}
              </Text>
              {act !== '' &&
                wrapCells(act, detailWidth, MAX_LINES.command).map((line, i) => (
                  <Text dimColor>
                    {'    '}
                    {i === 0 ? '$' : ' '} {line}
                  </Text>
                ))}
            </Box>
          )
        })
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={2}>
          <Button key="expand-all" hotkey="e" plain onPress={setAll(false)}>Expand all</Button>
          <Button key="collapse-all" hotkey="c" plain onPress={setAll(true)}>Collapse all</Button>
        </Box>
        <Text dimColor>{e.props.isFocused ? '↑↓ scroll · Enter folds a run, section or agent' : 'Click or ctrl+x tab here to scroll and fold'}</Text>
        <Text> </Text>
        {inbox.length > 0 && (
          <Box flexDirection="column" marginBottom={1}>
            <Text bold>⚖️ To decide · {inbox.length}</Text>
            {inbox.map(InboxRow)}
          </Box>
        )}
        {agents.length > 0 && (
          <Box flexDirection="column" marginBottom={1}>
            <Button key="agents" plain onPress={() => void update($, foldsAtom, old => ({ ...old, agents: !isAgentsFolded }))}>
              {isAgentsFolded ? '▸' : '▾'} Claude agents <Text dimColor>{countAgents(agents)}</Text>
            </Button>
            {!isAgentsFolded && agentRows(agents.filter(isAgentActive))}
            {!isAgentsFolded && finishedAgents.length > 0 && (
              <Box marginTop={1}>
                <Button
                  key="agents/finished"
                  plain
                  onPress={() => void update($, foldsAtom, old => ({ ...old, 'agents/finished': !isFinishedAgentsFolded }))}
                >
                  <Text dimColor>{'  '}{isFinishedAgentsFolded ? '▸' : '▾'} Finished · {finishedAgents.length}</Text>
                </Button>
              </Box>
            )}
            {!isAgentsFolded && !isFinishedAgentsFolded && agentRows(finishedAgents)}
          </Box>
        )}
        {runs.map(run => (
          <Box flexDirection="column" marginBottom={1}>
            <Button key={`run:${run.name}`} plain onPress={toggle(run)}>
              {isFolded(run) ? '▸' : '▾'} {run.name} <Text dimColor>{count(run)}</Text>
            </Button>
            {SumLine(run.tasks, '  Σ ')}
            {!isFolded(run) &&
              [...new Set(run.tasks.map(t => t.worker))].length > 1 &&
              [...new Set(run.tasks.map(t => t.worker))].map(w => {
                const line = SumLine(run.tasks.filter(t => t.worker === w), '')
                return line === null ? null : <Text dimColor>{'    '}{Worker(w)}: {line}</Text>
              })}
            {!isFolded(run) &&
              SECTIONS.flatMap(([label, color], i) => {
                const retried = retriedAttempts(run.tasks, attempts)
                const tasks = label === 'Retried' ? retried : run.tasks.filter(t => !retried.includes(t) && SECTIONS.findIndex(([, , test]) => test(t)) === i)
                if (label === 'Queued') tasks.sort(startOrder)
                // Place in its worker's line, for tasks only a slot holds back (not a dependency, not unlaunched).
                const place = new Map<Task, number>()
                const perWorker = new Map<string, number>()
                for (const t of tasks) {
                  if (!/^queued(?!, waiting for)/.test(t.status)) continue
                  const n = (perWorker.get(t.worker) ?? 0) + 1
                  perWorker.set(t.worker, n)
                  place.set(t, n)
                }
                if (tasks.length === 0) return []
                const key = `${run.name}/${label}`
                const isSectionFolded = folds[key] ?? label === 'Retried'
                const header = (
                  <Box marginTop={1}>
                    <Button key={`section:${key}`} plain onPress={() => void update($, foldsAtom, old => ({ ...old, [key]: !isSectionFolded }))}>
                      <Text bold color={color}>{'  '}{isSectionFolded ? '▸' : '▾'} {label} · {tasks.length}</Text>
                    </Button>
                  </Box>
                )
                if (isSectionFolded) return [header]
                return [header, ...tasks.map(t => (
                <Box flexDirection="column" marginTop={1}>
                  <Text color={markColor(t)}>
                    {'  '}{mark(t)} {t.title}
                    {needsReview(t) && <Text color="warning"> · needs review</Text>}
                    {needsFixes(t) && <Text color="error"> · needs fixes</Text>}
                  </Text>
                  <Text>
                    <Text dimColor>
                      {'    '}{t.id} · {Worker(t.worker)}{t.model ? ` · ${t.model}` : ''}{place.has(t) ? ` · ${t.status.replace(/^queued/, `queued #${place.get(t)}`)}` : rowStatus(t) ? ` · ${rowStatus(t)}` : ''}{t.after.length > 0 ? ` · after ${t.after.join(', ')}` : ''}
                    </Text>
                    {isQueued(t) && <Text dimColor> · </Text>}
                    {isQueued(t) && (t.priority !== 0 ? <Text color="warning" bold>P{t.priority}</Text> : <Text dimColor>P0</Text>)}
                    <Text dimColor>{t.startedMs > 0 ? ` · ${fmtElapsed((t.finishedMs || now) - t.startedMs)}` : ''}{t.branch ? ` · ⎇ ${t.branch}` : ''}</Text>
                  </Text>
                  {isFailed(t) &&
                    t.reason !== '' &&
                    wrapCells(t.reason, detailWidth, MAX_LINES.summary).map((line, i) => (
                      <Text color="error">
                        {'    '}
                        {i === 0 ? '↳' : ' '} {line}
                      </Text>
                    ))}
                  {isFailed(t) &&
                    t.logPath !== '' &&
                    wrapCells(`log ${t.logPath}`, detailWidth, MAX_LINES.command).map(line => (
                      <Text dimColor>
                        {'    '}  {line}
                      </Text>
                    ))}
                  {t.usage !== null && (
                    <Text dimColor>
                      {'    '}in {Tok(t.usage.input)}
                      {t.usage.input > 0 ? ` (${Math.round((t.usage.cached / t.usage.input) * 100)}% cached)` : ''} · out {Tok(t.usage.output)}
                      {t.usage.usd === null ? ' · no price' : <Text> · {Usd(t.usage.usd, t.usage.billing)}</Text>}
                      {t.usage.peak && <Text bold color="error"> peak</Text>}
                    </Text>
                  )}
                  {isRunning(t) &&
                    t.progress !== '' &&
                    wrapCells(t.progress, detailWidth, MAX_LINES.progress).map((line, i) => (
                      <Text>
                        {'    '}
                        {i === 0 ? <Text color="warning">↻</Text> : ' '} {line}
                      </Text>
                    ))}
                  {isRunning(t) &&
                    t.lastCmd !== '' &&
                    wrapCells(t.lastCmd, detailWidth, MAX_LINES.command).map((line, i) => (
                      <Text dimColor>
                        {'    '}
                        {i === 0 ? '$' : ' '} {line}
                      </Text>
                    ))}
                  {isRunning(t) && t.pctText !== '' && (
                    <Text dimColor>
                      {'    '}▰ {t.pctText}
                      {t.pct === 100 ? ' · finishing' : t.etaMs > 0 ? ` · ${fmtEta(t.etaMs)}` : ''}
                    </Text>
                  )}
                  {isRunning(t) && t.phase !== '' && (
                    <Text dimColor>{'    '}{[t.phase, fmtElapsed(now - t.phaseMs)].filter(Boolean).join(' · ')}</Text>
                  )}
                  {isRunning(t) && t.phase === '' && t.logMs > 0 && now - t.logMs > 120000 && (
                    <Text color="error">{'    '}no output for {fmtElapsed(now - t.logMs)}</Text>
                  )}
                  {t.summary !== '' &&
                    isFinished(t) &&
                    wrapCells(t.summary, detailWidth, MAX_LINES.summary).map((line, i) => (
                      <Text dimColor>
                        {'    '}
                        {i === 0 ? '↳' : ' '} {line}
                      </Text>
                    ))}
                </Box>
              ))]
              })}
          </Box>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // A run stays in the band while a task runs or its newest work still needs review or fixes.
    const runs = await read($, runsAtom)
    const attempts = lineage(runs)
    const active = runs.filter(r => isOpen(r, attempts))
    // Claude's own subagents count while one is still going; a finished one leaves the band quiet.
    const activeAgents = (await read($, agentsAtom)).filter(isAgentActive)
    // Work waiting for the user keeps the band up even after its run has finished.
    const asks = inboxOf(runs, attempts, await read($, sentAtom)).length
    if (e.props.hasSurvey || (active.length === 0 && activeAgents.length === 0 && asks === 0)) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    // One line across all runs, short enough for a docked pane: what needs you, most urgent first. The bar's number
    // already says what is accepted; the pane has the per-run detail.
    const inBand = active.flatMap(r => r.tasks)
    const tasks = [...new Set(inBand.map(attempts.newestOf))]
    const running = inBand.filter(isRunning)
    // Canceled work is no longer planned. Only accepted work fills the bar, so a full bar means all of it passed.
    const planned = tasks.length - tasks.filter(isCanceled).length
    const accepted = tasks.filter(isDone).length
    const filled = planned > 0 ? Math.floor((accepted / planned) * BAND_BAR) : 0
    const counts = ([
      [isQueued, '○', 'queued', 'cyan'],
      [isFailed, '✗', 'failed', 'red'],
      [needsFixes, '!', 'fix', 'error'],
      [needsReview, '◇', 'review', 'warning'],
    ] as const).map(([test, sign, label, color]) => ({ n: tasks.filter(test).length, sign, label, color })).filter(c => c.n > 0)
    const only = running.length === 1 ? running[0]! : undefined
    const parts = [
      asks > 0 && <Text color="warning" bold>⚖️ {asks} to decide</Text>,
      planned > 0 && (
        <Text>
          <Text color="green">{'█'.repeat(filled)}</Text>
          <Text dimColor>{'░'.repeat(BAND_BAR - filled)}</Text>
          <Text> {accepted}/{planned}</Text>
        </Text>
      ),
      only && (
        <Text>
          <Text color="yellow">◐ {only.title.length > 20 ? `${only.title.slice(0, 19)}…` : only.title}</Text>
          <Text> </Text>
          <Text bold color={workerColor(only.worker)}>{only.worker}</Text>
          <Text dimColor> {fmtElapsed(now - only.startedMs)}</Text>
        </Text>
      ),
      running.length > 1 && (
        <Text>
          <Text color="yellow">◐ running</Text>
          <Text dimColor> ×{running.length}</Text>
        </Text>
      ),
      ...counts.map(c => (
        <Text>
          <Text color={c.color} dimColor={!c.color}>{c.sign} {c.label}</Text>
          <Text dimColor> ×{c.n}</Text>
        </Text>
      )),
      activeAgents.length > 0 && <Text color="cyan">◐ agents<Text dimColor> ×{activeAgents.length}</Text></Text>,
    ].filter(Boolean)
    return (
      <Box flexDirection="row" flexWrap="wrap">
        <Text color="cyan" bold>⇉ muster </Text>
        {parts.map((part, i) => (
          <Text>
            {part}
            {i < parts.length - 1 && <Text dimColor> | </Text>}
          </Text>
        ))}
      </Box>
    )
  })
}
