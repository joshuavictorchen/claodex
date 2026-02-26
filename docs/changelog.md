# changelog

## 2026-02-26 — collab routing reliability fixes

this batch fixes ten bugs in the message routing and collaboration subsystems.
all were discovered through live two-agent sessions. they fall into four
categories: **delivery correctness**, **response detection**, **cursor
lifecycle**, and **UI fidelity**.

### delivery correctness

**1. missing user context in routed collab messages**

- **symptom**: peer agent received only the response text during `[COLLAB]`
  handoff — not the user messages that prompted it.
- **root cause**: `send_routed_message()` built payloads from `response_text`
  alone, never calling `build_delta_for_target()`. undelivered user events in
  the source JSONL were skipped and their delivery cursors advanced anyway.
  secondarily, the REPL watch-replacement path discarded earlier
  `PendingSend.blocks` when a second message was sent to an already-thinking
  agent, losing exchange log history.
- **fix**: `send_routed_message()` now fetches the undelivered delta, filters
  out source-agent assistant rows (already forwarded via `response_text`),
  and prepends remaining user events. REPL watch replacement preserves prior
  blocks and the earliest `sent_at`.
- **files**: `router.py`, `cli.py`, `test_router.py`, `test_cli.py`

**2. initial user message repeated within collab**

- **symptom**: the user's initial `/collab` message appeared twice in the
  routed payload — once as delta and once as the explicit message.
- **fix**: delta extraction now skips the echo. `send_routed_message` accepts
  an `echoed_user_anchor` parameter; the first matching user row in the delta
  is dropped as a routed echo. comparison is normalized (collapsed whitespace)
  with at-most-one-match semantics.
- **files**: `router.py`, `test_router.py`

**3. echo dedup dropping all matching user rows in routed delta**

- **symptom**: when a legitimate user message happened to match a previously
  routed echo, all instances were stripped — not just the first echo.
- **root cause**: echo dedup iterated all delta rows and dropped every match
  rather than stopping after the first.
- **fix**: `echo_dropped` flag ensures only the first matching row is removed;
  subsequent identical user rows pass through as legitimate messages.
- **files**: `router.py`, `test_router.py`

**4. interjection routing and ordering**

- **symptom**: user interjections typed during collab were delivered only to
  the next target, not replayed to the other agent. when delivered, they were
  placed before delta rows instead of in chronological order.
- **fix**: interjections are now replayed to both agents across consecutive
  turns. ordering within the payload reflects the actual timeline: delta rows
  first (events predating the turn), then interjections (typed during the
  turn), then the peer response.
- **files**: `cli.py`, `router.py`, `test_cli.py`, `test_router.py`

### response detection

**5. stop-event fallback returning stale assistant text**

- **symptom**: agent-initiated `[COLLAB]` detection silently failed. the idle
  poll detected a response but returned a 90-word intermediate frame instead
  of the 227-word final response. the `[COLLAB]` signal on the last line was
  permanently lost.
- **root cause**: the stop-event fallback in `poll_for_response` /
  `wait_for_response` races against JSONL flushes. claude code writes the
  debug-log Stop event before the final assistant text is flushed to the
  session JSONL. `_latest_assistant_message_between` returned whatever was on
  disk — often an intermediate frame from before a tool call.
- **fix**: new boundary-aware extractor
  `_latest_claude_stop_fallback_message_between` treats every `user/user` row
  (including `tool_result` rows) as a staleness boundary. if the final
  assistant text hasn't appeared past the last boundary, returns `None` and
  the stop-event latch survives to the next poll cycle.
- **residual risk**: consecutive text-only assistant frames with no intervening
  user boundary — not observed in practice.
- **files**: `router.py`, `extract.py`, `test_router.py`

**6. stop-event fallback boundaries for empty/meta-only user rows**

- **symptom**: stale assistant text still returned when the intervening user
  boundary was an empty string or meta-only content (e.g.,
  `<system-reminder>` injections).
