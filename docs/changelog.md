# changelog

## 2026-02-26 — fix stop-event fallback returning stale assistant text

**problem**: agent-initiated `[COLLAB]` detection silently failed. the idle
poll detected a response but reported 90 words instead of the expected 227 —
missing the `[COLLAB]` signal on the last line. the response was consumed
(watch deleted, latch cleared), so the signal was permanently lost.

**root cause**: the stop-event fallback in `poll_for_response` (and
`wait_for_response`) races against JSONL flushes. claude code writes the
debug-log Stop event before the final assistant text block is flushed to the
session JSONL. when the fallback fires, `_latest_assistant_message_between`
returns whichever assistant text is on disk — which can be an intermediate
frame from before a tool call, not the final response. in the observed case,
a 90-word "running tests" text appeared before a `tool_result` boundary, while
the 227-word response with `[COLLAB]` hadn't been flushed yet (32ms gap
between generation timestamp and Stop event).

**solution**: replaced `_latest_assistant_message_between` with a new
boundary-aware extractor `_latest_claude_stop_fallback_message_between` for
both `wait_for_response` and `poll_for_response` stop-event paths. the new
method treats every user row (including `tool_result` rows) as a boundary that
resets the "latest assistant text" accumulator. if the final assistant text
hasn't been flushed past the last boundary, the method returns `None` and the
stop-event latch survives to the next poll cycle.

additional fixes:
- entry-level `isSidechain` / `isMeta` rows are skipped, consistent with the
  primary extraction path in `extract.py`
- shared claude parsing helpers (`_extract_claude_assistant_text`,
  `_extract_claude_user_text`, `_is_tool_result_only_claude_user_entry`) moved
  to `extract.py` and imported by `router.py`, eliminating duplication
- `_extract_claude_room_events` now uses the shared assistant text helper

**residual risk**: if claude emits consecutive text-only assistant frames with
no intervening user boundary and the stop event fires before the last one is
flushed, the earlier frame would still be returned. this pattern has not been
observed in practice.

files changed: `claodex/router.py`, `claodex/extract.py`,
`tests/test_router.py`

verified: `PYTHONPATH=. pytest -q` → 201 passed

## 2026-02-26 — fix missing user context in routed collab messages

**problem**: when a user sent messages to an agent and that agent responded
with `[COLLAB]`, the peer agent received only the agent's response — not the
user messages that prompted it. separately, when the user sent a second message
while the agent was still thinking, the earlier message's `PendingSend` blocks
were discarded from the exchange log (delivery to the agent itself was
unaffected). the peer had no visibility into what was originally asked.

**root cause**: `send_routed_message()` built its payload exclusively from the
explicit `response_text` parameter and optional `user_interjections`. it never
called `build_delta_for_target()` to include undelivered events from the source
agent's JSONL. it then advanced the delivery cursor past those events, silently
marking them as delivered without ever sending them. a secondary issue in the
REPL watch-replacement logic discarded earlier `PendingSend` blocks when a
second message was sent to an already-thinking agent, causing the exchange log
to lose earlier messages.

**solution**:
- `send_routed_message()` now calls `build_delta_for_target()` to fetch
  undelivered events from the source agent's JSONL, filters out source-agent
  assistant rows (already forwarded via `response_text`), and prepends the
  remaining user events to the payload. the delivery cursor advances from the
  delta cursor rather than the raw read cursor.
- the REPL watch-replacement path now preserves prior `PendingSend.blocks` and
  the earliest `sent_at` timestamp when a second message supersedes a pending
  watch, so seeded exchange logs retain the full message history.
- collab exchange logging now records interjections directly from the drained
  queue rather than slicing `pending.blocks`, preventing double-logging of
  delta events that `send_routed_message()` now includes.

files changed: `claodex/router.py`, `claodex/cli.py`,
`tests/test_router.py`, `tests/test_cli.py`

verified: `PYTHONPATH=. pytest -q tests/test_router.py tests/test_cli.py`
→ 90 passed; `PYTHONPATH=. pytest -q` → 196 passed

## 2026-02-26 — fix stale delivery cursors after collab termination

**problem**: after a collab ended, the first normal-mode message to an agent
included an extra `--- user ---` block — an echo of a previously routed message
leaking through the peer's JSONL as undelivered delta.

**root cause**: when collab terminates (convergence, halt, error, or turn
limit), the last received response is never routed onward. the delivery cursor
for the non-final agent remains stale by one turn. when the user sends the next
message, `compose_user_message()` → `build_delta_for_target()` picks up the
stale events — including user entries that are echoes of previously routed
messages — and prepends them as delta. the prior routed-context fix made this
more visible because `strip_injected_context()` now cleanly extracts user
blocks from routed messages, producing recognizable duplicate user headers
instead of opaque nested blobs.

**solution**: added `Router.sync_delivery_cursors()` which aligns both delivery
cursors to current peer read positions. called in `_run_collab()`'s `finally`
block so all exit paths (converged, halted, error, turn limit) are covered. a
sync failure is logged but does not prevent exchange log close or interjection
drain.

files changed: `claodex/router.py`, `claodex/cli.py`,
`tests/test_router.py`, `tests/test_cli.py`

verified: `PYTHONPATH=. pytest -q tests/test_router.py tests/test_cli.py`
→ 93 passed; `PYTHONPATH=. pytest -q` → 199 passed
