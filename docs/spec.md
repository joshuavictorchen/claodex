# claodex — Multi-Agent Collaboration CLI

## Motivation

Current multi-agent workflow requires each agent to run `pull` scripts every
turn, consuming ~2K tokens per bash invocation for overhead alone. The
group-chat skill provides awareness but not collaboration — agents see each
other's history but cannot work together without manual user mediation at every
step.

claodex replaces this with a dedicated CLI that:

- Eliminates per-turn script overhead from agents (zero agent-side commands
  after initial registration)
- Provides a unified interface for routing messages between agents
- Enables automated agent-to-agent collaboration (collab mode)
- Gives the user full visibility into cross-agent communication

## Scope

### In scope

- CLI tool for multi-agent message routing and collaboration
- tmux session management (startup, layout, pane control)
- Agent skill for session registration and collaboration framing
- Delta-based message delivery between agents
- Automated collaboration mode with configurable turn limits
- Exchange logging for post-session review
- Split-pane REPL with dedicated input and sidebar panes
- Structured UI event system for runtime output routing
- Sidebar with metrics, scrolling log, and shell command runner

### Out of scope

- Direct agent-to-agent dispatch outside CLI mediation
- More than two agents per session
- Non-tmux environments
- Agent-side script execution per turn (the entire point is to avoid this)
- Real-time streaming of agent output to the input pane (CLI reads JSONL,
  does not intercept terminal output)
- Replacing or modifying the existing group-chat skill (claodex is independent)

## Architecture

Four processes run in a single tmux session:

1. **Claude** — interactive Claude Code session in a tmux pane
2. **Codex** — interactive Codex CLI session in a tmux pane
3. **claodex input** — long-running router and orchestrator in a tmux pane
4. **claodex sidebar** — curses-based log/metrics/shell display in a tmux pane

The input process owns all routing logic, event emission, and metrics updates.
The sidebar process is a read-only consumer of UI state files plus a local
shell command runner. The two processes share no in-memory state; all
communication flows through the filesystem (see **UI Event System**).

### Communication channels

| Direction | Channel |
|-----------|---------|
| Agent → CLI | JSONL session files (agents write; CLI reads by tailing) |
| CLI → Agent | tmux buffer paste into agent's input pane |
| Agent → Agent | none; all cross-agent routing goes through the CLI |
| Input → Sidebar | `.claodex/ui/events.jsonl` (append-only) and `.claodex/ui/metrics.json` (atomic snapshot) |

### Cursor model

The CLI maintains four cursors:

| Cursor | Tracks |
|--------|--------|
| `read-claude` | CLI's read position in Claude's JSONL |
| `read-codex` | CLI's read position in Codex's JSONL |
| `delivery-to-claude` | Position in Codex's JSONL up to which events have been delivered to Claude |
| `delivery-to-codex` | Position in Claude's JSONL up to which events have been delivered to Codex |

Read cursors advance as the CLI tails JSONL files. Delivery cursors advance
only when a message containing a delta is successfully pasted into the target
pane.

## Startup Protocol

A single command initiates the full session:

```
claodex [directory]
```

`directory` defaults to the current working directory. It MUST be a git
repository or contain a `.claodex/` state directory.

### Startup sequence

1. CLI MUST create a tmux session named `claodex` with four panes in this
   layout:

   ```
   ┌──────────────────┬──────────────────┐
   │                  │                  │
   │     Codex        │     Claude       │  (~75%)
   │                  │                  │
   ├───────────┬──────┴──────────────────┤
   │   Input   │       Sidebar          │  (~25%)
   └───────────┴─────────────────────────┘
       (60%)            (40%)
   ```

   Top row: agents side by side (equal width). Height: ~75%.
   Bottom row: input (left, 60%) and sidebar (right, 40%). Height: ~25%.

2. CLI MUST start `codex` in the top-left pane and `claude` in the top-right
   pane, both with `directory` as the working directory.

3. CLI MUST launch the sidebar process in the bottom-right pane:
   `python3 -m claodex sidebar <workspace_root>`.

4. CLI MUST wait for each agent's JSONL session file to appear on disk
   (indicating the agent process has initialized).

5. CLI MUST trigger the claodex skill in each agent pane by pasting the
   appropriate trigger command (`/claodex` for Claude, `$claodex` for Codex).

6. CLI MUST wait for a registration file to appear at
   `.claodex/participants/{agent}.json` for each agent.

7. After both registrations are confirmed, CLI MUST initialize all four
   cursors to the current line counts of the respective JSONL files. This
   excludes all pre-session history and skill-loading events from future
   deltas.

8. CLI MUST enter the interactive REPL.

### Constraints

