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

### Out of scope

- Agents calling each other directly (no agent-initiated dispatch)
- More than two agents per session
- Non-tmux environments
- Agent-side script execution per turn (the entire point is to avoid this)
- Real-time streaming of agent output to the CLI pane (CLI reads JSONL, does
  not intercept terminal output)
- Replacing or modifying the existing group-chat skill (claodex is independent)

## Architecture

Three processes run in a single tmux session:

1. **Claude** — interactive Claude Code session in a tmux pane
2. **Codex** — interactive Codex CLI session in a tmux pane
3. **claodex CLI** — long-running router and orchestrator in a tmux pane

### Communication channels

| Direction | Channel |
|-----------|---------|
| Agent → CLI | JSONL session files (agents write; CLI reads by tailing) |
| CLI → Agent | tmux buffer paste into agent's input pane |
| Agent → Agent | none; all cross-agent routing goes through the CLI |

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

1. CLI MUST create a tmux session named `claodex` with three panes in this
   layout:

   ```
   ┌──────────────────┬──────────────────┐
   │                  │                  │
   │     Codex        │     Claude       │
   │                  │                  │
   ├──────────────────┴──────────────────┤
   │                                     │
   │           claodex CLI               │
   │                                     │
   └─────────────────────────────────────┘
   ```

   Top row: agents side by side (equal width).
   Bottom row: CLI (full width). Height ratio: approximately 60/40.

2. CLI MUST start `codex` in the left pane and `claude` in the right pane,
   both with `directory` as the working directory.

3. CLI MUST wait for each agent's JSONL session file to appear on disk
   (indicating the agent process has initialized).

4. CLI MUST trigger the claodex skill in each agent pane by pasting the
   appropriate trigger command (`/claodex` for Claude, `$claodex` for Codex).

5. CLI MUST wait for a registration file to appear at
   `.claodex/participants/{agent}.json` for each agent.

6. After both registrations are confirmed, CLI MUST initialize all four
   cursors to the current line counts of the respective JSONL files. This
   excludes all pre-session history and skill-loading events from future
   deltas.

7. CLI MUST enter the interactive REPL and display readiness status.

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

1. CLI MUST verify a `claodex` tmux session exists and both agent panes are
   alive.
2. CLI MUST read existing registration files from `.claodex/participants/`.
3. CLI MUST read existing cursor files from `.claodex/cursors/` and
   `.claodex/delivery/`.
4. CLI MUST resume the interactive REPL with cursors intact.

If registration files are missing or panes are dead, CLI MUST report the
specific failure and exit. The user can then `tmux kill-session -t claodex`
and start fresh.

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

### Target selector

The CLI MUST maintain a current target: `claude` or `codex`. Default: `claude`.

The prompt MUST display the current target clearly:

```
claude ❯ _
```

The target MUST toggle with a single **Tab** press. After pressing Tab:

```
codex ❯ _
```

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
| `/collab [--turns N] <message>` | Start collaboration mode. Default turns: 10. |
| `/halt` | Stop collaboration mode after the current turn completes. |
| `/status` | Show agent status, cursor positions, collab state. |
| `/quit` | Kill agent sessions, tmux session, and exit. |

### Keyboard shortcuts

| Key | Normal mode | Collab mode |
|-----|-------------|-------------|
| Tab | Toggle target agent | No effect |
| Enter | Submit message | No effect (CLI is routing) |
| Ctrl+J | Insert newline | No effect |
| Ctrl+C | Clear current input | Halt collaboration |

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

The CLI MUST NOT block waiting for the agent's response in normal mode. The
user sends messages at their own pace and watches agent panes directly.

## Collaboration Mode

Collab mode automates message routing between agents for multi-turn
collaboration.

### Activation

```
/collab [--turns N] [--start <agent>] <message>
```

- `--turns N`: maximum turns (default: 10). One turn = one message sent to
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

The CLI MUST use source-native deterministic end markers only:

- **Claude:** `type == "system"` and `subtype == "turn_duration"`
- **Codex:** `type == "event_msg"` and `payload.type == "task_complete"`

When a marker is observed, the CLI MUST extract the latest assistant message
between the injected-message cursor and that marker line.

If a marker is missing at timeout, or a marker exists but no assistant message
is extractable in that marker window, the CLI MUST fail fast with an explicit
`SMOKE SIGNAL` error and halt routing. The CLI MUST NOT fall back to settle
timers or heuristic guessing.

### Termination

Collab mode ends when any of:

| Trigger | Behavior |
|---------|----------|
| Turn limit reached | CLI reports rounds completed |
| `/halt` or Ctrl+C | CLI stops after current turn completes |
| Per-turn timeout (default: 300s) | CLI reports which agent timed out |
| Agent pane exited | CLI reports which agent died |

Upon termination, the CLI MUST:

1. Update all delivery cursors to reflect what was actually delivered.
2. Save the exchange log to `.claodex/exchanges/`.
3. Report: stop reason, rounds completed, exchange file path.
4. Return to normal mode with the last active target selected.

### Status display

During collab, the CLI MUST display progress:

```
[collab] Round 1 → claude (waiting...)
[collab] Round 1 ← claude (312 words). → codex...
[collab] Round 2 ← codex (198 words). → claude...
[collab] Halted: 4 turns. Exchange: .claodex/exchanges/250222-1530.md
```

