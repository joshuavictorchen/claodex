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

1. Preserves the signal line in the routed message.
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
1. Update delivery state according to the Message Routing Matrix contracts.
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

Behavior contracts for every conversation routing branch. Each scenario shows
what the user does in the input pane and the exact payload each agent receives.
Agents are **A** and **B**. All payloads use `--- {source} ---` header blocks.
Unless stated otherwise, examples map **A = claude** and **B = codex**.
If any statement outside this matrix conflicts with a matrix scenario, the
matrix scenario is authoritative.

### Normal mode

#### N1. First message, no history

> User sends a message to A at the start of a session.

**input pane**:
1. `A ❯ hello`

**A sees**:
```
--- user ---
hello
```

**B sees**: nothing (not addressed)

---

#### N2. Basic round-trip, then switch

> User sends to A, A responds, user switches and sends to B.

**input pane**:
1. `A ❯ hello`
2. A responds
3. `B ❯ your turn`

**A sees**:
```
--- user ---
hello
```

**B sees**:
```
--- user ---
hello

--- claude ---
<A's response>

--- user ---
your turn
```

---

#### N3. Multiple exchanges with A, then switch to B

> User sends N messages to A (A responds to each), then sends to B. B receives
> the full A conversation as context.

**input pane**:
1. `A ❯ msg1`
2. A responds
3. `A ❯ msg2`
4. A responds
5. `B ❯ catch up`

**B sees**:
```
--- user ---
msg1

--- claude ---
<A's 1st response>

--- user ---
msg2

--- claude ---
<A's 2nd response>

--- user ---
catch up
```

---

#### N4. Round-trip: A → B → A

> User exchanges with A, switches to B, then back to A. Each agent sees what
> it missed.

**input pane**:
1. `A ❯ msg`
2. A responds
3. `B ❯ msg`
4. B responds
5. `A ❯ update`

**A sees** (on step 5):
```
--- user ---
<user's message to B>

--- codex ---
<B's response>

--- user ---
update
```

A does NOT see its own prior exchange echoed back.

---

#### N5. Stacked sends to same agent

> User sends to A, then sends again before A responds.

**input pane**:
1. `A ❯ first`
2. (A still thinking)
3. `A ❯ second`

**A sees**: Each message delivered separately as it is submitted. Both appear
as individual `--- user ---` blocks in A's context. No delta (B was not
addressed).

```
--- user ---
first
```
then:
```
--- user ---
second
```

---

**note (rapid switch timing)**: If a user switches targets before the prior
send is persisted to the source JSONL, the newest source-side rows MAY appear
on the next send instead of immediately. Delivery remains exactly-once and in
source-log order.

---

#### N6. Send to A, switch to B before A responds

> User sends to A, then immediately switches and sends to B. A has not
> responded yet.

**input pane**:
1. `A ❯ task for you`
2. (A still thinking)
3. `B ❯ different task`

**A sees**:
```
--- user ---
task for you
```

