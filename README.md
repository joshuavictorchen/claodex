# claodex

Multi-agent tmux router for Claude Code + Codex collaboration. You talk to
both agents from one place; claodex handles message delivery, context sharing,
and automated back-and-forth collaboration.

## Prerequisites

- Python 3.12+
- `tmux` installed (`sudo apt install tmux` or `brew install tmux`)
- `claude` CLI available on PATH (Claude Code)
- `codex` CLI available on PATH (OpenAI Codex)

## Quick start

```bash
# from the repository root
python3 -m claodex
```

This creates a tmux session with three panes (Codex, Claude, your CLI),
launches both agents, registers them for collaboration, and drops you into the
claodex REPL. Startup is usually ~15-30 seconds, but can take longer during
agent registration retries.

When you're done:

```bash
/quit          # kills agents, tmux session, and exits
```

### Resume after exit or WSL restart

Graceful exits (`/quit`, `Ctrl+D`) kill the agents and tmux session. To start
a fresh session that picks up each agent's conversation history, launch as usual
but use `/resume` manually before calling the `claodex` skill.


Both agents retain their full JSONL session logs across restarts. Sending a
primer message lets them re-orient from their own history without claodex
needing to replay state.

## tmux survival guide

tmux is a terminal multiplexer — it lets multiple terminal sessions run inside
one window. claodex uses it to run Claude, Codex, and your CLI side by side.

### Key concepts

- **Session**: a named container for panes. claodex creates one called `claodex`.
- **Pane**: one terminal inside the session. You'll see three: Codex (top-left),
  Claude (top-right), and your CLI (bottom).
- **Prefix key**: tmux commands start with `Ctrl+b` (press Ctrl+b, release, then
  press the next key). This is how you talk to tmux itself rather than the
  program running in a pane.

### Commands you'll actually use

| Action | Keys |
|---|---|
| Switch to another pane | `Ctrl+b` then arrow key |
| Scroll up in a pane | `Ctrl+b` then `[`, then arrow keys or PgUp/PgDn. Press `q` to exit scroll mode |
| Detach (leave session running) | `Ctrl+b` then `d` |
| Reattach from outside | `tmux attach -t claodex` (or `python3 -m claodex attach`) |
| Kill the whole session | `tmux kill-session -t claodex` |
| List running sessions | `tmux ls` |

### Tips

- **You don't need to switch panes during normal use.** The bottom CLI pane is
  where you type. Claude and Codex panes show their native output — you can
  glance at them or scroll up to see what they're doing, but claodex handles
  all message routing.
- **If your terminal disconnects** (SSH drop, window close), the tmux session
  keeps running. Reattach with `tmux attach -t claodex`. Note: this is
  different from `/quit` or `Ctrl+D`, which kill the session entirely.
- **If an agent pane crashes**, claodex will detect it and report a dead-pane
  error when you next send a message.

## REPL controls

The claodex prompt shows your current target:

```
claude ❯ _
```

| Key | Action |
|---|---|
| `Tab` | Toggle target between `claude` and `codex` |
| `Enter` | Send message to current target |
| `Ctrl+J` | Insert a newline (for multi-line messages) |
| Up/Down arrows | Browse input history |
| `Ctrl+D` | Quit (same as `/quit`) |

## Commands

| Command | Description |
|---|---|
| `/collab <message>` | Start automated collaboration — agents take turns responding to each other |
| `/collab --turns 4 <message>` | Limit collab to 4 turns (default: 10) |
| `/collab --start codex <message>` | Start collab with Codex going first |
| `/halt` | Request graceful halt of a running collab |
| `/status` | Show participant info and cursor positions |
| `/quit` | Kill agents, tmux session, and exit |

## How it works

1. **You send a message** to Claude or Codex via the REPL.
2. **claodex prepends undelivered context** from the other agent (if any), so
   each agent sees what the other has been saying.
3. **The message is pasted** into the target agent's tmux pane.
4. **claodex watches** the agent's JSONL session log for a deterministic
   turn-end marker, then extracts the response.

In `/collab` mode, steps 2-4 repeat automatically, alternating between agents
for the specified number of turns.

## State files

claodex keeps its state in `.claodex/` at the workspace root. This directory
is gitignored automatically.

```
.claodex/
├── participants/     # agent registration JSON
│   ├── claude.json
│   └── codex.json
├── cursors/          # read position in each agent's JSONL
│   ├── read-claude.cursor
│   └── read-codex.cursor
├── delivery/         # what's been delivered to each agent
│   ├── to-claude.cursor
│   └── to-codex.cursor
├── exchanges/        # collab exchange logs (markdown)
└── inbox/            # reserved for future use
```