## tmux Integration

### Message injection

The CLI MUST use safe tmux buffer operations:

1. `tmux load-buffer -` (stdin) — load message into a tmux buffer.
2. `tmux paste-buffer -p -t <pane>` — paste into the target pane (`-p` skips bracketed-paste wrappers).
3. `tmux send-keys -t <pane> Enter` — submit the message.

The CLI MUST NOT use `tmux send-keys -l` for message content (special
character escaping is unreliable).

Multi-line messages rely on the target application supporting bracketed paste
mode (standard in modern terminal applications). tmux paste-buffer sends
bracketed paste sequences when the target pane has opted in.

### Bracketed paste fallback

If bracketed paste is determined to be unreliable for a target (embedded
newlines cause premature submission):

1. CLI MUST write the message to `.claodex/inbox/{uuid}.md`.
2. CLI MUST send a single-line instruction instead:
   `Read and respond to .claodex/inbox/{uuid}.md`
3. This fallback SHOULD be detectable and activatable per-agent.

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
Agents: <agent_a> ↔ <agent_b>
Rounds: <N>
Stop reason: <turns_reached | user_halt | timeout | agent_exited>

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
| JSONL parse error (malformed line) | Skip line; log warning; advance cursor past it after 3 consecutive failures or 10 seconds stuck on the same line |
| Pane died during normal mode | Report on next send attempt to that agent |
| Pane died during collab mode | Halt collab; report which agent died |
| Collab turn timeout exceeded | Halt collab; report last successful round |
| tmux paste fails | Report; halt any active collab |
| Workspace `.claodex/` not writable | Refuse to start |

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

## Behavioral Examples

### Example 1: Normal mode with delta

User sends 2 messages to Claude. Then switches to Codex.

```
claude ❯ Design an API schema for auth
  [delivered to claude, no delta]

claude ❯ Add rate limiting to the design
  [delivered to claude, no delta]

  [Tab pressed]

codex ❯ Review the API design Claude just created
  [delivered to codex with delta:]

  --- user ---
  Design an API schema for auth

  --- claude ---
  <Claude's first response>

  --- user ---
  Add rate limiting to the design

  --- claude ---
  <Claude's second response>

  --- user ---
  Review the API design Claude just created
```

Codex now has full context of the Claude exchanges. Claude has seen nothing
from Codex yet (no Codex events to deliver).

### Example 2: Collab mode

```
claude ❯ /collab --turns 6 Design and implement an auth API together

[collab] → claude (waiting...)
[collab] ← claude (312 words). → codex...
[collab] ← codex (198 words). → claude...
[collab] ← claude (145 words). → codex...
[collab] ← codex (87 words). → claude...
[collab] ← claude (203 words). → codex...
[collab] ← codex (156 words).
[collab] Done: 6 turns. Exchange: .claodex/exchanges/250222-1530.md

claude ❯ _
```

### Example 3: Halt during collab

```
claude ❯ /collab Refactor the database layer

[collab] → claude (waiting...)
[collab] ← claude (412 words). → codex...
[collab] ← codex (267 words). → claude...
  [Ctrl+C]
[collab] Halted: 3 turns. Exchange: .claodex/exchanges/250222-1545.md

claude ❯ Now summarize what you and Codex agreed on
  [delivered to claude, no delta — claude already saw codex's last message]
```

### Example 4: Post-collab delta correctness

After collab ends, Claude's last message was routed to Codex but Codex's
response to it was the final collab turn — so Claude has NOT seen Codex's
last response. Next manual message to Claude:

```
claude ❯ What did Codex think of your last proposal?
  [delivered to claude with delta:]

  --- codex ---
  <Codex's final collab response that Claude hasn't seen>

  --- user ---
  What did Codex think of your last proposal?
```

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

5. **10 default collab turns.** Enough for substantive collaboration. Low
   enough to prevent runaway loops. Override with `--turns`.

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

## Acceptance Criteria

1. Running `claodex` from a workspace directory creates a tmux session with
   both agents running and the CLI ready, within 90 seconds.

2. Pressing Tab toggles the prompt between `claude ❯` and `codex ❯`
   immediately.

3. A message sent to Claude includes the correct Codex delta (and vice versa).
   Delta contains exactly the undelivered peer events — verified by sending
   messages to one agent, switching, and confirming the delta content.

4. Scenario: send 3 messages to Claude, switch, send 1 to Codex. Codex's
   message includes all 3 Claude exchanges as delta. Switch back to Claude;
   Claude's next message includes the 1 Codex exchange as delta. No
   duplicates, no gaps.

5. `/collab "task" --turns 4` produces 4 turns of automatic routing with
   round-by-round status displayed in the CLI pane.

6. `/halt` during collab stops routing within one turn boundary.

7. Ctrl+C during collab halts the collaboration without exiting the CLI.

8. An exchange log is written to `.claodex/exchanges/` on collab termination,
   containing the full back-and-forth text.

9. After collab ends, normal mode resumes with correct delivery cursors — the
   next manual message to either agent includes exactly the undelivered events
   from the peer (if any), with no duplicates from the collab session.

10. If an agent pane dies, the CLI detects and reports it on the next
    interaction. Active collab mode halts immediately.
