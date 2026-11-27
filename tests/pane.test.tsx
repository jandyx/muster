import { expect, mock, test } from 'claude-code/testing'

const R = '/h/.muster/runs'
const files: Record<string, string> = {
  [`${R}/20261105-1130-x/A/status`]: 'done\n',
  [`${R}/20261105-1130-x/A/meta`]: 'worker=codex-a\ntype=codex\ncwd=/x\nsession=S1\nstarted=2026-11-05 11:30:00\nfinished=2026-11-05 11:31:40\nexit=0\n',
  [`${R}/20261105-1130-x/A/result.md`]: '## Summary\nretry logic added, see [report.md](/h/.muster/runs/x/A/report.md)\n',
  [`${R}/20261101-0900-old/Z/status`]: 'done\n',
  [`${R}/20261101-0900-old/Z/meta`]: 'worker=codex-b\ntype=codex\nsession=S1\nstarted=2026-11-05 11:00:00\n',
  [`${R}/20261105-1130-x/A/prompt.md`]: '# Task: add `retry` backoff\n\nRules...\n',
  ['/x/.git']: 'gitdir: /repo/.git/worktrees/x\n',
  ['/repo/.git/worktrees/x/HEAD']: 'ref: refs/heads/feat/retry\n',
  [`${R}/20261104-0800-foreign/F/status`]: 'running\n',
  [`${R}/20261104-0800-foreign/F/meta`]: 'worker=codex-c\ntype=codex\nsession=OTHER\nstarted=2026-11-05 11:00:00\n',
  [`${R}/20261105-1130-x/A/usage`]: '{"model":"gpt-6-astra","input":1508687,"cached":1431680,"output":14136,"usd":2.9085,"billing":"subscription"}',
  [`${R}/20261105-1130-x/B/usage`]: '{"model":"deepseek-flash","input":28449,"cached":0,"output":3,"usd":0.0085,"billing":"metered","peak":true}',
  [`${R}/20261105-1130-x/G/status`]: 'running\n',
  [`${R}/20261105-1130-x/G/meta`]: 'worker=codex-b\ntype=codex\ncwd=/x\nsession=S1\nstarted=2026-11-05 11:30:00\n',
  [`${R}/20261105-1130-x/G/log.txt`]: "exec\n/bin/zsh -lc 'npm test -- retry' in /x\n succeeded in 1s:\nok\n\ncodex\nRetry tests pass, now updating `README`.\n\nhook: PreToolUse\n",
  [`${R}/20261105-1130-x/B/stream.jsonl`]: "{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"tool_use\", \"name\": \"Read\", \"input\": {\"file_path\": \"/x/README.md\"}}]}}\n{\"type\": \"assistant\", \"message\": {\"content\": [{\"type\": \"text\", \"text\": \"Read README, starting the review.\"}]}}\n",
  [`${R}/20261105-1130-x/L/status`]: 'running\n',
  [`${R}/20261105-1130-x/L/meta`]: 'worker=codex-a\ntype=codex\ncwd=/x\nsession=S1\npid=4242\nstarted=2026-11-05 11:30:00\n',
  [`${R}/20261105-1130-x/L/prompt.md`]: '# Lost task\n',
  [`${R}/20261105-1130-x/P/prompt.md`]: '# Queued task\n',
  [`${R}/20261105-1130-x/B/status`]: 'running\n',
  [`${R}/20261105-1130-x/B/meta`]: 'worker=deepseek\ntype=claude\ncwd=/x\nsession=S1\nstarted=2026-11-05 11:30:00\n',
}
const dir = (name: string) => ({ name, kind: 'dir', size: 0, mtimeMs: 0, isLink: false })
const PANE = {
  plugin: 'muster', component: 'Pane', requestId: 'muster',
  props: { title: 'Muster', isFocused: false, bodyColumns: 50, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

test('the pane shows each task of the newest run', async ($, on) => {
  on('command.register', () => ({ value: { command: 'muster-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', (_, e: any) => ({ value: e.name === 'HOME' ? '/h' : 'S1' }))
  on('fs.exists', () => ({ value: true }))
  on('fs.list', (_, e: any) => ({
    value: e.path === R ? [dir('20261101-0900-old'), dir('20261104-0800-foreign'), dir('20261105-1130-x')] : e.path.endsWith('-x') ? [dir('A'), dir('B'), dir('G'), dir('L'), dir('P'), dir('notes')] : e.path.endsWith('-old') ? [dir('Z')] : e.path.endsWith('-foreign') ? [dir('F')] : [],
  }))
  on('fs.read', (_, e: any) => (e.path in files ? { value: files[e.path] } : { deny: 'ENOENT' }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: new Date('2026-11-05T11:32:00').getTime() }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.id', () => ({ value: 'S1' }))
  on('process.run', (_, e: any) => ({ value: { exitCode: e.argv?.[2] === '4242' ? 1 : 0, stdout: '', stderr: '' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Button', text: /20261105-1130-x/ }))?.text).toMatch(/▾ 20261105-1130-x/)
    expect((await ui.find({ type: 'Text', text: /add retry/ }))?.text).toMatch(/✓ add retry backoff/)
    expect((await ui.find({ type: 'Text', text: /A · codex-a/ }))?.text).toMatch(/A · codex-a · gpt-6-astra · 1m40s · ⎇ feat\/retry/)
    expect((await ui.find({ type: 'Text', text: /retry logic/ }))?.text).toMatch(/↳ retry logic added, see report\.md$/)
    expect((await ui.find({ type: 'Text', text: /B · deepseek/ }))?.text).toMatch(/B · deepseek · deepseek-flash · \d/)
    expect((await ui.find({ type: 'Button', text: /20261101-0900-old/ }))?.text).toMatch(/[▸▾] 20261101-0900-old 0 running · 1 done/)
    expect(await ui.find({ type: 'Button', text: /foreign/ })).toBeUndefined()
    // A folder with no status, meta or prompt.md (Claude's notes) is not a queued task.
    expect(await ui.find({ type: 'Text', text: /notes/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /in 1\.51M/ }))?.text).toMatch(/in 1\.51M \(95% cached\) · out 14k · ≈\$2\.91/)
    expect((await ui.find({ type: 'Text', text: /in 28k/ }))?.text).toMatch(/out 3 · \$0\.0085 peak/)
    expect((await ui.find({ type: 'Text', text: /Σ/ }))?.text).toMatch(/Σ 1\.55M tokens · \$0\.0085 billed · ≈\$2\.91 sub/)
    expect((await ui.find({ type: 'Text', text: /codex-a: / }))?.text).toMatch(/codex-a: 1\.52M tokens · ≈\$2\.91 sub/)
    // No empty subtotal row is drawn for a worker without usage.
    expect(await ui.find({ type: 'Text', text: /^\s*codex-b: $/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /Retry tests/ }))?.text).toMatch(/↻ Retry tests pass, now updating README\./)
    expect((await ui.find({ type: 'Text', text: /npm test/ }))?.text).toMatch(/\$ npm test -- retry$/)
    expect((await ui.find({ type: 'Text', text: /Read README/ }))?.text).toMatch(/↻ Read README, starting the review\./)
    expect((await ui.find({ type: 'Text', text: /Read \/x\/README/ }))?.text).toMatch(/\$ Read \/x\/README\.md/)
    expect((await ui.find({ type: 'Text', text: /Lost task/ }))?.text).toMatch(/✗ Lost task/)
    expect((await ui.find({ type: 'Text', text: /L · / }))?.text).toMatch(/failed: lost/)
    // Tasks sit under status sections: running first, then not yet started, failed, done.
    const heads = (await ui.findAll({ type: 'Button' })).map((r: any) => r.text as string).filter(r => /^\s*▾ \w+ · \d+$/.test(r))
    expect(heads.slice(0, 4).map(h => h.trim())).toEqual(['▾ Running · 2', '▾ Queued · 1', '▾ Failed · 1', '▾ Done · 1'])
    // A task with no meta yet has no start time, so no elapsed time is shown.
    expect((await ui.find({ type: 'Text', text: /P · / }))?.text).toMatch(/P · \? · pending · P0$/)
    expect((await ui.find({ type: 'Text', text: /○/ }))?.text).toMatch(/○ /)
    // A section folds on its own; the rest of the run stays.
    await ui.press({ key: 'section:20261105-1130-x/Done' })
    expect(await ui.find({ type: 'Text', text: /retry logic/ })).toBeUndefined()
    expect((await ui.find({ type: 'Button', text: /Done · 1/ }))?.text).toMatch(/▸ Done · 1/)
    expect((await ui.find({ type: 'Text', text: /Lost task/ }))?.text).toMatch(/✗ Lost task/)
    await ui.press({ key: 'expand-all' })
    // Folding the newest run hides its rows; expanding all brings them back.
    await ui.press({ key: 'run:20261105-1130-x' })
    expect(await ui.find({ type: 'Text', text: /retry logic/ })).toBeUndefined()
    await ui.press({ key: 'expand-all' })
    expect((await ui.find({ type: 'Text', text: /retry logic/ }))?.text).toMatch(/↳ retry logic added, see report\.md$/)
    await ui.unmount()
  }
})

test('the band above the prompt colors each running run', async ($, on) => {
  on('command.register', () => ({ value: { command: 'muster-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', (_, e: any) => ({ value: e.name === 'HOME' ? '/h' : 'S1' }))
  on('fs.exists', () => ({ value: true }))
  on('fs.list', (_, e: any) => ({ value: e.path === R ? [dir('20261105-1130-x')] : e.path.endsWith('-x') ? [dir('A'), dir('B'), dir('G'), dir('L')] : [] }))
  on('fs.read', (_, e: any) => (e.path in files ? { value: files[e.path] } : { deny: 'ENOENT' }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: new Date('2026-11-05T11:32:00').getTime() }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.id', () => ({ value: 'S1' }))
  on('process.run', (_, e: any) => ({ value: { exitCode: e.argv?.[2] === '4242' ? 1 : 0, stdout: '', stderr: '' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })

  expect(await ui.find({ type: 'Text', text: /codex-a/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /1\/4/ }))?.text).toMatch(/1\/4/)
  expect((await ui.find({ type: 'Text', text: /running/ }))?.text).toMatch(/◐ running ×2/)
  await ui.unmount()
})

test('a muster prompt opens the pane; other prompts do not', async ($, on) => {
  const opened: string[] = []
  const registered: string[] = []
  on('command.register', (_, e) => {
    registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.open', (_, e: any) => {
    opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('env.get', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', () => ({ deny: 'ENOENT' }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('session.id', () => ({ value: 'S1' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_, e: any) => ({ text: e.text }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  expect(registered).toContain('muster-stats')

  await $.prompt.submit({ text: 'fix the login bug' })
  await $.prompt.submit({ text: '/muster-stats', wait: false, origin: { kind: 'composer' } })
  expect(opened).toEqual([])
  await $.prompt.submit({ text: '/muster profile the slowest tests' })
  await $.prompt.submit({ text: 'muster: audit the serial group' })
  expect(opened).toEqual(['muster', 'muster'])
})

// A small engine stand-in over an in-memory runs folder, for the cases below.
const mountWith = async (
  $: any,
  on: any,
  tree: Record<string, string>,
  opts: { config?: string; session?: string; clock?: boolean; agents?: any[]; toasts?: string[] } = {},
) => {
  const dirs = new Map<string, Set<string>>()
  for (const path of Object.keys(tree)) {
    const parts = path.split('/')
    for (let i = 1; i < parts.length; i++) {
      const parent = parts.slice(0, i).join('/') || '/'
      if (!dirs.has(parent)) dirs.set(parent, new Set())
      if (i < parts.length - 1) dirs.get(parent)!.add(parts[i]!)
    }
  }
  on('command.register', () => ({ value: { command: 'muster-panel' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', (_: any, e: any) => ({ value: e.name === 'HOME' ? '/h' : undefined }))
  on('fs.exists', (_: any, e: any) => ({ value: dirs.has(e.path) || e.path in tree }))
  on('fs.list', (_: any, e: any) => ({
    value: [...(dirs.get(e.path) ?? [])].map(name => ({ name, kind: 'dir', size: 0, mtimeMs: 0, isLink: false })),
  }))
  on('fs.read', (_: any, e: any) =>
    e.path === '/h/.muster/config.json' && opts.config !== undefined
      ? { value: opts.config }
      : e.path in tree
        ? { value: tree[e.path] }
        : { deny: 'ENOENT' },
  )
  if (!opts.clock) {
    on('clock.every', () => ({ value: undefined }))
    on('clock.now', () => ({ value: new Date('2026-11-05T11:32:00').getTime() }))
  }
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', (_: any, e: any) => {
    opts.toasts?.push(JSON.stringify(e))
    return { value: undefined }
  })
  on('session.id', () => ({ value: opts.session ?? 'S1' }))
  on('agent.list', () => ({ value: opts.agents ?? [] }))
  on('process.run', () => ({ value: { stdout: '', stderr: '', exitCode: 0 } }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
}

const task = (status: string, extra = '') => ({ status: `${status}\n`, meta: `worker=w1\ntype=codex\nsession=S1\nstarted=2026-11-05 11:00:00\n${extra}` })
const tree = (root: string, runs: Record<string, Record<string, { status: string; meta: string; title?: string }>>) => {
  const out: Record<string, string> = {}
  for (const [run, tasks] of Object.entries(runs))
    for (const [id, t] of Object.entries(tasks)) {
      out[`${root}/${run}/${id}/status`] = t.status
      out[`${root}/${run}/${id}/meta`] = t.meta
      out[`${root}/${run}/${id}/prompt.md`] = `# ${t.title ?? `task ${id}`}\n`
    }
  return out
}

test('runsDir comes from the config file', async ($, on) => {
  await mountWith($, on, tree('/data/runs', { '20261105-0900-custom': { A: task('done') } }), {
    config: JSON.stringify({ runsDir: '/data/runs' }),
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: /custom/ }))?.text).toMatch(/20261105-0900-custom/)
  await ui.unmount()
})

test('runsDir is read from a config with // comments', async ($, on) => {
  await mountWith($, on, tree('/data/runs', { '20261105-0900-custom': { A: task('done') } }), {
    config: '{\n  // where runs go\n  "runsDir": "/data/runs" // not "~/x"\n}',
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: /custom/ }))?.text).toMatch(/20261105-0900-custom/)
  await ui.unmount()
})

test('a finished run starts folded, a running one open', async ($, on) => {
  await mountWith(
    $,
    on,
    tree('/h/.muster/runs', {
      '20261105-0900-old': { A: { ...task('done'), title: 'old work' } },
      '20261105-1000-live': { B: { ...task('running', 'pid=0\n'), title: 'live work' } },
    }),
  )
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: /old/ }))?.text).toMatch(/^▸/)
  expect((await ui.find({ type: 'Button', text: /live/ }))?.text).toMatch(/^▾/)
  expect(await ui.find({ type: 'Text', text: /old work/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /live work/ }))?.text).toMatch(/… live work/)
  await ui.unmount()
})

test('a prompt with no title line shows its first prose line, not the bare task id', async ($, on) => {
  const t = tree('/h/.muster/runs', {
    '20261105-1000-x': { A: { ...task('running', 'pid=0\n') }, B: { ...task('running', 'pid=0\n') } },
  })
  t['/h/.muster/runs/20261105-1000-x/A/prompt.md'] = 'Rules for this task:\n- Do not push.\n'
  t['/h/.muster/runs/20261105-1000-x/B/prompt.md'] = 'Fix the workflow timeline\nRules for this task:\n- Do not push.\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /… Fix the workflow timeline/ }))?.text).toMatch(/… Fix the workflow timeline/)
  expect((await ui.find({ type: 'Text', text: /… A$/ }))?.text).toMatch(/… A$/)
  expect(await ui.find({ type: 'Text', text: /Rules for this task/ })).toBeUndefined()
  await ui.unmount()
})

test('a running task shows only the progress it declared, not numbers from command output', async ($, on) => {
  const t = tree('/h/.muster/runs', {
    '20261105-1000-x': { A: { ...task('running', 'pid=0\n'), title: 'building' }, B: { ...task('running', 'pid=0\n'), title: 'reading' } },
  })
  t['/h/.muster/runs/20261105-1000-x/A/log.txt'] = 'codex\nbuilding\nProgress: 12/40\nStep 39/40: compiling serde\n/dev/disk3s1 460Gi 350Gi 70Gi 84% 6.3M 735M 1% /System/Volumes/Data\n'
  t['/h/.muster/runs/20261105-1000-x/B/log.txt'] = 'reading /Users/x/sessions/2026/11/05/log.txt\nversion 1.2/3 ok\nStep 3/7 done, 45% cached\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /▰ 12\/40 \(30%\)/ }))?.text).toMatch(/▰ 12\/40 \(30%\)$/)
  expect((await ui.findAll({ type: 'Text' })).filter((r: any) => /▰/.test(r.text))).toHaveLength(1)
  await ui.unmount()
})

test('a claude worker\'s progress comes from its own text, on any line, and never from a tool result', async ($, on) => {
  const meta = 'worker=w1\ntype=claude\nsession=S1\nstarted=2026-11-05 11:00:00\npid=0\n'
  const t = tree('/h/.muster/runs', { '20261105-1000-x': { A: { status: 'running\n', meta, title: 'claude worker' } } })
  t['/h/.muster/runs/20261105-1000-x/A/stream.jsonl'] = [
    { type: 'assistant', message: { content: [{ type: 'text', text: 'Finished the parser.\nProgress: 3/5' }] } },
    { type: 'user', message: { content: [{ type: 'tool_result', content: 'Progress: 99%' }] } },
  ].map(e => JSON.stringify(e)).join('\n') + '\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /▰/ }))?.text).toMatch(/▰ 3\/5 \(60%\)$/)
  await ui.unmount()
})

test('an agy worker shows its joined text deltas and last tool call', async ($, on) => {
  const meta = 'worker=g1\ntype=agy\nsession=S1\nstarted=2026-11-05 11:00:00\npid=0\n'
  const t = tree('/h/.muster/runs', { '20261105-1000-x': { A: { status: 'running\n', meta, title: 'agy worker' } } })
  const step = (s: object) => JSON.stringify({ event: 'step_update', step_update: s })
  t['/h/.muster/runs/20261105-1000-x/A/stream.jsonl'] = [
    step({ step_index: 2, step_type: 'tool', tool_name: 'write_file', tool_info: { parameters: { Path: '/x/main.go' } } }),
    step({ step_index: 3, step_type: 'agent_response', text_delta: 'Wrote the pa' }),
    step({ step_index: 3, step_type: 'agent_response', text_delta: 'rser.\nProgress: 3/5' }),
  ].join('\n') + '\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /▰/ }))?.text).toMatch(/▰ 3\/5 \(60%\)$/)
  expect(await ui.find({ type: 'Text', text: /write_file \/x\/main\.go/ })).toBeTruthy()
  await ui.unmount()
})

test('a refused task shows as failed with its reason', async ($, on) => {
  await mountWith(
    $,
    on,
    tree('/h/.muster/runs', {
      '20261105-1000-x': {
        A: { ...task('failed: refused, worker w1 already runs 3 of 3 tasks (caps)'), title: 'over the cap' },
        B: { ...task('running', 'pid=0\n'), title: 'keeps the run open' },
      },
    }),
  )
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /over the cap/ }))?.text).toMatch(/✗ over the cap/)
  expect((await ui.find({ type: 'Text', text: /refused/ }))?.text).toMatch(/3 of 3 tasks/)
  await ui.unmount()
})

test('a failed task with no result shows the error line from its log', async ($, on) => {
  const t = tree('/h/.muster/runs', {
    '20261105-1000-x': {
      A: { ...task('failed: exit 101'), title: 'broke' },
      B: { ...task('running', 'pid=0\n'), title: 'keeps the run open' },
    },
  })
  t['/h/.muster/runs/20261105-1000-x/A/log.txt'] =
    'exec\n/bin/zsh -lc "make test" in /x\n\n2026-11-09T03:01:13.414647Z ERROR codex_core::session: failed to record rollout items: thread-store internal error: No space left on device (os error 28)\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /broke/ }))?.text).toMatch(/✗ broke/)
  expect((await ui.find({ type: 'Text', text: /ERROR codex_core/ }))?.text).toMatch(/^\s+↳ ERROR codex_core::session: failed to record/)
  await ui.unmount()
})

test('a failed task shows last_error and log_path from meta', async ($, on) => {
  await mountWith(
    $,
    on,
    tree('/h/.muster/runs', {
      '20261105-1000-x': {
        A: {
          ...task('failed: disk full (ENOSPC), 1.2G free on /', 'last_error=Error: ENOSPC: no space left on device, write\nlog_path=/h/.muster/runs/20261105-1000-x/A/log.txt\n'),
          title: 'filled the disk',
        },
        B: { ...task('running', 'pid=0\n'), title: 'keeps the run open' },
      },
    }),
  )
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /no space left/ }))?.text).toMatch(/^\s+↳ Error: ENOSPC: no space left on device/)
  expect((await ui.find({ type: 'Text', text: /^\s+log / }))?.text).toMatch(/^\s+log \/h\/\.muster\/runs\/20261105-1000-x\/A\/log/)
  await ui.unmount()
})

test('queued tasks show their dependencies and priority, in the order they will start', async ($, on) => {
  // Queued tasks have no `started` yet, so no elapsed time.
  const q = (status: string, extra: string) => ({ status: `${status}\n`, meta: `worker=w1\ntype=codex\nsession=S1\npid=0\n${extra}` })
  await mountWith(
    $,
    on,
    tree('/h/.muster/runs', {
      '20261105-1000-q': {
        A: { ...task('running', 'pid=0\npriority=7\n'), title: 'base work' },
        B: { ...q('queued, waiting for A', 'queued=2026-11-05 10:00:01\nafter=A\npriority=9\n'), title: 'needs base' },
        C: { ...q('queued, w1 runs 3 of 3', 'queued=2026-11-05 10:00:05\n'), title: 'late plain' },
        D: { ...q('queued, w1 runs 3 of 3', 'queued=2026-11-05 10:00:09\npriority=5\n'), title: 'urgent' },
        E: { ...q('queued, w1 runs 3 of 3', 'queued=2026-11-05 10:00:02\n'), title: 'early plain' },
        H: { status: 'queued, w1 runs 3 of 3, w2 runs 1 of 1\n', meta: 'workers=w1,w2\nsession=S1\npid=0\nqueued=2026-11-05 10:00:20\n', title: 'either worker' },
        F: { ...task('canceled: superseded by G'), title: 'old approach' },
      },
    }),
  )
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const titles = (await ui.findAll({ type: 'Text' })).map((r: any) => r.text as string).filter(t => /^\s*○ /.test(t)).map(t => t.trim())
  expect(titles).toEqual(['○ urgent', '○ early plain', '○ late plain', '○ either worker', '○ needs base'])
  expect((await ui.find({ type: 'Text', text: /H · / }))?.text).toMatch(/H · w1\|w2 · queued #1, w1 runs 3 of 3, w2 runs 1 of 1 · P0$/)
  expect((await ui.find({ type: 'Text', text: /B · / }))?.text).toMatch(/B · w1 · queued, waiting for A · after A · P9$/)
  expect((await ui.find({ type: 'Text', text: /D · / }))?.text).toMatch(/D · w1 · queued #1, w1 runs 3 of 3 · P5$/)
  expect((await ui.find({ type: 'Text', text: /^\s+A · w1/ }))?.text).not.toMatch(/P7|running/)
  expect((await ui.find({ type: 'Text', text: /C · / }))?.text).toMatch(/C · w1 · queued #3, /)
  expect((await ui.find({ type: 'Text', text: /old approach/ }))?.text).toMatch(/⊘ old approach/)
  expect((await ui.find({ type: 'Button', text: /Canceled/ }))?.text).toMatch(/Canceled · 1/)
  expect((await ui.find({ type: 'Button', text: /20261105-1000-q/ }))?.text).toMatch(/1 running · 0 done · 0 failed · 5 queued · 1 canceled/)
  await ui.unmount()
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /^○ queued/ }))?.text).toMatch(/^○ queued ×5/)
  expect(await band.find({ type: 'Text', text: /canceled/ })).toBeUndefined()
  await band.unmount()
})

test('the band stays out of the way when nothing runs', async ($, on) => {
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await mountWith($, on, tree('/h/.muster/runs', { '20261105-0900-old': { A: task('done') } }))
  const ui = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('engine')
  await ui.unmount()
})

test('the pane lists the session\'s Claude agents, running first and finished folded', async ($, on) => {
  await mountWith($, on, {}, {
    agents: [
      { id: 'a1', description: 'Running the full OpenUICore test suite', type: 'oh-my-claudecode:executor-high', status: 'running' },
      { id: 'a2', description: 'Searching OpenUI fixture and gallery references', type: 'oh-my-claudecode:explore', status: 'pending' },
      { id: 'a3', description: 'Older audit', type: 'Plan', status: 'completed' },
      { id: 'a4', description: 'Broken review', type: 'oh-my-claudecode:code-reviewer', status: 'failed' },
    ],
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: /Claude agents/ }))?.text).toMatch(/^▾ Claude agents 1 running · 1 done · 1 failed · 1 waiting$/)
  const rows = (await ui.findAll({ type: 'Text' })).map((r: any) => r.text as string).filter(t => /^\s*[✓…✗⊘○] /.test(t))
  expect(rows).toEqual(['  … Running the full OpenUICore test suite', '  ○ Searching OpenUI fixture and gallery references'])
  expect((await ui.find({ type: 'Text', text: /executor-high · running/ }))?.text).toMatch(/oh-my-claudecode:executor-high · running/)
  // A finished agent folds away with its kind, until the person opens it.
  expect(await ui.find({ type: 'Text', text: /Older audit/ })).toBeUndefined()
  expect((await ui.find({ type: 'Button', text: /Finished · 2/ }))?.text).toMatch(/▸ Finished · 2/)
  await ui.press({ key: 'agents/finished' })
  expect((await ui.find({ type: 'Text', text: /Older audit/ }))?.text).toMatch(/✓ Older audit/)
  expect((await ui.find({ type: 'Text', text: /Broken review/ }))?.text).toMatch(/✗ Broken review/)
  await ui.unmount()
})

