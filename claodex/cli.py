"""claodex CLI entrypoint and interactive loop."""

from __future__ import annotations

import os
import queue
import shlex
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .constants import (
    AGENTS,
    COLLAB_SIGNAL,
    CONVERGE_SIGNAL,
    DEFAULT_COLLAB_TURNS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TURN_TIMEOUT_SECONDS,
    SESSION_NAME,
)
from .errors import ClaodexError
from .extract import ExtractionError, resolve_workspace_root
from .input_editor import InputEditor
from .router import PendingSend, ResponseTurn, Router, RoutingConfig, count_words
from .state import (
    Participant,
    SessionParticipants,
    clear_ui_state_files,
    cursor_snapshot,
    delivery_cursor_file,
    ensure_gitignore_entry,
    ensure_state_layout,
    exchanges_dir,
    initialize_cursors_from_line_counts,
    load_participant,
    load_participants,
    participant_file,
    peer_agent,
    read_cursor_file,
)
from .tmux_ops import (
    PaneLayout,
    attach_cli_pane,
    create_session,
    ensure_dependencies,
    is_pane_alive,
    kill_session,
    pane_current_command,
    paste_content,
    prefill_skill_commands,
    resolve_layout,
    session_exists,
    start_agent_processes,
    start_sidebar_process,
)
from .ui import UIEventBus


def _last_line_is(text: str, signal: str) -> bool:
    """Check if the last non-empty line of *text* equals *signal*."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped == signal
    return False


def _strip_trailing_signal(text: str, signal: str) -> str:
    """Remove the last non-empty line if it matches *signal*."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            if lines[i].strip() == signal:
                lines.pop(i)
            break
    return "\n".join(lines).rstrip()


def _drain_queue(q: queue.Queue[str]) -> list[str]:
    """Drain all items from a queue without blocking."""
    items: list[str] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items



def _strip_routing_signals(text: str) -> str:
    """Strip all trailing [COLLAB] and [CONVERGED] signals from message text.

    Loops until no trailing signal remains, so order and stacking don't matter.
    """
    signals = (COLLAB_SIGNAL, CONVERGE_SIGNAL)
    result = text.rstrip()
    changed = True
    while changed:
        changed = False
        for signal in signals:
            stripped = _strip_trailing_signal(result, signal)
            if stripped != result:
                result = stripped.rstrip()
                changed = True
    return result


def _format_local_time(ts: datetime) -> str:
    """Format a datetime as local ``H:MM AM/PM``."""
    local = ts.astimezone()
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{local.strftime('%M %p')}"


@dataclass(frozen=True)
class CollabRequest:
    """Parsed `/collab` command payload."""

    turns: int
    start_agent: str
    message: str


