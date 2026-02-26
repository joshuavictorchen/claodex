# claodex — Multi-Agent Collaboration CLI

## Motivation

AI coding agents persist their sessions as append-only JSONL logs. An
external process can tail those logs, compute the delta of what one agent has
said since the other last heard from it, and inject that delta alongside the
user's next message — giving both agents near-full context of the group
conversation at the theoretically cheapest possible token cost: exactly the
undelivered events, nothing more.

claodex is the CLI that implements this for Claude Code and Codex:

- **Zero agent overhead after registration.** Agents never run routing
  scripts. The CLI reads their logs and pastes composed messages into their
  tmux panes.
- **Exactly-once delta delivery.** Cursor-tracked delivery ensures each peer
  event reaches the other agent once — no duplicates, no gaps.
- **Automated collaboration.** The CLI routes responses back and forth between
  agents for multi-turn exchanges without user intervention.
- **Structured observability.** A sidebar process renders live metrics and a
  scrolling event log, powered by a shared event bus.

## Scope

### In scope

- CLI tool for multi-agent message routing and collaboration
- tmux session management (startup, 4-pane layout, pane lifecycle)
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
- Agent-side script execution per turn
- Real-time streaming of agent output to the input pane
- Replacing or modifying the existing group-chat skill

## Architecture

Four processes run in a single tmux session:

1. **Codex** — interactive Codex CLI in the top-left pane
2. **Claude** — interactive Claude Code in the top-right pane
3. **Input** — long-running claodex router and orchestrator in the bottom-left pane
4. **Sidebar** — curses-based log/metrics/shell display in the bottom-right pane

The input process owns all routing logic, event emission, and metrics
updates. The sidebar process is a read-only consumer of UI state files plus
a local shell runner. The two share no in-memory state; all communication
flows through the filesystem.

### Communication channels

| Direction | Channel |
|---|---|
| Agent → CLI | JSONL session files (agents write; CLI tails) |
| CLI → Agent | tmux buffer paste into agent's input pane |
| Agent → Agent | None; all cross-agent routing goes through the CLI |
| Input → Sidebar | `.claodex/ui/events.jsonl` (append-only) and `.claodex/ui/metrics.json` (atomic snapshot) |

### Cursor model

The CLI maintains four cursors:

| Cursor | Tracks |
|---|---|
| `read-claude` | CLI's read position in Claude's JSONL |
| `read-codex` | CLI's read position in Codex's JSONL |
| `delivery-to-claude` | Position in Codex's JSONL up to which events have been delivered to Claude |
| `delivery-to-codex` | Position in Claude's JSONL up to which events have been delivered to Codex |

Read cursors advance as the CLI tails JSONL files. Delivery cursors advance
only when a message containing a delta is successfully pasted into the target
pane.

## Startup

```
claodex [directory]
```

`directory` defaults to cwd. Workspace resolution MUST use git top-level when
`directory` is inside a git repository; otherwise it MUST use the resolved
directory path directly. Startup MUST NOT require a git repository or an
existing `.claodex/` state directory.

### Sequence

1. Derive the session name from workspace root and create a tmux session with
   four panes. Session name format MUST be `claodex-<dirname>-<hash>`, where:
   - `<dirname>` is workspace basename (or `root` for `/`) with `.` and `:`
     replaced by `-`
   - `<hash>` is the first 6 hex characters of SHA-1 of absolute workspace
     path

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

2. Start `codex` in the top-left pane and `claude` in the top-right pane,
   both with resolved workspace root as cwd.
3. Launch the sidebar process in the bottom-right pane.
4. Wait for each agent CLI to start (pane command transitions from shell).
5. Prefill the skill trigger command in each agent pane (`/claodex` for
   Claude, `$claodex` for Codex).
6. Paste the attach command into the input pane and attach to tmux — the
   user presses Enter in each agent pane to trigger registration.
7. The attach-mode REPL waits for registration files at
   `.claodex/participants/{agent}.json`.