test('a live Claude agent shows its latest tool call', async ($, on) => {
  await mountWith($, on, {}, {
    agents: [
      { id: 'a1', description: 'Running the suite', type: 'oh-my-claudecode:executor-high', status: 'running' },
      { id: 'a2', description: 'Waiting for the run', type: 'oh-my-claudecode:executor-high', status: 'pending' },
    ],
  })
  // A Bash call reads as its command; any other tool carries its name. No tool runs beneath the test's hooks.
  await $.tool.call({ tool: 'Bash', command: 'npm test -- retry', agentId: 'a1' } as any).catch(() => {})
  await $.tool.call({ tool: 'Read', file_path: '/x/README.md', agentId: 'a2' } as any).catch(() => {})
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /npm test -- retry/ }))?.text).toMatch(/^\s+\$ npm test -- retry$/)
  expect((await ui.find({ type: 'Text', text: /Read \/x\/README/ }))?.text).toMatch(/^\s+\$ Read \/x\/README\.md$/)
  await ui.unmount()
})

test('a Claude agents block with nothing running starts folded', async ($, on) => {
  await mountWith($, on, {}, {
    agents: [{ id: 'a1', description: 'Finished audit', type: 'Plan', status: 'completed' }],
  })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: /Claude agents/ }))?.text).toMatch(/^▸ Claude agents 0 running · 1 done · 0 failed$/)
  expect(await ui.find({ type: 'Text', text: /Finished audit/ })).toBeUndefined()
  // Opening the block still keeps finished agents behind their own fold.
  await ui.press({ key: 'agents' })
  expect((await ui.find({ type: 'Button', text: /Finished · 1/ }))?.text).toMatch(/▸ Finished · 1/)
  await ui.press({ key: 'agents/finished' })
  expect((await ui.find({ type: 'Text', text: /Finished audit/ }))?.text).toMatch(/✓ Finished audit/)
  await ui.unmount()
})

