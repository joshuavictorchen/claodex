from __future__ import annotations

from claodex.state import ensure_claodex_gitignore


def test_ensure_claodex_gitignore_creates_internal_gitignore(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ensure_claodex_gitignore(workspace)

    gitignore = workspace / ".claodex" / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == "*\n"


def test_ensure_claodex_gitignore_is_idempotent_when_file_exists(tmp_path):
    workspace = tmp_path / "workspace"
    state_dir = workspace / ".claodex"
    state_dir.mkdir(parents=True)
    gitignore = state_dir / ".gitignore"
    gitignore.write_text("# keep custom rules\n", encoding="utf-8")

    ensure_claodex_gitignore(workspace)

    assert gitignore.read_text(encoding="utf-8") == "# keep custom rules\n"


def test_ensure_claodex_gitignore_does_not_modify_root_gitignore(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root_gitignore = workspace / ".gitignore"
    root_gitignore.write_text(".venv/\n", encoding="utf-8")

    ensure_claodex_gitignore(workspace)

    assert root_gitignore.read_text(encoding="utf-8") == ".venv/\n"