8. After both registrations, initialize all four cursors to current JSONL
   line counts (excluding pre-session history).
9. Enter the interactive REPL.

### Constraints

- If the workspace-derived session already exists, MUST refuse and suggest
  `claodex attach` or `tmux kill-session -t <session_name>`.
- Agent startup timeout: 30 seconds.
- Registration timeout: 300 seconds (user must press Enter in each pane).
- On timeout, report which agent failed and exit cleanly.

### Attach (resume)

```
claodex attach [directory]
```

1. Verify the workspace-derived tmux session (`claodex-<dirname>-<hash>`)
   exists with exactly 4 panes.
2. Verify both agent panes are alive.
3. Verify the sidebar pane is alive; relaunch if dead.
4. Load existing registration and cursor files.
5. Resume the interactive REPL.

If the session does not have exactly 4 panes, MUST fail with a descriptive
error (e.g. `"expected 4 panes in session 'claodex-myproject-a1b2c3', found N"`).

## Agent Skill

Each agent loads a minimal skill providing collaboration framing and session
registration. The skill is installed at `~/.claude/skills/claodex/` and
`~/.codex/skills/claodex/`. The `claodex` command installs/updates the skill
automatically before triggering.

### Framing

The skill communicates:

- The agent is in a collaborative session with a named peer.
- Messages arrive with `--- {source} ---` headers (source is `claude`,
  `codex`, or `user`).
- The agent's primary role on peer messages is critical review.
- The agent SHOULD write plain text; claodex injects headers automatically.

### Registration

The skill runs a registration script that writes:

**File**: `.claodex/participants/{agent}.json`

| Field | Type | Description |
|---|---|---|
| `agent` | string | `"claude"` or `"codex"` |
| `session_file` | string | Absolute path to agent's JSONL |
| `session_id` | string | Session identifier from JSONL |
| `tmux_pane` | string | tmux pane ID (auto-detected) |
| `cwd` | string | Absolute workspace path |
| `registered_at` | string | ISO 8601 with timezone |

All paths MUST be absolute. Timestamp MUST include timezone.

## CLI Interface

### Pane layout

- **Input pane** (bottom-left, ~57%): pure text input. After the REPL
  starts, MUST contain only the prompt and user-typed text. No status, no
  logs, no routing feedback.
- **Sidebar pane** (bottom-right, ~43%): curses display. See **Sidebar**.

### Target selector

The CLI maintains a current target: `claude` or `codex`. Default: `claude`.

Prompt displays the target with agent-specific color:
- `claude ❯` in orange (`\033[38;5;216m`)
- `codex ❯` in blue-cyan (`\033[38;5;116m`)

The prompt renderer MUST account for ANSI escape length when computing cursor
positions via a `visible_len()` helper.

Target toggles with a single **Tab** press.

### Text input

The CLI MUST provide:
- Multi-line editing: **Ctrl+J** inserts a newline; **Enter** submits.
- Input history (up/down arrows in single-line mode)
- Standard line-editing keys (home, end, delete, backspace)
- Bracketed paste support (pasted `\r` becomes newline, not submit)

### Commands

| Command | Description |
|---|---|
| `/collab [--turns N] [--start <agent>] <message>` | Start collab mode |
| `/halt` | Stop collab after current turn |
| `/status` | Emit status event to sidebar |
| `/quit` | Kill agents, tmux session, exit |

### Keyboard shortcuts

| Key | Normal mode | Collab mode |
|---|---|---|
| Tab | Toggle target | No effect |
| Enter | Submit message | Submit interjection |
| Ctrl+J | Insert newline | Insert newline |
| Ctrl+C | Clear input | Halt collab |
| Ctrl+D | Quit | No effect |

## UI Event System

All runtime output from the input process routes through a `UIEventBus`.
`router.py` MUST NOT import or depend on `UIEventBus` — router warnings
and status are returned to the CLI layer via callbacks and exceptions.

### Event bus interface

