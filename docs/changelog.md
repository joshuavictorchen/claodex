# changelog

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