test('the band counts the Claude agents still running', async ($, on) => {
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await mountWith($, on, {}, {
    agents: [
      { id: 'a1', description: 'live agent', type: 'Explore', status: 'running' },
      { id: 'a2', description: 'old agent', type: 'Plan', status: 'completed' },
    ],
  })
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /agents/ }))?.text).toMatch(/◐ agents ×1/)
  await band.unmount()
})

test('another session cannot see this one\'s runs', async ($, on) => {
  await mountWith($, on, tree('/h/.muster/runs', { '20261105-0900-mine': { A: task('done') } }), { session: 'OTHER' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /No Muster runs yet/ }))?.text).toBe('No Muster runs yet.')
  await ui.unmount()
})

test('a run whose meta is not written yet shows once it is', async ($, on) => {
  const clock = mock.clock(on, { now: new Date('2026-11-05T11:32:00').getTime() })
  const t = tree('/h/.muster/runs', { '20261105-1000-new': { A: { ...task('running', 'pid=0\n'), title: 'fresh work' } } })
  const meta = t['/h/.muster/runs/20261105-1000-new/A/meta']!
  delete t['/h/.muster/runs/20261105-1000-new/A/meta'] // the runner makes the dir first, meta a moment later
  await mountWith($, on, t, { clock: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /No Muster runs yet/ })).toBeDefined()
  t['/h/.muster/runs/20261105-1000-new/A/meta'] = meta
  await clock.advance(5000)
  expect((await ui.find({ type: 'Button', text: /new/ }))?.text).toMatch(/20261105-1000-new/)
  await ui.unmount()
})