- `log(kind, message, *, agent=None, target=None, meta=None)` — append
  one event to `events.jsonl`.
- `update_metrics(**fields)` — merge into canonical snapshot, validate,
  atomically write to `metrics.json`.
- `close()` — flush and close.

The bus holds a `threading.Lock` around all writes (main thread + halt
listener).

### Event JSONL schema

Each line in `.claodex/ui/events.jsonl`:

| Field | Type | Required | Description |
|---|---|---|---|
| `ts` | string | yes | ISO 8601 with timezone |
| `kind` | string | yes | Event kind |
| `agent` | string | no | Agent name |
| `target` | string | no | Target agent |
| `message` | string | yes | Human-readable text |
| `meta` | object | no | Structured metadata |

#### Persisted event kinds

| Kind | Description |
|---|---|
| `sent` | Message delivered to an agent |
| `recv` | Response received from an agent |
| `collab` | Collab lifecycle (start, routing, halt, converge) |
| `watch` | Pending watch (created, expired, error) |
| `error` | Error condition |
| `system` | System lifecycle (startup, shutdown, registration) |
| `status` | `/status` command output |

#### Sidebar-local pseudo-kinds

| Kind | Description |
|---|---|
| `shell` | Shell command execution (in-memory only, never in `events.jsonl`) |

### Metrics JSON schema

`.claodex/ui/metrics.json` is atomically overwritten (temp file + `os.replace`):

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
|---|---|---|
| `target` | string | Current target agent |
| `mode` | string | `"normal"` or `"collab"` |
| `collab_turn` | int/null | Current collab turn (null in normal mode) |
| `collab_max` | int/null | Max collab turns (null in normal mode) |
| `uptime_start` | string | ISO 8601 of REPL start |
| `agents.{name}.status` | string | `"idle"` or `"thinking"` |
| `agents.{name}.thinking_since` | string/null | ISO 8601 when thinking started |
| `agents.{name}.last_words` | int/null | Word count of last response |
| `agents.{name}.last_latency_s` | float/null | Seconds from send to response |

`last_latency_s` is authoritative only for collab and watched turns. Normal
fire-and-forget sends have no reliable latency. MUST be `null` when
unavailable.

### File lifecycle

Both files MUST be cleared on session start. The sidebar MUST tolerate
missing or empty files on startup.

## Sidebar

A separate Python process running curses in the bottom-right pane:

```
python3 -m claodex sidebar <workspace_root>
```

### Layout

```
╔═ metrics ═══════════════════════╗
║ ⣷ claude | collaborative | ... ║
╠═ log ═══════════════════════════╣
║ 01:23:45  [sent] -> claude      ║
║ 01:24:12  [recv] <- claude      ║
║ 01:25:01 [collab] start 100     ║
╠═════════════════════════════════╣
║ $ _                             ║
╚═════════════════════════════════╝
```

1. **Metrics strip** (top, 1 line): current status/thinking indicator,
   mode, thinking time, uptime, per-agent turn counts. Priority-based
   truncation when terminal is narrow.

2. **Scrolling log** (middle, fills remaining height): tails `events.jsonl`.
   Color-coded by agent (codex=blue-cyan, claude=orange, errors=red,
   system=dim, shell=gray). Scrollbar when content exceeds view.

3. **Shell input** (bottom, 1 line): non-interactive commands in workspace
   cwd. Output capped at 100 lines / 10KB. Timeout: 30 seconds.

### Color coding

| Source | Color |
|---|---|
| Codex events | Blue-cyan (256-color 116, or cyan fallback) |
| Claude events | Orange (256-color 216, or yellow fallback) |
| Error events | Red |
| System/status events | Dim |
| Shell output | Gray (256-color 250, or white fallback) |

### Lifecycle

- **Start**: launched by input process during session creation.
- **Attach**: relaunched if dead on reattach.
- **Shutdown**: killed with the tmux session.
- **Resize**: handles `KEY_RESIZE` and redraws.
- **Tolerance**: handles missing/empty/malformed UI files gracefully.

