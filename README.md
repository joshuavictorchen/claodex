# claodex

Multi-agent collaboration CLI for Claude Code and Codex.

`claodex` runs Claude Code and Codex as live peers in one workspace. It
forwards only the new peer messages each agent has not seen yet, so long
exchanges stay synchronized without bloating the prompt. The result is a
three-way group chat between you, Claude, and Codex, with every message
tagged by author.

Both agents run in their normal CLI sessions inside two tmux panes. A router
tails each agent's session JSONL, tracks delivery state, and injects the next
peer events as a plain user message on the following turn. From each agent's
point of view, the conversation simply continues with clear source-tagged
headers (`--- user ---`, `--- claude ---`, `--- codex ---`).

## What you gain

- **Efficient long exchanges.** Each agent receives exactly the peer events it
  has not seen yet, so token use grows with the conversation instead of with
  repeated history.
- **Agents run untouched.** Claude Code and Codex keep their normal CLI
  sessions, skills, MCP servers, and hooks. claodex integrates through a
  single skill file plus a one-line registration command; the rest of the
  routing, state, and UI runs outside the agent.
- **Clear peer review.** Source headers preserve authorship, so each agent can
  challenge the other as a peer instead of collapsing into one blended voice.
- **Live operator control.** Type into the REPL during an automated exchange
  and your input joins the next routed turn as a `--- user ---` block.

## Collab mode

`/collab <message>` runs the routing loop without human intervention each
turn: deliver to agent A, wait for its response, deliver to agent B, wait,
repeat. The loop stops when the turn limit is hit, you run `/halt`, or both
agents end consecutive turns with `[CONVERGED]` on the last line.

Messages you type during collab are queued and included in the next routed
turn as `--- user ---` blocks. The first routed turn of a user-initiated
collab carries `(collab initiated by user)` so both agents can see who
started the exchange. Agents can also request collab themselves by ending a
turn with `[COLLAB]` on its own line; the REPL prompts you to approve before
the exchange begins.

Default turn limit is 12. Override with `--turns N`.

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
│                   │                   │
│                   │                   │
├───────────────────┴──┬────────────────┤
│        Input         │    Sidebar     │
│                      │                │
└──────────────────────┴────────────────┘
```

Multiple instances run side by side. Each workspace gets its own tmux
session, keyed by directory.

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
| `/collab --turns N <message>` | Limit to N turns (default: 12) |
| `/collab --start codex <message>` | Start with Codex going first |
| `/halt` | Halt a running collaboration after the current turn |
| `/status` | Show runtime status in the sidebar |
| `/quit` | Kill agents, tmux session, and exit |

## Sidebar

The bottom-right pane runs a curses sidebar with three sections:

- **Metrics strip**: current target, mode, per-agent thinking status, uptime,
  turn counts
- **Scrolling log**: timestamped, color-coded routing events
- **Shell runner**: `$` prompt for non-interactive commands in the workspace

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

claodex manages the tmux session for you, so you rarely need raw tmux
commands. The essentials:

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
