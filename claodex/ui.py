"""UI event bus and metrics snapshot writer."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .constants import AGENTS, UI_EVENTS_FILE, UI_METRICS_FILE
from .errors import ClaodexError

PERSISTED_EVENT_KINDS = frozenset(
    {
        "sent",
        "recv",
        "collab",
        "watch",
        "error",
        "system",
        "status",
    }
)
METRIC_MODES = frozenset({"normal", "collab"})
AGENT_STATUSES = frozenset({"idle", "thinking"})


class UIEventBus:
    """Thread-safe writer for UI events and metrics snapshots."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        default_target: str = "claude",
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize event bus files and canonical metrics state.

        Args:
            workspace_root: Workspace root path.
            default_target: Initial routing target.
            now_provider: Optional timestamp provider for deterministic tests.
        """
        if default_target not in AGENTS:
            raise ClaodexError(f"validation error: unsupported target: {default_target}")

        self._workspace_root = workspace_root
        self._events_path = workspace_root / UI_EVENTS_FILE
        self._metrics_path = workspace_root / UI_METRICS_FILE
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._closed = False

        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        self._events_handle = self._events_path.open("a", encoding="utf-8")

        self._metrics_snapshot = _default_metrics_snapshot(
            target=default_target,
            uptime_start=_iso_timestamp(self._now()),
        )
        _validate_metrics_snapshot(self._metrics_snapshot)
        self._write_metrics_locked()

    def log(
        self,
        kind: str,
        message: str,
        *,
        agent: str | None = None,
        target: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Append one persisted event to events.jsonl.

        Args:
            kind: Persisted event kind.
            message: Human-readable event text.
            agent: Optional agent identity.
            target: Optional target agent identity.
            meta: Optional structured metadata.
        """
        if kind not in PERSISTED_EVENT_KINDS:
            raise ClaodexError(f"validation error: unsupported event kind: {kind}")
        if not isinstance(message, str):
            raise ClaodexError("validation error: event message must be a string")
        if agent is not None and agent not in AGENTS:
            raise ClaodexError(f"validation error: unsupported agent: {agent}")
        if target is not None and target not in AGENTS:
            raise ClaodexError(f"validation error: unsupported target: {target}")
        if meta is not None and not isinstance(meta, dict):
            raise ClaodexError("validation error: event meta must be an object")

        event: dict[str, Any] = {
            "ts": _iso_timestamp(self._now()),
            "kind": kind,
            "agent": agent,
            "target": target,
            "message": message,
            "meta": meta,
        }

        with self._lock:
            self._ensure_open_locked()
            self._events_handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._events_handle.flush()

    def update_metrics(self, **fields: Any) -> None:
        """Merge fields into canonical metrics snapshot and persist atomically.

        Args:
            **fields: Partial metrics fields to merge.
        """
        if not fields:
            return

        with self._lock:
            self._ensure_open_locked()
            updated_snapshot = deepcopy(self._metrics_snapshot)
            _merge_with_schema(updated_snapshot, fields, path="metrics")
            _validate_metrics_snapshot(updated_snapshot)
            self._metrics_snapshot = updated_snapshot
            self._write_metrics_locked()

    def close(self) -> None:
        """Flush and close open handles."""
        with self._lock:
            if self._closed:
                return
            self._events_handle.flush()
            self._events_handle.close()
            self._closed = True

    def _write_metrics_locked(self) -> None:
        """Write canonical metrics snapshot atomically.

        Assumes caller holds `_lock`.
        """
        tmp_path = self._metrics_path.with_name(f"{self._metrics_path.name}.tmp")
        payload = json.dumps(self._metrics_snapshot, ensure_ascii=False, indent=2) + "\n"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self._metrics_path)

    def _ensure_open_locked(self) -> None:
        """Raise when bus is closed.

        Assumes caller holds `_lock`.
        """
        if self._closed:
            raise ClaodexError("ui event bus is closed")