class ClaodexApplication:
    """Application coordinator for startup, attach, and REPL."""

    def __init__(self) -> None:
        """Initialize application defaults."""
        self._editor = InputEditor()
        self._pending_watches: dict[str, PendingSend] = {}
        self._collab_seed: tuple[PendingSend, ResponseTurn] | None = None
        self._collab_interjections: queue.Queue[str] = queue.Queue()
        self._input_prefill: str = ""
        self._post_halt: bool = False

    def run(self, argv: list[str]) -> int:
        """Run the CLI from argv.

        Args:
            argv: CLI args excluding program name.

        Returns:
            Exit status code.
        """
        if argv and argv[0] in {"-h", "--help"}:
            self._print_help()
            return 0

        mode = "start"
        directory_arg = argv[0] if argv else "."
        if argv and argv[0] == "attach":
            mode = "attach"
            directory_arg = argv[1] if len(argv) > 1 else "."
        if argv and argv[0] == "sidebar":
            mode = "sidebar"
            directory_arg = argv[1] if len(argv) > 1 else "."

        workspace_root = self._resolve_workspace(Path(directory_arg))

        try:
            if mode == "start":
                return self._run_start(workspace_root)
            if mode == "attach":
                return self._run_attach(workspace_root)
            return self._run_sidebar(workspace_root)
        except ClaodexError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ExtractionError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    def _resolve_workspace(self, directory: Path) -> Path:
        """Resolve and validate workspace root.

        Args:
            directory: User-provided directory argument.

        Returns:
            Workspace root path.
        """
        candidate = directory.expanduser().resolve()
        workspace_root = resolve_workspace_root(candidate)
        if not (workspace_root / ".git").exists() and not (workspace_root / ".claodex").exists():
            raise ClaodexError(
                "workspace must be a git repository or already contain .claodex state"
            )
        return workspace_root

    def _run_start(self, workspace_root: Path) -> int:
        """Run startup flow then attach to tmux.

        Startup sequence:
        1. clear stale participant files
        2. create tmux session with 4 panes
        3. launch agent processes (codex, claude)
        4. wait for agents to be ready (pane command transition)
        5. prepopulate skill commands in each pane
        6. paste REPL command into input pane
        7. attach to tmux — user presses Enter in agent panes to register
        8. REPL (attach mode) waits for registration, binds layout, inits cursors

        Args:
            workspace_root: Workspace root path.

        Returns:
            Exit status code.
        """
        ensure_dependencies()
        if session_exists(SESSION_NAME):
            raise ClaodexError(
                "tmux session 'claodex' already exists; run 'claodex attach' or kill the session"
            )

        ensure_state_layout(workspace_root)
        ensure_gitignore_entry(workspace_root)
        self._install_skill_assets()

        # clear all stale state from prior sessions: participant files (so
        # registration wait won't accept leftovers) and cursor files (so
        # attach mode will reinitialize rather than reuse stale positions)
        self._clear_session_state(workspace_root)

        created_session = False
        try:
            print("creating tmux session...")
            layout = create_session(workspace_root, session_name=SESSION_NAME)
            created_session = True
            print(
                "  panes: "
                f"codex={layout.codex}  claude={layout.claude}  "
                f"input={layout.input}  sidebar={layout.sidebar}"
            )

            print("launching sidebar process...")
            start_sidebar_process(layout, workspace_root)

            # capture baseline shell commands before launching agents
            baseline = {
                "codex": pane_current_command(layout.codex),
                "claude": pane_current_command(layout.claude),
            }
            print("launching agent processes...")
            start_agent_processes(layout, workspace_root)
            self._wait_for_agents_ready(layout, baseline)

            # prepopulate skill triggers so user only needs to press Enter
            print("prefilling skill commands...")
            prefill_skill_commands(layout)

            # paste REPL into input pane and attach immediately so the user
            # can see the agent panes and press Enter to trigger registration
            attach_cli_pane(layout)
            exe = shlex.quote(sys.executable)
            ws = shlex.quote(str(workspace_root))
            repl_cmd = f"{exe} -m claodex attach {ws}"
            paste_content(layout.input, repl_cmd)

            # hand the user's terminal to tmux
            if os.environ.get("TMUX"):
                os.execvp("tmux", ["tmux", "switch-client", "-t", SESSION_NAME])
            else:
                os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION_NAME])
        except Exception:
            if created_session:
                kill_session(SESSION_NAME)
            raise

    @staticmethod
    def _run_sidebar(workspace_root: Path) -> int:
        """Run the standalone sidebar process."""
        from .sidebar import run_sidebar

        return run_sidebar(workspace_root)

    def _run_attach(self, workspace_root: Path) -> int:
        """Attach to existing claodex tmux session.

        Handles two cases:
        - Fresh start: registration hasn't happened yet; wait for user to
          press Enter in agent panes, then init cursors.
        - Reattach: participants already registered; validate and resume.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Exit status code.
        """
        ensure_dependencies()
        if not session_exists(SESSION_NAME):
            raise ClaodexError("tmux session 'claodex' does not exist")

        layout = resolve_layout(SESSION_NAME)
        participants = self._load_or_wait_participants(workspace_root)
        participants = self._bind_participants_to_layout(participants, layout)
        self._validate_registered_panes(participants)
        self._ensure_sidebar_running(layout, workspace_root)

        # init cursors on fresh start, validate on reattach
        if self._cursors_missing(workspace_root):
            initialize_cursors_from_line_counts(workspace_root, participants)
        else:
            self._ensure_cursor_files_exist(workspace_root)

        attach_cli_pane(layout)

        self._run_repl(workspace_root, participants)
        return 0

    def _load_or_wait_participants(self, workspace_root: Path) -> SessionParticipants:
        """Load participants, waiting for registration if needed.

        On fresh start the REPL launches before the user has pressed Enter
        in the agent panes. Only falls back to the polling wait when
        participant files are absent; malformed files raise immediately.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Loaded participant metadata.
        """
        # check whether both participant files exist before attempting load
        both_exist = all(
            participant_file(workspace_root, agent).exists() for agent in AGENTS
        )
        if both_exist:
            # files exist — load and let parse/validation errors propagate
            return load_participants(workspace_root)

        print("waiting for agent registration (press Enter in each agent pane)...")
        participants = self._wait_for_registration(workspace_root)
        self._clear_terminal_screen()
        return participants

    def _cursors_missing(self, workspace_root: Path) -> bool:
        """Check whether any cursor files are absent.

        Args:
            workspace_root: Workspace root path.

        Returns:
            True when at least one cursor file is missing.
        """
        for agent in AGENTS:
            if not read_cursor_file(workspace_root, agent).exists():
                return True
        for target in AGENTS:
            if not delivery_cursor_file(workspace_root, target).exists():
                return True
        return False

    def _bind_participants_to_layout(
        self,
        participants: SessionParticipants,
        layout: PaneLayout,
    ) -> SessionParticipants:
        """Override participant pane IDs with current tmux layout bindings.

        Registration scripts may detect pane IDs incorrectly when running in
        sandbox or subprocess contexts. Routing always trusts the live layout.
        """

        def bind(participant: Participant, pane_id: str) -> Participant:
            if participant.tmux_pane != pane_id:
                print(
                    f"  note: overriding {participant.agent} pane "
                    f"{participant.tmux_pane} -> {pane_id}"
                )
            return Participant(
                agent=participant.agent,
                session_file=participant.session_file,
                session_id=participant.session_id,
                tmux_pane=pane_id,
                cwd=participant.cwd,
                registered_at=participant.registered_at,
            )

        return SessionParticipants(
            claude=bind(participants.claude, layout.claude),
            codex=bind(participants.codex, layout.codex),
        )

    def _ensure_cursor_files_exist(self, workspace_root: Path) -> None:
        """Require existing cursor files for attach mode.

        Args:
            workspace_root: Workspace root path.
        """
        missing: list[Path] = []
        for agent in AGENTS:
            candidate = read_cursor_file(workspace_root, agent)
            if not candidate.exists():
                missing.append(candidate)
        for target in AGENTS:
            candidate = delivery_cursor_file(workspace_root, target)
            if not candidate.exists():
                missing.append(candidate)
        if missing:
            joined = ", ".join(str(path) for path in missing)
            raise ClaodexError(f"attach requires existing cursor files; missing: {joined}")

    def _validate_registered_panes(self, participants: SessionParticipants) -> None:
        """Fail when registered panes are dead.

        Args:
            participants: Loaded participant metadata.
        """
        dead: list[str] = []
        for participant in (participants.claude, participants.codex):
            if not is_pane_alive(participant.tmux_pane):
                dead.append(f"{participant.agent} ({participant.tmux_pane})")
        if dead:
            raise ClaodexError(f"registered panes are not alive: {', '.join(dead)}")

    def _ensure_sidebar_running(self, layout: PaneLayout, workspace_root: Path) -> None:
        """Ensure sidebar pane is alive and running the sidebar process.

        Args:
            layout: Resolved tmux pane layout.
            workspace_root: Workspace root path.
        """
        if not is_pane_alive(layout.sidebar):
            raise ClaodexError(f"sidebar pane is not alive: {layout.sidebar}")

        current = pane_current_command(layout.sidebar)
        if current is not None and current.startswith("python"):
            return

        print("sidebar process not running; relaunching...")
        start_sidebar_process(layout, workspace_root)

    def _wait_for_agents_ready(
        self, layout: PaneLayout, baseline: dict[str, str | None]
    ) -> None:
        """Wait for agent CLIs to be running in their panes.

        Polls `pane_current_command` until both panes transition away from
        their pre-launch baseline command (the shell). This avoids hardcoding
        shell names and handles Codex reporting as `node` instead of `codex`.

        Args:
            layout: Active pane layout.
            baseline: Pre-launch `pane_current_command` for each agent label.
        """
        deadline = time.time() + 30
        agents = [
            ("codex", layout.codex),
            ("claude", layout.claude),
        ]
        waiting = {label for label, _ in agents}
        print("waiting for agent processes to start...")

        while waiting:
            if time.time() > deadline:
                for label, pane_id in agents:
                    if label in waiting:
                        cmd = pane_current_command(pane_id)
                        print(
                            f"  {label} pane {pane_id}: "
                            f"current_command={cmd!r} baseline={baseline.get(label)!r}"
                        )
                pending = ", ".join(sorted(waiting))
                raise ClaodexError(
                    f"agent startup timeout: {pending} never appeared in pane; "
                    "check tmux panes for errors"
                )

            for label, pane_id in agents:
                if label not in waiting:
                    continue
                if not is_pane_alive(pane_id):
                    raise ClaodexError(
                        f"{label} pane died during startup; "
                        f"check tmux pane {pane_id} for errors"
                    )
                cmd = pane_current_command(pane_id)
                if cmd and cmd != baseline.get(label):
                    print(f"  {label} ready (process: {cmd})")
                    waiting.discard(label)

            if waiting:
                time.sleep(1.0)

    def _clear_session_state(self, workspace_root: Path) -> None:
        """Remove stale participant and cursor files from a prior session.

        Called on fresh start to prevent registration wait from accepting
        leftover entries and to force cursor reinitialization.

        Args:
            workspace_root: Workspace root path.
        """
        for agent in AGENTS:
            path = participant_file(workspace_root, agent)
            if path.exists():
                path.unlink()
        for agent in AGENTS:
            path = read_cursor_file(workspace_root, agent)
            if path.exists():
                path.unlink()
        for target in AGENTS:
            path = delivery_cursor_file(workspace_root, target)
            if path.exists():
                path.unlink()
        clear_ui_state_files(workspace_root)

    def _wait_for_registration(self, workspace_root: Path) -> SessionParticipants:
        """Wait for both agents to complete registration.

        The user presses Enter in each agent pane to trigger the prefilled
        skill command. The skill runs register.py which writes a participant
        JSON file. We poll for those files.

        Args:
            workspace_root: Workspace root path.

        Returns:
            Loaded participant metadata.
        """
        # generous timeout: user may need time to read the panes, press Enter
        deadline = time.time() + 300
        waiting = {"claude", "codex"}

        while waiting:
            if time.time() > deadline:
                pending = ", ".join(sorted(waiting))
                raise ClaodexError(f"registration timeout waiting for: {pending}")

            for agent in list(waiting):
                path = participant_file(workspace_root, agent)
                if not path.exists():
                    continue
                try:
                    load_participant(workspace_root, agent)
                except ClaodexError:
                    continue
                print(f"  {agent} registered")
                waiting.remove(agent)

            if waiting:
                time.sleep(1.0)

        return load_participants(workspace_root)

    def _install_skill_assets(self) -> None:
        """Install/update claodex skill into claude/codex home skill dirs."""
        source = Path(__file__).resolve().parent / "skill"
        if not source.exists():
            raise ClaodexError(f"skill source missing: {source}")

        # files that must exist after a successful copy
        required_files = ("SKILL.md", "scripts/register.py")

        claude_root = Path(
            os.getenv("CLAODEX_CLAUDE_SKILLS_DIR", str(Path.home() / ".claude" / "skills"))
        )
        codex_root = Path(
            os.getenv("CLAODEX_CODEX_SKILLS_DIR", str(Path.home() / ".codex" / "skills"))
        )

        print("installing skill assets...")
        for root in (claude_root, codex_root):
            target = root / "claodex"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                print(f"  removing stale {target}")
                shutil.rmtree(target)
            # skip __init__.py and __pycache__ — they exist for setuptools
            # package discovery but are not needed in the installed skill
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__init__.py", "__pycache__"),
            )

            # verify every required file landed
            missing = [f for f in required_files if not (target / f).exists()]
            if missing:
                raise ClaodexError(
                    f"skill install to {target} incomplete, missing: {', '.join(missing)}"
                )
            print(f"  installed {target}")

    def _run_repl(self, workspace_root: Path, participants: SessionParticipants) -> None:
        """Run the interactive command loop.

        Args:
            workspace_root: Workspace root path.
            participants: Active participants.
        """
        config = RoutingConfig(
            poll_seconds=float(os.getenv("CLAODEX_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
            turn_timeout_seconds=int(
                os.getenv("CLAODEX_TURN_TIMEOUT_SECONDS", str(DEFAULT_TURN_TIMEOUT_SECONDS))
            ),
        )
        target = "claude"
        bus = UIEventBus(workspace_root=workspace_root, default_target=target)
        try:
            router = Router(
                workspace_root=workspace_root,
                participants=participants,
                paste_content=paste_content,
                pane_alive=is_pane_alive,
                config=config,
                warning_callback=lambda warning: self._log_event(bus, "error", warning),
            )

            idle_callback = self._make_idle_callback(router, bus=bus)
            self._log_event(bus, "system", "claodex ready")

            while True:
                try:
                    event = self._read_event(target, on_idle=idle_callback)
                except KeyboardInterrupt:
                    self._log_event(bus, "system", "keyboard interrupt")
                    continue

                if event.kind == "quit":
                    self._log_event(bus, "system", "shutting down")
                    kill_session(SESSION_NAME)
                    self._log_event(bus, "system", "session killed")
                    return

                if event.kind == "toggle":
                    target = peer_agent(target)
                    self._input_prefill = event.value
                    self._update_metrics(bus, target=target)
                    continue

                if event.kind == "collab_initiated" and self._collab_seed is not None:
                    seed_pending, seed_response = self._collab_seed
                    self._collab_seed = None
                    # preserve in-progress user input for after collab
                    if event.value:
                        self._input_prefill = event.value
                    self._clear_watches(router)
                    peer = peer_agent(seed_response.agent)
                    request = CollabRequest(
                        turns=DEFAULT_COLLAB_TURNS,
                        start_agent=peer,
                        message="",
                    )
                    self._log_event(
                        bus,
                        "collab",
                        f"{seed_response.agent} initiated collaboration",
                        agent=seed_response.agent,
                    )
                    self._clear_terminal_line()
                    self._run_collab(
                        workspace_root,
                        router,
                        request,
                        seed_turn=(seed_pending, seed_response),
                        bus=bus,
                    )
                    self._clear_terminal_line()
                    continue

                if event.kind != "submit":
                    continue

                try:
                    text = event.value.strip()
                    if not text:
                        continue

                    if text.startswith("/"):
                        if text == "/quit":
                            self._log_event(bus, "system", "shutting down")
                            kill_session(SESSION_NAME)
                            self._log_event(bus, "system", "session killed")
                            return
                        if text == "/status":
                            self._emit_status(
                                workspace_root=workspace_root,
                                participants=participants,
                                target=target,
                                bus=bus,
                            )
                            continue
                        if text == "/halt":
                            self._log_event(bus, "system", "no active collaboration to halt")
                            continue
                        if text.startswith("/collab"):
                            request = parse_collab_request(text, default_start=target)
                            self._clear_watches(router)
                            self._clear_terminal_line()
                            self._run_collab(
                                workspace_root,
                                router,
                                request,
                                bus=bus,
                            )
                            self._clear_terminal_line()
                            continue
                        self._log_event(bus, "error", f"unknown command: {text}")
                        continue

                    if self._post_halt:
                        text = f"(collab halted by user)\n\n{text}"
                        self._post_halt = False

                    pending = router.send_user_message(target, text)
                    self._log_event(bus, "sent", f"-> {target}", target=target)
                    self._mark_agent_thinking(bus, target, sent_at=pending.sent_at)

                    # track for idle [COLLAB] detection; supersede warning
                    # if there was already a pending watch for this target
                    if target in self._pending_watches:
                        old = self._pending_watches[target]
                        router.clear_poll_latch(target, old.before_cursor)
                        self._log_event(bus, "watch", f"replaced pending collab watch for {target}")
                    self._pending_watches[target] = pending
                except ClaodexError as exc:
                    self._log_event(bus, "error", str(exc))
                    continue
        finally:
            bus.close()

    def _clear_watches(self, router: Router) -> None:
        """Clear all pending watches and their poll latches."""
        for agent, pending in self._pending_watches.items():
            router.clear_poll_latch(agent, pending.before_cursor)
        self._pending_watches.clear()

    def _make_idle_callback(
        self,
        router: Router,
        bus: UIEventBus | None = None,
    ) -> "Callable[[], InputEvent | None]":
        """Build an idle callback that polls pending watches for [COLLAB].

        Args:
            router: Active message router.

        Returns:
            Callback suitable for InputEditor.read(on_idle=...).
        """
        from .input_editor import InputEvent

        def _poll() -> InputEvent | None:
            expired = []
            # iterate a snapshot to allow mutation during the loop
            for agent, pending in list(self._pending_watches.items()):
                # check for watch timeout
                if pending.sent_at is not None:
                    elapsed = (datetime.now(timezone.utc) - pending.sent_at).total_seconds()
                    if elapsed > router.config.turn_timeout_seconds:
                        expired.append(agent)
                        continue

                try:
                    response = router.poll_for_response(pending)
                except ClaodexError as exc:
                    del self._pending_watches[agent]
                    router.clear_poll_latch(agent, pending.before_cursor)
                    self._log_event(bus, "watch", f"error polling {agent}: {exc}", agent=agent)
                    return None

                if response is None:
                    continue

                # agent responded — remove watch regardless of signal
                del self._pending_watches[agent]
                words = count_words(response.text)
                latency = self._response_latency_seconds(pending)
                self._mark_agent_idle(
                    bus,
                    response.agent,
                    words=words,
                    latency_seconds=latency,
                )
                self._log_event(
                    bus,
                    "recv",
                    f"<- {response.agent} ({words} words)",
                    agent=response.agent,
                )

                if _last_line_is(response.text, COLLAB_SIGNAL):
                    clean_text = _strip_trailing_signal(response.text, COLLAB_SIGNAL)
                    if not clean_text.strip():
                        self._log_event(
                            bus,
                            "watch",
                            f"{agent} signaled [COLLAB] with no content, ignoring",
                            agent=agent,
                        )
                        return None
                    clean_response = ResponseTurn(
                        agent=response.agent,
                        text=clean_text,
                        source_cursor=response.source_cursor,
                        received_at=response.received_at,
                    )
                    # stash the seed turn for the REPL to pick up
                    self._collab_seed = (pending, clean_response)
                    return InputEvent(kind="collab_initiated")

                return None

            for agent in expired:
                pending = self._pending_watches.pop(agent)
                router.clear_poll_latch(agent, pending.before_cursor)
                self._log_event(bus, "watch", f"expired collab watch for {agent}", agent=agent)

            return None

        return _poll

    def _read_event(self, target: str, on_idle: "Callable[[], InputEvent | None] | None" = None):
        """Read one REPL event.

        Args:
            target: Current target label.
            on_idle: Optional idle callback for the input editor.

        Returns:
            Input event from editor or fallback input mode.
        """
        if sys.stdin.isatty() and sys.stdout.isatty():
            prefill = self._input_prefill
            self._input_prefill = ""
            return self._editor.read(target, on_idle=on_idle, prefill=prefill)

        try:
            line = input(f"{target} > ")
        except EOFError:
            from .input_editor import InputEvent

            return InputEvent(kind="quit")
        from .input_editor import InputEvent

        return InputEvent(kind="submit", value=line)

    @staticmethod
    def _clear_terminal_line() -> None:
        """Clear the active terminal line in TTY mode."""
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    @staticmethod
    def _clear_terminal_screen() -> None:
        """Clear the active terminal screen in TTY mode."""
        if not sys.stdout.isatty():
            return
        sys.stdout.write("\033[2J\033[H\033[3J")
        sys.stdout.flush()

    def _run_collab(
        self,
        workspace_root: Path,
        router: Router,
        request: CollabRequest,
        *,
        seed_turn: tuple[PendingSend, ResponseTurn] | None = None,
        bus: UIEventBus | None = None,
    ) -> None:
        """Run multi-turn automated collaboration.

        Args:
            workspace_root: Project root.
            router: Active message router.
            request: Parsed collaboration request.
            seed_turn: Optional first turn for agent-initiated collab.
                When provided the agent's response is routed to the peer
                as turn 1 and the initial user-message send is skipped.
        """
        self._log_event(
            bus,
            "collab",
            f"starting: target={request.start_agent} turns={request.turns}",
            target=request.start_agent,
        )
        self._update_metrics(bus, mode="collab", collab_turn=None, collab_max=request.turns)
        started_at = datetime.now().astimezone().replace(microsecond=0)

        stop_reason = "turns_reached"
        turns_completed = 0
        pending: PendingSend | None = None
        last_active_target = request.start_agent

        turn_records: list[tuple[PendingSend, ResponseTurn]] = []
        # prevent stale interjections from a previous collab from leaking
        # into this run
        _drain_queue(self._collab_interjections)

        halt_event = threading.Event()
        stop_listener = threading.Event()
        listener_kwargs: dict[str, object] = {
            "halt_event": halt_event,
            "stop_event": stop_listener,
            "editor": self._editor,
        }
        if bus is not None:
            listener_kwargs["bus"] = bus
        listener = threading.Thread(
            target=self._halt_listener,
            kwargs=listener_kwargs,
            daemon=True,
        )
        listener.start()

        try:
            if seed_turn:
                # agent-initiated: first turn already completed, route to peer
                seed_pending, seed_response = seed_turn
                turns_completed = 1
                turn_records.append(seed_turn)
                words = count_words(seed_response.text)
                self._mark_agent_idle(
                    bus,
                    seed_response.agent,
                    words=words,
                    latency_seconds=self._response_latency_seconds(seed_pending),
                )
                self._update_metrics(bus, collab_turn=turns_completed, collab_max=request.turns)
                self._log_event(
                    bus,
                    "recv",
                    f"<- {seed_response.agent} ({words} words)",
                    agent=seed_response.agent,
                )
                self._log_event(
                    bus,
                    "collab",
                    f"turn 1 <- {seed_response.agent} ({words} words)",
                    agent=seed_response.agent,
                )
                next_target = peer_agent(seed_response.agent)
                pending = router.send_routed_message(
                    target_agent=next_target,
                    source_agent=seed_response.agent,
                    response_text=seed_response.text,
                )
                last_active_target = next_target
                self._mark_agent_thinking(bus, next_target, sent_at=pending.sent_at)
                self._log_event(bus, "collab", f"routing -> {next_target}", target=next_target)
            else:
                pending = router.send_user_message(request.start_agent, request.message)
                last_active_target = pending.target_agent
                self._mark_agent_thinking(bus, pending.target_agent, sent_at=pending.sent_at)

            while turns_completed < request.turns:
                self._log_event(
                    bus,
                    "collab",
                    f"turn {turns_completed + 1} -> {pending.target_agent} (waiting...)",
                    target=pending.target_agent,
                )
                try:
                    response = router.wait_for_response(pending)
                except KeyboardInterrupt:
                    halt_event.set()
                    stop_reason = "user_halt"
                    break

                turns_completed += 1
                words = count_words(response.text)
                self._mark_agent_idle(
                    bus,
                    response.agent,
                    words=words,
                    latency_seconds=self._response_latency_seconds(pending),
                )
                self._update_metrics(bus, collab_turn=turns_completed, collab_max=request.turns)
                self._log_event(
                    bus,
                    "recv",
                    f"<- {response.agent} ({words} words)",
                    agent=response.agent,
                )
                self._log_event(
                    bus,
                    "collab",
                    f"turn {turns_completed} <- {response.agent} ({words} words)",
                    agent=response.agent,
                )

                turn_records.append((pending, response))

                # convergence: both agents signaled in consecutive turns
                if (
                    len(turn_records) >= 2
                    and _last_line_is(turn_records[-1][1].text, CONVERGE_SIGNAL)
                    and _last_line_is(turn_records[-2][1].text, CONVERGE_SIGNAL)
                ):
                    stop_reason = "converged"
                    break

                if halt_event.is_set():
                    stop_reason = "user_halt"
                    break

                if turns_completed >= request.turns:
                    stop_reason = "turns_reached"
                    break

                # include any user interjections typed during this turn
                interjections = _drain_queue(self._collab_interjections)

                next_target = peer_agent(response.agent)
                pending = router.send_routed_message(
                    target_agent=next_target,
                    source_agent=response.agent,
                    response_text=response.text,
                    user_interjections=interjections or None,
                )
                last_active_target = next_target
                self._mark_agent_thinking(bus, next_target, sent_at=pending.sent_at)
                if interjections:
                    self._log_event(
                        bus,
                        "collab",
                        f"routing -> {next_target} (with {len(interjections)} user interjection(s))",
                        target=next_target,
                    )
                else:
                    self._log_event(bus, "collab", f"routing -> {next_target}", target=next_target)

        except ClaodexError as exc:
            stop_reason = str(exc)
            self._log_event(bus, "collab", f"halted: {exc}")
            self._log_event(bus, "error", str(exc))
        except KeyboardInterrupt:
            stop_reason = "user_halt"
            self._log_event(bus, "collab", "halted by user")
        finally:
            stop_listener.set()
            listener.join(timeout=0.5)

        # any queued interjections that were not routed inline are dropped
        # when collab stops
        remaining = _drain_queue(self._collab_interjections)
        if remaining:
            self._log_event(
                bus,
                "collab",
                f"dropped {len(remaining)} queued interjection(s)",
            )

        initiated_by = seed_turn[1].agent if seed_turn else "user"
        exchange_path = self._write_exchange_log(
            workspace_root=workspace_root,
            turn_records=turn_records,
            initial_message=request.message,
            started_at=started_at,
            turns=turns_completed,
            stop_reason=stop_reason,
            initiated_by=initiated_by,
        )
        self._log_event(
            bus,
            "collab",
            f"halted: {turns_completed} turns, reason={stop_reason}, exchange={exchange_path}",
        )
        self._log_event(
            bus,
            "collab",
            f"last active target: {last_active_target}",
            target=last_active_target,
        )
        if stop_reason == "user_halt":
            self._post_halt = True
        self._update_metrics(
            bus,
            mode="normal",
            collab_turn=None,
            collab_max=None,
        )

    def _halt_listener(
        self,
        halt_event: threading.Event,
        stop_event: threading.Event,
        editor: InputEditor | None = None,
        bus: UIEventBus | None = None,
    ) -> None:
        """Watch stdin for `/halt` while collab mode runs.

        Args:
            halt_event: Set when halt is requested.
            stop_event: Set when listener should stop.
            editor: Shared input editor to preserve prompt history across modes.
            bus: Optional event bus for collab status events.
        """
        if not sys.stdin.isatty():
            return

        from .input_editor import InputEvent

        shared_editor = editor or self._editor

        def _on_idle() -> InputEvent | None:
            if stop_event.is_set() or halt_event.is_set():
                return InputEvent(kind="quit")
            return None

        while not stop_event.is_set() and not halt_event.is_set():
            try:
                event = shared_editor.read("collab", on_idle=_on_idle)
            except KeyboardInterrupt:
                dropped = len(_drain_queue(self._collab_interjections))
                halt_event.set()
                if dropped:
                    self._log_event(
                        bus,
                        "collab",
                        f"halt requested (dropped {dropped} queued interjection(s))",
                    )
                else:
                    self._log_event(bus, "collab", "halt requested")
                break

            if event.kind == "quit":
                continue
            if event.kind != "submit":
                continue

            stripped = event.value.strip()
            if stripped == "/halt":
                dropped = len(_drain_queue(self._collab_interjections))
                halt_event.set()
                if dropped:
                    self._log_event(
                        bus,
                        "collab",
                        f"halt requested (dropped {dropped} queued interjection(s))",
                    )
                else:
                    self._log_event(bus, "collab", "halt requested")
            elif stripped:
                # queue for inclusion in the next routed message;
                # collab keeps flowing
                self._collab_interjections.put(stripped)
                self._log_event(bus, "collab", "interjection queued")

    def _response_latency_seconds(self, pending: PendingSend) -> float | None:
        """Return latency in seconds from send metadata."""
        if pending.sent_at is None:
            return None
        delta = datetime.now(timezone.utc) - pending.sent_at
        return max(0.0, delta.total_seconds())

    def _mark_agent_thinking(
        self,
        bus: UIEventBus | None,
        agent: str,
        *,
        sent_at: datetime | None = None,
    ) -> None:
        """Set one agent to thinking in metrics."""
        if bus is None:
            return
        thinking_since = (sent_at or datetime.now(timezone.utc)).isoformat()
        bus.update_metrics(
            agents={
                agent: {
                    "status": "thinking",
                    "thinking_since": thinking_since,
                }
            }
        )

    def _mark_agent_idle(
        self,
        bus: UIEventBus | None,
        agent: str,
        *,
        words: int | None = None,
        latency_seconds: float | None = None,
    ) -> None:
        """Set one agent to idle in metrics and record optional turn stats."""
        if bus is None:
            return
        update: dict[str, object] = {
            "status": "idle",
            "thinking_since": None,
        }
        if words is not None:
            update["last_words"] = words
        update["last_latency_s"] = latency_seconds
        bus.update_metrics(agents={agent: update})

    def _update_metrics(self, bus: UIEventBus | None, **fields: object) -> None:
        """Apply metrics updates when a bus is available."""
        if bus is None:
            return
        bus.update_metrics(**fields)

    def _log_event(
        self,
        bus: UIEventBus | None,
        kind: str,
        message: str,
        *,
        agent: str | None = None,
        target: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> None:
        """Emit one UI event when a bus is available."""
        if bus is None:
            return
        bus.log(kind, message, agent=agent, target=target, meta=meta)

    def _write_exchange_log(
        self,
        workspace_root: Path,
        turn_records: list[tuple[PendingSend, ResponseTurn]],
        initial_message: str,
        started_at: datetime,
        turns: int,
        stop_reason: str,
        initiated_by: str = "user",
    ) -> Path:
        """Persist collab exchange log as a group-chat transcript.

        Each message appears exactly once in chronological order with a
        ``**source** · H:MM AM/PM`` header line.

        Args:
            workspace_root: Workspace root for state output.
            turn_records: Collected turn send/response tuples.
            initial_message: User collab prompt.
            started_at: Collab start timestamp.
            turns: Turns completed.
            stop_reason: Terminal reason string.
            initiated_by: Who started the collab ("user" or agent name).

        Returns:
            Path to written markdown file.
        """
        timestamp = datetime.now().strftime("%y%m%d-%H%M%S")
        output_dir = exchanges_dir(workspace_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{timestamp}.md"

        header_message = initial_message.strip().replace("\n", " ")[:80]
        lines = [
            f"# Collaboration: {header_message}",
            "",
            f"Started: {started_at.isoformat()}",
            f"Initiated by: {initiated_by}",
            "Agents: claude ↔ codex",
            f"Turns: {turns}",
            f"Stop reason: {stop_reason}",
            "",
            "---",
            "",
        ]

        # build flat chronological message list from structured blocks:
        # (source, body, timestamp or None)
        messages: list[tuple[str, str, datetime | None]] = []

        for i, (pending, response) in enumerate(turn_records):
            if i == 0:
                # first turn: all blocks are new (delta context + user message)
                for source, body in pending.blocks:
                    messages.append((source, body, pending.sent_at))
            else:
                # subsequent turns: first block is the peer response already
                # logged above — skip it; remaining blocks are user interjections
                for source, body in pending.blocks[1:]:
                    messages.append((source, body, pending.sent_at))

            messages.append((response.agent, response.text, response.received_at))

        for i, (source, body, ts) in enumerate(messages):
            ts_str = f" · {_format_local_time(ts)}" if ts else ""
            body = _strip_routing_signals(body)
            if i > 0:
                lines.append("---")
                lines.append("")
            lines.append(f"**{source}**{ts_str}")
            lines.append(body)
            lines.append("")

        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path

    def _emit_status(
        self,
        workspace_root: Path,
        participants: SessionParticipants,
        target: str,
        bus: UIEventBus | None,
    ) -> None:
        """Emit concise runtime status to the event bus."""
        snapshot = cursor_snapshot(workspace_root)
        payload = {
            "target": target,
            "participants": {
                "claude": {
                    "pane": participants.claude.tmux_pane,
                    "session_id": participants.claude.session_id,
                    "session_file": str(participants.claude.session_file),
                },
                "codex": {
                    "pane": participants.codex.tmux_pane,
                    "session_id": participants.codex.session_id,
                    "session_file": str(participants.codex.session_file),
                },
            },
            "cursors": snapshot,
            "pending_watches": sorted(self._pending_watches.keys()),
            "collab_seed_agent": self._collab_seed[1].agent if self._collab_seed else None,
        }
        self._log_event(
            bus,
            "status",
            "status snapshot",
            target=target,
            meta=payload,
        )

    @staticmethod
    def _print_help() -> None:
        """Print command usage."""
        print("usage:")
        print("  python3 -m claodex [directory]")
        print("  python3 -m claodex attach [directory]")
        print("  python3 -m claodex sidebar [directory]")


def parse_collab_request(command_text: str, default_start: str) -> CollabRequest:
    """Parse `/collab` command arguments.

    Args:
        command_text: Raw command text.
        default_start: Default start agent when option omitted.

    Returns:
        Parsed collaboration request.
    """
    # split on whitespace — no shell quoting rules, so apostrophes
    # and other punctuation in the message body work naturally
    pieces = command_text.split()
    if not pieces or pieces[0] != "/collab":
        raise ClaodexError("validation error: malformed collab command")

    turns = DEFAULT_COLLAB_TURNS
    start_agent = default_start

    index = 1
    while index < len(pieces):
        token = pieces[index]
        if token == "--turns":
            if index + 1 >= len(pieces):
                raise ClaodexError("validation error: --turns requires a value")
            try:
                turns = int(pieces[index + 1])
            except ValueError as exc:
                raise ClaodexError("validation error: --turns must be an integer") from exc
            if turns <= 0:
                raise ClaodexError("validation error: --turns must be positive")
            index += 2
            continue

        if token == "--start":
            if index + 1 >= len(pieces):
                raise ClaodexError("validation error: --start requires a value")
            candidate = pieces[index + 1]
            if candidate not in AGENTS:
                raise ClaodexError("validation error: --start must be 'claude' or 'codex'")
            start_agent = candidate
            index += 2
            continue

        # explicit end-of-options marker
        if token == "--":
            index += 1
            break
        # reject unrecognized options before they silently become message text
        if token.startswith("--"):
            raise ClaodexError(f"validation error: unknown option '{token}'")
        break

    # extract the message as the raw remainder after consuming option tokens,
    # using maxsplit to preserve original spacing and punctuation
    parts = command_text.split(maxsplit=index)
    message = parts[index].strip() if len(parts) > index else ""
    if not message:
        raise ClaodexError("validation error: /collab requires a message")

    return CollabRequest(turns=turns, start_agent=start_agent, message=message)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Args:
        argv: Optional argv vector.

    Returns:
        Exit status code.
    """
    application = ClaodexApplication()
    return application.run(argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
