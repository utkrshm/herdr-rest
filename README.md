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

I tried to build this plugin keeping all of that in mind, and coming to a general solution for everything.

## Features

- Automatically pause idle agents in unfocused projects to free memory.
- Resume the same native session when you focus its pane.
- Find sleeping agents in Go To, with their coding-agent type and session title.
- View agent states, inactivity countdowns, and last-active times inside Herdr.
- Recover sleeping sessions across Herdr restarts when their destinations can
  be verified.
- Use one configuration across your Herdr sessions, with separate runtime state
  for each session.

## Requirements

- Herdr **0.9.1 or newer**.
- Python **3.11 or newer**, with `curses` support, available as `python3`.
- Linux or macOS.
- Supported coding-agent executables available to Herdr.
- Native Herdr integrations for Codex and Pi if you use those agents.

The plugin uses the Python standard library; no additional Python packages are
required.

## Installation

1. Clone or download this repository to a directory you will keep on disk.
2. Link the checkout into Herdr:

   ```bash
   herdr plugin link /path/to/herdr-rest
   ```

   Replace the path with the absolute path to your checkout.

3. If you use Codex or Pi, install the corresponding Herdr integration:

   ```bash
   herdr integration install codex
   herdr integration install pi
   ```

   Run only the command for each agent you use.

4. Start a Herdr server session, or restart an existing server to run the
   plugin's startup hook. Herdr Rest starts its background watcher automatically.
