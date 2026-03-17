# Changelog

Changes to claodex with motivation and evidence. Each entry has: branch name,
date, problem, root cause, and changes. Be concise — lead with facts, skip
filler, use tables over prose where possible. Latest entries first.

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
