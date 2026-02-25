# claodex

Multi-agent collaboration CLI for Claude Code and Codex.

## The problem

Running two AI coding agents side by side sounds powerful until you try it.
Each agent lives in its own session with its own context window. The moment
you want Agent B to see what Agent A said, you're copying text between
terminals — or burning tokens on pull scripts that fetch, parse, and reinject
conversation history every single turn.

The fundamental tension: **both agents need near-full context of the group
conversation, but delivering that context has a cost** — and the naive
approaches (per-turn fetch scripts, full history replay) either waste agent
tool-call overhead or blow up token budgets.

## The insight

Each agent already persists its session as an append-only JSONL log. An
external process can tail those logs, compute the *delta* of what one agent
has said since the other last heard from it, and inject that delta into the
next message — all without the agents executing a single command.

This means:

- **Zero per-turn agent overhead.** After one-time registration, agents never
  run routing scripts. The CLI does all the work.
- **Near-full group context at minimal token cost.** Each agent receives
  exactly the peer events it hasn't seen yet — no duplicates, no full-history
  replays, no wasted context window.
- **Automated collaboration.** The CLI can route responses back and forth
  between agents autonomously, turning two independent sessions into a
  real-time pair-programming conversation.

claodex is the CLI that implements this. You talk to both agents from one
place; it handles message delivery, context sharing, and automated
back-and-forth collaboration.

## Prerequisites

- Python 3.12+
- `tmux` installed (`sudo apt install tmux` or `brew install tmux`)
- `claude` CLI available on PATH (Claude Code)
- `codex` CLI available on PATH (OpenAI Codex)

## Quick start

```bash
python3 -m claodex            # from any directory
```

This creates a tmux session with four panes, launches both agents, installs
the collaboration skill, and drops you into the claodex REPL. You'll see the
agent panes with prefilled registration commands — press Enter in each agent
pane to complete registration. Startup takes ~10-15 seconds.

You can run multiple instances simultaneously — each workspace gets its own
tmux session (named `claodex-<dirname>-<hash>`).

```
┌───────────────────┬───────────────────┐
│                   │                   │
│       Codex       │      Claude       │
│                   │                   │  (~67%)
│                   │                   │
├───────────────────┴──┬────────────────┤
│        Input         │    Sidebar     │
│                      │                │  (~33%)
└──────────────────────┴────────────────┘
         (57%)               (43%)
```

When you're done:

```bash
/quit          # kills agents, tmux session, and exits
```

### Resume after exit

If the CLI exits but the tmux session survives (e.g. terminal disconnect):

```bash
python3 -m claodex attach           # from the same directory
python3 -m claodex attach ~/myproject  # or specify the path
```

This reattaches to the running session with cursors intact. If the sidebar
process died, it's relaunched automatically.

Graceful exits (`/quit`, `Ctrl+D`) kill agents and the tmux session entirely.
To start fresh, just run `python3 -m claodex` again.

**Agent re-registration.** If an agent session expires or you `/resume` inside
an agent pane, the agent writes a new session file. claodex detects this
automatically and hot-swaps to the new file — no restart needed. This happens
during normal REPL polling, so just send a message or wait a moment after
the agent comes back.

**Finding your sessions.** Each workspace gets a unique tmux session. Use
`tmux ls` to see all running sessions, or `tmux kill-session -t <name>` to
clean one up manually.

## REPL controls

The prompt shows your current target agent:

```
claude ❯ _
```

| Key | Action |
|---|---|
| `Tab` | Toggle target between `claude` and `codex` |
| `Enter` | Send message to current target |
| `Ctrl+J` | Insert a newline (for multi-line messages) |
| Up/Down | Navigate input history (single-line) or move cursor (multi-line) |
| `Ctrl+C` | Clear input (normal mode) or halt collab (collab mode) |
| `Ctrl+D` | Quit (same as `/quit`) |

## Commands

| Command | Description |
|---|---|
| `/collab <message>` | Start automated collaboration between agents |
| `/collab --turns N <message>` | Limit to N turns (default: 100) |
| `/collab --start codex <message>` | Start with Codex going first |
| `/halt` | Halt a running collaboration after the current turn |
| `/status` | Show runtime status in the sidebar |
| `/quit` | Kill agents, tmux session, and exit |

