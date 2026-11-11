import type { EngineInterface, Register, Timer } from 'claude-code'

const STALE_MS = 86_400_000
const TYPING_RECHECK_MS = 30_000
const KEY_PREFIX = 'armed:'
const OFF_EXIT = 3 // goon.py's exit code for "switched off"; any other failure is a crash
const RETRY_MS = 60_000

let resumeText = 'Go on.'
let quietHours = ''
let skipWhileTyping = true
let timer: Timer | undefined
let isWaitingForTyping = false
let turns = 0

const disarm = async ($: EngineInterface, sessionId: string) => {
  timer?.cancel()
  timer = undefined
  isWaitingForTyping = false
  await $.store.delete(KEY_PREFIX + sessionId)
}

const goon = ($: EngineInterface, ...args: string[]) => $.process.run(['python3', `${$.plugin.root}/hooks/goon.py`, ...args])

// goon.py reads the transcript's limit error and the quiet hours: one parser for both halves.
const resumeAt = async ($: EngineInterface, sessionId: string) => {
  const { exitCode, stdout } = await goon($, 'reset', '--session', sessionId, '--quiet', quietHours)
  if (exitCode !== 0 || !stdout.trim()) return undefined
  const parsed = JSON.parse(stdout).resumeAt
  return typeof parsed === 'number' ? parsed * 1000 : undefined
}

const later = ($: EngineInterface, sessionId: string, delay: number) => {
  timer = $.clock.after(delay, () => void fire($, sessionId).catch(error => $.ui.log(`goon: ${error}`, { to: 'debug' })))
}

const fire = async ($: EngineInterface, sessionId: string) => {
  const { exitCode } = await goon($, 'power', 'status')
  if (exitCode === OFF_EXIT) {
    await disarm($, sessionId)
    $.ui.toast('Goon is off: this session was not continued')
    return
  }
  if (exitCode !== 0) {
    $.ui.log(`goon: could not read the on/off switch (exit ${exitCode})`, { to: 'debug' })
    later($, sessionId, RETRY_MS)
    return
  }
  if (skipWhileTyping && (await $.prompt.read()).text.trim() !== '') {
    if (!isWaitingForTyping) $.ui.toast('Goon: waiting for you to finish typing')
    isWaitingForTyping = true
    later($, sessionId, TYPING_RECHECK_MS)
    return
  }
  await disarm($, sessionId)
  $.ui.toast('Goon: the limit has reset, continuing')
  await $.prompt.submit({ text: resumeText })
}

const arm = async ($: EngineInterface, sessionId: string, at: number) => {
  const delay = Math.max(0, at - (await $.clock.now()))
  timer?.cancel() // after the await, so two overlapping calls cannot both slip past it
  later($, sessionId, delay)
  await $.store.set(KEY_PREFIX + sessionId, at)
  $.ui.toast(`Goon will continue this session at ${new Date(at).toISOString().slice(11, 16)} UTC`)
}

// The turn that hit the limit ended with an API error; a newer turn means the person got there first.
const onLimit = async ($: EngineInterface) => {
  const seen = turns
  const sessionId = await $.session.id()
  const at = await resumeAt($, sessionId)
  if (at !== undefined && turns === seen) await arm($, sessionId, at)
}

// A reopened session has no timer: the stored resume time re-arms it, and stale ones are dropped.
const restore = async ($: EngineInterface) => {
  const sessionId = await $.session.id()
  const now = await $.clock.now()
  for (const key of await $.store.keys()) {
    if (!key.startsWith(KEY_PREFIX)) continue
    const at = Number(await $.store.get(key))
    if (key === KEY_PREFIX + sessionId && at > now) await arm($, sessionId, at)
    else if (key === KEY_PREFIX + sessionId || at + STALE_MS < now) await $.store.delete(key)
  }
}

export const register: Register = (on, options) => {
  resumeText = String(options.resume_text || resumeText)
  quietHours = String(options.quiet_hours ?? '')
  skipWhileTyping = options.skip_while_typing !== false

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'goon', description: 'Turn Goon on or off', argumentHint: 'on|off|status' })
    const started = await next(e)
    void restore($).catch(error => $.ui.log(`goon: restore failed: ${error}`, { to: 'debug' }))
    return started
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'goon' }, async ($, e) => {
    const word = e.args.trim()
    if (!['on', 'off', 'status', ''].includes(word)) return { text: 'Use /goon on, /goon off or /goon status.' }
    return { text: (await goon($, 'power', word || 'status')).stdout.trim() }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && e.reason === 'error') {
      void onLimit($).catch(error => $.ui.log(`goon: could not arm: ${error}`, { to: 'debug' }))
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      turns += 1
      await disarm($, await $.session.id())
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('session.end', async ($, e, next) => {
    timer?.cancel()
    timer = undefined
    if (e.reason === 'clear') await $.store.delete(KEY_PREFIX + (await $.session.id()))
    return next(e)
  }).catch(($, e, next) => next(e))
}