**B sees**: the user's message to A appears as context, but A's response does
not (A hasn't responded yet):
```
--- user ---
task for you

--- user ---
different task
```

---

#### N7. Stacked sends to A, then switch to B

> User sends to A twice (before A responds), then switches and sends to B.

**input pane**:
1. `A ❯ first`
2. `A ❯ second`
3. (A still thinking)
4. `B ❯ your turn`

**B sees**: both user messages to A as delta, but no A response (A hasn't
responded):
```
--- user ---
first

--- user ---
second

--- user ---
your turn
```

---

#### N8. Stacked sends to A, A responds, then switch to B

> User sends to A twice before A responds. A eventually responds (to both).
> User then switches and sends to B.

**input pane**:
1. `A ❯ first`
2. `A ❯ second`
3. A responds
4. `B ❯ your turn`

**B sees**: both user messages and A's response:
```
--- user ---
first

--- user ---
second

--- claude ---
<A's response>

--- user ---
your turn
```

---

#### N9. Send to A, switch to B, B responds, A responds, send to A

> User sends to A, then to B before A responds. B responds first, then A
> responds. User sends to A again.

**input pane**:
1. `A ❯ task`
2. `B ❯ other task`
3. B responds
4. A responds
5. `A ❯ follow-up`

**A sees** (on step 5): B's exchange as delta:
```
--- user ---
other task

--- codex ---
<B's response>

--- user ---
follow-up
```

---

#### N10. Send to A, switch to B before A responds, B responds, send to A

> Same as N9 but user sends to A before A has responded. A eventually sees
> B's exchange as delta on the user's follow-up.

**input pane**:
1. `A ❯ task`
2. `B ❯ other task`
3. B responds
4. (A still thinking)
5. `A ❯ follow-up`

**A sees** (on step 5):
```
--- user ---
other task

--- codex ---
<B's response>

--- user ---
follow-up
```

A receives the follow-up immediately.
when A eventually responds, that response may address both `task` and
`follow-up`.

---

#### N11. Rapid alternation: A, B, A, B

> User alternates between agents. Each receives the other's exchange as delta.

**input pane**:
1. `A ❯ m1`
2. A responds
3. `B ❯ m2`
4. B responds
5. `A ❯ m3`
6. A responds
7. `B ❯ m4`

**B sees** (on step 7): only the exchanges since B's last delivery — m3 + A's
response:
```
--- user ---
m3

--- claude ---
<A's response to m3>

--- user ---
m4
```

B does NOT see m1/A's-first-response again — those were already delivered with
m2.

---

#### N12. Stacked sends to A, then B; B responds before A

> User sends two messages to A before A responds, then sends to B. B responds
> first, A responds later, then user sends to A.

**input pane**:
1. `A ❯ first`
2. `A ❯ second`
3. `B ❯ handoff`
4. B responds
5. A responds
6. `A ❯ follow-up`

**B sees** (on step 3):
```
--- user ---
first

--- user ---
second

--- user ---
handoff
```

**A sees** (on step 6):
```
--- user ---
handoff

--- codex ---
<B's response>

--- user ---
follow-up
```

A does NOT re-receive `first`/`second` as delta (A originated them).

---

#### N13. Stacked sends to A, then B; A responds before B

> User sends two messages to A before A responds, then sends to B. A responds
> first, B responds later, then user sends to B.

**input pane**:
1. `A ❯ first`
2. `A ❯ second`
3. `B ❯ handoff`
4. A responds
5. B responds
6. `B ❯ follow-up`

**B sees** (on step 6):
```
--- claude ---
<A's response>

--- user ---
follow-up
```

B does NOT re-receive `first`/`second` on step 6 (those were already delivered
on step 3).

---

### Collab mode — user-initiated

#### C1. Basic collab exchange

> User starts a collab targeting A. Agents alternate turns.

**input pane**:
1. `A ❯ /collab discuss the API`

**turn 1 — A sees**:
```
--- user ---
discuss the API
```

(plus any prior B delta, if B had undelivered events)

**turn 2 — B sees**:
```
--- user ---
discuss the API

--- claude ---
<A's response>
```

B receives the user's original message alongside A's response.
On subsequent routed turns, each agent sees only content it has not already
received.

**turn 3 — A sees**:
```
--- codex ---
<B's response>
```

Alternation continues until termination.

---

#### C2. Collab with single user interjection

> User types a message while an agent is thinking during collab.

**input pane**:
1. (collab active, A is thinking)
2. `important note`

**next routed turn — B sees**:
```
--- user ---
important note

--- claude ---
<A's response>
```

Interjections appear before the peer response (chronological order: the user
typed during the turn, the response completed after). Interjections are
replayed to the other agent on the following turn so both agents see them.

**following routed turn — A sees**:
```
--- user ---
important note

--- codex ---
<B's response>
```

---

#### C2b. Collab with multiple user interjections

> User types two messages while an agent is thinking during collab.

**input pane**:
1. (collab active, A is thinking)
2. `first note`
3. `second note`

**next routed turn — B sees**:
```
--- user ---
first note

--- user ---
second note

--- claude ---
<A's response>
```

Multiple interjections each get their own `--- user ---` block, all placed
before the peer response. Ordering is chronological: all interjections first,
then the response.

**following routed turn — A sees**:
```
--- user ---
first note

--- user ---
second note

--- codex ---
<B's response>
```

---

#### C3. Collab convergence

> Both agents signal `[CONVERGED]` on consecutive turns.

**turn N — A responds with `[CONVERGED]`**. A's full response (including
`[CONVERGED]`) is routed to B.

**B sees**:
```
--- claude ---
<A's response>

[CONVERGED]
```

**turn N+1 — B responds with `[CONVERGED]`**. Collab stops. B's response
(including `[CONVERGED]`) is NOT routed to A during the collab — it appears as
delta in A's next normal-mode message (same preservation as C4/C6).

If only one agent signals `[CONVERGED]` and the peer does not, the signal is
void. Both MUST re-signal in consecutive turns.

---

#### C4. Collab turn limit

> Collab reaches the configured maximum turns.

**Behavior**: Collab stops after the final response is received. The final
response is NOT routed to the peer during the collab — it is preserved as
delta for the peer on the next normal-mode message (same preservation as
C6/C8).

---

### Collab mode — agent-initiated

#### C5. Agent signals `[COLLAB]`

> User sends to A in normal mode. A's response ends with `[COLLAB]`.

**input pane**:
1. `A ❯ design the auth flow`
2. A responds with `[COLLAB]` on the last line

**A sees**: the user's message (normal delivery). A responds with `[COLLAB]`.

**B sees** (turn 1, automatic): A's full response including the `[COLLAB]`
signal, plus any earlier user context B has not yet received:
```
--- user ---
design the auth flow

--- claude ---
<A's response>

[COLLAB]
```

Collab continues from turn 2 as in C1.

---

### Collab termination — halt

#### C6. `/halt` after one agent responds

> A responds during collab. User types `/halt` before the response is routed
> to B.