- If a tmux session named `claodex` already exists, CLI MUST refuse to start
  and report the conflict. Use `claodex attach` to reconnect (see below).
- Agent initialization timeout: 30 seconds per agent (JSONL file appears).
- Skill registration timeout: 60 seconds per agent (participant file appears).
- If either timeout is exceeded, CLI MUST report which agent failed and exit
  cleanly (killing the tmux session it created).

### Attach (resume after CLI exit)

```
claodex attach [directory]
```

If the CLI exits or crashes but the tmux session and agents are still running:

1. CLI MUST verify a `claodex` tmux session exists with exactly 4 panes.
2. CLI MUST verify both agent panes are alive.
3. CLI MUST verify the sidebar pane is alive; if dead, CLI MUST relaunch the
   sidebar process in it.
4. CLI MUST read existing registration files from `.claodex/participants/`.
5. CLI MUST read existing cursor files from `.claodex/cursors/` and
   `.claodex/delivery/`.
6. CLI MUST resume the interactive REPL with cursors intact.

If the session has 3 panes (legacy layout), CLI MUST fail with:
`"expected 4 panes (new layout); kill existing session with 'tmux kill-session -t claodex' and restart"`.

If registration files are missing or agent panes are dead, CLI MUST report the
specific failure and exit.

## Agent Skill

Each agent loads a minimal skill that provides collaboration framing and
session registration. The skill is installed at:

- Claude: `~/.claude/skills/claodex/`
- Codex: `~/.codex/skills/claodex/`

The `claodex` startup command MAY install/update the skill automatically
before triggering it.

### Framing

The skill MUST communicate the following to the agent:

- The agent is in a collaborative session with a named peer.
- Messages from the peer and from the user are delivered with clear
  separator headers: `--- {source} ---` where source is `claude`, `codex`,
  or `user`. See **Message Format** for the full specification.
- The agent SHOULD respond to peer messages collaboratively, as part of a
  team working toward a shared goal.

### Registration

The skill procedure MUST run a registration script that writes:

**File**: `.claodex/participants/{agent}.json`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `agent` | string | yes | `"claude"` or `"codex"` |
| `session_file` | string | yes | Absolute path to agent's JSONL session file |
| `session_id` | string | yes | Session identifier extracted from JSONL |
| `tmux_pane` | string | yes | tmux pane ID (auto-detected via `tmux display-message -p '#{pane_id}'`) |
| `cwd` | string | yes | Absolute path to working directory |
| `registered_at` | string | yes | ISO 8601 timestamp with timezone |

All paths MUST be absolute. The timestamp MUST include timezone offset.

