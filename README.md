# Herdr Rest

Herdr Rest is a Python based Herdr plugin to pause your coding agent sessions if they've been inactive for too long
and resume them once you come back to the pane of that agent.

It uses a background Daemon to keep track of all your agent sessions and removes the agents that 
are not in focus and have not been visited for a specific amount of time (configurable through the plugin settings).

## Motivation

I've started to use Herdr a lot for managing my coding agent sessions, but I have a tiny laptop
without a lot of RAM; and when managing multiple projects, out of which I'm only working on
only one at the moment, Herdr eats up a lot of RAM because each coding agent (be it Claude Code, or Opencode)
takes up about ~300-500 MB of RAM. Across multiple projects, 4GB was being used by Herdr alone.

Unfortunately, Herdr doesn't come with a pre-built option to pause the sessions if they are inactive. So I built my own version for that.

I tried the other plugins as well, but they do not provide all the functionality that I need. 
The other plugins don't provide a comprehensive view of the paused coding agents in the goto screen 
(which I normally use to navigate in Herdr).
I also don't get a comprehensive Inactivity view of all my agents, giving a snapshot of the status of all my agents.



## Install

Link this directory with `herdr plugin link /path/to/herdr-rest`. The
startup hook starts one detached watcher; an atomic lock makes repeated
startup hooks harmless. Runtime commands execute with the plugin root as their
working directory, as specified by Herdr.

Herdr reports the editable configuration directory with:

```bash
herdr plugin config-dir herdr.rest
```

The watcher starts automatically with Herdr's server startup hook. Hibernated
sessions remain in Go To, and focusing their pane resumes them automatically.
The small internal parser in `__main__.py` dispatches Herdr's manifest entry
points (`start`, `run`, `focus`, `open-view`, and `view`). The startup command is
simply `python3 -m herdr_rest start`; there are no manual list/resume/restart
actions or standalone debugging commands.

On first startup, the plugin atomically creates a default `config.toml` in
Herdr's plugin configuration directory. It never overwrites an existing file;
the fallback configuration directory is shared by the plugin, not the
socket-hashed runtime state directory. Configuration is shared by the user's
Herdr sessions.

Edit that file when you want to change the policy:

```toml
[hibernate]
idle_seconds = 900
poll_seconds = 2
focus_debounce_seconds = 0.15
terminate_wait_seconds = 15
herdr_binary = "herdr" # useful for a fake CLI in tests
```

`idle_seconds` is the single global inactivity policy for every supported agent.

`HERDR_BIN_PATH` takes precedence over `herdr_binary`. Workspace focus is
observed by polling `workspace.list`; any focused pane or tab makes its entire
project safe and clears every agent's inactivity timer. When the project becomes
unfocused, each eligible agent starts a fresh timer. Missing or malformed focus
data, an agent identity change, or a Herdr API outage clears inactivity tracking
and fails closed. Activity sequence, status, or native session changes reset
only that agent's timer. The `pane.focused` hook writes one coalesced,
per-session wake request, so a focus event normally starts the authoritative
check within about 50 ms instead of waiting for the next two-second poll. The
0.15-second debounce remains asynchronous and configurable: it prevents an
eager resume while the user is only passing across a pane. The debounce deadline
is scheduled directly, rather than quantized to the next poll. Startup time for
the resumed agent is separate and may be longer.

When an eligible native session is within 30 seconds of hibernation, Herdr Rest
tries to show one notification per native session and inactivity interval. The
notification may be unavailable when Herdr has no foreground client, is busy,
rate-limited, or has toast delivery disabled; those failures never postpone or
cancel the safety checks.

## Safety and limitations

* A native session reference, terminal ID, pane ID, supported agent kind, and
  discoverable foreground process are all required. Missing or ambiguous data
  fails closed. State and locks are scoped by the exact `HERDR_SOCKET_PATH`,
  not a shared default directory.
* Agent eligibility uses semantic `idle` state and excludes focused or
  launch-pending agents. `interactive_ready` is not required: Herdr omits that
  launch hint for ordinary idle agents started directly from a shell.
  `done` agents are protected and have no running idle timer; once Herdr
  reports `idle`, a fresh countdown starts while their project is unfocused.