## Message Protocol

### Event extraction

The CLI extracts events from agent JSONL files:

- **User messages**: verbatim text, stripped of command wrappers and
  tool-result-only entries.
- **Agent responses**: final non-empty text per turn. No tool calls, no
  intermediate thinking, no streaming partials.

### Delta computation

When sending to a target agent:

1. Identify the peer agent.
2. Read events from the peer's JSONL between `delivery-to-{target}` cursor
   and current read position.
3. If undelivered events exist, format them as a delta block.
4. Prepend the delta to the user's message.
5. After successful paste, advance the delivery cursor.

**Invariant**: each peer event is delivered exactly once.

### Message format

All messages use `--- {source} ---` headers where source is `claude`,
`codex`, or `user`.

**Normal mode with delta**:
```
--- user ---
<user message to peer>

--- {peer} ---
<peer response>

--- user ---
<user's actual message>
```

**Normal mode without delta**:
```
--- user ---
<user's message>
```

**Collab routed message**:
```
--- {peer} ---
<peer's full response text>
```

**Collab with user interjections**:
```
--- {peer} ---
<peer's full response text>

--- user ---
<user interjection>
```

### Delta hygiene

Injected messages (with `--- agent ---` headers) appear in the target's JSONL
as user messages. When later extracted as delta for the other agent, the CLI
SHOULD strip previously-injected headers, retaining only the final
`--- user ---` block as the user event.

## Collaboration Mode

### Activation

```
/collab [--turns N] [--start <agent>] <message>
```

- `--turns N`: maximum turns (default: 100). One turn = one message sent
  and one response received.
- `--start <agent>`: which agent receives the initial message (default:
  current target).

### Flow

1. Compute delta for the starting agent; deliver delta + user message.
2. Wait for response (turn detection).
3. Route full response to the peer.
4. Wait for peer response.
5. Route back. Repeat until termination.

### Turn detection

**Codex**: `event_msg.payload.type == "task_complete"`. When `task_started`
appears in the scan window, require a subsequent `task_complete` (prevents
stale marker matching).

**Claude** (priority order):

1. **Fast path**: `system.subtype == "turn_duration"` in JSONL.
2. **Stop-event fallback**: `Getting matching hook commands for Stop` in
   Claude debug log (`~/.claude/debug/{session_id}.txt`) with timestamp
   after send time, plus extractable assistant text.
3. **Hard timeout**: fail fast with `SMOKE SIGNAL` error.

When a marker is found but no assistant message is extractable, MUST fail
with `SMOKE SIGNAL`. No heuristic fallback.

**Interference detection**: during Claude collab waits, non-meta user rows
after the anchor trigger an `interference detected` error and abort.

### Agent-initiated collab

An agent can end its response with `[COLLAB]` on its own line. The CLI:

1. Strips the signal line.
2. Routes the response to the peer as turn 1.
3. Continues the standard collab loop.

### User interjections

During collab, typed messages are queued and included in the next routed
turn as `--- user ---` blocks. `/halt` and `Ctrl+C` stop the exchange.

### Convergence

Both agents signal `[CONVERGED]` on their own line. When both signal in
consecutive turns, collab ends. A rejected convergence (one signals, peer
does not) voids the prior signal — both must re-signal.

### Termination

| Trigger | Behavior |
|---|---|
| Turn limit reached | Report rounds completed |
| `/halt` or Ctrl+C | Stop after current turn |
| Turn timeout (default: 18000s) | Report which agent timed out |
| Agent pane died | Report which agent died |
| Convergence | Report converged |

On termination:
1. Update delivery cursors.
2. Save exchange log to `.claodex/exchanges/`.
3. Emit collab termination events.
4. Return to normal mode.

## tmux Integration

### Layout creation

1. Create session (first pane).
2. Split vertically: top ~67% / bottom ~33%.
3. Split top row horizontally: 50/50.
4. Split bottom row horizontally: ~57% input / ~43% sidebar.

### Layout resolution