## How it works

### Normal mode

1. You type a message and press Enter.
2. claodex computes the **delta** — everything the peer agent has said since
   the target last heard from it.
3. The delta (if any) is prepended to your message with `--- source ---`
   headers.
4. The composed message is pasted into the target agent's tmux pane.
5. claodex watches the agent's JSONL for a deterministic turn-end marker,
   then records the response.

Each agent sees a continuous conversation: the peer's latest exchanges
followed by your message. No context is duplicated or lost.

### Collab mode

`/collab` automates the routing loop. After the initial message:

1. Wait for the target agent to respond.
2. Route the full response to the peer agent.
3. Wait for the peer to respond.
4. Route back to the first agent.
5. Repeat until the turn limit, `/halt`, or both agents signal `[CONVERGED]`.

You can type messages mid-collab — they're included in the next routed turn
as `--- user ---` blocks without halting the exchange. Agents can also
initiate collab by ending a response with `[COLLAB]`.

### Sidebar

The bottom-right pane runs a curses-based sidebar that shows:

- **Metrics strip** — current target, mode (normal/collaborative), agent
  thinking status with spinner, uptime, turn counts
- **Scrolling log** — timestamped, color-coded events (sends, receives,
  collab progress, errors, system events)
- **Shell runner** — type commands at the `$` prompt to run non-interactive
  shell commands in the workspace context

The sidebar is a read-only consumer of the UI state files written by the
input process. Shell command output stays sidebar-local.

## State files

claodex keeps all runtime state in `.claodex/` at the workspace root (auto-
gitignored):

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
├── ui/               # sidebar data (events.jsonl, metrics.json)
├── exchanges/        # collab exchange logs (markdown)
└── inbox/            # reserved for file-based delivery fallback
```

## tmux survival guide

tmux is a terminal multiplexer — it lets multiple terminal sessions run
inside one window. claodex uses it to run Claude, Codex, and your CLI side
by side.

### Key concepts

- **Session**: a named container for panes. claodex creates one per workspace
  (e.g. `claodex-myproject-a1b2c3`).
- **Pane**: one terminal inside the session. You'll see four: Codex
  (top-left), Claude (top-right), your input (bottom-left), and the sidebar
  (bottom-right).
- **Prefix key**: tmux commands start with `Ctrl+b` (press Ctrl+b, release,
  then press the next key).

### Commands you'll actually use

| Action | Keys |
|---|---|
| Switch to another pane | `Ctrl+b` then arrow key |
| Scroll up in a pane | `Ctrl+b` then `[`, then arrows or PgUp/PgDn. `q` exits scroll |
| Detach (leave session running) | `Ctrl+b` then `d` |
| Reattach from outside | `python3 -m claodex attach` (or `tmux attach -t <session>`) |
| Kill the whole session | `tmux kill-session -t <session>` (find name with `tmux ls`) |
| List running sessions | `tmux ls` |

### Tips

- **You don't need to switch panes during normal use.** The input pane is
  where you type. Agent panes show their native output — glance at them or
  scroll up, but claodex handles all routing.
- **If your terminal disconnects** (SSH drop, window close), the tmux session
  keeps running. Reattach with `python3 -m claodex attach` from the same
  directory.
- **If an agent pane crashes**, claodex detects it and reports a dead-pane
  error on the next interaction.

## Configuration

Environment variables for tuning (all optional):

| Variable | Default | Description |
|---|---|---|
| `CLAODEX_POLL_SECONDS` | `0.5` | JSONL poll interval |
| `CLAODEX_TURN_TIMEOUT_SECONDS` | `18000` | Max seconds to wait for a turn |
| `CLAODEX_PASTE_SUBMIT_DELAY_SECONDS` | adaptive | Fixed paste-to-submit delay |
| `CLAODEX_CLAUDE_SKILLS_DIR` | `~/.claude/skills` | Claude skill install root |
| `CLAODEX_CODEX_SKILLS_DIR` | `~/.codex/skills` | Codex skill install root |