test('a long note wraps to at most three lines, a long command to two', async ($, on) => {
  const note = 'Shell and executor tests pass; fixing the path check that treats /var and /private/var as different places, then rerunning every runtime file and the full lint and typecheck pass before reporting.'
  const cmd = 'bash tests/run-runtime.sh tests/runtime/developer/developer-entry.test.mjs && git diff --check && git status --short && make lint'
  const t = tree('/h/.muster/runs', { '20261105-1000-x': { A: { ...task('running', 'pid=0\n'), title: 'long one' } } })
  t['/h/.muster/runs/20261105-1000-x/A/log.txt'] = `exec\n/bin/zsh -lc '${cmd}' in /x\n\ncodex\n${note}\n\nhook: PreToolUse\n`
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 50 } })
  // Whole rows only: a row's colored marker is a nested Text of its own.
  const rows = (await ui.findAll({ type: 'Text' })).map((r: any) => r.text as string).filter(r => r.startsWith('    '))
  const start = rows.findIndex(r => r.includes('↻'))
  const cmdStart = rows.findIndex(r => /^\s*\$ /.test(r))
  expect(start).toBeGreaterThan(-1)
  expect(cmdStart - start).toBe(3) // three note rows, then the command
  expect(rows[start + 2]).toMatch(/…$/)
  expect(rows[cmdStart + 1]).toMatch(/…$/)
  expect(rows[cmdStart + 2] ?? '').not.toMatch(/^\s{6}\S/)
  for (const r of rows.slice(start, cmdStart + 2)) expect([...r].length).toBeLessThanOrEqual(50)
  await ui.unmount()
})

test('a done task with a recorded diff shows needs review until review.md approves that diff', async ($, on) => {
  const sha = 'a'.repeat(64)
  const meta = `changes=2 files +5 -1\nchanges_sha256=${sha}\n`
  const t = tree('/h/.muster/runs', {
    '20261117-1000-rev': {
      fresh: { ...task('done', meta), title: 'Fresh fix' },
      stale: { ...task('done', meta), title: 'Stale review' },
      good: { ...task('done', meta), title: 'Good review' },
      template: { ...task('done', meta), title: 'Template copied' },
      quoted: { ...task('done', meta), title: 'Quoted approval' },
      bold: { ...task('done', meta), title: 'Bold label' },
      legacy: { ...task('done'), title: 'Legacy task' }, // before muster recorded diffs: no review state
      live: { ...task('running', meta), title: 'Still going' },
    },
  })
  t['/h/.muster/runs/20261117-1000-rev/stale/review.md'] = `Verdict: Approved\nchanges-sha256: ${'c'.repeat(64)}\n`
  t['/h/.muster/runs/20261117-1000-rev/good/review.md'] = `Reviewer: subagent\nVerdict: Approved\nchanges-sha256: ${sha}\n`
  t['/h/.muster/runs/20261117-1000-rev/quoted/review.md'] = `> Verdict: Approved\nVerdict: Needs fixes\nchanges-sha256: ${sha}\n`
  t['/h/.muster/runs/20261117-1000-rev/bold/review.md'] = `**Verdict**: Approved\nchanges-sha256: ${sha}\n`
  t['/h/.muster/runs/20261117-1000-rev/template/review.md'] = `Verdict: <Approved, or Needs fixes>\nchanges-sha256: ${sha}\n`
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const line = async (title: RegExp) => (await ui.find({ type: 'Text', text: title }))?.text ?? ''
  expect(await line(/Fresh fix/)).toMatch(/needs review/)
  expect(await line(/Stale review/)).toMatch(/needs review/)
  expect(await line(/Good review/)).not.toMatch(/needs review/)
  expect(await line(/Template copied/)).toMatch(/needs review/)
  expect(await line(/Quoted approval/)).toMatch(/needs fixes/)
  expect(await line(/Bold label/)).not.toMatch(/needs review/)
  expect(await line(/Legacy task/)).not.toMatch(/needs review/)
  expect(await line(/Still going/)).not.toMatch(/needs review/)
  await ui.unmount()
})

test('the band sums every run on one line and folds several running tasks into a count', async ($, on) => {
  await mountWith($, on, tree('/h/.muster/runs', {
    '20261117-1200-a': { one: { ...task('running'), title: 'First long task' }, two: task('running'), bad: task('failed') },
    '20261117-1300-b': { three: task('running'), worse: task('failed') },
  }))
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /running/ }))?.text).toMatch(/◐ running ×3/)
  expect(await band.find({ type: 'Text', text: /First long task/ })).toBeUndefined()
  expect((await band.find({ type: 'Text', text: /^✗ failed/ }))?.text).toMatch(/^✗ failed ×2/)
  expect((await band.find({ type: 'Text', text: /0\/5/ }))?.text).toMatch(/░{8} 0\/5/)
  await band.unmount()
})

test('the band bar fills only for accepted work and leaves canceled tasks out', async ($, on) => {
  const sha = 'a'.repeat(64)
  const t = tree('/h/.muster/runs', {
    '20261117-1400-bar': {
      ok: task('done'),
      wait: task('done', `changes=1 files +1 -0\nchanges_sha256=${sha}\n`),
      bad: task('failed'),
      live: task('running'),
      gone: task('canceled'),
    },
  })
  await mountWith($, on, t)
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /1\/4/ }))?.text).toMatch(/█{2}░{6} 1\/4/)
  await band.unmount()
})

test('fix rounds count as one piece of work and the newest attempt decides', async ($, on) => {
  const run = '20261118-0900-rounds'
  await mountWith($, on, tree('/h/.muster/runs', {
    [run]: {
      fixed: task('failed: exit 1'),
      'fixed-r2': task('done'),
      broken: task('done'),
      'broken-r2': task('done'),
      'broken-r3': task('failed: done check failed'),
      dropped: task('canceled: not needed'),
      kept: task('done'),
      'kept-r2': task('canceled: fix round dropped'),
      gone: task('canceled: wrong worker'),
      'gone-r2': task('failed: exit 1'),
      'gone-r3': task('done'),
      live: task('running'),
    },
  }))
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /3\/5/ }))?.text).toMatch(/█{4}░{4} 3\/5/)
  expect((await band.find({ type: 'Text', text: /^✗ failed/ }))?.text).toMatch(/^✗ failed ×1/)
  await band.unmount()
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: new RegExp(run) }))?.text).toMatch(/1 running · 3 done · 1 failed · 1 canceled · 6 retried$/)
  expect((await ui.find({ type: 'Button', text: /Retried/ }))?.text).toMatch(/▸ Retried · 6/)
  expect((await ui.find({ type: 'Button', text: /Failed/ }))?.text).toMatch(/Failed · 1/)
  expect(await ui.find({ type: 'Text', text: /^\s+fixed · / })).toBeUndefined()
  await ui.press({ key: `section:${run}/Retried` })
  expect((await ui.find({ type: 'Text', text: /^\s+fixed · / }))?.text).toMatch(/fixed · w1/)
  await ui.unmount()
})