The registration script MUST auto-detect the tmux pane ID (the script runs
inside the agent's pane, so tmux queries return the correct pane).

## CLI Interface

### Pane layout

The CLI occupies two tmux panes in the bottom row:

- **Input pane** (bottom-left, 60%): pure text input. After the REPL starts,
  this pane MUST contain only the prompt and user-typed text. No status
  messages, no log output, no routing feedback. The input pane is silent.
- **Sidebar pane** (bottom-right, 40%): curses-based display with metrics,
  scrolling log, and shell command runner. See **Sidebar**.

Pre-attach startup messages (creating session, waiting for registration) MAY
use stdout in the input pane because they run before the tmux layout is fully
live. After the REPL loop starts, all runtime output MUST route through the
UI event bus.

### Target selector

The CLI MUST maintain a current target: `claude` or `codex`. Default: `claude`.

The prompt MUST display the current target with agent-specific color:

- `claude ❯` in orange (`\033[38;5;208m`)
- `codex ❯` in blue (`\033[94m`)

The prompt renderer MUST account for ANSI escape sequence length when
computing cursor positions. A `visible_len()` helper MUST strip escape
sequences before calculating display width.

The target MUST toggle with a single **Tab** press.

All user messages (non-commands) are routed to the current target. The user
does not need to @-mention or prefix — just type and send.

### Text input

The CLI MUST provide a text input area supporting:

- Multi-line editing: **Ctrl+J** inserts a newline; **Enter** submits.
- Input history (up/down arrows recall previous inputs)
- Standard line-editing keys (home, end, delete, backspace, word movement)

The input experience SHOULD be comparable to Claude Code and Codex CLI input
fields.

### Commands

Commands use a `/` prefix. Bare text (no `/` prefix) is treated as a message
to the current target.

| Command | Description |
|---------|-------------|
| `/collab [--turns N] <message>` | Start collaboration mode. Default turns: 100. |
| `/halt` | Stop collaboration mode after the current turn completes. |
| `/status` | Emit structured status event to the sidebar log. |
| `/quit` | Kill agent sessions, tmux session, and exit. |

`/status` MUST NOT print to stdout after the REPL starts. It MUST emit a
single event with `kind=status` to the event bus, containing the full status
payload in `meta`. The sidebar renders this as a highlighted log entry.

### Keyboard shortcuts

| Key | Normal mode | Collab mode |
|-----|-------------|-------------|
| Tab | Toggle target agent | No effect |
| Enter | Submit message | No effect (CLI is routing) |
| Ctrl+J | Insert newline | No effect |
| Ctrl+C | Clear current input | Halt collaboration |

## UI Event System

All runtime output from the input process routes through a `UIEventBus`
abstraction. The router module (`router.py`) MUST NOT import or depend on
`UIEventBus`. Router warnings and status information MUST be returned to the
CLI layer (as return values, exceptions, or warning strings) for the CLI to
publish through the bus.

### Event bus interface

The `UIEventBus` class provides:

- `log(kind, message, *, agent=None, target=None, meta=None)` — append one
  event to `.claodex/ui/events.jsonl`.
- `update_metrics(**fields)` — merge fields into the in-memory canonical
  metrics snapshot, then atomically write the full snapshot to
  `.claodex/ui/metrics.json`. The canonical snapshot MUST be initialized
  with schema-valid defaults at construction. Every write produces a
  complete, schema-valid JSON object regardless of which fields were updated.
- `close()` — flush and close file handles.

Internal concurrency: the bus MUST hold a `threading.Lock` around all write
operations. The halt-listener thread and main thread both emit events.

### Event JSONL schema

Each line in `.claodex/ui/events.jsonl` is a JSON object:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ts` | string | yes | ISO 8601 timestamp with timezone |
| `kind` | string | yes | Event kind (see below) |
| `agent` | string | no | Agent name when relevant |
| `target` | string | no | Target agent when relevant |
| `message` | string | yes | Human-readable event description |
| `meta` | object | no | Arbitrary structured metadata |

#### Persisted event kinds

These kinds are written to `events.jsonl` by the input process:

| Kind | Description |
|------|-------------|
| `sent` | Message delivered to an agent |
| `recv` | Response received from an agent |
| `collab` | Collab lifecycle event (start, routing, halt, converge) |
| `watch` | Pending watch event (created, expired, error, replaced) |
| `error` | Error condition |
| `system` | System lifecycle event (startup, shutdown, registration) |
| `status` | `/status` command output |

#### Sidebar-local pseudo-kinds

These kinds exist only in the sidebar's in-memory log buffer. They MUST NOT
appear in `events.jsonl`:

| Kind | Description |
|------|-------------|
| `shell` | Shell command execution and output |

### Metrics JSON schema

`.claodex/ui/metrics.json` is atomically overwritten (write to `.tmp`, then
`os.replace`) by the input process. The sidebar reads it periodically (~0.5s).

```json
{
  "target": "claude",
  "mode": "normal",
  "collab_turn": null,
  "collab_max": null,
  "uptime_start": "2026-02-24T01:30:00+00:00",
  "agents": {
    "claude": {
      "status": "idle",
      "thinking_since": null,
      "last_words": 312,
      "last_latency_s": 2.1
    },
    "codex": {
      "status": "idle",
      "thinking_since": null,
      "last_words": 89,
      "last_latency_s": 0.8
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `target` | string | Current target agent |
| `mode` | string | `"normal"` or `"collab"` |
| `collab_turn` | int or null | Current collab turn number (null in normal mode) |
| `collab_max` | int or null | Max collab turns (null in normal mode) |
| `uptime_start` | string | ISO 8601 timestamp of REPL start |
| `agents.{name}.status` | string | `"idle"` or `"thinking"` |
| `agents.{name}.thinking_since` | string or null | ISO 8601 when thinking started |
| `agents.{name}.last_words` | int or null | Word count of last response |
| `agents.{name}.last_latency_s` | float or null | Seconds from send to response detection |

#### Response latency semantics

`last_latency_s` is authoritative only for collab and watched turns — cases
where the input process has both a send timestamp (`PendingSend.sent_at`) and
a deterministic response detection time. Normal fire-and-forget sends do not
block for responses, so no reliable latency is available. `last_latency_s`
MUST be `null` when latency data is unavailable.

### File lifecycle

`.claodex/ui/events.jsonl` and `.claodex/ui/metrics.json` MUST be cleared on
session start (alongside other session state). The input process creates them
when the REPL starts. The sidebar process MUST tolerate missing or empty files
on startup (the sidebar may launch before the input process initializes the
event bus).

## Sidebar

The sidebar is a separate Python process running a curses application in the
bottom-right tmux pane. It is launched via:

```
python3 -m claodex sidebar <workspace_root>
```

### Layout

The sidebar renders three sections within its curses window:

```
╔═ metrics ════════════════════╗
║ target: claude │ mode: normal║
║ claude: idle   │ codex: idle ║
║ last: claude 312w 2.1s      ║
╠═ log ════════════════════════╣
║ 01:23:45 [sent] -> claude   ║
║ 01:24:12 [recv] <- claude   ║
║ 01:25:01 [collab] start 100 ║
║                              ║
╠══════════════════════════════╣
║ $ _                          ║
╚══════════════════════════════╝
```

1. **Metrics strip** (top, 3-4 lines fixed): reads `.claodex/ui/metrics.json`
   on a timer (~0.5s). Displays current target, mode (with collab turn count
   when active), agent status (idle/thinking with elapsed time), and last
   response stats per agent.

2. **Scrolling log** (middle, fills remaining height): tails
   `.claodex/ui/events.jsonl`. Renders timestamped, color-coded entries.
   Maintains a ring buffer for display (last N entries where N fits the
   window).

3. **Shell input** (bottom, 1 line): simple command runner for non-interactive
   CLI commands.

### Log color coding

| Event source | Color |
|--------------|-------|
| Events involving codex | Blue (`curses.COLOR_BLUE`) |
| Events involving claude | Orange (256-color 208, or yellow fallback) |
| Error events | Red (`curses.COLOR_RED`) |
| System events | Dim gray (bright black / `curses.A_DIM`) |
| Shell output | Cyan (`curses.COLOR_CYAN`) |

### Shell command runner

The shell input at the bottom of the sidebar accepts non-interactive commands.
On Enter:

1. Execute `subprocess.run(cmd, shell=True, capture_output=True, timeout=30, cwd=workspace_root)`.
2. Append output lines to the in-memory log buffer with `kind=shell`.
3. Append exit code entry to the log buffer.
4. Render updated log.

Shell output MUST remain sidebar-local (in-memory log buffer only). It MUST
NOT be written to `.claodex/ui/events.jsonl`. The input process is the sole
writer to that file.

Constraints:
- Non-interactive only. Commands requiring a TTY (vim, top, less) are not
  supported.
- Output MUST be capped (e.g., 100 lines / 10KB) to protect UI
  responsiveness. Excess output is truncated with a `[truncated]` indicator.
- Commands execute with `cwd` set to the workspace root.
- Timeout: 30 seconds. Timed-out commands show a `[timeout]` indicator.

### Lifecycle

- **Start**: sidebar process is launched by the input process during session
  creation (pasted into the sidebar tmux pane).
- **Attach**: input process verifies the sidebar pane is alive. If the pane
  exists but the process is dead, the sidebar is relaunched.
- **Shutdown**: tmux session kill terminates the sidebar along with all other
  panes. No explicit shutdown protocol needed.
- **Resize**: sidebar MUST handle `SIGWINCH` and redraw all sections.
- **Startup tolerance**: sidebar MUST tolerate missing or empty UI state files
  (it may start before the input process initializes the event bus).

## Message Protocol

### Event extraction

The CLI extracts events from agent JSONL files using the same logic as the
existing group-chat extraction pipeline:

- **User messages**: verbatim text, stripped of command wrappers and
  tool-result-only entries.
- **Agent responses**: final non-empty text per turn. No tool calls, no
  intermediate thinking, no streaming partials.

The extraction logic from `extract_transcript.py` (or an equivalent copy)
provides the implementation.

### Delta computation

When sending a message to a target agent, the CLI MUST:

1. Identify the peer agent (the other one).
2. Read events from the peer's JSONL starting at the `delivery-to-{target}`
   cursor through the current read position.
3. If undelivered events exist, format them as a delta block.
4. Prepend the delta to the user's message.
5. After successful paste into the target pane, advance the delivery cursor.

**Invariant**: each agent sees every peer event exactly once. No duplicates,
no gaps.

### Delta bounds

Delta size is unbounded by default — all undelivered peer events are
included. The CLI MAY support an optional `CLAODEX_MAX_DELTA_EVENTS` cap
in the future if prompt sizes become a practical problem.

### Message format

All messages injected into agent panes use a consistent separator format.
Each block of content is preceded by a header line:

```
--- {source} ---
```

Where `{source}` is one of: `claude`, `codex`, or `user`.

**Delta with user message** (undelivered peer events exist):

```
--- user ---
<user message to peer>

--- {peer} ---
<peer response>

--- user ---
<another user message to peer>

--- {peer} ---
<another peer response>

--- user ---
<user's actual message to this agent>
```

**No delta** (no undelivered peer events):

```
--- user ---
<user's message>
```

In normal mode when there is no delta, the `--- user ---` header MAY be
omitted — the message is delivered bare. The header MUST be present when
any delta precedes the user message, so the agent can distinguish context
from instruction.

**Collab routed message** (peer response forwarded during collab):

```
--- {peer} ---
<peer's full response text>
```

No `--- user ---` block. The agent responds to the peer directly.

**Collab initial message** (first turn of collab mode):

```
--- user ---
<user's collab task message>
```

Delta from the peer (if any) is prepended before the `--- user ---` block
using the same format as normal-mode delta.

### Delta hygiene

Injected messages (containing `--- {agent} ---` headers) appear in the
target agent's JSONL as user messages. When these messages are later
extracted as delta events for the *other* agent, the headers create nested
context.

The CLI SHOULD strip previously-injected `--- {agent} ---` headers and
their content from extracted user messages before including them in outbound
deltas. Only the final `--- user ---` block (the user's actual message)
should be retained as the extracted user event.

If stripping is not feasible, the CLI MAY deliver the event as-is.
Functional correctness is not affected — only readability.

### Normal mode delivery

When the user submits a message:

1. Compute delta from the peer.
2. Format delta + user message.
3. Paste into the target agent's pane.
4. Advance delivery cursor.

The CLI MUST NOT block waiting for the agent's response after delivery.
Instead, the CLI stores a pending-send watch for the target agent and
returns to the prompt immediately. The input editor's idle callback polls
pending watches; if the agent's response ends with `[COLLAB]` on its own
line, the CLI enters collab mode automatically.

## Collaboration Mode

Collab mode automates message routing between agents for multi-turn
collaboration.

### Activation

```
/collab [--turns N] [--start <agent>] <message>
```

- `--turns N`: maximum turns (default: 100). One turn = one message sent to
  one agent and one response received. A full round-trip (both agents) = 2
  turns.
- `--start <agent>`: which agent receives the initial message. Default:
  current target.

### Flow

1. CLI computes delta from the peer (if any) for the starting agent.
2. CLI formats and delivers: delta + user's collab message.
3. CLI waits for the starting agent's response (turn detection).
4. CLI extracts the response text.
5. CLI formats a routed message for the other agent (see **Message format**).
6. CLI pastes the routed message into the other agent's pane.
7. CLI waits for that agent's response.
8. CLI extracts and routes back to the first agent.
9. Repeat until termination.

### Routed message format

Messages routed between agents during collab use the standard separator
format (see **Message format**):

```
--- {sending_agent} ---
<full response text>
```

No delta prefix is added during active routing — the routed message IS the
delta. Delivery cursors for both agents are advanced after each successful
route.

### Turn detection

The CLI uses a priority chain of deterministic signals:

**Codex:** `type == "event_msg"` and `payload.type == "task_complete"`. When
`task_started` is observed in the scan window, the CLI MUST require a subsequent
`task_complete` in the same window (preventing stale marker matching). When no
`task_started` appears, a `task_complete` MAY be accepted directly.

**Claude (priority order):**

1. **Fast path:** `type == "system"` and `subtype == "turn_duration"` in JSONL.
   Instant and 100% reliable when present.
2. **Stop-event fallback:** `Getting matching hook commands for Stop` in the
   Claude debug log (`~/.claude/debug/{session_id}.txt`). Emitted at
   end-of-turn by the Claude Code runtime even with zero hooks configured.
   The event timestamp MUST be after the send time. Assistant text MUST exist
   in the JSONL window before accepting completion.
3. **Hard timeout:** fail fast with `SMOKE SIGNAL`.

When a marker or Stop event is observed, the CLI MUST extract the latest
assistant message between the injected-message cursor and that marker line.

If no signal arrives at timeout, or a signal exists but no assistant message
is extractable in that window, the CLI MUST fail fast with an explicit
`SMOKE SIGNAL` error and halt routing. The CLI MUST NOT fall back to settle
timers or heuristic guessing.

**Interference detection:** During a Claude collab wait, if a non-meta user
row appears in the JSONL after the anchor (our injected message) but before
completion, the CLI MUST fail fast with an `interference detected` error and
abort the collab turn. Meta user rows (command wrappers, system reminders,
task notifications, continuation boilerplate) are excluded from this check.

### Agent-initiated collab

An agent can initiate collab by ending its response with `[COLLAB]` on its
own line. When the CLI detects this signal after a normal send:

1. The signal line is stripped from the response text.
2. The response is routed to the peer as turn 1 of a new collab exchange.
3. The standard collab loop continues from there.

The turn budget, convergence exit, and all other collab mechanics apply
identically to agent-initiated and user-initiated collab.

### Termination

Collab mode ends when any of:

| Trigger | Behavior |
|---------|----------|
| Turn limit reached | CLI reports rounds completed |
| `/halt` or Ctrl+C | CLI stops after current turn completes |
| Per-turn timeout (default: 300s) | CLI reports which agent timed out |
| Agent pane exited | CLI reports which agent died |
| Convergence (`[CONVERGED]` from both agents) | CLI reports converged |

Upon termination, the CLI MUST:

1. Update all delivery cursors to reflect what was actually delivered.
2. Save the exchange log to `.claodex/exchanges/`.
3. Emit collab termination events to the event bus (stop reason, rounds
   completed, exchange file path).
4. Return to normal mode with the last active target selected.

### Status display

During collab, the CLI MUST emit progress events to the event bus with
`kind=collab`. The sidebar renders these as timestamped log entries:

```
01:23:45 [collab] start: target=claude turns=100
01:23:52 [collab] turn 1 <- claude (312 words)
01:23:53 [collab] routing -> codex
01:24:01 [collab] turn 2 <- codex (198 words)
01:24:02 [collab] routing -> claude
01:25:30 [collab] halted: 4 turns, reason=user_halt
```

The metrics snapshot MUST be updated each turn with `mode`, `collab_turn`,
`collab_max`, agent status, and latency.

## tmux Integration

### Pane layout

The CLI creates a 4-pane tmux layout:

| Pane | Position | Role |
|------|----------|------|
| codex | top-left | Codex CLI |
| claude | top-right | Claude Code |
| input | bottom-left (60%) | claodex input editor |
| sidebar | bottom-right (40%) | claodex sidebar (curses) |

Creation order:
1. Create session (first pane).
2. Split vertically: top ~75% / bottom ~25%.
3. Split top row horizontally: left 50% / right 50%.
4. Split bottom row horizontally: left 60% / right 40%.

### Layout resolution

`resolve_layout` MUST require exactly 4 panes. It groups panes by
`pane_top` into two rows:

- Top row (2 panes): sorted by `pane_left` → codex (left), claude (right).
- Bottom row (2 panes): sorted by `pane_left` → input (left), sidebar (right).

If the session has fewer or more than 4 panes, `resolve_layout` MUST fail
fast with a descriptive error.

### Message injection

The CLI MUST use safe tmux buffer operations:

1. `tmux load-buffer -` (stdin) — load message into a tmux buffer.
2. `tmux paste-buffer -p -t <pane>` — paste into the target pane (`-p` skips bracketed-paste wrappers).
3. `tmux send-keys -t <pane> Enter` — submit the message.

The CLI MUST NOT use `tmux send-keys -l` for message content (special
character escaping is unreliable).

The `-p` flag on `paste-buffer` causes tmux to paste content directly without
wrapping it in bracketed-paste escape sequences. This is required because some
agent TUIs (notably Codex) intercept bracketed-paste sequences and mangle the
content. Multi-line content is pasted as raw text; the target application
receives literal newlines.

### File-based delivery fallback (future)

If direct paste is determined to be unreliable for a target (e.g., embedded
newlines cause premature submission in a future agent TUI):

1. CLI MUST write the message to `.claodex/inbox/{uuid}.md`.
2. CLI MUST send a single-line instruction instead:
   `Read and respond to .claodex/inbox/{uuid}.md`
3. This fallback SHOULD be detectable and activatable per-agent.

This fallback is not active in the current implementation. It is documented
here as a defined escape hatch for future compatibility.

### Pane health

The CLI MUST validate that a target pane is alive before injection:

```
tmux list-panes -t claodex -F '#{pane_id} #{pane_dead}'
```

If a pane is dead or missing, the CLI MUST report the failure.

## State Schema

All runtime state lives under `.claodex/` in the workspace root.

The CLI MUST ensure `.claodex/` is added to `.gitignore` on first run.

```
.claodex/
├── participants/
│   ├── claude.json           # registration data
│   └── codex.json
├── cursors/
│   ├── read-claude.cursor    # CLI's read position in Claude's JSONL
│   └── read-codex.cursor     # CLI's read position in Codex's JSONL
├── delivery/
│   ├── to-claude.cursor      # Codex events delivered to Claude up to here
│   └── to-codex.cursor       # Claude events delivered to Codex up to here
├── ui/
│   ├── events.jsonl          # structured event log (input process writes)
│   └── metrics.json          # metrics snapshot (input process writes)
├── inbox/                    # bracketed-paste fallback messages (if needed)
└── exchanges/                # collab mode logs
    └── 250222-1530.md
```

### Cursor files

Each cursor file contains a single non-negative integer (1-indexed JSONL line
number) followed by a newline. Value `0` means "start of file."

Cursors MUST only advance, never retreat.

### Exchange log format

```markdown
# Collaboration: <initial message, first 80 chars>

Started: <ISO 8601>
Initiated by: <user | claude | codex>
Agents: <agent_a> ↔ <agent_b>
Rounds: <N>
Stop reason: <turns_reached | user_halt | timeout | agent_exited | converged>

## Round 1

### → <starting_agent>
<message sent>

### ← <starting_agent>
<response received>

### → <other_agent>
<routed message>

### ← <other_agent>
<response received>

## Round 2
...
```

## Error Semantics

| Condition | Behavior |
|-----------|----------|
| tmux not installed | Refuse to start; report dependency |
| `claodex` session exists | Refuse to start; suggest `tmux kill-session -t claodex` |
| `claude` or `codex` CLI not found | Refuse to start; report which is missing |
| Agent process fails to start | Report which agent; kill session; exit |
| Agent fails to register in time | Report which agent; kill session; exit |
| JSONL parse error (malformed line) | Skip line; log warning via event bus; advance cursor past it after 3 consecutive failures or 10 seconds stuck on the same line |
| Pane died during normal mode | Report on next send attempt to that agent |
| Pane died during collab mode | Halt collab; report which agent died |
| Collab turn timeout exceeded | Halt collab; report last successful round |
| tmux paste fails | Report; halt any active collab |
| Workspace `.claodex/` not writable | Refuse to start |
| Session has 3 panes on attach | Fail with remediation: kill session and restart |
| Sidebar pane dead on attach | Relaunch sidebar process |

## Invariants

1. **Zero agent overhead.** After initial skill loading, agents do not need
   to execute any routing scripts for the remainder of the session. All
   per-turn routing work is done by the CLI. Agents are not restricted from
   running other commands — they simply have no reason to.

2. **Exactly-once delivery.** Each peer event is delivered to the other agent
   exactly once. Delivery cursors enforce this. No duplicates, no gaps.

3. **Cursor monotonicity.** All cursors (read and delivery) MUST only advance
   or stay unchanged. They MUST NOT retreat.

4. **Agent independence (detach/crash).** Agents continue running if the
   CLI detaches (`Ctrl+b d`) or crashes. The CLI observes agents; it does
   not own their lifecycle during normal operation. `/quit` and `Ctrl+D`
   are explicit teardown commands that kill agents and the tmux session.

5. **Session isolation.** One `claodex` tmux session at a time per machine.
   Concurrent sessions are not supported.

6. **Input pane silence.** After the REPL loop starts, the input pane MUST
   contain only the prompt and user-typed text. All runtime output (routing
   events, errors, status, collab progress) routes through the UI event bus
   to the sidebar.

7. **Single writer for UI state.** The input process is the sole writer to
   `.claodex/ui/events.jsonl` and `.claodex/ui/metrics.json`. The sidebar
   is read-only for these files. Shell command output is sidebar-local
   (in-memory only).

8. **Router UI-agnosticism.** `router.py` MUST NOT import or depend on the
   UI event system. All routing status, warnings, and errors are returned to
   the CLI layer as values or exceptions. The CLI decides how to publish them.

## Behavioral Examples

### Example 1: Normal mode with delta

User sends 2 messages to Claude. Then switches to Codex.

```
claude ❯ Design an API schema for auth
  sidebar log: 01:23:45 [sent] -> claude

claude ❯ Add rate limiting to the design
  sidebar log: 01:24:10 [sent] -> claude

  [Tab pressed]

codex ❯ Review the API design Claude just created
  sidebar log: 01:25:30 [sent] -> codex (with delta)
```

Codex receives all Claude exchanges as delta. Claude has seen nothing from
Codex yet.

### Example 2: Collab mode

```
claude ❯ /collab --turns 6 Design and implement an auth API together

  sidebar log:
    01:30:00 [collab] start: target=claude turns=6
    01:30:15 [collab] turn 1 <- claude (312w, 14.8s)
    01:30:16 [collab] routing -> codex
    01:30:28 [collab] turn 2 <- codex (198w, 11.5s)
    01:30:29 [collab] routing -> claude
    ...
    01:32:10 [collab] halted: 6 turns, reason=turns_reached

  sidebar metrics:
    target: claude │ mode: collab 6/6
    claude: idle │ codex: idle

claude ❯ _
```

### Example 3: Shell command in sidebar

```
  sidebar shell:
    $ git log --oneline -5
    01:35:00 [shell] $ git log --oneline -5
    01:35:00 [shell] abc1234 Add auth endpoint
    01:35:00 [shell] def5678 Fix rate limiter
    01:35:00 [shell] ghi9012 Initial commit
    01:35:00 [shell] (exit 0)
```

### Example 4: Post-collab delta correctness

After collab ends, Claude's last message was routed to Codex but Codex's
response to it was the final collab turn — so Claude has NOT seen Codex's
last response. Next manual message to Claude includes the missing delta.

## Non-Obvious Design Decisions

Documenting choices made under yolo authority:

1. **Tab for toggle.** Single keystroke, intuitive "switch" semantics. If Tab
   conflicts with input autocomplete, the binding is configurable.

2. **Default target is claude.** Arbitrary but consistent. The `--start` flag
   on `/collab` overrides for collaboration.

3. **Enter to submit, Ctrl+J for newline.** Enter submits. Ctrl+J inserts
   a literal newline for multi-line messages. Per user preference.

4. **Deterministic marker-only turn detection.** Turn ends are accepted only
   when source-native end markers arrive (`turn_duration` / `task_complete`).
   Missing markers produce a fail-fast `SMOKE SIGNAL` instead of heuristic
   fallback.

5. **100 default collab turns.** High enough for free-flowing collaboration.
   Agents can exit early via `[CONVERGED]`. Override with `--turns`.

6. **Full response routing.** Collab routes the complete agent response text,
   not a summary. Per user preference and because summarization would require
   an additional LLM call or lossy truncation.

7. **No auto-halt on pane activity.** User commits to not touching agent panes
   while the CLI is active. Manual `/halt` is the only stop mechanism.
   Simplicity over cleverness.

8. **CLI installs skills on startup.** The `claodex` command copies the skill
   directory to `~/.claude/skills/claodex/` and `~/.codex/skills/claodex/`
   before triggering. This ensures the skill is always in sync with the CLI
   version.

9. **Single tmux session.** Named `claodex`, one at a time. No concurrent
   multi-session support. Keeps state management trivial.

10. **Exchange logs in markdown.** Human-readable and agent-readable. Could be
    JSON but markdown is friendlier for review.

11. **Sidebar-local shell output.** Shell command output is not persisted to
    `events.jsonl` to maintain single-writer invariant for the event file.
    The input process is the sole writer; the sidebar reads and renders.

12. **Option C architecture (tmux split + curses sidebar).** The input editor
    stays as a raw-terminal process with minimal changes (colored prompt,
    no print statements). The sidebar is a self-contained curses app in its
    own tmux pane. IPC via filesystem. Chosen over full-curses (Option A,
    high regression risk from editor rewrite) and pure-tmux-split (Option B,
    coordination overhead for shared state in the right pane).

13. **Response latency is collab/watched-turn-only.** Normal fire-and-forget
    sends have no blocking wait, so latency cannot be reliably measured.
    Metrics display `null` for unavailable latency.

## Acceptance Criteria

1. Running `claodex` from a workspace directory creates a tmux session with
   4 panes: both agents running, input pane ready, sidebar displaying
   metrics — within 90 seconds.

2. Pressing Tab toggles the prompt between `claude ❯` (orange) and
   `codex ❯` (blue) immediately. Prompt color is visible.

3. A message sent to Claude includes the correct Codex delta (and vice versa).
   Delta contains exactly the undelivered peer events — verified by sending
   messages to one agent, switching, and confirming the delta content.

4. Scenario: send 3 messages to Claude, switch, send 1 to Codex. Codex's
   message includes all 3 Claude exchanges as delta. Switch back to Claude;
   Claude's next message includes the 1 Codex exchange as delta. No
   duplicates, no gaps.

5. `/collab "task" --turns 4` produces 4 turns of automatic routing with
   turn-by-turn progress visible in the sidebar log.

6. `/halt` during collab stops routing within one turn boundary.

7. Ctrl+C during collab halts the collaboration without exiting the CLI.

8. An exchange log is written to `.claodex/exchanges/` on collab termination,
   containing the full back-and-forth text.

9. After collab ends, normal mode resumes with correct delivery cursors — the
   next manual message to either agent includes exactly the undelivered events
   from the peer (if any), with no duplicates from the collab session.

10. If an agent pane dies, the CLI detects and reports it (via event bus) on
    the next interaction. Active collab mode halts immediately.

11. The input pane contains only the prompt and user text after the REPL
    starts. No status messages, routing events, or errors appear in the
    input pane.

12. All routing events (`[sent]`, `[recv]`, `[collab]`, `[watch]`, errors)
    appear as timestamped entries in the sidebar log.

13. The sidebar metrics strip shows current target, mode, agent status, and
    last response stats. Metrics update within ~1 second of state changes.

14. Shell commands typed in the sidebar produce output in the sidebar log
    area. Shell output is not written to `events.jsonl`.

15. `claodex attach` on a 3-pane session fails with a clear message directing
    the user to kill the session and restart.

16. `/status` produces a structured status entry in the sidebar log, not in
    the input pane.
