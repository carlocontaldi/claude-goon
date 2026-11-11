import { expect, mock, test } from 'claude-code/testing'

const START = 1_800_000_000_000
const RESET = START + 3_600_000
const MARGIN = 60_000
const TEXT = 'Go on. The usage limit has reset: continue what you were doing.'
const KEY = 'armed:sess-1'

const stopped = { reason: 'error', answer: '', durationMs: 1, isAborted: false, turnId: 't1' } as const

// The engine beneath the plugin: a session, a store, a limit lookup that answers `stdout`, and the prompts it is sent.
const world = (on: Parameters<typeof mock.clock>[0], stdout: string, entries: Record<string, unknown> = {}, lookupMs = 0) => {
  const draft = { text: '' }
  const power = { isOn: true, isBroken: false }
  const clock = mock.clock(on, { now: START })
  const store = new Map(Object.entries(entries))
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('store.delete', (_, e) => {
    store.delete(e.key)
    return { value: undefined }
  })
  on('store.keys', () => ({ value: [...store.keys()] }))
  const submitted: string[] = []
  const lookups: string[][] = []
  on('session.id', () => ({ value: 'sess-1' }))
  on('process.run', async (_, e) => {
    const rest = e.argv.slice(2)
    const result = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
    if (rest[0] === 'power') {
      if (power.isBroken) return { value: { ...result, exitCode: 1, stdout: '' } }
      if (rest[1] === 'on' || rest[1] === 'off') power.isOn = rest[1] === 'on'
      return { value: { ...result, exitCode: power.isOn ? 0 : 3, stdout: power.isOn ? 'Goon is on.\n' : 'Goon is off: nothing is resumed until /goon on.\n' } }
    }
    lookups.push([...e.argv])
    await clock.sleep(lookupMs)
    return { value: { ...result, exitCode: 0, stdout } }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.read', () => ({ value: { text: draft.text, cursor: draft.text.length } }))
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('command.register', () => ({ value: { command: 'goon' } }))
  on('turn.complete', () => ({ text: '' }))
  on('turn.start', () => ({ turnId: 't2' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.end', () => ({ sessionId: 'sess-1' }))
  return { clock, store, submitted, lookups, draft, power }
}

const reply = (resumeAt: number) => JSON.stringify({ resetAt: RESET / 1000, resumeAt: resumeAt / 1000, window: 'five_hour' })
const limit = reply(RESET + MARGIN)

test('a limit stop continues the session a minute after the reset', async ($, on) => {
  const { clock, store, submitted, lookups } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  expect(lookups[0]?.slice(-4)).toEqual(['--session', 'sess-1', '--quiet', ''])

  await clock.advance(RESET - START + MARGIN - 1)
  expect(submitted).toEqual([])
  await clock.advance(1)
  expect(submitted).toEqual([TEXT])
  expect(store.has(KEY)).toBe(false)
})

test('a new turn before the reset cancels it', async ($, on) => {
  const { clock, store, submitted } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  expect(store.get(KEY)).toBe(RESET + MARGIN)
  await $.turn.start({ text: 'I am back', turnId: 't2' })

  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect(store.has(KEY)).toBe(false)
})

test('a turn that starts while the reset is looked up wins', async ($, on) => {
  const { clock, store, submitted } = world(on, limit, {}, 5_000)

  await $.turn.complete(stopped)
  await $.turn.start({ text: 'already back', turnId: 't2' })
  await clock.advance(5_000)

  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect([...store.keys()]).toEqual([])
})

test('an error that is no usage limit arms nothing', async ($, on) => {
  const { clock, store, submitted } = world(on, '')

  await $.turn.complete(stopped)
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect([...store.keys()]).toEqual([])
})

test("a subagent's error is not the session's limit", async ($, on) => {
  const { clock, lookups } = world(on, limit)

  await $.turn.complete({ ...stopped, agentId: 'a1' })
  await clock.settle()
  expect(lookups).toEqual([])
})

test('a session reopened before the reset is armed again from the store', async ($, on) => {
  const { clock, submitted } = world(on, '', { [KEY]: RESET + MARGIN })

  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })
  await clock.settle()
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([TEXT])
})

test('a stored resume time that has already passed is dropped, other sessions kept', async ($, on) => {
  const { clock, store, submitted } = world(on, '', { [KEY]: START - MARGIN, 'armed:other': RESET + MARGIN })

  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })
  await clock.advance(MARGIN)
  expect(submitted).toEqual([])
  expect([...store.keys()]).toEqual(['armed:other'])
})

test('clearing the conversation forgets the limit', async ($, on) => {
  const { clock, store, submitted } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  await $.session.end({ reason: 'clear', sessionId: 'sess-1', resume: { id: 'sess-1' } })

  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect([...store.keys()]).toEqual([])
})

test('a draft in the prompt box holds the resume until it is gone', async ($, on) => {
  const { clock, submitted, draft } = world(on, limit)
  draft.text = 'half a thought'

  await $.turn.complete(stopped)
  await clock.settle()
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  await clock.advance(120_000)
  expect(submitted).toEqual([])

  draft.text = ''
  await clock.advance(30_000)
  expect(submitted).toEqual([TEXT])
})

test('a turn started while Goon waits for the typing to stop ends the wait', async ($, on) => {
  const { clock, store, submitted, draft } = world(on, limit)
  draft.text = 'half a thought'

  await $.turn.complete(stopped)
  await clock.settle()
  await clock.advance(RESET - START + MARGIN)
  await $.turn.start({ text: 'sent it', turnId: 't2' })

  draft.text = ''
  await clock.advance(120_000)
  expect(submitted).toEqual([])
  expect([...store.keys()]).toEqual([])
})

test('with the typing check off, a draft does not hold the resume', { options: { skip_while_typing: false } }, async ($, on) => {
  const { clock, submitted, draft } = world(on, limit)
  draft.text = 'half a thought'

  await $.turn.complete(stopped)
  await clock.settle()
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([TEXT])
})

test('the quiet hours are handed to goon.py and its resume time is the one armed', { options: { quiet_hours: '22:00-07:00' } }, async ($, on) => {
  const morning = RESET + 8 * 3_600_000
  const { clock, submitted, lookups } = world(on, reply(morning))

  await $.turn.complete(stopped)
  await clock.settle()
  expect(lookups[0]?.slice(-2)).toEqual(['--quiet', '22:00-07:00'])

  await clock.advance(morning - START - 1)
  expect(submitted).toEqual([])
  await clock.advance(1)
  expect(submitted).toEqual([TEXT])
})

const slash = (args: string) => ({
  command: 'goon',
  args,
  origin: { kind: 'composer' },
  presentation: { isFullscreen: false, columns: 80 },
}) as const

test('a limit that resets while Goon is off is left alone', async ($, on) => {
  const { clock, store, submitted, power } = world(on, limit)
  power.isOn = false

  await $.turn.complete(stopped)
  await clock.settle()
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect([...store.keys()]).toEqual([])
})

test('switching Goon back on before the reset still continues the session', async ($, on) => {
  const { clock, submitted, power } = world(on, limit)
  power.isOn = false

  await $.turn.complete(stopped)
  await clock.settle()
  power.isOn = true
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([TEXT])
})

test('a failing switch check retries instead of treating Goon as off', async ($, on) => {
  const { clock, store, submitted, power } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  power.isBroken = true
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect(store.has(KEY)).toBe(true)

  power.isBroken = false
  await clock.advance(60_000)
  expect(submitted).toEqual([TEXT])
})

test('/goon off and /goon on switch it and say so', async ($, on) => {
  const { power } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  expect((await $.command.run(slash('off'))).text).toContain('Goon is off')
  expect(power.isOn).toBe(false)
  expect((await $.command.run(slash('on'))).text).toBe('Goon is on.')
  expect(power.isOn).toBe(true)
  expect((await $.command.run(slash(''))).text).toBe('Goon is on.')
})

test('/goon with anything else says how to use it and changes nothing', async ($, on) => {
  const { power } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  expect((await $.command.run(slash('maybe'))).text).toBe('Use /goon on, /goon off or /goon status.')
  expect(power.isOn).toBe(true)
})
