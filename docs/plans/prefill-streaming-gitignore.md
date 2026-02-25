# Plan: Prefill Verification, Streaming Exchange Logs, .claodex/.gitignore

Four items, implemented in dependency order.

## Item 1: Prefill send-then-verify

**Problem:** `prefill_skill_commands` fires `send-keys -l` immediately after
`_wait_for_agents_ready`, which only proves the agent process started — not
that its TUI is ready to accept input. Keystrokes arrive before the input
field renders and get silently dropped.

**Approach:** Send the keystrokes, then verify delivery by polling the pane
tail. Non-fatal warning on timeout; no retry (avoids duplicate text).

### Changes

**`claodex/tmux_ops.py`**

- Add `verify_prefill(pane_id, expected_text, timeout=5.0, poll=0.3)`:
  - Polls `tmux capture-pane -p -S -8 -E -1 -t <pane>` for `expected_text`
    as a substring of the tail output.
  - Returns `True` if found within `timeout`, `False` otherwise.
- Modify `prefill_skill_commands(layout)` → returns `list[str]` of warnings
  (empty on success). After each `send-keys`, calls `verify_prefill`. On
  failure, appends a warning string with pane ID and the manual command to type.

**`claodex/cli.py`**

- Call site (line ~254): capture return value of `prefill_skill_commands`,
  print any warnings.

### Tests

- `tests/test_tmux_ops.py`: test `verify_prefill` with mocked `_run_tmux`
  returning matching/non-matching output. Test `prefill_skill_commands`
  warning path.

---

## Item 2: Streaming exchange logs

**Problem:** Exchange log is written as a single batch after collab ends.
Crashes lose the entire transcript. User cannot inspect mid-collab.

**Approach:** Open the file at collab start, append each message as it
arrives (with flush), write a summary footer on close. File handle managed
via `try/finally`.

### Format change

Message identifiers change from bold text to h2 headers:

**Before:** `**claude** · 10:02 PM`
**After:** `## claude · 10:02 PM`

Header metadata moves from top-of-file to footer:

**Before (top):**
```markdown
# Collaboration: <prompt>

Started: <ISO>
Initiated by: <user|agent>
Agents: claude ↔ codex
Turns: <N>
Stop reason: <reason>
```

**After (top — at open time):**
```markdown
# Collaboration: <prompt>

Started: <ISO>
Initiated by: <user|agent>
Agents: claude ↔ codex
```

**After (footer — at close time):**
```markdown
---

*Turns: <N> · Stop reason: <reason>*
```

Filename uses the collab *start* timestamp (already the case, just moves the
`datetime.now()` call to file-open time).

### Changes

**`claodex/cli.py`**

- Remove `_write_exchange_log` method.
- Add `_open_exchange_log(workspace_root, initial_message, started_at, initiated_by) -> tuple[Path, IO]`:
  - Creates file with header (no turn count or stop reason — unknown at start).
  - Returns `(path, file_handle)`.
- Add `_append_exchange_message(handle, source, body, timestamp)`:
  - Writes `---\n\n## source · H:MM AM/PM\nbody\n\n` and flushes.
  - Strips routing signals from body.
  - First message skips the leading `---` separator.
- Add `_close_exchange_log(handle, turns, stop_reason)`:
  - Appends summary footer: `---\n\n*Turns: N · Stop reason: reason*\n`
  - Closes handle.
- Modify `_run_collab`:
  - Open exchange log file immediately after `started_at` is set and halt
    listener is started.
  - Track `_exchange_first_message: bool` to control leading separator.
  - **Seeded collab path** (line ~916): after recording `seed_turn` in
    `turn_records`, immediately append the seed turn's messages (initial
    user message blocks + seed response) to the exchange file.
  - **User-initiated path** (line ~951): append the initial user message
    blocks after sending.
  - **Main loop** (line ~991, after `turn_records.append`): append the
    response. Also append user interjections when routing (line ~1018).
  - **Subsequent turns** (line ~1013): when routing to peer, append any
    user interjection blocks.
  - Wrap file I/O in `try/finally` — `_close_exchange_log` runs in
    `finally` block (replaces the old `_write_exchange_log` call site).

### Spec update

**`docs/spec.md`** (lines 583–612):
- Update exchange log format section:
  - Message headers: `## source · H:MM AM/PM` (was `**source** · H:MM AM/PM`)
  - Header block: remove `Turns:` and `Stop reason:` lines
  - Add footer section with `*Turns: N · Stop reason: reason*`
  - Note: file is written incrementally (append-on-receive)

### Test updates

**`tests/test_cli.py`**:
- All exchange log tests currently call `_write_exchange_log` directly.
  Replace with tests against the new streaming API:
  - `test_exchange_log_basic_flow`: use `_open/_append/_close` sequence.
    Assert `## user` / `## claude` / `## codex` headers (was `**user**` etc).
    Assert footer contains `Turns:` and `Stop reason:`.
  - `test_exchange_log_user_interjections`: same conversion; verify ordering.
  - `test_exchange_log_strips_signals`: verify via `_append_exchange_message`.
  - `test_exchange_log_literal_header_in_body_not_split`: assert
    `content.count("## user")` and `content.count("## claude")` instead of
    bold markers.
  - `test_exchange_log_timestamps_present`: assert `## user ·` and
    `## claude ·` instead of bold markers.
  - `test_exchange_log_seed_turn_has_timestamp`: assert `## \w+ ·` pattern
    (no bare headers without timestamps).
- Keep `test_strip_routing_signals` and `test_format_local_time` unchanged
  (they test standalone helpers).

---

## Item 3: `.claodex/.gitignore` strategy

**Problem:** `.claodex/` entry in root `.gitignore` is the wrong place. A
`.gitignore` inside `.claodex/` is cleaner and self-contained.

### Changes

**`claodex/state.py`**

- Replace `ensure_gitignore_entry` with `ensure_claodex_gitignore(workspace_root)`:
  - Writes `.claodex/.gitignore` with content `*\n` if it doesn't exist.
  - Does NOT touch root `.gitignore`.
  - Does NOT remove existing `.claodex/` entries from root `.gitignore`
    (that's the user's file — don't modify unprompted).

**`claodex/cli.py`**

- Update import: `ensure_gitignore_entry` → `ensure_claodex_gitignore`.
- Update call site (line ~221).

**`docs/spec.md`**

- Line 557: update "auto-added to `.gitignore`" to "contains its own
  `.gitignore` with `*`".

### Tests

- Add `tests/test_state.py` or inline in `test_cli.py`: test that
  `.claodex/.gitignore` is created with `*` content; test idempotency.

---

## Item 4: Codemap update

After all changes land, update `docs/codemap.md` to reflect:
- New `prefill_skill_commands` return type and `verify_prefill` function
- Streaming exchange log API (open/append/close)
- `.claodex/.gitignore` strategy
- Updated search anchors if line numbers shifted significantly

---

## Implementation order

1. Item 3 (gitignore) — standalone, no dependencies
2. Item 1 (prefill verification) — standalone
3. Item 2 (streaming exchange logs) — largest change, spec + tests
4. Item 4 (codemap) — after all code changes land

## Completion criteria

- `PYTHONPATH=. python -m pytest -q` passes all tests
- Spec examples match implementation
- No regressions in existing test suite
