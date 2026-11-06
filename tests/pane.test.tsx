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
    value: e.path === R ? [dir('20261101-0900-old'), dir('20261104-0800-foreign'), dir('20261105-1130-x')] : e.path.endsWith('-x') ? [dir('A'), dir('B'), dir('G'), dir('L'), dir('P')] : e.path.endsWith('-old') ? [dir('Z')] : e.path.endsWith('-foreign') ? [dir('F')] : [],
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
    expect((await ui.find({ type: 'Text', text: /A · codex-a/ }))?.text).toMatch(/A · codex-a · done · 1m40s · ⎇ feat\/retry/)
    expect((await ui.find({ type: 'Text', text: /retry logic/ }))?.text).toMatch(/↳ retry logic added, see report\.md$/)
    expect((await ui.find({ type: 'Text', text: /B · deepseek/ }))?.text).toMatch(/B · deepseek · running/)
    expect((await ui.find({ type: 'Button', text: /20261101-0900-old/ }))?.text).toMatch(/[▸▾] 20261101-0900-old 0 running · 1 done/)
    expect(await ui.find({ type: 'Button', text: /foreign/ })).toBeUndefined()
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
    expect((await ui.find({ type: 'Text', text: /P · / }))?.text).toMatch(/P · \? · pending$/)
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
  expect((await ui.find({ type: 'Text', text: /^✓1/ }))?.text).toMatch(/✓1/)
  expect((await ui.find({ type: 'Text', text: /^… B/ }))?.text).toMatch(/^… B deepseek/)
  await ui.unmount()
})

test('a muster prompt opens the pane; other prompts do not', async ($, on) => {
  const opened: string[] = []
  on('command.register', () => ({ value: { command: 'muster-panel' } }))
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

  await $.prompt.submit({ text: 'fix the login bug' })
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
  opts: { config?: string; session?: string; clock?: boolean } = {},
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
  on('ui.toast', () => ({ value: undefined }))
  on('session.id', () => ({ value: opts.session ?? 'S1' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
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

test('queued tasks show their dependencies and priority, in the order they will start', async ($, on) => {
  // Queued tasks have no `started` yet, so no elapsed time.
  const q = (status: string, extra: string) => ({ status: `${status}\n`, meta: `worker=w1\ntype=codex\nsession=S1\npid=0\n${extra}` })
  await mountWith(
    $,
    on,
    tree('/h/.muster/runs', {
      '20261105-1000-q': {
        A: { ...task('running', 'pid=0\n'), title: 'base work' },
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
  expect((await ui.find({ type: 'Text', text: /H · / }))?.text).toMatch(/H · w1\|w2 · queued #1, w1 runs 3 of 3, w2 runs 1 of 1$/)
  expect((await ui.find({ type: 'Text', text: /B · / }))?.text).toMatch(/B · w1 · queued, waiting for A · after A · priority 9$/)
  expect((await ui.find({ type: 'Text', text: /D · / }))?.text).toMatch(/D · w1 · queued #1, w1 runs 3 of 3 · priority 5$/)
  expect((await ui.find({ type: 'Text', text: /C · / }))?.text).toMatch(/C · w1 · queued #3, /)
  expect((await ui.find({ type: 'Text', text: /old approach/ }))?.text).toMatch(/⊘ old approach/)
  expect((await ui.find({ type: 'Button', text: /Canceled/ }))?.text).toMatch(/Canceled · 1/)
  expect((await ui.find({ type: 'Button', text: /20261105-1000-q/ }))?.text).toMatch(/1 running · 0 done · 0 failed · 5 queued · 1 canceled/)
  await ui.unmount()
  const band = await $.ui.mount({ plugin: 'muster', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false } as any })
  expect((await band.find({ type: 'Text', text: /^○/ }))?.text).toMatch(/○5/)
  expect((await band.find({ type: 'Text', text: /^⊘/ }))?.text).toMatch(/⊘1/)
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
