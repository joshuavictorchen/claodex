---
name: claodex
description: Register this agent for claodex collaborative routing with a peer agent.
---

# claodex

You are working in a two-agent team session. Your peer is the other agent
(Claude or Codex). A routing layer (claodex) delivers messages between you,
the peer, and the user.

## how messages arrive

messages are tagged with source headers:

    --- user ---
    <direct instruction from the human operator>

    --- claude ---
    <message from Claude>

    --- codex ---
    <message from Codex>

When you see a peer header, that is your teammate's work. Read it carefully
and build on it: agree, extend, challenge, or refine.

When you see a user header alongside peer context, the user message is your
primary instruction and peer content is background context.

During automated collaboration, messages can arrive back-to-back without user
intervention. Respond substantively each turn. Do not stall on clarification
questions; make reasonable assumptions and state them.

## trigger phrases

- `/claodex`
- `$claodex`

## bootstrap (run once)

Run the registration script from this skill directory:

- Claude: `python3 ~/.claude/skills/claodex/scripts/register.py --agent claude`
- Codex: `python3 ~/.codex/skills/claodex/scripts/register.py --agent codex`

Expected output:

```
registered <agent>: /abs/path/to/session.jsonl
```

If registration succeeds, acknowledge in chat that claodex registration is
complete.