`resolve_layout` requires exactly 4 panes. Groups by `pane_top` into two
rows, sorts by `pane_left`: top-left=codex, top-right=claude,
bottom-left=input, bottom-right=sidebar.

### Message injection

```
tmux load-buffer -         # load from stdin (no size limit)
tmux paste-buffer -p -t X  # paste without bracketed-paste escapes
tmux send-keys -t X Enter  # submit
```

The `-p` flag is critical: without it, Codex's TUI intercepts
bracketed-paste sequences and mangles content. An adaptive delay between
paste and submit scales with payload size (base 0.3s, +0.1s per 1000 chars
over 2000, capped at 2s).

### Pane health

Validated via `tmux list-panes -F '#{pane_id} #{pane_dead}'` before every
injection.

## State Schema

All state lives under `.claodex/` (directory contains its own `.gitignore`
with `*`):

```
.claodex/
├── .gitignore
├── participants/
│   ├── claude.json
│   └── codex.json
├── cursors/
│   ├── read-claude.cursor
│   └── read-codex.cursor
├── delivery/
│   ├── to-claude.cursor
│   └── to-codex.cursor
├── ui/
│   ├── events.jsonl
│   └── metrics.json
├── inbox/
└── exchanges/
    └── 250222-1530.md
```

### Cursor files

Single non-negative integer (1-indexed JSONL line number) followed by
newline. `0` means start of file. Cursors MUST only advance, never retreat.

### Exchange log format

Group-chat transcript written incrementally during collab. Each message
appears exactly once in chronological order. Routing signals
(`[COLLAB]`, `[CONVERGED]`) are stripped. Messages are separated by
horizontal rules.

```markdown
# Collaboration: <initial message, first 80 chars>

Started: <ISO 8601>
Initiated by: <user | claude | codex>
Agents: claude ↔ codex

## user · 8:49 PM
<message text>

---

## claude · 8:50 PM
<response text>

---

## codex · 8:51 PM
<response text>

---

*Turns: <N> · Stop reason: <turns_reached | user_halt | converged | error text>*
```

## Error Semantics

| Condition | Behavior |
|---|---|
| tmux not installed | Refuse to start; report dependency |
| Workspace-derived session exists | Refuse to start; suggest attach or kill |
| `claude` or `codex` CLI missing | Refuse to start; report which |
| Agent fails to start | Report; kill session; exit |
| Agent fails to register | Report; exit |
| JSONL parse error | Skip after 3 failures or 10s stuck; log warning |
| Pane died in normal mode | Report on next send attempt |
| Pane died in collab | Halt collab; report |
| Collab turn timeout | Halt; report last successful round |
| tmux paste fails | Report; halt collab |
| Non-4-pane session on attach | Fail with descriptive error (expected vs actual pane count) |
| Sidebar pane dead on attach | Relaunch sidebar |

## Invariants

1. **Zero agent overhead.** After registration, agents execute no routing
   scripts. The CLI does all per-turn work.

2. **Exactly-once delivery.** Each peer event is delivered once. Delivery
   cursors enforce this.

3. **Cursor monotonicity.** All cursors only advance or stay unchanged.

4. **Agent independence.** Agents survive CLI detach/crash. The CLI observes
   agents; it does not own their lifecycle. `/quit` is explicit teardown.

5. **Session isolation.** At most one tmux session per workspace root; multiple
   workspaces MAY run concurrently.

6. **Input pane silence.** After the REPL starts, the input pane contains
   only the prompt and user text.

7. **Single writer for UI state.** The input process is the sole writer to
   `events.jsonl` and `metrics.json`. The sidebar is read-only. Shell output
   is sidebar-local.

8. **Router UI-agnosticism.** `router.py` MUST NOT import the UI event
   system. Warnings and errors return to the CLI as values or exceptions.

## Acceptance Criteria

1. `claodex` creates a 4-pane tmux session with agents running, input ready,
   sidebar displaying metrics — within 90 seconds.