* The daemon re-fetches the project and agent immediately before stopping it,
  checks that the same agent is still eligible, unfocused, and past its own
  inactivity interval, persists recovery state, and verifies the exact
   foreground process before the agent-specific exit action. It never guesses a PID or steals
  focus.
* OpenCode resumes with `opencode --session ID`; Claude with `claude --resume ID`;
  Codex with `codex resume SESSION`; and Pi with `pi --session REF`. Pi prefers
  Herdr's absolute JSONL session path and otherwise uses its ID. The path is
  persisted as a path reference; the plugin does not read it or scan the disk.
* Codex is stopped only with one validated Herdr `agent send-keys ... ctrl+d`
  request, and only after a live identity recheck and a conservative visible
  screen check proves the last composer row is exactly `› Ask Codex to do
  anything`. A non-empty or unknown draft is a no-op: no recovery record is
  retained, no text is submitted, no signal is sent, and a later poll can retry
  after the draft clears. The plugin never sends `/quit`, never sends multiple
   keys, and never escalates to SIGKILL.
   Codex versions with a different placeholder are conservatively skipped by
   the current screen check, even if their composer is empty.
* Pi is stopped with one SIGTERM after exact process-profile validation. Pi's
  current released interactive mode handles SIGTERM/SIGHUP, emits its
  `session_shutdown` cleanup, and restores the terminal (Pi 0.77+); older Pi
  builds without that behavior are not promised. OpenCode and Claude retain their existing
  one-SIGTERM behavior. Every method waits for both the verified process and
  Herdr's agent registration to clear; a timeout retains a failed recovery
  record and is not reported as success.

The implementation follows Herdr's documented `agent.list`, `pane.list`,
`workspace.list`, `pane.process-info`, and `agent start` surfaces. It reconciles missing panes
and updates records when a live terminal receives a new pane ID. Herdr plugin
startup commands are not supervised, so the watcher is explicitly detached
and singleton-locked. Focus events are best-effort wake hints; startup or older
servers that do not emit them continue to work through polling.
The plugin does not buffer input, wake on background requests, use LRU or
memory-pressure policies, or claim lossless draft preservation. Install the
native Herdr integrations separately (do not run these as part of plugin
startup): `herdr integration install codex` and `herdr integration install pi`.
The installed `codex` and `pi` commands must also be available to Herdr's
`agent start` path.
This implementation is validated against fake Herdr/CLI fixtures; it does not
claim that every installed agent version or packaging layout has been tested.

## Supported profiles

An isolated PTY test of installed Codex 0.160.0 confirmed that SIGTERM sent
directly to the native binary exits with signal 15 but leaves raw input, echo
suppression, bracketed paste, and focus reporting enabled. Enhanced keyboard
reporting also remains enabled when active. Native Ctrl-D restores all measured
modes. The comparison was repeated with keyboard enhancement enabled and
disabled, without submitting any model prompts or touching existing sessions.

| Agent | Session reference | Resume argv | Stop method |
|---|---|---|---|
| OpenCode | ID | `opencode --session ID` | verified SIGTERM |
| Claude | ID | `claude --resume ID` | verified SIGTERM |
| Codex | ID, source `herdr:codex` | `codex resume ID` | one safe `ctrl+d` only when empty |
| Pi | absolute path preferred, or ID, source `herdr:pi` | `pi --session REF` | verified SIGTERM |

Pi process matching is fail-closed: it accepts the native `pi` executable or a
`node`/`bun` wrapper whose script is exactly `dist/cli.js` under the known
`@mariozechner/pi-coding-agent` or `@earendil-works/pi-coding-agent` package
structure. An arbitrary script containing `pi` is not accepted.

Upstream references checked for this implementation:

* Herdr plugin manifest, runtime environment, and event-hook contract:
  <https://herdr.dev/docs/plugins/>
* Herdr `pane.focused` event and plugin event-hook behavior:
  <https://herdr.dev/docs/socket-api/>

* Herdr resume/session validation and argv planning:
  <https://raw.githubusercontent.com/herdrdev/herdr/master/src/agent_resume.rs>
* Herdr key validation and agent send-keys surface (v0.9 reference):
  <https://github.com/ogulcancelik/herdr/blob/3d9d2b18dab139ba226ebc5a1c9a9f2c9c3ee4df/docs/versions/0.9.0/website/src/content/docs/cli-reference.mdx>