- **root cause**: boundary logic had three branches from turn-anchoring:
  `tool_result` rows and non-empty non-meta text rows reset the boundary, but
  empty and meta-only text rows were silently skipped. correct for turn
  anchoring, incorrect for staleness detection.
- **fix**: collapsed to one unconditional reset: every `entry_type == "user"
  and role == "user"` row resets the boundary. removed now-unused helpers
  (`_extract_claude_user_text`, `_is_tool_result_only_claude_user_entry`).
- **files**: `router.py`, `test_router.py`

### cursor lifecycle

**7. stale delivery cursors leaking extra user headers after collab**

- **symptom**: first normal-mode message after collab included an extra
  `--- user ---` block — an echo of a previously routed message leaking as
  undelivered delta.
- **root cause**: on collab termination, the last received response is never
  routed onward. the delivery cursor for the non-final agent was stale by one
  turn, causing `build_delta_for_target()` to pick up echo artifacts.
- **fix**: `Router.sync_delivery_cursors()` aligns both delivery cursors to
  current peer read positions. called in `_run_collab()`'s `finally` block so
  all exit paths are covered (converged, halted, error, turn limit).
- **files**: `router.py`, `cli.py`, `test_router.py`, `test_cli.py`

**8. `/halt` dropping unrouted responses from peer context**

- **symptom**: when a collab was halted immediately after one agent replied
  (before routing to the peer), the response disappeared from subsequent peer
  context entirely.
- **root cause**: `sync_delivery_cursors()` (fix #7) unconditionally advanced
  both cursors. on early `/halt`, this marked the responding agent's output as
  "delivered" to a peer that never received it.
- **fix**: `_run_collab()` tracks `last_unrouted_response_agent`. on
  `user_halt`, the peer target that didn't receive the response is excluded
  from cursor sync. `sync_delivery_cursors()` now accepts an optional target
  subset with validation. other exit paths (converged, turns_reached, error)
  still sync all cursors.
- **files**: `cli.py`, `router.py`, `test_cli.py`, `test_router.py`,
  `codemap.md`

### UI fidelity

**9. sidebar think counter underreporting during collab**

- **symptom**: sidebar `think` counter showed only in-flight time for the
  current agent instead of accumulating across completed collab turns.
- **root cause**: sidebar derives completed-thinking time by pairing
  `sent(target)` with `recv(agent)` events. `_run_collab()` emitted `recv`
  events but not `sent` events for collab sends.
- **fix**: `_run_collab()` now emits `sent` UI events for all collab send
  sites: seed-turn routed send, user-initiated collab start send, and each
  routed send in the main loop.
- **files**: `cli.py`, `test_cli.py`

### refactoring

**10. shared claude parsing helpers**

- `_extract_claude_assistant_text`, `_extract_claude_user_text`, and
  `_is_tool_result_only_claude_user_entry` moved from `router.py` to
  `extract.py` and imported by both modules, eliminating duplication.
  `_extract_claude_room_events` now uses the shared assistant text helper.
- **files**: `extract.py`, `router.py`

### summary

| # | category | fix | files |
|---|---|---|---|
| 1 | delivery | user context in routed messages | router, cli, tests |
| 2 | delivery | initial message echo suppression | router, tests |
| 3 | delivery | echo dedup scoping (first-match only) | router, tests |
| 4 | delivery | interjection routing + chronological ordering | cli, router, tests |
| 5 | detection | stop-event fallback staleness (boundary-aware) | router, extract, tests |
| 6 | detection | stop-event boundary for empty/meta user rows | router, tests |
| 7 | cursor | stale delivery cursors after collab exit | router, cli, tests |
| 8 | cursor | selective sync on `/halt` (preserve unrouted peer) | cli, router, tests |
| 9 | UI | sidebar think counter `sent` event emission | cli, tests |
| 10 | refactor | shared claude parsing helpers in extract.py | extract, router |

verified: `PYTHONPATH=. pytest -q` → 211 passed