2. Tab toggles prompt between `claude ❯` (orange) and `codex ❯` (blue-cyan).

3. Messages include correct peer deltas — exactly the undelivered events.

4. Scenario: 3 messages to Claude, switch, 1 to Codex. Codex receives all 3
   Claude exchanges as delta. Switch back; Claude receives the 1 Codex
   exchange. No duplicates, no gaps.

5. `/collab --turns 4 Design an auth API together` produces 4 turns with
   progress in sidebar.

6. `/halt` stops collab within one turn boundary.

7. Ctrl+C during collab halts without exiting the CLI.

8. Exchange log written to `.claodex/exchanges/` on collab termination.

9. Post-collab normal mode resumes with correct delivery cursors.

10. Dead agent pane detected and reported via event bus.

11. Input pane contains only prompt and user text after REPL starts.

12. All routing events appear as timestamped entries in sidebar log.

13. Sidebar metrics strip shows target, mode, status, response stats.

14. Shell commands in sidebar produce sidebar-local output.

15. `claodex attach` on a non-4-pane session fails with a descriptive error.

16. `/status` produces a status entry in sidebar, not in input pane.

## Message Routing Matrix

This section defines expected behavior for every feasible conversation logic
branch. Each scenario specifies preconditions, the triggering action, and the
exact payload and cursor effects. Agents are denoted **A** (first agent) and
**B** (peer agent). Concrete examples use `claude`/`codex`.

### Notation

- `D(X)` — delivery cursor for agent X (tracks position in the peer's JSONL up
  to which events have been delivered to X)
- `R(X)` — read cursor for agent X (CLI's parse position in X's JSONL)
- `delta(X)` — undelivered events from X's peer's JSONL between `D(X)` and
  `R(peer(X))`
- `→ X` — message pasted into agent X's pane
- `← X` — response received from agent X

### Normal mode

#### N1. User sends to A, no prior peer activity

**precondition**: `D(A) == R(B)` (no undelivered peer events)

**action**: user submits message targeting A

**payload → A**:
```
--- user ---
<user message>
```

**cursor effect**: `R(A)` advances as A processes; `D(A)` unchanged (no delta
to deliver)

#### N2. User sends to A, prior peer activity exists

**precondition**: `D(A) < R(B)` (B produced events not yet delivered to A)

**action**: user submits message targeting A

**payload → A**:
```
--- user ---
<prior user message to B>

--- codex ---
<B's response>

--- user ---
<user's actual message>
```

**cursor effect**: `D(A)` advances to `R(B)` after successful paste

**invariant**: each peer event appears in the delta exactly once. delta events
are ordered chronologically. `strip_injected_context()` extracts the user's
original message from previously-routed payloads so headers are not nested.

#### N3. User sends multiple messages to A, then sends to B

**precondition**: user sent N messages to A, A responded to each. user switches
target to B.

**action**: user submits message targeting B

**payload → B**: all N user+A exchanges as delta, then the user's message:
```
--- user ---
<1st message to A>

--- claude ---
<A's 1st response>

--- user ---
<2nd message to A>

--- claude ---
<A's 2nd response>

...

--- user ---
<user's message to B>
```

**cursor effect**: `D(B)` advances to `R(A)`

#### N4. User sends to A, switches to B, then back to A

**precondition**: user exchanged with A, switched to B and exchanged, now sends
to A again.

**payload → A**: B's exchange as delta, then user's message:
```
--- user ---
<user's message to B>

--- codex ---
<B's response>

--- user ---
<user's new message to A>
```

**cursor effect**: `D(A)` advances to `R(B)`

**invariant**: A sees only the events it missed; no duplication of A's own
prior messages.

#### N5. User sends to A while A is still thinking (watch replacement)

**precondition**: user sent message to A, A has not responded. user sends
another message to A.

**action**: second `send_user_message` to A

**payload → A**: second message with any new peer delta (typically none since B
was not addressed)

