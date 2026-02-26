# claodex

Multi-agent collaboration CLI for Claude Code and Codex.

Routes messages between two AI coding agents with **zero per-turn agent
overhead** — no tool calls, no fetch scripts. An external process tails each
agent's session log, computes the delta of unseen peer events, and injects it
as a plain user message. Each agent sees a continuous conversation at minimal
token cost: exactly the peer events it hasn't seen yet, no duplicates, no
full-history replays.

Agents integrate through a **skill file** (`SKILL.md`) installed at startup.
The skill tells each agent how messages are routed, how to read source headers
(`--- user ---`, `--- claude ---`, `--- codex ---`), and how to behave during
automated collab. No plugins or special tooling — the skill file and a
one-time registration command are the entire integration surface.

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
the skill, and drops you into the REPL. Press Enter in each agent pane to
invoke the skill and complete registration.

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

Multiple instances work simultaneously — each workspace gets its own tmux
session.

## REPL controls

The prompt shows your current target agent (`claude ❯ _` or `codex ❯ _`).

| Key | Action |
|---|---|
| `Tab` | Toggle target between `claude` and `codex` |
| `Enter` | Send message to current target |
| `Ctrl+J` | Insert newline (multi-line messages) |
| Up/Down | Navigate history (single-line) or move cursor (multi-line) |
| `Ctrl+C` | Clear input, or halt collab if one is running |
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

## Collab mode

`/collab` automates the routing loop: send to agent A → wait for response →
route to agent B → wait → route back → repeat. The loop runs until the turn
limit, `/halt`, or both agents signal `[CONVERGED]`.

You can type messages mid-collab — they're included in the next routed turn as
`--- user ---` blocks. Agents can also initiate collab by ending a response
with `[COLLAB]`.

## Sidebar

The bottom-right pane runs a curses-based sidebar showing:

- **Metrics strip** — target, mode, agent thinking status, uptime, turn counts
- **Scrolling log** — timestamped, color-coded events
- **Shell runner** — `$` prompt for non-interactive commands in the workspace

## Resuming sessions

If the CLI exits but the tmux session survives (e.g. terminal disconnect):

```bash
python3 -m claodex attach             # from the same directory
python3 -m claodex attach ~/myproject  # or specify the path
```

If an agent session expires or you `/resume` inside an agent pane, claodex
detects the new session file and hot-swaps automatically.

Use `tmux ls` to list sessions, `tmux kill-session -t <name>` to clean up.

## tmux basics

claodex manages the tmux session for you — you rarely need tmux commands
directly. The essentials:

| Action | Keys |
|---|---|
| Switch pane | `Ctrl+b` then arrow key |
| Scroll up | `Ctrl+b` then `[`, arrows/PgUp, `q` to exit |
| Detach (session keeps running) | `Ctrl+b` then `d` |
| Reattach | `python3 -m claodex attach` |

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|---|---|---|
| `CLAODEX_POLL_SECONDS` | `0.5` | JSONL poll interval |
| `CLAODEX_TURN_TIMEOUT_SECONDS` | `18000` | Max seconds to wait for a turn |
| `CLAODEX_PASTE_SUBMIT_DELAY_SECONDS` | adaptive | Fixed paste-to-submit delay |
| `CLAODEX_CLAUDE_SKILLS_DIR` | `~/.claude/skills` | Claude skill install root |
| `CLAODEX_CODEX_SKILLS_DIR` | `~/.codex/skills` | Codex skill install root |
