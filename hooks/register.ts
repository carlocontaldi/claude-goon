import type { EngineInterface, Register, Timer } from 'claude-code'

const STALE_MS = 86_400_000
const TYPING_RECHECK_MS = 30_000
const KEY_PREFIX = 'armed:'
const OFF_EXIT = 3 // goon.py's exit code for "switched off"; any other failure is a crash
const RETRY_MS = 60_000
const PAGE = 4 // the dialog shows 2 to 4 options and takes anything typed under its Other
const MORE = 'More presets…'
const DOG = '🐕‍🦺' // Goon's mark, in front of what it shows the person

let resumeOption = ''
let quietHours = ''
let topicOption = ''
let tokenOption = ''
let skipWhileTyping = true
let timer: Timer | undefined
let isWaitingForTyping = false
let turns = 0

const toast = ($: EngineInterface, text: string) => $.ui.toast(`${DOG} ${text}`)

const disarm = async ($: EngineInterface, sessionId: string) => {
  timer?.cancel()
  timer = undefined
  isWaitingForTyping = false
  await $.store.delete(KEY_PREFIX + sessionId)
}

// The script reads these options from its environment, as it does when a hook runs it.
const scriptEnv = () => ({
  ...(topicOption === '' ? {} : { CLAUDE_PLUGIN_OPTION_NTFY_TOPIC: topicOption }),
  ...(tokenOption === '' ? {} : { CLAUDE_PLUGIN_OPTION_NTFY_TOKEN: tokenOption }),
  ...(resumeOption === '' ? {} : { CLAUDE_PLUGIN_OPTION_RESUME_TEXT: resumeOption }),
})

const goonWith = ($: EngineInterface, args: string[]) =>
  $.process.run(['python', `${$.plugin.root}/hooks/goon.py`, ...args], { env: scriptEnv() })

const goon = ($: EngineInterface, ...args: string[]) => goonWith($, args)

// goon.py reads the transcript's limit error and the quiet hours: one parser for both halves.
const resumeAt = async ($: EngineInterface, sessionId: string) => {
  const { exitCode, stdout } = await goon($, 'reset', '--session', sessionId, '--quiet', quietHours)
  if (exitCode !== 0 || !stdout.trim()) return undefined
  const parsed = JSON.parse(stdout).resumeAt
  return typeof parsed === 'number' ? parsed * 1000 : undefined
}

type Plan = { message: string; presets: string[] }

const readPlan = (stdout: string): Plan | undefined => {
  try {
    const { message, presets } = JSON.parse(stdout)
    if (typeof message !== 'string' || message === '' || !Array.isArray(presets)) return undefined
    return { message, presets: presets.filter(preset => typeof preset === 'string') }
  } catch {
    return undefined
  }
}

const planFor = async ($: EngineInterface, sessionId: string) => {
  const { exitCode, stdout } = await goon($, 'plan', '--session', sessionId)
  return { exitCode, plan: exitCode === 0 ? readPlan(stdout) : undefined }
}

const later = ($: EngineInterface, sessionId: string, delay: number) => {
  timer = $.clock.after(delay, () => void fire($, sessionId).catch(error => $.ui.log(`goon: ${error}`, { to: 'debug' })))
}

// The message and the switches are read from goon.py each time, so a change made while waiting counts.
const fire = async ($: EngineInterface, sessionId: string) => {
  const { exitCode, plan } = await planFor($, sessionId)
  if (exitCode === OFF_EXIT) {
    await disarm($, sessionId)
    toast($, 'Goon is off: this session was not continued')
    return
  }
  if (plan === undefined) {
    $.ui.log(`goon: could not read the plan (exit ${exitCode})`, { to: 'debug' })
    later($, sessionId, RETRY_MS)
    return
  }
  if (skipWhileTyping && (await $.prompt.read()).text.trim() !== '') {
    if (!isWaitingForTyping) toast($, 'Goon: waiting for you to finish typing')
    isWaitingForTyping = true
    later($, sessionId, TYPING_RECHECK_MS)
    return
  }
  await disarm($, sessionId)
  toast($, 'Goon: the limit has reset, continuing')
  await $.prompt.submit({ text: plan.message })
  await goon($, 'command', 'session', sessionId, 'reset') // the choices were for this interruption only
}