**effect**: the pending watch is replaced. if the first message had
`PendingSend.blocks`, they are preserved on the replacement watch so that
agent-initiated `[COLLAB]` exchange logs retain full history.

### Collab mode — user-initiated

#### C1. User starts collab targeting A

**action**: `/collab [--turns N] [--start A] <message>`

**turn 1 → A**:
```
--- user ---
<prior peer delta if any>

--- user ---
<collab message>
```

Delivered via `send_user_message()`. includes any undelivered B delta.

**turn 1 ← A**: CLI detects turn end (turn_duration / task_complete). response
extracted.

**turn 2 → B** (routed):
```
--- user ---
<delta user rows from A's JSONL, excluding A's assistant rows and echoed anchor>

--- claude ---
<A's full response>
```

Delivered via `send_routed_message()`. `D(B)` advances. echo of the original
user message is suppressed (at-most-one-match dedup via normalized anchor
comparison).

**turn 2 ← B**: response extracted.

**turn 3 → A** (routed):
```
--- user ---
<delta user rows from B's JSONL, excluding B's assistant rows and echoed anchor>

--- codex ---
<B's full response>
```

**echo dedup**: when a routed payload appears in the target's JSONL as a user
row, subsequent delta extraction for the other agent recognizes it as an echo.
the `echoed_user_anchor` parameter marks the first matching user row for
removal. only the first match is dropped; later identical rows are preserved.

**alternation continues** until termination.

#### C2. Collab with user interjections

**precondition**: collab is active. user types a message during agent A's turn.

**action**: message queued in `_collab_interjections`

**next routed payload → B**:
```
--- user ---
<delta rows predating the turn>

--- user ---
<interjection text>

--- claude ---
<A's response>
```

Interjections are placed chronologically: after delta rows (which predate the
turn) and before the peer response (which just completed). this reflects the
actual timeline.

**replay**: interjections from turn N are replayed in turn N+1 so the other
agent also receives them. fresh interjections from turn N+1 are appended after
replayed ones.

#### C3. Collab convergence

**precondition**: agent A ends response with `[CONVERGED]`

**next turn**: A's response (with signal stripped) is routed to B. B responds.

- If B also ends with `[CONVERGED]`: collab stops. `sync_delivery_cursors()`
  runs for both agents. stop reason = `converged`.
- If B does not signal: prior `[CONVERGED]` is void. both agents must
  re-signal in consecutive turns to converge.

#### C4. Collab turn limit reached

**precondition**: `turns_completed == request.turns`

**effect**: collab stops after the current response is received.
`sync_delivery_cursors()` runs for both agents. stop reason = `turns_reached`.

**cursor effect**: both `D(A)` and `D(B)` advance to current peer read
positions. the final unrouted response is not delivered as delta — it is
absorbed by the sync.

### Collab mode — agent-initiated

#### C5. Agent A ends response with `[COLLAB]`

**precondition**: user sent message to A in normal mode. A's response ends with
`[COLLAB]` on its own line.

**detection**: idle `poll_for_response` detects the response. `[COLLAB]` signal
is stripped from response text.

**turn 1 → B** (routed): A's response routed to B as a seed turn. the seed
turn's `PendingSend.blocks` (including the user's original messages) are
preserved from the watch.

```
--- user ---
<delta user rows from A's JSONL>

--- claude ---
<A's response (signal stripped)>
```

**collab continues** from turn 2 onward as in C1.

### Collab termination — halt

#### C6. `/halt` after response received, before routing

**precondition**: agent A responded. response captured but not yet routed to B.
user types `/halt`.

**effect**: collab stops. `stop_reason = "user_halt"`.