test('a rerun under another name in another run replaces the attempt it names', async ($, on) => {
  const [first, second] = ['20261118-0800-first', '20261118-0900-second']
  await mountWith($, on, tree('/h/.muster/runs', {
    [first]: { login: task('failed: exit 1'), other: task('done') },
    [second]: {
      'login-again': task('done', `retry_of=${first}/login\n`),
      'same-name': task('failed: exit 1', 'retry_of=20261118-0000-gone/nothing\n'),
      loop: task('failed: exit 1', `retry_of=${second}/pool\n`),
      pool: task('failed: exit 1', `retry_of=${second}/loop\n`),
      live: task('running'),
    },
  }))
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /^✗ failed/ }))?.text).toMatch(/^✗ failed ×3/)
  expect((await band.find({ type: 'Text', text: /1\/5/ }))?.text).toMatch(/1\/5/)
  await band.unmount()
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', text: new RegExp(first) }))?.text).toMatch(/0 running · 1 done · 0 failed · 1 retried$/)
  expect((await ui.find({ type: 'Button', text: new RegExp(second) }))?.text).toMatch(/1 running · 1 done · 3 failed$/)
  await ui.unmount()
})

test('a read-only review is finished whatever it found, and a fixed task leaves the band', async ($, on) => {
  const sha = 'b'.repeat(64)
  const t = tree('/h/.muster/runs', {
    '20261118-1000-readonly': { audit: { ...task('done', `changes=0 files +0 -0\nchanges_sha256=${sha}\n`), title: 'Audit the PR' } },
    '20261118-1100-fixed': {
      work: task('done', `changes=2 files +3 -1\nchanges_sha256=${sha}\n`),
      'work-r2': task('done', `changes=1 files +1 -0\nchanges_sha256=${sha}\n`),
    },
  })
  t['/h/.muster/runs/20261118-1000-readonly/audit/review.md'] = `Verdict: Needs fixes\nchanges-sha256: ${sha}\n`
  t['/h/.muster/runs/20261118-1100-fixed/work/review.md'] = `Verdict: Needs fixes\nchanges-sha256: ${sha}\n`
  t['/h/.muster/runs/20261118-1100-fixed/work-r2/review.md'] = `Verdict: Approved\nchanges-sha256: ${sha}\n`
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await mountWith($, on, t)
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text' }))?.text).toBe('engine')
  await band.unmount()
})

test('the band keeps a finished run that still has unreviewed tasks', async ($, on) => {
  await mountWith($, on, tree('/h/.muster/runs', {
    '20261117-1100-band': { done: task('done', `changes=1 files +1 -0\nchanges_sha256=${'a'.repeat(64)}\n`) },
  }))
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /review/ }))?.text).toMatch(/◇ review ×1/)
  await band.unmount()
})

test('a task needing review or fixes gets its own section and count, and is not done', async ($, on) => {
  const sha = 'a'.repeat(64)
  const meta = `changes=2 files +5 -1\nchanges_sha256=${sha}\n`
  const run = '20261118-1000-sections'
  const t = tree('/h/.muster/runs', {
    [run]: {
      ok: { ...task('done', meta), title: 'Approved work' },
      legacy: { ...task('done'), title: 'Legacy work' },
      open: { ...task('done', meta), title: 'Unread work' },
      bad: { ...task('done', meta), title: 'Rejected work' },
      old: { ...task('done', meta), title: 'Rejected older diff' },
    },
  })
  t[`/h/.muster/runs/${run}/ok/review.md`] = `Verdict: Approved\nchanges-sha256: ${sha}\n`
  t[`/h/.muster/runs/${run}/bad/review.md`] = `Verdict: Needs fixes\nchanges-sha256: ${sha}\n`
  t[`/h/.muster/runs/${run}/old/review.md`] = `Verdict: Needs fixes\nchanges-sha256: ${'c'.repeat(64)}\n`
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  // Nothing runs, yet the run stays open and does not read as all done.
  expect((await ui.find({ type: 'Button', text: new RegExp(run) }))?.text).toMatch(/^▾ .* 0 running · 2 done · 0 failed · 1 needs fixes · 2 needs review$/)
  const heads = (await ui.findAll({ type: 'Button' })).map((r: any) => (r.text as string).trim()).filter(r => /· \d+$/.test(r))
  expect(heads).toEqual(['▾ Needs fixes · 1', '▾ Needs review · 2', '▾ Done · 2'])
  expect((await ui.find({ type: 'Text', text: /Rejected work/ }))?.text).toMatch(/! Rejected work · needs fixes$/)
  expect((await ui.find({ type: 'Text', text: /Rejected older diff/ }))?.text).toMatch(/◇ Rejected older diff · needs review$/)
  expect((await ui.find({ type: 'Text', text: /Unread work/ }))?.text).toMatch(/◇ Unread work · needs review$/)
  expect((await ui.find({ type: 'Text', text: /Approved work/ }))?.text).toMatch(/✓ Approved work$/)
  expect((await ui.find({ type: 'Text', text: /Legacy work/ }))?.text).toMatch(/✓ Legacy work$/)
  expect((await ui.find({ type: 'Text', text: /^\s+bad · / }))?.text).toMatch(/bad · w1 · needs fixes/)
  expect((await ui.find({ type: 'Text', text: /^\s+open · / }))?.text).toMatch(/open · w1 · needs review/)
  await ui.press({ key: `section:${run}/Needs fixes` })
  expect(await ui.find({ type: 'Text', text: /Rejected work/ })).toBeUndefined()
  expect((await ui.find({ type: 'Text', text: /Unread work/ }))?.text).toMatch(/Unread work/)
  await ui.unmount()
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /2\/5/ }))?.text).toMatch(/2\/5/)
  expect((await band.find({ type: 'Text', text: /! fix/ }))?.text).toMatch(/! fix ×1/)
  expect((await band.find({ type: 'Text', text: /review/ }))?.text).toMatch(/◇ review ×2/)
  await band.unmount()
})

test('the toast names the review state of a task that finishes unreviewed', async ($, on) => {
  const toasts: string[] = []
  const clock = mock.clock(on, { now: new Date('2026-11-05T11:32:00').getTime() })
  const t = tree('/h/.muster/runs', { '20261118-1100-toast': { A: { ...task('running', 'pid=0\n'), title: 'Toasted' } } })
  await mountWith($, on, t, { clock: true, toasts })
  await clock.settle()
  t['/h/.muster/runs/20261118-1100-toast/A/status'] = 'done\n'
  t['/h/.muster/runs/20261118-1100-toast/A/meta'] += 'changes=1 files +1 -0\n'
  await clock.advance(5000)
  expect(toasts.join('\n')).toMatch(/Toasted \(w1\): needs review/)
})

test('a silent worker wakes its own session, again as the silence doubles, once per round', async ($, on) => {
  const start = new Date('2026-11-05T11:00:00').getTime()
  const clock = mock.clock(on, { now: start + 21 * 60_000 })
  const notices: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    notices.push(e.text)
    return { text: e.text }
  })
  on('fs.stat', () => ({ value: { kind: 'file', size: 0, isLink: false, mtimeMs: start } }))
  const other = { ...task('running', 'pid=0\n'), title: 'Not mine' }
  other.meta = other.meta.replace('session=S1', 'session=S2')
  await mountWith($, on, tree('/h/.muster/runs', {
    '20261118-1250-other': { foreign: other },
    '20261118-1300-stall': { hung: { ...task('running', 'pid=0\n'), title: 'Hung build' } },
  }), { clock: true })
  await clock.settle()
  expect(notices).toHaveLength(1)
  expect(notices[0]).toContain('task 20261118-1300-stall/hung ("Hung build") on w1 has written nothing to its log for 21m')
  expect(notices[0]).not.toContain('third notice')
  await clock.advance(5000)
  expect(notices).toHaveLength(1)
  await clock.advance(20 * 60_000)
  expect(notices).toHaveLength(2)
  await clock.advance(40 * 60_000)
  expect(notices).toHaveLength(3)
  expect(notices[2]).toContain('third notice')
})

test('a finished task left unreviewed wakes its session once, after a grace period', async ($, on) => {
  const clock = mock.clock(on, { now: new Date('2026-11-05T11:02:00').getTime() })
  const notices: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    notices.push(e.text)
    return { text: e.text }
  })
  const meta = `finished=2026-11-05 11:00:00\nchanges=1 files +1 -0\nchanges_sha256=${'d'.repeat(64)}\n`
  const t = tree('/h/.muster/runs', {
    '20261118-1400-left': { a: { ...task('done', meta), title: 'Left alone' }, b: { ...task('done', meta), title: 'Reviewed' }, c: task('running') },
  })
  t['/h/.muster/runs/20261118-1400-left/b/review.md'] = `Verdict: Approved\nchanges-sha256: ${'d'.repeat(64)}\n`
  await mountWith($, on, t, { clock: true })
  await clock.settle()
  expect(notices).toHaveLength(0)
  await clock.advance(60_000)
  expect(notices).toHaveLength(1)
  expect(notices[0]).toContain('task 20261118-1400-left/a ("Left alone") on w1 ended 3m00s ago (needs review)')
  expect(notices[0]).not.toContain('Reviewed')
  await clock.advance(10 * 60_000)
  expect(notices).toHaveLength(1)
})

