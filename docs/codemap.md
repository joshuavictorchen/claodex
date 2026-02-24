Last updated: 2026-02-24 (rev2)

## Overview

claodex is a multi-agent tmux router that enables Claude Code and OpenAI Codex to collaborate from a single CLI. It manages a tmux session with four panes (Codex, Claude, input, sidebar), routes messages between agents via JSONL session log parsing, and supports automated multi-turn collaboration. Runtime REPL output is emitted as structured UI events/metrics under `.claodex/ui/`. Python 3.12+, no external dependencies beyond tmux and the agent CLIs.

## Directory Structure

```text
claodex/
├── claodex/               # core package
│   ├── skill/             # agent-side skill assets (deployed to ~/.claude/skills/ and ~/.codex/skills/)
│   │   └── scripts/       # registration script run inside agent sessions
├── docs/                  # spec and codemap
└── tests/                 # pytest suite
```

## Top-Level Files

- `pyproject.toml` — build config, entry point `claodex = claodex.cli:main`
- `.gitignore` — excludes `.claodex/` state directory

## Key Entry Points

- `claodex/cli.py:main` — CLI entry; dispatches to `ClaodexApplication.run()`
- `claodex/__main__.py` — `python -m claodex` support
- `claodex/skill/scripts/register.py:main` — agent-side registration (run inside agent tmux panes)

## Component Boundaries

#### CLI (`claodex/cli.py`)

- **Owns**: startup sequence, REPL loop, collab orchestration, idle watch polling for agent-initiated collab, exchange logging
- **Key files**: `cli.py` (all-in-one: startup, attach, REPL, collab, `/status`, `/quit`)
- **Interface**: `main()` entry point; `parse_collab_request()` also used by tests
- **Depends on**: router, state, extract, input_editor, tmux_ops, constants, errors
- **Depended on by**: `__main__.py`, `pyproject.toml` entry point

#### Router (`claodex/router.py`)

- **Owns**: event extraction, delta composition, message delivery, blocking + non-blocking response waiting, turn-end detection, interference detection
- **Key files**: `router.py` (Router class, render_block, strip_injected_context, _is_meta_user_text)
- **Interface**: `Router.send_user_message()`, `Router.send_routed_message()`, `Router.wait_for_response()`, `Router.poll_for_response()`, `Router.clear_poll_latch()`, `render_block()`, `strip_injected_context()`
- **Depends on**: extract, state, constants, errors; Claude debug log (`~/.claude/debug/{session_id}.txt`) as side-channel for Stop-event fallback
- **Depended on by**: cli
- **Invariants**: delivery cursor never exceeds peer read cursor; read cursor never moves backward; user messages are stripped of injected context before delta composition; Claude turn detection uses `turn_duration → Stop-event → timeout` priority chain; poll stop-event latches are keyed by `(agent, before_cursor)` and must be cleared when a watch is discarded; unexpected non-meta user input during collab wait triggers interference error

#### Extraction (`claodex/extract.py`)

- **Owns**: JSONL parsing, session discovery, room-event extraction for both agent formats
- **Key files**: `extract.py` (Claude and Codex JSONL parsers, session discovery)
- **Interface**: `extract_room_events_from_window()`, `discover_session()`, `resolve_workspace_root()`
- **Depends on**: (stdlib only)
- **Depended on by**: router, cli
- **Invariants**: per-turn extraction keeps only the last non-empty assistant message; malformed tail entries are deferred (not skipped) to handle partial writes

#### State (`claodex/state.py`)

- **Owns**: filesystem state (participants, cursors, delivery tracking)
- **Key files**: `state.py` (Participant/SessionParticipants dataclasses, cursor I/O, participant validation)
- **Interface**: `Participant`, `SessionParticipants`, cursor read/write helpers, `peer_agent()`
- **Depends on**: constants, errors
- **Depended on by**: router, cli

#### Input Editor (`claodex/input_editor.py`)

- **Owns**: raw-mode terminal line editor for REPL input, idle callback scheduling, input prefill restoration
- **Key files**: `input_editor.py` (InputEditor class, raw terminal mode context manager)
- **Interface**: `InputEditor.read(target, on_idle, idle_interval, prefill)` returns `InputEvent(kind, value)`
- **Depends on**: (stdlib only)
- **Depended on by**: cli
- **Invariants**: tracks visual line count (accounting for terminal wrapping) to correctly clear/redraw multi-line input; suppresses idle callback while bracketed paste is active; when idle callback interrupts with a non-empty draft, draft text is emitted in `InputEvent.value` for caller-side restore

#### UI Event Bus (`claodex/ui.py`)

- **Owns**: structured REPL runtime output persistence (event JSONL + metrics snapshot), schema validation, thread-safe writes, atomic metrics updates
- **Key files**: `ui.py` (`UIEventBus`, metrics schema validators)
- **Interface**: `UIEventBus.log()`, `UIEventBus.update_metrics()`, `UIEventBus.close()`
- **Depends on**: constants, errors
- **Depended on by**: cli (router warnings are bridged by callback through cli)
- **Invariants**: only persisted kinds (`sent`, `recv`, `collab`, `watch`, `error`, `system`, `status`) are accepted; every metrics write is a complete schema-valid document; metrics writes use temp file + `os.replace`; writes are protected by a lock for main-thread + halt-listener concurrency

