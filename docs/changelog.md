# Changelog

Changes to claodex with motivation and evidence. Each entry has: branch name,
date, problem, root cause, and changes. Be concise — lead with facts, skip
filler, use tables over prose where possible. Latest entries first.

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
REPL handler. When an agent signals `[COLLAB]`, the CLI now prompts
`"allow? (y to accept)"` before starting the exchange. Decline restores any
in-progress draft. Added `InputEvent` to top-level imports.

**`claodex/input_editor.py`**: added Ctrl+U (`\x15`) handler in `_read_loop`
to clear the input buffer, reset cursor and history navigation state, and
re-render. Suppressed during bracketed paste, consistent with Ctrl+C/Ctrl+D.

**`docs/spec.md`**: added Ctrl+U to keyboard shortcuts table. Updated Ctrl+C
description from "Clear input" to "Interrupt". Updated agent-initiated collab
section with confirmation step.

**`docs/codemap.md`**: updated Input Editor interface description and
collab prevention invariant.

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
