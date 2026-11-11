# Claude Goon

A Claude Code plugin that tells interrupted sessions to go on. When a usage limit stops a session, Goon waits for the reset time the limit message states, then sends the session a resume message. The waiting costs no tokens; only the resumed turn does.

Two halves share the work, so a session is brought back whether or not its window is still open:

- **Open sessions.** A mod inside the session notices the limit, arms a timer for the reset time plus a minute, and submits the resume message into the same session and panel. If the prompt box holds a draft at that moment, it waits and checks again every 30 seconds. A newer turn (you, or the built-in `/rate-limit-options` wait) cancels it. The resume time is stored, so a session reopened before then is re-armed.
- **Closed sessions.** A `StopFailure` hook starts a detached watcher per reset time. At reset plus a minute it checks each interrupted session: still ending in the limit error, and no running Claude Code process holding it. Those it resumes headless, one every 15 seconds, with `claude --resume <id>` over stream-json, in the session's own directory. Sessions already continued are left alone, and so are open ones, which belong to the mod inside them.

If you set an ntfy topic, the watcher then pushes one line, such as `Limit reset: resumed 2 closed sessions, 1 open session left to the Goon mod inside.`

Claude Code 2.1.319 also has a built-in wait (`/rate-limit-options`, "Wait here, then continue automatically"). It is per session, you choose it by hand each time, and the session has to stay open. Goon arms itself, and also covers sessions that were closed.

## Requirements

- Linux (the watcher uses `fcntl.flock`), Python 3.9 or newer with the standard library only, and `claude` on the machine. Only Linux is tested.
- To read whether a session is open, the watcher lists `~/.claude/sessions/*.json`, a registry Claude Code keeps but does not document. If it cannot be read, closed sessions are skipped and logged rather than guessed at.

## Install

At the prompt of a terminal session:

```
/plugin install goon --marketplace carlocontaldi/claude-goon
```

From a shell:

```
claude plugin install goon --marketplace carlocontaldi/claude-goon
```

## Switch it on and off

Goon is on by default. In any session, `/goon off` stops it everywhere, `/goon on` starts it again and `/goon status` says which it is. The switch is the file `~/.claude/goon/off`, so it covers every open session and the closed-session watcher until you flip it back. It is read when Goon is about to resume something, so turning it on before a reset still resumes that session, and turning it off stops resumes that have not started. A resume already running is not stopped.

## Configure

All seven values are optional.

| Value | Meaning |
|---|---|
| Resume message | What an interrupted session is sent. Default: "Go on. The usage limit has reset: continue what you were doing." |
| Wait while you are typing | On by default. An open session is not continued while its prompt box holds a draft. Closed sessions have no prompt box, so it does not apply to them. |
| Quiet hours | Off by default. A daily window such as `22:00-07:00` (24 h, this machine's clock) or `22:00-07:00 Europe/Lisbon`, in which nothing is resumed. A limit that resets inside it is acted on when the window ends, for open and closed sessions alike. An invalid window is logged and ignored. |
| Permission mode for closed sessions | `acceptEdits`, `auto`, `dontAsk`, `plan` or `manual`, for headless resumes. Unset uses your settings. Anything else, `bypassPermissions` included, is ignored. |
| ntfy topic | A hard-to-guess topic; on the public server anyone who knows it can read and publish. No topic, no push. |
| Notification email | Also have ntfy email the summary. |
| ntfy access token | Needed only for the email: ntfy.sh refuses emails from anonymous requests. |

A closed session that reaches a permission prompt is paused, not refused: Goon keeps the prompt open and, if a topic is set, pushes `A resumed session is waiting on a permission prompt for <tool>.` The run is ended after 24 hours. Answering the prompt from Goon is not built yet. With an email set, Goon sends one email per limit reset (and a second when a prompt is waiting); the credits-restored notifier sends its own, and the free ntfy tier allows five emails a day.

What leaves your machine: one HTTPS request to ntfy.sh (or `GOON_NTFY_URL`) per reset, with the topic in the URL, your email and token in headers, and a one-line body of counts. No project names, paths or session ids.

## Check it

`~/.claude/goon/log.txt` records each limit seen, each session's outcome (`continued`, `open`, `resumed`, `unknown`, `no-cwd`, `gave-up`, ...), each push and each exit code. The stderr of each headless resume is in `~/.claude/goon/runs/`. Set `GOON_DIR` to use another folder.

A session is resumed at most four times a day, so one that keeps hitting a limit is not looped forever. On a cloud machine the clock is often UTC, which is what quiet hours without a zone mean there.

## Limits

- Not yet seen on a real limit: that the VS Code panel delivers the `StopFailure` hook and a `turn.complete` with `reason: 'error'` for a usage-limit stop, and how the mod and the built-in wait interact when both are armed. A headless resume of a real session, and the plugin loading in a real session, were run; the rest is covered by tests against fake `claude` and ntfy.
- A paused prompt cannot be answered yet: the session stays paused until its run is ended after 24 hours or you stop it.
- A headless resume loads the whole conversation again, as continuing by hand would. Several large sessions resumed together can use a lot of the window.
- An open session without the mod loaded (started before the plugin was installed) is reported, not resumed.
- A reboot or shutdown ends a pending watcher, and nothing re-arms it.

## Test

```
python3 -m unittest discover -s tests
claude plugin test .
```

## Develop

Run Claude Code with the clone as a plugin folder; the mod hot-reloads on save:

```
claude --plugin-dir /path/to/claude-goon
claude plugin validate .
```

## License

[MIT](LICENSE)