**cursor sync**: `last_unrouted_response_agent = A`. selective sync: only
`peer_agent(A)` is excluded from `sync_delivery_cursors()`. specifically:
- `D(A)` is synced (A already received its content)
- `D(B)` is NOT synced (A's response was never delivered to B)

**post-halt behavior**: `_post_halt = True`. next user message in normal mode
is prefixed with `(collab halted by user)\n\n`.

**next message → B**: A's unrouted response appears as delta because `D(B)` was
preserved:
```
--- user ---
<original collab message>

--- claude ---
<A's response>

--- user ---
(collab halted by user)

<user's new message>
```

#### C7. `/halt` before any response

**precondition**: collab started, message sent to A, A has not responded.
user types `/halt`.

**effect**: `halt_event` is set. `wait_for_response` continues polling until A
responds or times out. when it returns or raises:
- if response received: same as C6 (selective sync)
- if `KeyboardInterrupt`: `last_unrouted_response_agent` is None → full sync
  for both agents. no response exists to preserve.
- if `ClaodexError` (timeout): full sync. no response to preserve.

#### C8. `/halt` mid-collab (after multiple turns)

**precondition**: collab completed N turns. agent A responds on turn N+1.
`halt_event` was set during A's turn.

**effect**: A's response is the last captured turn. not routed to B. selective
sync: `D(B)` preserved, `D(A)` synced.

**invariant**: all routed turns were fully delivered to both agents via
`send_routed_message`. only the final unrouted response is affected.

### Collab termination — error

#### C9. Agent pane dies during collab

**effect**: `wait_for_response` or `send_routed_message` raises `ClaodexError`.
full `sync_delivery_cursors()` for both agents. error logged.

#### C10. Turn timeout

**effect**: `wait_for_response` exceeds `turn_timeout_seconds`. raises
`ClaodexError` with `SMOKE SIGNAL` message. full sync. collab stops.

#### C11. Interference detected

**precondition**: during a Claude collab wait, a non-meta user row appears in
Claude's JSONL that doesn't match the anchor (someone typed into Claude's pane
directly).

**effect**: `ClaodexError("interference detected")`. full sync. collab aborts.

### Edge cases

#### E1. Delta contains previously-injected routed payload

**scenario**: user sends to A. payload includes a `--- codex ---` block as
delta. A's JSONL records this as a user message. when later extracted as delta
for B, the nested headers would be confusing.

**handling**: `strip_injected_context()` detects the block structure and
extracts only the final `--- user ---` block as the user event. nested headers
are never forwarded.

#### E2. Collab with no peer delta to deliver

**scenario**: collab starts targeting A. B has no prior activity.

**payload → A**: just the user's message (via `send_user_message`). no delta
block.

#### E3. Empty or meta-only user rows in JSONL

**examples**: `<system-reminder>`, `<command-name>`, `<task-notification>`,
continuation boilerplate.

**handling**: meta user rows are skipped during turn anchoring and interference
detection (`_is_meta_user_text`). they are treated as staleness boundaries in
stop-event fallback (`_latest_claude_stop_fallback_message_between`). they are
NOT extracted as deliverable events.

#### E4. Duplicate user messages in delta

**scenario**: user sends the same message text twice to A. extracted as two
delta events for B.

**handling**: both are delivered. echo dedup only removes the first match
against the `echoed_user_anchor` from a routed send. identical user messages
that are not echoes are preserved.

#### E5. JSONL parse stall (malformed lines)

**handling**: `refresh_source` tracks stuck cursor state. after 3 attempts or
10 seconds, the malformed line is skipped (cursor advances by 1). a warning is
emitted. the skipped content is lost but does not block subsequent delivery.

#### E6. Stop event fires before JSONL flush completes

**scenario**: Claude debug log shows Stop event, but the final assistant text
isn't in the JSONL yet.

**handling**: `_latest_claude_stop_fallback_message_between` returns `None`
because no assistant text exists past the last user boundary. the stop-event
latch (`_poll_stop_seen`) persists. next poll cycle re-checks and finds the
flushed text.

#### E7. Post-collab normal-mode message to non-participating agent

**scenario**: user ran a collab, halted early, then sends a message to the
agent that didn't receive the final response.

**handling**: selective cursor sync (C6/C8) preserved that agent's delivery
cursor. the collab content appears as delta in the normal-mode payload. no
content is lost.
