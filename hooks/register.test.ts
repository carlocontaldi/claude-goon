import { expect, mock, test } from 'claude-code/testing'

const START = 1_800_000_000_000
const RESET = START + 3_600_000
const MARGIN = 60_000
const TEXT = 'Go on'
const KEY = 'armed:sess-1'
const PRESETS = [
  'Go on',
  'Go on and review',
  'Go on and recap',
  'Go on and wrap up',
  'Go on, then review and recap',
  'Go on, then review and wrap up',
]

const stopped = { reason: 'error', answer: '', durationMs: 1, isAborted: false, turnId: 't1' } as const

// The engine beneath the plugin: a session, a store, a limit lookup that answers `stdout`, and the prompts it is sent.
const world = (on: Parameters<typeof mock.clock>[0], stdout: string, entries: Record<string, unknown> = {}, lookupMs = 0) => {
  const draft = { text: '' }
  const power = { isOn: true, isBroken: false }
  const plan = { message: TEXT, presets: PRESETS, isOffHere: false }
  const asked: { question: string; options: string[] }[] = []
  const answers: (string | undefined)[] = []
  const commands: string[][] = []
  const tracked: string[][] = []
  const askDelay = { ms: 0 }
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
  const ran: { args: string[]; env?: Record<string, string> }[] = []
  on('session.id', () => ({ value: 'sess-1' }))
  on('process.run', async (_, e) => {
    const rest = e.argv.slice(2)
    const result = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
    if (rest[0] === 'topic' || rest[0] === 'test') {
      ran.push({ args: rest, env: e.init?.env })
      return { value: { ...result, exitCode: 0, stdout: `ran ${rest.join(' ')}\n` } }
    }
    if (rest[0] === 'plan') {
      if (power.isBroken) return { value: { ...result, exitCode: 1, stdout: '' } }
      if (!power.isOn || plan.isOffHere) return { value: { ...result, exitCode: 3, stdout: '' } }
      return { value: { ...result, exitCode: 0, stdout: JSON.stringify({ message: plan.message, presets: plan.presets }) } }
    }
    if (rest[0] === 'track') {
      tracked.push(rest.slice(1))
      return { value: { ...result, exitCode: 0, stdout: '' } }
    }
    if (rest[0] === 'command') {
      commands.push(rest.slice(1))
      return { value: { ...result, exitCode: 0, stdout: 'done\n' } }
    }
    if (rest[0] === 'power') {
      if (power.isBroken) return { value: { ...result, exitCode: 1, stdout: '' } }
      if (rest[1] === 'on' || rest[1] === 'off') power.isOn = rest[1] === 'on'
      return { value: { ...result, exitCode: power.isOn ? 0 : 3, stdout: power.isOn ? 'Goon is on.\n' : 'Goon is off: nothing is resumed until /goon on.\n' } }
    }
    lookups.push([...e.argv])
    await clock.sleep(lookupMs)
    return { value: { ...result, exitCode: 0, stdout } }
  })
  on('tool.call', { tool: 'AskUserQuestion' }, async (_, e) => {
    const [question] = e.questions
    if (question === undefined) return { deny: 'no question' }
    asked.push({ question: question.question, options: question.options.map(option => option.label) })
    await clock.sleep(askDelay.ms)
    const answer = answers.shift()
    if (answer === undefined) return { deny: 'dismissed' }
    return { result: { questions: e.questions, answers: { [question.question]: answer } } }
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
  return { clock, store, submitted, lookups, draft, power, ran, plan, asked, answers, commands, tracked, askDelay }
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

test('/goon with anything else goes to the script as one argument, text and spaces kept', async ($, on) => {
  const { commands, power } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  expect((await $.command.run(slash('list'))).text).toBe('done')
  await $.command.run(slash(' message  Go on, then review and recap '))
  await $.command.run(slash('maybe'))
  expect(commands).toEqual([['list'], ['message  Go on, then review and recap'], ['maybe']])
  expect(power.isOn).toBe(true)
})

test('/goon session this stands for the session the command is typed in', async ($, on) => {
  const { commands } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  await $.command.run(slash('session this off'))
  await $.command.run(slash('session 1a2b off'))
  expect(commands).toEqual([['session sess-1 off'], ['session 1a2b off']])
})

test('/goon topic, topic new and test run the script and show what it says', async ($, on) => {
  const { ran } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  expect((await $.command.run(slash('topic'))).text).toBe('ran topic')
  expect((await $.command.run(slash('topic new'))).text).toBe('ran topic new')
  expect((await $.command.run(slash(' test '))).text).toBe('ran test')
  expect(ran.map(run => run.args)).toEqual([['topic'], ['topic', 'new'], ['test']])
  expect(ran[0]?.env).toEqual({ CLAUDE_PLUGIN_OPTION_RESUME_TEXT: TEXT })
})

test('a topic set in the options reaches the script', { options: { ntfy_topic: ' my-topic ' } }, async ($, on) => {
  const { ran } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  await $.command.run(slash('topic'))
  expect(ran[0]?.env).toEqual({ CLAUDE_PLUGIN_OPTION_NTFY_TOPIC: 'my-topic', CLAUDE_PLUGIN_OPTION_RESUME_TEXT: TEXT })
})

test('the access token set in the options reaches the script, so /goon test is sent as the real pushes are', { options: { ntfy_token: ' tk_fake ' } }, async ($, on) => {
  const { ran } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  await $.command.run(slash('test'))
  expect(ran[0]?.env).toEqual({ CLAUDE_PLUGIN_OPTION_NTFY_TOKEN: 'tk_fake', CLAUDE_PLUGIN_OPTION_RESUME_TEXT: TEXT })
})

test('the resume message set in the options reaches the script, which decides what is sent', { options: { resume_text: ' carry on ' } }, async ($, on) => {
  const { ran } = world(on, limit)
  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })

  await $.command.run(slash('topic'))
  expect(ran[0]?.env).toEqual({ CLAUDE_PLUGIN_OPTION_RESUME_TEXT: 'carry on' })
})

test('the message is read from the script when it fires: a change made while waiting is the one sent', async ($, on) => {
  const { clock, submitted, plan } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  plan.message = 'Go on and recap'
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual(['Go on and recap'])
})

test('a message changed while Goon waits for the typing to stop is the one sent after it', async ($, on) => {
  const { clock, submitted, draft, plan } = world(on, limit)
  draft.text = 'half a thought'

  await $.turn.complete(stopped)
  await clock.settle()
  await clock.advance(RESET - START + MARGIN)
  plan.message = 'Go on and wrap up'
  draft.text = ''
  await clock.advance(30_000)
  expect(submitted).toEqual(['Go on and wrap up'])
})

test('a session switched off while waiting is not continued', async ($, on) => {
  const { clock, store, submitted, plan } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  plan.isOffHere = true
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([])
  expect(store.has(KEY)).toBe(false)
})

test('the choices made for the session are dropped once it was continued', async ($, on) => {
  const { clock, commands } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  commands.length = 0
  await clock.advance(RESET - START + MARGIN)
  expect(commands).toEqual([['session', 'sess-1', 'reset']])
})

test('a limit stop asks for the session message, the default first, and keeps what is picked', async ($, on) => {
  const { clock, asked, answers, commands } = world(on, limit)
  answers.push('Go on and recap')

  await $.turn.complete(stopped)
  await clock.settle()
  expect(asked).toHaveLength(1)
  expect(asked[0]?.options).toEqual(['Go on (default)', 'Go on and review', 'Go on and recap', 'More presets…'])
  expect(commands).toEqual([
    ['session', 'sess-1', 'reset'],
    ['session', 'sess-1', 'message', 'Go on and recap'],
  ])
})

test('picking the default keeps no message of its own, so a later change to the default still counts', async ($, on) => {
  const { clock, answers, commands } = world(on, limit)
  answers.push('Go on (default)')

  await $.turn.complete(stopped)
  await clock.settle()
  expect(commands).toEqual([['session', 'sess-1', 'reset']])
})

test('the default is the one the script names, even when it is no preset', async ($, on) => {
  const { clock, asked, answers, plan } = world(on, limit)
  plan.message = 'my own default'
  answers.push(undefined)

  await $.turn.complete(stopped)
  await clock.settle()
  expect(asked[0]?.options[0]).toBe('my own default (default)')
  expect(asked[0]?.options).toHaveLength(4)
  expect(asked[0]?.options).toContain('Go on')
})

test('text typed under Other becomes the message', async ($, on) => {
  const { clock, answers, commands } = world(on, limit)
  answers.push('check the logs first')

  await $.turn.complete(stopped)
  await clock.settle()
  expect(commands.at(-1)).toEqual(['session', 'sess-1', 'message', 'check the logs first'])
})

test('a dismissed question keeps the default and still arms the resume', async ($, on) => {
  const { clock, store, submitted, commands } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  expect(commands).toEqual([['session', 'sess-1', 'reset']])
  expect(store.get(KEY)).toBe(RESET + MARGIN)
  await clock.advance(RESET - START + MARGIN)
  expect(submitted).toEqual([TEXT])
})

test('more presets than the dialog holds come on further pages', async ($, on) => {
  const { clock, asked, answers, commands, plan } = world(on, limit)
  plan.presets = [...PRESETS, 'five', 'six']
  answers.push('More presets…', 'More presets…', 'six')

  await $.turn.complete(stopped)
  await clock.settle()
  expect(asked.map(page => page.options)).toEqual([
    ['Go on (default)', 'Go on and review', 'Go on and recap', 'More presets…'],
    ['Go on and wrap up', 'Go on, then review and recap', 'Go on, then review and wrap up', 'More presets…'],
    ['five', 'six'],
  ])
  expect(commands.at(-1)).toEqual(['session', 'sess-1', 'message', 'six'])
})

test('a turn the person starts while the question is open drops the answer', async ($, on) => {
  const { clock, answers, commands, askDelay } = world(on, limit)
  answers.push('Go on and recap')
  askDelay.ms = 5_000

  await $.turn.complete(stopped)
  await clock.settle()
  await $.turn.start({ text: 'I am back', turnId: 't2' })
  await clock.advance(5_000)
  expect(commands).toEqual([['session', 'sess-1', 'reset']])
})

test('with Goon off nothing is asked', async ($, on) => {
  const { clock, asked, power } = world(on, limit)
  power.isOn = false

  await $.turn.complete(stopped)
  await clock.settle()
  expect(asked).toEqual([])
})

test('arming tells goon.py which session and when, so /goon list can show it', async ($, on) => {
  const { clock, tracked } = world(on, limit)

  await $.turn.complete(stopped)
  await clock.settle()
  expect(tracked).toEqual([['--session', 'sess-1', '--resume-at', String((RESET + MARGIN) / 1000)]])
})

test('an error that is no usage limit is not tracked', async ($, on) => {
  const { clock, tracked } = world(on, '')

  await $.turn.complete(stopped)
  await clock.settle()
  expect(tracked).toEqual([])
})