test('a running task past its worker shows the done check instead of a silence warning', async ($, on) => {
  on('fs.stat', () => ({ value: { kind: 'file', size: 0, isLink: false, mtimeMs: new Date('2026-11-05T11:20:00').getTime() } }))
  await mountWith($, on, tree('/h/.muster/runs', {
    '20261118-1200-phase': {
      quiet: { ...task('running', 'pid=0\n'), title: 'Silent worker' },
      saving: { ...task('running', 'pid=0\nworker_exited=2026-11-05 11:31:50\n'), title: 'Saving' },
      waits: { ...task('running', 'pid=0\nworker_exited=2026-11-05 11:25:00\ncheck_waiting=2026-11-05 11:30:00\n'), title: 'Waiting' },
      checks: { ...task('running', 'pid=0\nworker_exited=2026-11-05 11:25:00\ncheck_waiting=2026-11-05 11:28:00\ncheck_started=2026-11-05 11:28:52\n'), title: 'Checking' },
    },
  }))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const rows = (await ui.findAll({ type: 'Text' })).map((r: any) => (r.text as string).trim())
  expect(rows.filter(r => r.startsWith('no output for'))).toEqual(['no output for 12m00s'])
  expect(rows).toContain('running done check · 3m08s')
  expect(rows).toContain('waiting for done check · 2m00s')
  expect(rows).toContain('saving results · 10s')
  await ui.unmount()
})

test('each task shows the model it ran on, from meta when done and from the log while running', async ($, on) => {
  const t = tree('/h/.muster/runs', {
    '20261117-1200-model': {
      fin: { ...task('done', 'model=gpt-6-astra\neffort=high\n'), title: 'Finished one' },
      live: { status: 'running\n', meta: 'worker=w1\ntype=codex\nsession=S1\nstarted=2026-11-05 11:00:00\n', title: 'Live one' },
      noisy: { status: 'running\n', meta: 'worker=w1\ntype=codex\nsession=S2\nstarted=2026-11-05 11:00:00\n', title: 'Noisy one' },
    },
  })
  t['/h/.muster/runs/20261117-1200-model/noisy/log.txt'] =
    'ERROR rmcp transport failed\n'.repeat(400) + 'OpenAI Codex\n--------\nmodel: gpt-6-nova\nreasoning effort: medium\n--------\n'
  t['/h/.muster/runs/20261117-1200-model/live/log.txt'] = 'OpenAI Codex\n--------\nmodel: gpt-6-mini\nreasoning effort: low\n--------\n'
  t['/h/.muster/runs/20261117-1200-model/fin/result.md'] = 'Status: DONE_WITH_CONCERNS\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /^\s+fin · / }))?.text).toMatch(/fin · w1 · gpt-6-astra high · \d/)
  expect((await ui.find({ type: 'Text', text: /^\s+live · / }))?.text).toMatch(/live · w1 · gpt-6-mini low · \d/)
  expect((await ui.find({ type: 'Text', text: /^\s+noisy · / }))?.text).toMatch(/noisy · w1 · gpt-6-nova medium · \d/)
  expect((await ui.find({ type: 'Text', text: /DONE_WITH_CONCERNS/ }))?.text).toBeTruthy()
  await ui.unmount()
})

test('the Stop hook sends the session back once per waiting task, and only for its own settled runs', async ($, on) => {
  const sha = 'd'.repeat(64)
  const meta = `changes=1 files +1 -0\nchanges_sha256=${sha}\n`
  const other = { ...task('done', meta), meta: task('done', meta).meta.replace('session=S1', 'session=S2') }
  const t = tree('/h/.muster/runs', {
    '20261119-1000-wait': { rev: task('done', meta), fix: task('done', meta), ok: task('done', meta), foreign: other },
    '20261119-1100-busy': { idle: task('done', meta), live: task('running') },
    '20261119-1200-redo': { x: task('done', meta), 'x-r2': task('done', meta) },
  })
  const dir = '/h/.muster/runs/20261119-1000-wait'
  t[`${dir}/fix/review.md`] = `Verdict: Needs fixes\nchanges-sha256: ${sha}\n`
  t[`${dir}/ok/review.md`] = `Verdict: Approved\nchanges-sha256: ${sha}\n`
  t['/h/.muster/runs/20261119-1200-redo/x-r2/review.md'] = `Verdict: Approved\nchanges-sha256: ${sha}\n`
  on('classic.Stop', () => ({}))
  await mountWith($, on, t)
  const first: any = await $.classic.Stop({ stop_hook_active: true, session_id: 'S1' } as any)
  expect(first.block).toBeUndefined()
  const second: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1' } as any)
  expect(second.block).toContain('- 20261119-1000-wait/rev: needs review')
  expect(second.block).toContain('- 20261119-1000-wait/fix: needs fixes')
  expect(second.block).not.toMatch(/ok:|foreign|busy|redo/)
  const third: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1' } as any)
  expect(third.block).toBeUndefined()
})

test("after compaction the session is told the user's language, and that muster is still on unless turned off", async ($, on) => {
  on('classic.SessionStart', () => ({ additionalContext: ['lower hook context'] }))
  const t = tree('/h/.muster/runs', { '20261119-1000-sticky': { a: task('running') } })
  on('prompt.submit', (_: any, e: any) => e)
  await mountWith($, on, t)
  await $.prompt.submit({ text: 'also rework the retry logic', origin: { kind: 'composer' } } as any)
  await $.prompt.submit({ text: 'Muster decision for x/y, "Which?": option 1, A.', origin: { kind: 'plugin', name: 'muster', asUser: true } } as any)
  const compacted: any = await $.classic.SessionStart({ source: 'compact', session_id: 'S1' } as any)
  expect(compacted.additionalContext[0]).toBe('lower hook context')
  expect(compacted.additionalContext[1]).toContain('last own message began: "also rework the retry logic"')
  expect(compacted.additionalContext[2]).toMatch(/^Muster is active in this session/)
  const startup: any = await $.classic.SessionStart({ source: 'startup', session_id: 'S1' } as any)
  expect(startup.additionalContext).toEqual(['lower hook context'])
  const other: any = await $.classic.SessionStart({ source: 'compact', session_id: 'S2' } as any)
  expect(other.additionalContext).toEqual(['lower hook context'])
  t['/h/.muster/runs/.off-S1'] = ''
  const off: any = await $.classic.SessionStart({ source: 'compact', session_id: 'S1' } as any)
  expect(off.additionalContext).toHaveLength(2)
  expect(off.additionalContext.join()).not.toContain('Muster is active')
})

const waitingTree = (extra: Record<string, string> = {}) => {
  const meta = `changes=1 files +1 -0\nchanges_sha256=${'d'.repeat(64)}\n`
  return { ...tree('/h/.muster/runs', { '20261119-1000-wait': { rev: task('done', meta) } }), ...extra }
}

test('the Stop hook joins its reminder to a block from a lower hook', async ($, on) => {
  on('classic.Stop', () => ({ block: 'lower says stop', additionalContext: ['lower hook context'] }))
  await mountWith($, on, waitingTree())
  const r: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1' } as any)
  expect(r.block).toContain('lower says stop')
  expect(r.block).toContain('Muster: finished tasks')
  expect(r.additionalContext).toEqual(['lower hook context'])
})

test('the Stop hook waits while subagents or workflows run, without using the reminder up', async ($, on) => {
  on('classic.Stop', () => ({}))
  await mountWith($, on, waitingTree())
  const busy: any = await $.classic.Stop({
    stop_hook_active: false,
    session_id: 'S1',
    background_tasks: [{ id: 'x', type: 'subagent', status: 'running', description: 'r' }],
  } as any)
  expect(busy.block).toBeUndefined()
  const later: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1', background_tasks: [] } as any)
  expect(later.block).toContain('20261119-1000-wait/rev')
})

test('the Stop hook does not remind when a lower hook prevents continuing, and keeps the reminder', async ($, on) => {
  let prevent = true
  on('classic.Stop', () => (prevent ? { preventContinuation: true } : {}))
  await mountWith($, on, waitingTree())
  const first: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1' } as any)
  expect(first.block).toBeUndefined()
  prevent = false
  const second: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1' } as any)
  expect(second.block).toContain('20261119-1000-wait/rev')
})

test('the Stop hook still reminds when the run holds a pending task', async ($, on) => {
  on('classic.Stop', () => ({}))
  await mountWith($, on, waitingTree({ '/h/.muster/runs/20261119-1000-wait/queued/prompt.md': '# queued\n' }))
  const r: any = await $.classic.Stop({ stop_hook_active: false, session_id: 'S1' } as any)
  expect(r.block).toContain('20261119-1000-wait/rev')
})