* Codex current TUI source: `ChatWidget` uses `composer_is_empty` for its
  Ctrl+D quit path, while the composer emptiness check includes attachments:
  <https://github.com/openai/codex/blob/main/codex-rs/tui/src/chatwidget.rs>
  and <https://github.com/openai/codex/blob/main/codex-rs/tui/src/bottom_pane/chat_composer.rs>
  (historical behavior change: <https://github.com/openai/codex/issues/1443>)
* Pi interactive signal ownership and terminal cleanup:
  <https://github.com/earendil-works/pi/pull/4426>
  <https://pi.dev/changelog/releases/0.77.0>
  and <https://pi.dev/changelog/releases/0.79.4>
  and <https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/src/modes/interactive/interactive-mode.ts>

## Sleeping labels in Go To

Terminal IDs identify a live terminal instance and change when Herdr recreates
panes after a server restart. Saved hibernation records are rebound using their
persistent pane/workspace/tab IDs, working directory, exact owned sleeping
label, and a verified shell (or the matching already-restored native session).
Old process IDs are discarded during rebinding. Records that cannot yet be
verified are archived in `orphaned.json`, rather than deleted, and retried on
later checks. Ambiguous or occupied destinations are never guessed. Binding
recovery is logged in `daemon.log`.

Confirmed hibernated panes receive a temporary label in the format
`[sleeping] <agent-name>: <session-title>`, for example
`[sleeping] opencode: Fix authentication tests`. Session titles are captured
before stopping the agent; an existing custom pane label is preserved separately
for restoration. Older records without a title fall back to their stored label
or native session ID. Herdr's Go To navigator uses the pane label, so these
entries remain identifiable and selectable even with the agent process stopped.
The prefix also appears on other surfaces that display pane labels.

The previous custom label is restored after the agent resumes; automatic labels
are restored by clearing the temporary label. A label you edit while the agent
sleeps is preserved. Existing hibernation records are labelled on the watcher's
next check, and failed stops are not labelled while the agent is still live.

## Agent inactivity inside Herdr

Bind the plugin's **Agent inactivity** action in Herdr's config:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "herdr.rest.inactivity"
description = "Agent inactivity"
```

Reload Herdr's keybindings with **prefix, Shift-R**. Open the view with
**prefix, i**. With the default prefix this is Ctrl-B then i; with a
Ctrl-Space prefix it is Ctrl-Space then i. No shell command is needed.

The view is a Herdr-managed popup covering the current session's projects.
It keeps the underlying tab/pane focus and does not acknowledge completions or
reset timers. Its columns are exactly:

```text
Project  Agent  Session  State  Remaining  Last active
```

`Remaining` shows **protected** for done agents and every agent in the focused
project. Unfocused idle agents show their countdown; sleeping, working, blocked,
and unknown agents show **—**. An idle agent with unavailable/stale timer data
shows **unknown**. Sleeping sessions use their saved titles.

`Last active` shows a local timestamp (`YYYY-MM-DD HH:MM:SS`) for the latest
observed work, activity/state change, or focus of that agent's pane. Focusing
the project still resets every idle timer, but does not rewrite the activity
timestamps of its other panes. Activity timestamps survive watcher reloads and
hibernation. Sessions that were already idle or asleep before tracking began
show **unknown** until activity is observed; the plugin does not invent a
historical time. The snapshot title shows the local timezone.

The table is a timestamped snapshot; it does not refresh itself. Close and reopen
it for a new snapshot. Use j/k or arrow keys to scroll, gg/G for the beginning/end,
h/l for horizontal scrolling on narrow screens, and q or Escape to close.

## Updating the plugin

For a locally linked plugin, edits use the same checkout. After changing the
manifest (including adding actions), relink the checkout so Herdr registers
the new declarations:

```bash
herdr plugin link /path/to/herdr-rest
```

The daemon watches the shared configuration and package source files at the
normal polling cadence. Valid changes trigger an internal restart; malformed
configuration or invalid imports leave the current watcher running and record
the error in its log. A watcher restart begins fresh idle countdowns. Startup
after a Herdr server restart is handled by the startup hook.