#### Sidebar (`claodex/sidebar.py`)

- **Owns**: curses-based right-pane UI (metrics strip, event log tail, local shell runner)
- **Key files**: `sidebar.py` (`SidebarApplication`, JSONL tailing, shell command execution)
- **Interface**: `run_sidebar(workspace_root)` invoked via `claodex sidebar <workspace>`
- **Depends on**: state (UI file paths), stdlib curses/subprocess/json
- **Depended on by**: cli (`ClaodexApplication.run` sidebar mode dispatch)
- **Invariants**: tolerates missing/empty/malformed UI files; persists nothing to UI event files; shell output remains sidebar-local and capped; handles resize via `KEY_RESIZE`

#### tmux Ops (`claodex/tmux_ops.py`)

- **Owns**: all tmux subprocess commands (session/pane lifecycle, content injection)
- **Key files**: `tmux_ops.py` (session create/kill, layout resolution, sidebar launch, paste_content, _submit_delay)
- **Interface**: `create_session()`, `start_sidebar_process()`, `paste_content()`, `resolve_layout()`, `is_pane_alive()`, `PaneLayout`
- **Depends on**: constants, errors
- **Depended on by**: cli
- **Invariants**: paste uses `load-buffer -` (stdin) + `paste-buffer -p -t` (atomic, avoids tmux CLI-argument size limits, and `-p` skips bracketed-paste escapes that Codex's TUI mangles); submit delay scales with payload size (base 0.3s, +0.1s/1000 chars over 2000, capped at 2s)

#### Skill (`claodex/skill/`)

- **Owns**: agent-side prompt instructions and registration script
- **Key files**: `SKILL.md` (agent behavioral contract), `scripts/register.py` (session discovery + participant JSON write)
- **Interface**: agents trigger via `/claodex` (Claude) or `$claodex` (Codex); register.py writes `.claodex/participants/{agent}.json`
- **Depends on**: (standalone — no imports from core package)
- **Depended on by**: cli (installs skill assets on startup)

## Data Flow

```
User types in CLI REPL
  → cli.py composes message (with peer delta from other agent's JSONL)
  → router.py:render_block() wraps events with --- source --- headers
  → tmux_ops.py:paste_content() injects into target pane via load-buffer/paste-buffer
  → cli.py emits structured runtime output to ui.py:UIEventBus
  → CLI stores a per-target pending watch for idle `[COLLAB]` detection
  → Agent processes message, writes response to its JSONL
  → input_editor idle tick triggers router.py:poll_for_response()
  → if response ends with `[COLLAB]`, CLI seeds _run_collab() and routes to peer
  → collab loop uses router.py:wait_for_response() for blocking turn waits

Sidebar process loop
  → sidebar.py tails `.claodex/ui/events.jsonl` from tracked file offset
  → sidebar.py polls `.claodex/ui/metrics.json` every ~0.5s
  → curses render draws metrics strip + scrolling log + shell prompt
  → shell commands run in workspace cwd; output is appended only to sidebar-local log buffer
```

State on disk:
```
.claodex/participants/   ← agent registration JSON (session file, pane, cwd)
.claodex/cursors/        ← read position in each agent's JSONL (1-indexed line number)
.claodex/delivery/       ← what peer events have been delivered to each agent
.claodex/exchanges/      ← collab exchange logs (markdown)
.claodex/ui/             ← runtime UI event + metrics files (`events.jsonl`, `metrics.json`)
```

## Feature → Code Locations

| Feature | Primary Location | Notes |
| --- | --- | --- |
| Startup / session creation | `cli.py:_run_start` | Creates tmux, launches agents, installs skills |
| Reattach | `cli.py:_run_attach` | Resolves layout, validates agent panes, relaunches sidebar if not running, resumes REPL |
| Normal message sending | `cli.py:_run_repl`, `router.py:send_user_message` | Fire-and-forget send; registers/updates one pending watch per target (superseding prior watch) |
| Agent-initiated collab detection | `cli.py:_make_idle_callback`, `router.py:poll_for_response` | Idle poll checks pending watches for `[COLLAB]`, seeds `_run_collab` on trigger |
| Collab mode | `cli.py:_run_collab` | Automated multi-turn; uses `Router.send_routed_message` + `wait_for_response` |
| Response detection | `router.py:_scan_*_turn_end_marker`, `_scan_claude_debug_stop_event`, `_detect_interference` | Claude: `turn_duration` → debug-log Stop event → timeout; Codex: `task_complete` after `task_started`. Interference detection aborts on unexpected user input. |
| JSONL extraction | `extract.py:_extract_claude_room_events`, `_extract_codex_room_events` | Agent-specific parsers |
| Header stripping | `router.py:strip_injected_context` | Removes nested `--- source ---` blocks from forwarded user messages |
| Registration | `skill/scripts/register.py` | Discovers session file, writes participant JSON |
| Terminal input | `input_editor.py:InputEditor.read` | Raw-mode editor with history, Tab toggle, Ctrl+J newlines, idle callback, optional prefill |
| Runtime output routing | `cli.py:_run_repl`, `ui.py:UIEventBus` | REPL/collab/status output emits structured events; no runtime stdout prints after REPL starts |
| Sidebar runtime UI | `sidebar.py:SidebarApplication` | Renders metrics/log panes and executes non-interactive shell commands locally |
| Adaptive paste delay | `tmux_ops.py:_submit_delay` | Scales with payload size; env override `CLAODEX_PASTE_SUBMIT_DELAY_SECONDS` |

## Invariants

- **Two agents only**: `AGENTS = ("claude", "codex")` enforced throughout
- **Cursor monotonicity**: read cursors never move backward; delivery cursor ≤ peer read cursor
- **Turn boundary**: each user message flushes the pending assistant event, so only the last assistant frame per turn is extracted
- **Stuck cursor recovery**: after 3 failed parse attempts or 10s on the same line, the cursor skips forward 1 line
- **Pane liveness**: dead panes cause immediate `ClaodexError` on send or wait
- **Pending watch model**: one watch per target agent; newer sends supersede older watches and clear their poll latches
- **Output routing model**: after REPL starts, runtime status/errors/progress are emitted to `UIEventBus` instead of stdout
- **Skill asset deployment**: `_install_skill_assets()` copies `claodex/skill/` to `~/.claude/skills/claodex/` and `~/.codex/skills/claodex/` on every startup

## Cross-Cutting Concerns

- **Error handling**: `ClaodexError` for all runtime/validation failures; caught at REPL loop level in `cli.py:_run_repl`
- **Constants**: `claodex/constants.py` — agent names, directory paths, default turns/timeouts, collab/converge signals
- **UI state**: `.claodex/ui/events.jsonl` (append-only structured events) and `.claodex/ui/metrics.json` (atomic snapshot)
- **Configuration**: env vars `CLAODEX_POLL_SECONDS`, `CLAODEX_TURN_TIMEOUT_SECONDS`, `CLAODEX_PASTE_SUBMIT_DELAY_SECONDS`, `CLAODEX_CLAUDE_SKILLS_DIR`, `CLAODEX_CODEX_SKILLS_DIR`

## Conventions and Patterns

- Google-style docstrings on all public functions
- `dataclass(frozen=True)` for value objects
- Tests in `tests/test_*.py` (164 tests across `test_cli.py`, `test_input_editor.py`, `test_router.py`, `test_sidebar.py`, `test_tmux_ops.py`, `test_ui.py`; no `test_extract.py` or `test_state.py`)
- Router accepts `paste_content` and `pane_alive` as constructor callbacks (testable without tmux)
- Registration script is standalone (no imports from core `claodex` package) so it can run inside agent skill directories

## Search Anchors

| Symbol | Location |
| --- | --- |
| `ClaodexApplication` | `claodex/cli.py:103` |
| `Router` | `claodex/router.py:93` |
| `extract_room_events_from_window` | `claodex/extract.py:193` |
| `paste_content` | `claodex/tmux_ops.py:348` |
| `_submit_delay` | `claodex/tmux_ops.py:312` |
| `render_block` | `claodex/router.py:919` |
| `strip_injected_context` | `claodex/router.py:935` |
| `InputEditor` | `claodex/input_editor.py:71` |
| `Participant` | `claodex/state.py:28` |
| `register.py:main` | `claodex/skill/scripts/register.py:350` |
| `HEADER_LINE_PATTERN` | `claodex/router.py:46` |

## Known Gotchas

- **Skill duplication**: `claodex/skill/scripts/register.py` duplicates session discovery logic from `claodex/extract.py` because the skill must be standalone (no package imports)
- **Agent self-headers**: agents may format responses with `--- agent ---` headers despite SKILL.md instructions, causing double headers on delivery. The current mitigation is prompt-level only (SKILL.md line 25); no code-level stripping exists
- **Codex turn detection**: requires `task_started` → `task_complete` sequence; a `task_complete` without prior `task_started` in the scan window is accepted but could match a stale marker from a previous turn
- **Claude debug log dependency**: the Stop-event fallback reads `~/.claude/debug/{session_id}.txt`, which is undocumented and may change between Claude Code versions. Falls through to timeout if the file is missing or the format changes.
- **Auto-collab interruption behavior**: idle-triggered `[COLLAB]` can interrupt typed drafts; drafts are restored via `InputEvent.value` + CLI prefill on next prompt.
- **Bracketed paste interaction**: idle callback is intentionally suppressed while bracketed paste is active to avoid splitting the paste stream.
- **Exact-width wrap boundary**: `input_editor.py` handles the terminal phantom-row case at exact column multiples by clamping cursor to the last content row; this may place the cursor one column early on some terminals