5. Locate the generated configuration directory:

   ```bash
   herdr plugin config-dir herdr.rest
   ```

   It contains `config.toml` after the first startup. To check agent tracking,
   open the [inactivity view](#inactivity-view).

## Configuration

Edit `config.toml` in the directory returned by
`herdr plugin config-dir herdr.rest`. Herdr Rest creates it on first startup and
preserves existing settings. Configuration is shared across your Herdr sessions.

```toml
[hibernate]
idle_seconds = 900
poll_seconds = 2
focus_debounce_seconds = 0.15
terminate_wait_seconds = 15
herdr_binary = "herdr"
```

All durations are in seconds.

| Setting | Default | Purpose |
| --- | --- | --- |
| `idle_seconds` | `900` | Inactivity interval before an eligible agent sleeps (15 minutes). |
| `poll_seconds` | `2` | Interval between background status checks. |
| `focus_debounce_seconds` | `0.15` | How long a sleeping pane must stay focused before resume begins. |
| `terminate_wait_seconds` | `15` | Maximum wait for the agent process and its Herdr registration to clear. |
| `herdr_binary` | `"herdr"` | Herdr executable name or path. |

Durations must be positive, except `focus_debounce_seconds`, which may be `0`.
Herdr's `HERDR_BIN_PATH` environment variable takes precedence over
`herdr_binary`.

Valid configuration changes apply automatically at the next background check
and start fresh inactivity countdowns.

## Usage

Once installed, pause and resume are automatic:

1. Work in a project as usual. A project is a Herdr workspace; all agents in the
   focused workspace are protected, including agents in its other tabs.
2. Switch to another project. Each eligible idle agent in the previous project
   starts its own countdown. Agent activity or status changes reset its timer;
   returning to the project clears all its timers.
3. After the configured interval, the idle agent stops and its pane stays in
   Go To with a sleeping label:

   ```text
   [sleeping] opencode: Fix authentication tests
   ```

4. Select that pane to resume the same session. Resume begins after the focus
   debounce; the coding agent's own startup may take longer.

Herdr Rest attempts a notification 30 seconds before an eligible agent sleeps.
Pausing does not depend on notification delivery.

After resume, the original pane label or automatic naming is restored. Labels
you edit while a session sleeps and explicitly assigned agent names are
preserved. Temporary launch names are cleared so Go To and the sidebar show
normal tab/pane names and coding-agent labels.

## Inactivity view

Add the **Agent inactivity** action to your Herdr `config.toml` (normally
`~/.config/herdr/config.toml`):

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "herdr.rest.inactivity"
description = "Agent inactivity"
```

To change the shortcut, edit `key` in this entry. For example, use
`key = "ctrl+alt+i"` for a direct shortcut without the prefix, or choose another
unused `prefix+…` binding. Keep `type = "plugin_action"` and
`command = "herdr.rest.inactivity"` unchanged.

Reload Herdr's keybindings with **prefix, Shift-R**, then open the view with
your chosen shortcut. With the example's `prefix+i` binding and a Ctrl-Space
prefix, press Ctrl-Space, then i.

The popup covers the current Herdr session's projects and preserves the
underlying pane focus. Opening it does not reset inactivity timers or acknowledge
agent completions.

| Column | Meaning |
| --- | --- |
| Project | Herdr workspace containing the agent. |
| Agent | Coding-agent type. |
| Session | Native session title, including saved titles for sleeping sessions. |
| State | Current agent state or `sleeping`. |
| Remaining | Inactivity countdown, `protected`, `unknown`, or `—`. |
| Last active | Latest observed activity in local time, or `unknown`. |

**Remaining** shows `protected` for every agent in the focused project and for
`done` agents. Unfocused idle agents show a countdown, or `unknown` when timer
data is unavailable. Other states show `—`.

**Last active** tracks observed work, activity/state changes, and focus of that
agent's pane. Focusing a project resets its idle timers without changing the
other panes' last-active timestamps. Timestamps survive watcher reloads and
sleep; older sessions show `unknown` until activity is observed.

The view is a timestamped snapshot, not a live-updating table. Close and reopen
it for a fresh snapshot.

| Keys | Action |
| --- | --- |
| `j` / `k`, arrow keys | Scroll vertically. |
| `gg` / `G` | Jump to the beginning / end. |
| `h` / `l` | Scroll horizontally. |
| `q`, Escape | Close the view. |

## Supported agents

| Agent | Session reference | Resume command | Stop method |
| --- | --- | --- | --- |
| OpenCode | Session ID | `opencode --session ID` | Verified SIGTERM. |
| Claude Code | Session ID | `claude --resume ID` | Verified SIGTERM. |
| Codex | Session ID from Herdr's Codex integration | `codex resume ID` | One Ctrl-D when the composer is verified empty. |
| Pi | Absolute session path preferred, otherwise ID | `pi --session REF` | Verified SIGTERM. |

Codex's current empty-composer check requires the visible placeholder
`› Ask Codex to do anything`. Versions with a different placeholder are skipped
even if the composer is empty. Ctrl-D is used to preserve terminal cleanup.

Pi requires version **0.77 or newer** for its interactive SIGTERM cleanup.
Supported process layouts include the native `pi` executable and Node/Bun
wrappers for the known `@mariozechner/pi-coding-agent` and
`@earendil-works/pi-coding-agent` packages.

## Safety model

- Only agents Herdr reports as `idle` are eligible. Working, blocked, unknown,
  launching, and `done` agents are protected.
- Every agent in the focused project is protected. Returning to a project
  clears its inactivity timers.
- A native session reference and verified process identity are required.
  Missing or ambiguous data, including unavailable focus information, causes
  the plugin to skip pausing the agent.
- Session identity, process identity, focus, and eligibility are checked again
  immediately before stopping an agent. Recovery metadata is saved first.
- Codex is skipped if its composer may contain a draft. Stop methods never
  escalate to SIGKILL; a timeout retains recovery metadata for later checks.
- Recovery only resumes into a verified destination. Unmatched records are
  retained for later recovery; conversation history remains in the coding
  CLI's native storage.

Pausing stops the agent process; resuming reopens its native conversation.
Unsaved input is not guaranteed to survive for agents stopped with
SIGTERM. Resume is triggered by pane focus, not background requests or memory
pressure.

## Updating

For a locally linked plugin, update the same checkout. The watcher automatically
reloads valid package source changes and configuration changes. Reloading starts
fresh inactivity countdowns.

After changes to `herdr-plugin.toml`, relink the checkout so Herdr registers the
updated hooks, actions, and panes:

```bash
herdr plugin link /path/to/herdr-rest
```

## Development

Run the test suite from the repository root:

```bash
python3 -m unittest discover -v
```

Tests use fake Herdr/CLI fixtures to cover inactivity policy, agent lifecycle,
name and label restoration, restart recovery, and the inactivity view. Passing
these tests does not establish compatibility with every coding-agent version
or packaging layout.

## Contributing

For bug reports, include your operating system, Herdr and Python versions,
coding-agent version, relevant configuration, and steps to reproduce the issue.

Keep contributions focused and include regression coverage for lifecycle or
recovery changes. Run the test suite before submitting a pull request.