**input pane**:
1. (collab active, A just responded)
2. `/halt`

**Behavior**: Collab stops. A's response was received but never delivered to B.

**Next normal-mode message → B**: A's unrouted response appears as delta so B
does not miss it. If this is the first turn (nothing was ever routed to B),
the original user message also appears as delta since B has never seen it:
```
--- user ---
<original collab message>

--- claude ---
<A's response>

--- user ---
(collab halted by user)

<user's new message>
```

If the halt occurs after multiple routed turns, B already received earlier
content. Only A's final unrouted response appears as delta — not the full
history.

**Next normal-mode message → A**: No stale delta — A already received its own
content.

---

#### C7. `/halt` before any response

> User starts collab, then halts before the target agent responds.

**input pane**:
1. `A ❯ /collab do the thing`
2. (A still thinking)
3. `/halt`

**Behavior**: The CLI waits for the current turn to complete or time out.

- If the agent responds before the halt is processed: same as C6 — the
  response is preserved for the peer.
- If the wait is interrupted or times out: no response exists. Both agents'
  delivery state is synchronized. No content is lost because none was produced.

---

#### C8. `/halt` mid-collab after multiple turns

> Collab ran N turns successfully. Agent A responds on turn N+1. User halts.

**Behavior**: All N routed turns were already delivered to both agents. Only
A's final (unrouted) response on turn N+1 is affected — it is preserved as
delta for B on the next normal-mode message (same as C6).

---

### Collab termination — error

#### C9. Agent pane dies

> One agent's pane is killed during collab.

**Behavior**: Collab aborts with an error and reports the dead agent.
If a completed response was received but not routed when the abort occurred,
that response is preserved for the peer's next normal-mode message.
Otherwise, no stale collab delta is injected.

---

#### C10. Turn timeout

> An agent fails to respond within the timeout (default: 18000s).

**Behavior**: Collab aborts with an error identifying which agent timed out.
If a completed response was received but not routed when the abort occurred,
that response is preserved for the peer's next normal-mode message.
Otherwise, no stale collab delta is injected.

---

### Post-collab normal mode

#### PC1. Normal send to the peer after clean collab exit

> Collab ended normally (convergence, turn limit). User sends to the agent
> that did NOT produce the final response.

**Behavior**: The final collab response was not routed during the collab. It
appears as delta alongside the user's new message, so the peer sees the last
thing the other agent said.

---

#### PC1b. Normal send to the responding agent after clean collab exit

> Collab ended normally. User sends to the agent that DID produce the final
> response.

**Behavior**: No stale delta — that agent already has its own content. The
user's message is delivered normally with any new peer events.

---

#### PC2. Normal send to the agent that missed the halt response

> Collab was halted (C6/C8). User sends to the agent that didn't receive the
> final response.

**Behavior**: The final collab response appears as delta alongside the user's
new message. No content is lost. The `(collab halted by user)` prefix provides
context.

---

#### PC3. Normal send to the agent that DID respond before halt

> Collab was halted. User sends to the agent that produced the final response.

**Behavior**: No stale delta — that agent already has its own content. The
user's message is delivered normally with any new peer events.

---

#### PC4. Halted collab: send to responder first, then to peer

> Collab was halted after A produced an unrouted final response. User first
> sends to A, A replies, then user sends to B.

**input pane**:
1. `A ❯ first post-halt message`
2. A responds
3. `B ❯ direct to peer`

**B sees** (on step 3):
```
--- claude ---
<A's final collab response>

--- user ---
(collab halted by user)

first post-halt message

--- claude ---
<A's reply to first post-halt message>

--- user ---
direct to peer
```

This ensures no content is dropped when the first post-halt send goes to the
responding agent before the peer.

---

### Edge cases

#### E1. Duplicate user messages

> User sends the exact same message text twice to A. Both are extracted as
> delta for B.

**Behavior**: Both messages are delivered as separate user blocks in order.

---

#### E2. Meta user rows

> Agent runtime injects meta content (system reminders, command wrappers,
> task notifications) as user rows.

**Behavior**: Meta rows are not extracted as deliverable events. They are
invisible to the peer agent.

---

#### E3. Malformed log lines

> An agent's log contains unparseable lines.

**Behavior**: After repeated failures (3 attempts or 10 seconds), the
malformed line is skipped with a warning. Subsequent lines are processed
normally.

---

#### E4. Post-halt send to both agents

> User halts collab, then sends to the agent that missed content (B), then
> sends to the other (A).

**B sees**: Collab content as delta (preserved from halt), plus user's message.

**A sees** (after B responds): B's new exchange as delta, plus user's message.
No collab content is re-delivered to A.

---

#### E5. Collab started with prior peer delta

> User exchanged with B, then starts a collab targeting A. A has undelivered
> B events.

**A sees** (turn 1): B's delta prepended before the collab message:
```
--- user ---
<prior message to B>

--- codex ---
<B's response>

--- user ---
<collab message>
```