const arm = async ($: EngineInterface, sessionId: string, at: number) => {
  const delay = Math.max(0, at - (await $.clock.now()))
  timer?.cancel() // after the await, so two overlapping calls cannot both slip past it
  later($, sessionId, delay)
  await $.store.set(KEY_PREFIX + sessionId, at)
  toast($, `Goon will continue this session at ${new Date(at).toISOString().slice(11, 16)} UTC`)
}

// The presets one page at a time: the default first, then "More presets…" while some are left.
const askMessage = async ($: EngineInterface, { message, presets }: Plan) => {
  const options = [message, ...presets.filter(preset => preset !== message)]
  const question = `What should Goon send this session when the limit resets (dismiss to keep "${message}")?`
  for (let from = 0; ; ) {
    const rest = options.slice(from)
    const shown = rest.length <= PAGE ? rest : rest.slice(0, PAGE - 1)
    const labels = shown.map((text, i) => (from + i === 0 ? `${text} (default)` : text))
    const answer = await $.ui.ask(question, { options: shown.length < rest.length ? [...labels, MORE] : labels, header: `${DOG} Goon` })
    if (answer === MORE) {
      from += shown.length
      continue
    }
    const picked = labels.indexOf(answer)
    if (picked === -1) return answer.trim() === '' ? undefined : answer // typed under Other
    return from + picked === 0 ? undefined : shown[picked]
  }
}

// A choice left from an earlier interruption is dropped, then the person picks one for this.
const chooseMessage = async ($: EngineInterface, sessionId: string, seen: number) => {
  await goon($, 'command', 'session', sessionId, 'reset')
  const { plan } = await planFor($, sessionId)
  if (plan === undefined) return
  const answer = await askMessage($, plan).catch(() => undefined) // dismissed, or nobody to ask
  if (answer === undefined || turns !== seen) return
  toast($, (await goon($, 'command', 'session', sessionId, 'message', answer)).stdout.trim())
}

// The turn that hit the limit ended with an API error; a newer turn means the person got there first.
const onLimit = async ($: EngineInterface) => {
  const seen = turns
  const sessionId = await $.session.id()
  const at = await resumeAt($, sessionId)
  if (at === undefined || turns !== seen) return
  await arm($, sessionId, at)
  await goon($, 'track', '--session', sessionId, '--resume-at', String(Math.floor(at / 1000))) // for /goon list
  await chooseMessage($, sessionId, seen)
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
  resumeOption = String(options.resume_text ?? '').trim()
  quietHours = String(options.quiet_hours ?? '')
  topicOption = String(options.ntfy_topic ?? '').trim()
  tokenOption = String(options.ntfy_token ?? '').trim()
  skipWhileTyping = options.skip_while_typing !== false

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'goon',
      description: 'Turn Goon on or off, choose what it sends each session, or set up its ntfy topic',
      argumentHint: 'on|off|status|list|presets|preset|message|session|topic|test',
    })
    const started = await next(e)
    void restore($).catch(error => $.ui.log(`goon: restore failed: ${error}`, { to: 'debug' }))
    return started
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'goon' }, async ($, e) => {
    const args = e.args.trim()
    const words = args.split(/\s+/).filter(Boolean)
    const word = words.join(' ')
    if (['topic', 'topic new', 'test'].includes(word)) return { text: (await goonWith($, words)).stdout.trim() }
    if (['on', 'off', 'status', ''].includes(word)) return { text: (await goon($, 'power', word || 'status')).stdout.trim() }
    const forwarded = /^session\s+this\b/i.test(args) ? args.replace(/^session\s+this\b/i, `session ${await $.session.id()}`) : args
    return { text: (await goon($, 'command', forwarded)).stdout.trim() }
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
