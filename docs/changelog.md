# Changelog

Changes to claodex with motivation and evidence. Each entry has: branch name,
date, problem, root cause, and changes. Be concise — lead with facts, skip
filler, use tables over prose where possible. Latest entries first.

---

## user-initiated-collab-marker — 2026-03-22

### Problem

Explicit `/collab` starts were visible only in the local exchange log header.
The agents themselves could not distinguish a user-started collab from an
agent-approved `[COLLAB]` request, because no runtime marker was injected into
the message stream.

### Root cause

`ClaodexApplication._run_collab()` tracked `initiated_by` only for exchange-log
metadata. The initial `/collab` seed message reused the normal `send_user_message`
path without any collab-origin annotation.

### Changes

**`claodex/cli.py`**: explicit `/collab` now prepends
`"(collab initiated by user)"` inside the first `user` block sent to the
starting agent. Agent-approved `[COLLAB]` flows are unchanged.

**`tests/test_cli.py`**: updated collab assertions to check the new marker on
user-started collab and added a focused regression test for the initial send.

**`claodex/skill/SKILL.md`**: documented the runtime marker so agents interpret
it as routing context rather than task content.

**`docs/spec.md` / `docs/codemap.md` / `README.md`**: documented that explicit
`/collab` injects the marker only into the first `user` block.

---

## enhancements — 2026-03-22

### Problem

Three recurring agent misbehaviors during collaborative sessions:

1. Agents auto-initiate collab (`[COLLAB]`) without user request, despite
   SKILL.md discouraging it.
2. Agents place `[COLLAB]`/`[CONVERGED]` at the beginning of messages where
   the router (which checks the last non-empty line) cannot detect them.
3. Agents verbally agree ("yep we're good") without emitting `[CONVERGED]`,
   causing infinite back-and-forth collab loops.

Separately, no keyboard shortcut existed to clear in-progress input — users
had to backspace through the entire buffer.

### Root cause

1. SKILL.md had a subjective escape clause ("unless the task genuinely requires
   peer input") and no user-approval gate in the code path.
2. SKILL.md did not explain that signals are detected on the last line only.
3. SKILL.md did not explicitly state that verbal agreement is insufficient —
   the literal flag is required.

### Changes

**`claodex/skill/SKILL.md`**: rewrote `## collab mode` section. `[COLLAB]`
now framed as a request requiring user approval. Added `### signals`
subsection explaining last-line-only detection. Added `### convergence`
subsection with explicit rules: verbal agreement does not count, literal
`[CONVERGED]` flag is required.

**`claodex/cli.py`**: added user confirmation gate in the `collab_initiated`
REPL handler. When an agent signals `[COLLAB]`, the CLI shows an inline
accept/deny selector (default: deny). On denial, a global rejection flag
is set and `"(collab rejected by user)"` is prepended to the next user
message delivered in normal mode, to either agent (mirrors `_post_halt`
pattern). The annotation is consumed on delivery and not retained per-agent.
On acceptance, collab starts as before. Added `InputEvent` to top-level
imports. Added `_post_reject` boolean state field.

**`claodex/input_editor.py`**: added `InputEditor.confirm(question)` method
for the inline accept/deny selector. Uses raw terminal mode, left/right
arrow toggle, Enter to confirm, Ctrl+C/Ctrl+D to deny. Does not record
history, does not emit the submit separator, fully clears itself from the
terminal after use. Added Ctrl+U (`\x15`) handler in `_read_loop` to clear
the input buffer, reset cursor and history navigation state, and re-render.
Suppressed during bracketed paste, consistent with Ctrl+C/Ctrl+D.

**`claodex/skill/SKILL.md`**: added Claude-only instruction to avoid plan
mode and present plans as normal conversation messages.

**`docs/spec.md`**: added Ctrl+U to keyboard shortcuts table. Updated Ctrl+C
description from "Clear input" to "Interrupt". Updated agent-initiated collab
section with confirmation step. Updated C5 matrix scenario with confirmation
gate and deny path.

**`docs/codemap.md`**: updated Input Editor interface description, added
confirm method, and noted transient confirmation UI invariant.

### Future enhancement

Auto-stall detection: after N consecutive turns where both agents respond with
short messages (<50 words) and neither signals `[CONVERGED]`, auto-warn or
auto-halt. This would catch the verbal-agreement death spiral at the code
level rather than relying solely on instructions.

---

## claude-jsonl-turn_duration-change — 2026-03-17

### Problem

After upgrading Claude Code to v2.1.77, `poll_for_response` never detected
Claude turn completion. `[COLLAB]` signals were never routed to Codex.

### Root cause

Claodex had two Claude turn-end detection paths:

1. **`system.turn_duration` JSONL entry** — intermittent in ALL versions
2. **`Stop` event in `~/.claude/debug/{session_id}.txt`** — the actual
   reliable mechanism, but debug log no longer created in v2.1.77

| Marker | Present on every turn? | Notes |
|--------|----------------------|-------|
| `assistant.stop_reason == "end_turn"` | Yes (all 21 sessions) | Always 1 line before `turn_duration` when both exist |
| `system.turn_duration` | No (33/89 in v2.1.59, 8/23 in v2.1.61) | Never reliable |
| Debug log Stop event | Yes for v2.1.59/v2.1.61, absent in v2.1.77 | Was compensating for missing `turn_duration` |

### Changes

**`claodex/router.py` — `_scan_claude_turn_end_marker`**: scan now checks for
`stop_reason == "end_turn"` on non-sidechain/non-meta assistant entries, in
addition to the legacy `turn_duration` check. First match wins. Safe because
`_latest_assistant_message_between` includes the marker line, and the assistant
entry is always at or before the `turn_duration` line. Debug log fallback
retained as tertiary path.

**`claodex/router.py` — `_turn_end_marker_label`**: updated error label.

**`tests/test_router.py`**: added `test_wait_for_response_claude_end_turn_stop_reason`
and `test_poll_for_response_claude_end_turn_stop_reason` covering the new marker
through both detection paths (`poll_for_response` was the user-visible failure).
Updated SMOKE SIGNAL match string.