const inboxTree = () => {
  const sha = 'a'.repeat(64)
  const run = '20261123-1100-inbox'
  const t = tree('/h/.muster/runs', {
    [run]: {
      ask: { ...task('failed: worker reported NEEDS_CONTEXT'), title: 'Rename the section' },
      ship: { ...task('done', `cwd=/x\nchanges=2 files +5 -1\nchanges_sha256=${sha}\n`), title: 'Fix two review comments' },
      plain: { ...task('done'), title: 'Plain report' },
    },
  })
  t['/x/.git'] = 'gitdir: /repo/.git/worktrees/x\n'
  t['/repo/.git/worktrees/x/HEAD'] = 'ref: refs/heads/muster/ship\n'
  t[`/h/.muster/runs/${run}/ask/decision.md`] =
    'Question: Which name should the section key use?\nContext: The doc says TweakSection, the code says GenerativeUI.\nThe split task waits on this.\nOptions:\n- Keep the doc name: three call sites change\n- Keep the code name: the doc changes\nRecommend: 1, the doc is not merged yet\n'
  t[`/h/.muster/runs/${run}/ship/review.md`] = `Verdict: Approved\nchanges-sha256: ${sha}\nSummary: Removed a force unwrap and asserted the main thread.\n`
  t[`/h/.muster/runs/${run}/plain/result.md`] = 'Status: DONE\nAdded the retry table.\n'
  return t
}

test('the inbox shows a decision with its options, reviewed work stays out of it, and the buttons reach Claude', async ($, on) => {
  const submitted: string[] = []
  const filled: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('prompt.fill', (_: any, e: any) => {
    filled.push(e.text)
    return { isFilled: true, text: e.text, cursor: e.text.length }
  })
  await mountWith($, on, inboxTree())
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 120 } })
  expect((await ui.find({ type: 'Text', text: /To decide/ }))?.text).toMatch(/⚖️ To decide · 1/)
  expect((await ui.find({ type: 'Text', text: /⚖️ Which/ }))?.text).toMatch(/⚖️ Which name should the section key use\?/)
  expect((await ui.find({ type: 'Text', text: /TweakSection/ }))?.text).toMatch(/The doc says TweakSection, the code says GenerativeUI\. The split task waits on this\./)
  expect((await ui.find({ type: 'Text', text: /2\. Keep the code name/ }))?.text).toMatch(/2\. Keep the code name: the doc changes/)
  expect((await ui.find({ type: 'Text', text: /→/ }))?.text).toMatch(/→ 1, the doc is not merged yet/)
  // Approved work is Claude's to merge: no merge item, no Merge button.
  expect(await ui.find({ type: 'Text', text: /ready to merge/ })).toBeUndefined()
  expect(await ui.find({ type: 'Button', text: /Merge/ })).toBeUndefined()
  await ui.press({ key: 'expand-all' })
  // review.md's Summary stands for the task, and a bare `Status: DONE` is not shown as one.
  expect((await ui.find({ type: 'Text', text: /↳ Removed a force/ }))?.text).toMatch(/↳ Removed a force unwrap/)
  expect((await ui.find({ type: 'Text', text: /↳ Added the retry/ }))?.text).toMatch(/↳ Added the retry table\./)

  // Buttons carry the choice's name; one without a short name before a colon falls back to its number.
  expect((await ui.find({ type: 'Button', text: /Keep the doc name/ }))?.text).toBe('Keep the doc name')
  await ui.press({ key: 'other:20261123-1100-inbox/ask' })
  expect(filled).toEqual(['Muster decision for 20261123-1100-inbox/ask, "Which name should the section key use?": '])
  expect(submitted).toEqual([])

  await ui.press({ key: 'decide:20261123-1100-inbox/ask:2' })
  expect(submitted).toEqual(['Muster decision for 20261123-1100-inbox/ask, "Which name should the section key use?": option 2, Keep the code name: the doc changes. (Append "Decided: option 2" to that task\'s decision.md in the muster runsDir, then carry it out as step 6 of the muster skill says. The pane wrote this message in English; reply in the language of my own earlier messages.)'])
  expect((await ui.find({ type: 'Text', text: /sent:/ }))?.text).toMatch(/✓ sent: Keep the code name · waiting for Claude/)
  expect(await ui.find({ type: 'Button', text: /Keep the doc name/ })).toBeUndefined()
  await ui.unmount()
})

test('a hidden item leaves the inbox', async ($, on) => {
  await mountWith($, on, inboxTree())
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 120 } })
  await ui.press({ key: 'hide:20261123-1100-inbox/ask' })
  expect(await ui.find({ type: 'Text', text: /⚖️/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /To decide/ })).toBeUndefined()
  await ui.unmount()
})

test('the band counts what waits for the user even after the run finished', async ($, on) => {
  await mountWith($, on, inboxTree())
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /to decide/ }))?.text).toMatch(/⚖️ 1 to decide/)
  await band.unmount()
})

test('a decided question leaves the inbox', async ($, on) => {
  const t = inboxTree()
  t['/h/.muster/runs/20261123-1100-inbox/ask/decision.md'] += 'Decided: 2\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /⚖️/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /To decide/ })).toBeUndefined()
  await ui.unmount()
})

test('CJK lines of a decision join without a space, and a wrapped line never starts with closing punctuation', async ($, on) => {
  const t = inboxTree()
  t['/h/.muster/runs/20261123-1100-inbox/ask/decision.md'] =
    'Question: \u7528\u54ea\u5957\u547d\u540d\uff1f\nContext: \u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u516b\u3002\nOptions:\n- \u7532\nRecommend: \u4e24\u8fb9\u5bf9\u4e0d\u4e0a\u3002\nworker \u505c\u4e0b\u6765\u4e86\n'
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /\u4e24\u8fb9\u5bf9\u4e0d\u4e0a/ }))?.text).toMatch(/\u4e24\u8fb9\u5bf9\u4e0d\u4e0a\u3002worker \u505c\u4e0b\u6765\u4e86/)
  const lines = (await ui.findAll({ type: 'Text' })).map((r: any) => (r.text as string).trim())
  expect(lines.some(l => l.startsWith('\u3002'))).toBe(false)
  // 21 wide characters fill the 43-cell line, so the full stop falls at the break and takes the character before it down.
  expect(lines).toContain('\u516b\u3002')
  expect((await ui.find({ type: 'Button', text: /Option 1/ }))?.text).toBe('Option 1')
  await ui.unmount()
})

// The decision buttons' labels, for a decision with these options in a pane this wide.
const choiceButtons = async ($: any, on: any, options: string[], bodyColumns: number) => {
  const t = inboxTree()
  t['/h/.muster/runs/20261123-1100-inbox/ask/decision.md'] = `Question: Which?\nOptions:\n${options.map(o => `- ${o}\n`).join('')}`
  await mountWith($, on, t)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns } })
  // Every button but the pane's own controls and the run's fold row.
  const labels = (await ui.findAll({ type: 'Button' }))
    .map((b: any) => b.text as string)
    .filter((l: string) => !/^(Expand all|Collapse all|Other…|Hide|[▸▾] )/.test(l))
  await ui.unmount()
  return labels
}

test('a button names its choice, and a colon inside the name (a time, a URL) does not end it', async ($, on) => {
  expect(await choiceButtons($, on, ['12:30 standup: move it', 'https://x.dev: use the mirror'], 120)).toEqual(['12:30 standup', 'https://x.dev'])
})

test('a full-width colon ends a choice name', async ($, on) => {
  expect(await choiceButtons($, on, ['\u63a5\u53d7 RUDP \u7f16\u5165\uff1a\u96f6\u4fee\u6539', '\u6253\u8865\u4e01\uff1a\u7b49\u4e0a\u6e38'], 80)).toEqual(['\u63a5\u53d7 RUDP \u7f16\u5165', '\u6253\u8865\u4e01'])
})

test('choice names that repeat number every button', async ($, on) => {
  expect(await choiceButtons($, on, ['Fix: in place', 'Fix: upstream'], 120)).toEqual(['Option 1', 'Option 2'])
})

const LONG = ['Keep the generated name', 'Rename every call site', 'Split into two sections']
test('choice names that do not fit the row next to Other… and Hide number every button', async ($, on) => {
  expect(await choiceButtons($, on, LONG.map(n => `${n}: x`), 80)).toEqual(['Option 1', 'Option 2', 'Option 3'])
})

test('long choice names show when the row fits them', async ($, on) => {
  expect(await choiceButtons($, on, LONG.map(n => `${n}: x`), 140)).toEqual(LONG)
})