def _iso_timestamp(value: datetime) -> str:
    """Return ISO 8601 timestamp with timezone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _default_metrics_snapshot(target: str, uptime_start: str) -> dict[str, Any]:
    """Build schema-valid default metrics snapshot."""
    return {
        "target": target,
        "mode": "normal",
        "collab_turn": None,
        "collab_max": None,
        "uptime_start": uptime_start,
        "agents": {
            "claude": {
                "status": "idle",
                "thinking_since": None,
                "last_words": None,
                "last_latency_s": None,
            },
            "codex": {
                "status": "idle",
                "thinking_since": None,
                "last_words": None,
                "last_latency_s": None,
            },
        },
    }


def _merge_with_schema(
    destination: dict[str, Any],
    updates: dict[str, Any],
    *,
    path: str,
) -> None:
    """Merge updates into destination while enforcing known schema keys."""
    for key, value in updates.items():
        if key not in destination:
            raise ClaodexError(f"validation error: unknown metrics field: {path}.{key}")

        current = destination[key]
        if isinstance(current, dict):
            if not isinstance(value, dict):
                raise ClaodexError(
                    f"validation error: expected object for metrics field: {path}.{key}"
                )
            _merge_with_schema(current, value, path=f"{path}.{key}")
            continue

        destination[key] = value


def _validate_metrics_snapshot(snapshot: dict[str, Any]) -> None:
    """Validate canonical metrics payload."""
    target = snapshot.get("target")
    if target not in AGENTS:
        raise ClaodexError("validation error: metrics.target must be 'claude' or 'codex'")

    mode = snapshot.get("mode")
    if mode not in METRIC_MODES:
        raise ClaodexError("validation error: metrics.mode must be 'normal' or 'collab'")

    collab_turn = snapshot.get("collab_turn")
    if collab_turn is not None and (not isinstance(collab_turn, int) or collab_turn < 1):
        raise ClaodexError("validation error: metrics.collab_turn must be null or a positive integer")

    collab_max = snapshot.get("collab_max")
    if collab_max is not None and (not isinstance(collab_max, int) or collab_max < 1):
        raise ClaodexError("validation error: metrics.collab_max must be null or a positive integer")

    uptime_start = snapshot.get("uptime_start")
    if not isinstance(uptime_start, str):
        raise ClaodexError("validation error: metrics.uptime_start must be a timestamp")
    _validate_timestamp_with_timezone(uptime_start, "metrics.uptime_start")

    agents = snapshot.get("agents")
    if not isinstance(agents, dict):
        raise ClaodexError("validation error: metrics.agents must be an object")
    if set(agents.keys()) != set(AGENTS):
        raise ClaodexError("validation error: metrics.agents must include claude and codex")

    for agent, data in agents.items():
        if not isinstance(data, dict):
            raise ClaodexError(f"validation error: metrics.agents.{agent} must be an object")

        status = data.get("status")
        if status not in AGENT_STATUSES:
            raise ClaodexError(
                f"validation error: metrics.agents.{agent}.status must be 'idle' or 'thinking'"
            )

        thinking_since = data.get("thinking_since")
        if thinking_since is not None:
            if not isinstance(thinking_since, str):
                raise ClaodexError(
                    f"validation error: metrics.agents.{agent}.thinking_since must be null or timestamp"
                )
            _validate_timestamp_with_timezone(
                thinking_since,
                f"metrics.agents.{agent}.thinking_since",
            )

        last_words = data.get("last_words")
        if last_words is not None and (not isinstance(last_words, int) or last_words < 0):
            raise ClaodexError(
                f"validation error: metrics.agents.{agent}.last_words must be null or non-negative integer"
            )

        last_latency = data.get("last_latency_s")
        if last_latency is not None:
            if not isinstance(last_latency, (int, float)) or last_latency < 0:
                raise ClaodexError(
                    f"validation error: metrics.agents.{agent}.last_latency_s must be null or non-negative number"
                )


def _validate_timestamp_with_timezone(value: str, field_name: str) -> None:
    """Validate strict ISO8601 timestamp with timezone offset."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ClaodexError(f"validation error: {field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ClaodexError(f"validation error: {field_name} must include timezone offset")