const processReport = (stdout: string, exitCode = 0, stderr = '') => ({
  stdout, stderr, exitCode, isStdoutTruncated: false, isStderrTruncated: false,
})
const statsCommand = {
  command: 'muster-stats', args: '', origin: { kind: 'composer' },
  presentation: { isFullscreen: true, columns: 120 },
} as const
const statsPane = {
  ...PANE, surface: 'terminal', requestId: 'muster-stats',
  props: { ...PANE.props, title: 'Muster stats', bodyColumns: 116, isFocused: true },
} as const
const statsTable = (group: string[], groups: string[][]) => ({
  columns: [...group, 'Settled', 'First pass', 'Sent back', 'Claude fixed', 'Blocked', 'Check failed', 'Cost/accepted'],
  short: [...group, 'Set', 'First pass', 'Back', 'Fix', 'Blk', 'Chk', '$/ok'],
  keys: group.length, has_small: true,
  rows: groups.map(values => ({ cells: [...values, '4', ' 75%*', '1', '0', '0', '0', '≥$0.26'], bar: 8, small: true })),
})
const statsView = {
  version: 2, today: '2026-11-27', legacy_note: '2 pieces from before reviews were recorded are left out.',
  labels: {
    tabs: ['Overview', 'Repos', 'Workers'], weekdays: ['Mon', '', 'Wed', '', 'Fri', '', ''],
    less: 'Less', more: 'More', shades: '·░▒▓█', pane_hint: 'Navigation hint from Python',
  },
  days: { '2026-11-23': 1, '2026-11-24': 2, '2026-11-25': 3, '2026-11-26': 4 },
  weeks: [{
    days: ['2026-11-23', '2026-11-24', '2026-11-25', '2026-11-26', '2026-11-27', '2026-11-28', '2026-11-29'],
    month: 'Nov', starts_month: false, first_label: false,
  }],
  ranges: ['All time', 'Last 7 days', 'Last 30 days'].map((name, i) => ({
    name, metrics: [['Pieces of work', i === 1 ? '2 (2 settled/0 open)' : '4 (4 settled/0 open)']],
    usage: 'Input 1.7b · Output 4.6m · Cost ≥$1 · Lines +8 -2',
    types: 'codex 75% first pass (n=4)', fun: 'Workers added 8 of the ~9k lines in the original UNIX v6 kernel.',
    tables: {
      Repos: statsTable(['Repo', 'Type'], ['project-a', 'project-b', 'project-c', 'project-d', 'project-e', 'project-f'].map(repo => [repo, 'codex'])),
      Workers: statsTable(['Tier', 'Worker', 'Model'], [['low', 'worker-a', 'model-a']]),
    },
    sample_note: '* fewer than 30 settled: small sample',
  })),
}

test('muster-stats runs the Python report and returns its summary when the pane cannot be placed', async ($, on) => {
  const opened: string[] = []
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/my plugin' } }))
  on('env.get', (_, e) => ({ value: e.name === 'MUSTER_CONFIG' ? '/custom/config.json' : undefined }))
  on('process.run', (_, e) => {
    expect(e.argv).toEqual(['python3', '/my plugin/skills/muster/muster.py', 'stats', '--json'])
    expect(e.init?.env?.MUSTER_CONFIG).toBe('/custom/config.json')
    expect(e.init?.timeoutMs).toBe(30000)
    return { value: processReport(JSON.stringify(statsView)) }
  })
  on('ui.open', (_, e) => {
    expect(e.title).toBe('Muster stats')
    expect(e.focus).toBe(true)
    opened.push(e.id)
    return { value: { isPlaced: false, reason: 'no-surface' } }
  })
  const result = await $.command.run(statsCommand)
  expect(opened).toEqual(['muster-stats'])
  expect(result.text?.split('\n')).toEqual([
    'Pieces of work: 4 (4 settled/0 open)', statsView.ranges[0]!.usage, statsView.ranges[0]!.types, statsView.ranges[0]!.fun,
  ])
})

for (const [problem, reply, reason] of [
  ['Python exits with an error', { value: processReport('', 1, 'cannot read runs\nsecond line') }, /^Muster stats failed: cannot read runs$/],
  ['Python times out', { deny: 'process timed out' }, /^Muster stats failed: .*timed out/],
  ['Python returns invalid JSON', { value: processReport('{') }, /^Muster stats failed: .+/],
] as const) {
  test(`muster-stats shows the error and opens nothing when ${problem}`, async ($, on) => {
    const opened: string[] = []
    on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
    on('env.get', () => ({ value: undefined }))
    on('process.run', () => reply)
    on('ui.open', (_, e) => {
      opened.push(e.id)
      return { value: { isPlaced: true } }
    })
    expect((await $.command.run(statsCommand)).text).toMatch(reason)
    expect(opened).toEqual([])
  })
}

test('muster-stats rejects a malformed table without reopening a previous report', async ($, on) => {
  let stdout = JSON.stringify(statsView)
  const opened: string[] = []
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
  on('env.get', () => ({ value: undefined }))
  on('process.run', () => ({ value: processReport(stdout) }))
  on('ui.open', (_, e) => {
    opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  expect((await $.command.run(statsCommand)).text).toContain('Pieces of work: 4')
  stdout = JSON.stringify(statsView, (key, value) => key === 'bar' ? 15 : value)
  expect((await $.command.run(statsCommand)).text).toMatch(/^Muster stats failed: .+/)
  expect(opened).toEqual(['muster-stats'])
})

test('the stats pane puts two metrics on a line only when both fit', async ($, on) => {
  const metrics = [['Pieces of work', '190 (184 settled/6 open)'], ['First pass', '75%'], ['Accepted', '159'], ['Attempts per work', '1.3']]
  const view = { ...statsView, ranges: statsView.ranges.map(r => ({ ...r, metrics })) }
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
  on('env.get', () => ({ value: undefined }))
  on('process.run', () => ({ value: processReport(JSON.stringify(view)) }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  await $.command.run(statsCommand)
  const ui = await $.ui.mount({ ...statsPane, props: { ...statsPane.props, bodyColumns: 120 } })
  // A two-column row holds its left pair in a Box of fixed width.
  const leftColumn = async () => (await ui.findAll({ type: 'Box' }))
    .filter(box => typeof box.props.width === 'number' && box.text.startsWith('Pieces of work')).map(box => box.props.width)
  expect(await leftColumn()).toEqual(['Pieces of work 190 (184 settled/6 open)'.length + 3])
  await ui.redraw({ ...statsPane.props, bodyColumns: 50 })
  expect(await leftColumn()).toEqual([])
  for (const [label, value] of metrics) expect(await ui.find({ type: 'Text', text: `${label} ${value}` })).toBeDefined()
  await ui.unmount()
})

test('the stats pane lays out Python values and follows focus and supplies every table row for native scrolling', async ($, on) => {
  let runs = 0
  const view = { ...statsView, labels: { ...statsView.labels, less: 'Less activity', more: 'More activity' } }
  on('process.hookEnv', () => ({ value: { CLAUDE_PLUGIN_ROOT: '/plugin' } }))
  on('env.get', () => ({ value: undefined }))
  on('process.run', () => {
    runs++
    return { value: processReport(JSON.stringify(view)) }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  const landed: string[] = []
  on('ui.focus', { plugin: 'muster' }, (_, e) => {
    if (e.element) landed.push(e.element)
    return {}
  })
  await $.command.run(statsCommand)
  const ui = await $.ui.mount({ ...statsPane, props: { ...statsPane.props, scroll: { offset: 0, bodyRows: 7 } } })
  const line = async (text: RegExp | string) => (await ui.find({ type: 'Text', text }))?.text
  for (const name of ['Overview', 'Repos', 'Workers']) expect(await ui.find({ text: name })).toBeDefined()
  expect(await line(/Mon\s+░░/)).toBeDefined()
  expect(await line(/▒▒/)).toBeDefined()
  expect(await line(/Wed\s+▓▓/)).toBeDefined()
  expect(await line(/██/)).toBeDefined()
  expect(await line(/Fri\s+··/)).toBeDefined()
  expect(await line(/Less activity ░ ▒ ▓ █ More activity/)).toBeDefined()
  for (const text of [statsView.ranges[0]!.usage, statsView.ranges[0]!.types, statsView.ranges[0]!.fun, statsView.legacy_note]) {
    expect(await line(text)).toBeDefined()
  }
  expect((await ui.find({ type: 'Text', text: statsView.ranges[0]!.fun }))?.props.color).toBe('suggestion')
  expect(await line(view.labels.pane_hint)).toBeDefined()
  expect(await line(/4 \(4 settled\/0 open\)/)).toBeDefined()
  const autofocus = async () => (await ui.findAll({ type: 'Button' })).filter(button => button.props.autoFocus).map(button => button.text)
  expect(await autofocus()).toEqual(['Overview'])
  const focus = (element: string) => $.ui.focus({
    component: 'Pane', requestId: 'muster-stats', plugin: 'muster', element, origin: { kind: 'person' },
  })
  await focus('stats-range-1')
  expect(await line(/2 \(2 settled\/0 open\)/)).toBeDefined()
  await focus('stats-tab-1')
  expect(await autofocus()).toEqual(['Repos'])
  expect(await line(/Cost\/accepted/)).toBeDefined()
  expect(await line(/project-a.*75%\* ████████··.*≥\$0\.26/)).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /^\s*████████··$/ }))?.props.color).toBe('claude')
  expect((await ui.find({ type: 'Text', text: /^\s*75%\*$/ }))?.props.dimColor).toBe(true)
  expect(await line(statsView.ranges[0]!.sample_note)).toBeDefined()
  expect(await line(/project-f/)).toBeDefined()
  await focus('stats-tab-2')
  expect(await line(/low\s+worker-a\s+model-a/)).toBeDefined()
  await focus('stats-tab-1')
  expect(await line(/project-a/)).toBeDefined()
  expect(landed).toEqual(['stats-range-1', 'stats-tab-1', 'stats-tab-2', 'stats-tab-1'])
  await ui.redraw({ ...statsPane.props, bodyColumns: 70 })
  expect(await line(/Repo\s+Type\s+Set\s+First pass.*\$\/ok/)).toBeDefined()
  await ui.press({ key: 'stats-tab-0' })
  expect(await line(/2 \(2 settled\/0 open\)/)).toBeDefined()
  await ui.press({ key: 'stats-range-0' })
  expect(await line(/4 \(4 settled\/0 open\)/)).toBeDefined()
  expect(runs).toBe(1)
  await ui.unmount()
})
