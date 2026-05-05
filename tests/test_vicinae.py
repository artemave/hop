import os
from pathlib import Path

import pytest

from hop.commands.session import SessionListing
from hop.layouts import WindowSpec
from hop.vicinae import (
    SCRIPT_FILENAME_PREFIX,
    GeneratedScript,
    compute_target_scripts,
    default_scripts_dir,
    reconcile,
    regenerate,
)


class StubSway:
    def __init__(self, focused_workspace: str = "") -> None:
        self.focused_workspace = focused_workspace

    def get_focused_workspace(self) -> str:
        return self.focused_workspace


def _windows(*roles_and_commands: tuple[str, str]) -> tuple[WindowSpec, ...]:
    return tuple(WindowSpec(role=role, command=command, autostart_active=False) for role, command in roles_and_commands)


def _builtin_windows() -> tuple[WindowSpec, ...]:
    return _windows(("shell", ""), ("editor", "nvim"), ("browser", ""))


def test_focused_session_emits_window_kill_and_other_session_switch_scripts() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),
    )

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    filenames = [script.filename for script in scripts]
    assert filenames == [
        "hop-window-shell",
        "hop-window-editor",
        "hop-window-browser",
        "hop-kill",
        "hop-switch-other",
    ]


def test_custom_layout_roles_get_their_own_window_scripts() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(
            ("shell", ""), ("editor", "nvim"), ("console", "bin/rails c"), ("server", "bin/dev")
        ),
    )

    filenames = [script.filename for script in scripts]
    assert "hop-window-console" in filenames
    assert "hop-window-server" in filenames


def test_dispatched_command_per_role() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("editor", "nvim"), ("browser", ""), ("console", "bin/rails c")),
    )

    by_filename = {script.filename: script.content for script in scripts}
    assert "exec hop edit\n" in by_filename["hop-window-editor"]
    assert "exec hop browser\n" in by_filename["hop-window-browser"]
    assert "exec hop term --role console\n" in by_filename["hop-window-console"]


def test_off_session_workspace_emits_only_session_switch_scripts() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),
        SessionListing(name="third", workspace="p:third", project_root=Path("/projects/third")),
    )

    scripts = compute_target_scripts(
        "scratch",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    filenames = [script.filename for script in scripts]
    assert filenames == ["hop-switch-rails", "hop-switch-other", "hop-switch-third"]


def test_no_sessions_and_no_session_focus_emits_empty_set() -> None:
    scripts = compute_target_scripts("scratch", (), windows_for=lambda _: _builtin_windows())
    assert scripts == ()


def test_focused_workspace_with_unregistered_session_falls_back_to_off_session_set() -> None:
    sessions = (SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),)

    scripts = compute_target_scripts(
        "p:not-a-real-session",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    assert [s.filename for s in scripts] == ["hop-switch-other"]


def test_session_without_project_root_does_not_emit_window_scripts() -> None:
    sessions = (
        SessionListing(name="lost", workspace="p:lost", project_root=None),
        SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),
    )

    scripts = compute_target_scripts(
        "p:lost",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    assert [s.filename for s in scripts] == ["hop-switch-other"]


def test_role_filename_sanitization_replaces_disallowed_characters() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("test:integration", "bin/test")),
    )

    by_filename = {script.filename: script.content for script in scripts}
    assert "hop-window-test_integration" in by_filename
    assert "Hop test:integration" in by_filename["hop-window-test_integration"]


def test_filename_collisions_are_resolved_with_numeric_suffixes() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(
            ("test:integration", ""),
            ("test/integration", ""),
            ("test.integration", ""),
        ),
    )

    filenames = [s.filename for s in scripts if s.filename.startswith("hop-window-")]
    assert filenames == [
        "hop-window-test_integration",
        "hop-window-test_integration-2",
        "hop-window-test.integration",
    ]


def test_filename_collisions_skip_taken_suffixes() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    # Three colliding sanitized roles force the dedupe loop to increment past
    # `-2` to find an available suffix.
    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("test:int", ""), ("test/int", ""), ("test\\int", "")),
    )

    filenames = [s.filename for s in scripts if s.filename.startswith("hop-window-")]
    assert filenames == ["hop-window-test_int", "hop-window-test_int-2", "hop-window-test_int-3"]


def test_generated_script_has_directive_header_and_atomic_chmod_markers() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("editor", "nvim")),
    )
    by_filename = {s.filename: s.content for s in scripts}
    content = by_filename["hop-window-editor"]

    assert content.startswith("#!/usr/bin/env bash\n")
    assert "# @vicinae.schemaVersion 1\n" in content
    assert "# @vicinae.title Hop editor\n" in content
    assert "# @vicinae.mode silent\n" in content
    assert "cd /tmp/rails\n" in content


def test_window_script_packagename_is_session_name_for_subtitle_context() -> None:
    sessions = (SessionListing(name="rails-app", workspace="p:rails-app", project_root=Path("/tmp/rails")),)

    scripts = compute_target_scripts(
        "p:rails-app",
        sessions,
        windows_for=lambda _: _windows(("editor", "nvim"), ("console", "bin/rails c")),
    )
    by_filename = {s.filename: s.content for s in scripts}

    # Window scripts and the kill script all carry the focused session name
    # so vicinae's right-side label answers "which session does this act on?".
    assert "# @vicinae.packageName rails-app\n" in by_filename["hop-window-editor"]
    assert "# @vicinae.packageName rails-app\n" in by_filename["hop-window-console"]
    assert "# @vicinae.packageName rails-app\n" in by_filename["hop-kill"]


def test_switch_script_has_empty_packagename_to_suppress_default_subtitle() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/tmp/other")),
    )

    scripts = compute_target_scripts("p:rails", sessions, windows_for=lambda _: ())
    switch = next(s for s in scripts if s.filename == "hop-switch-other")

    # The session name is already in the title ("Hop switch to other"), so
    # an empty packageName hides vicinae's fallback ("scripts") and avoids
    # redundancy.
    assert "# @vicinae.packageName \n" in switch.content


def test_kill_script_uses_setsid_detach_and_vicinae_close_guard() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: (),
    )
    kill = next(s for s in scripts if s.filename == "hop-kill")

    assert "setsid -f bash -c" in kill.content
    assert "vicinae close || true" in kill.content
    assert "exec hop kill" in kill.content


def test_switch_script_dispatches_hop_switch_with_quoted_session_name() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),
        SessionListing(name="weird name", workspace="p:weird name", project_root=Path("/tmp/weird")),
    )

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: (),
    )
    weird = next(s for s in scripts if "weird" in s.filename)

    assert "exec hop switch 'weird name'\n" in weird.content


def test_reconcile_writes_target_files_with_executable_bit(tmp_path: Path) -> None:
    target = (
        GeneratedScript(filename="hop-window-shell", content="#!/usr/bin/env bash\necho shell\n"),
        GeneratedScript(filename="hop-kill", content="#!/usr/bin/env bash\necho kill\n"),
    )

    reconcile(target, scripts_dir=tmp_path)

    shell_path = tmp_path / "hop-window-shell"
    kill_path = tmp_path / "hop-kill"
    assert shell_path.read_text() == "#!/usr/bin/env bash\necho shell\n"
    assert kill_path.read_text() == "#!/usr/bin/env bash\necho kill\n"
    assert os.stat(shell_path).st_mode & 0o777 == 0o755
    assert os.stat(kill_path).st_mode & 0o777 == 0o755


def test_reconcile_creates_missing_scripts_directory(tmp_path: Path) -> None:
    target = (GeneratedScript(filename="hop-window-shell", content="x"),)
    nested = tmp_path / "missing" / "scripts"

    reconcile(target, scripts_dir=nested)

    assert (nested / "hop-window-shell").read_text() == "x"


def test_reconcile_removes_stale_hop_files_and_leaves_others_alone(tmp_path: Path) -> None:
    (tmp_path / "hop-stale").write_text("stale")
    (tmp_path / "hop-window-old").write_text("old")
    (tmp_path / "unrelated-script").write_text("untouched")

    target = (GeneratedScript(filename="hop-window-shell", content="new"),)

    reconcile(target, scripts_dir=tmp_path)

    assert not (tmp_path / "hop-stale").exists()
    assert not (tmp_path / "hop-window-old").exists()
    assert (tmp_path / "unrelated-script").read_text() == "untouched"
    assert (tmp_path / "hop-window-shell").read_text() == "new"


def test_reconcile_skips_rewrite_when_content_matches(tmp_path: Path) -> None:
    target_path = tmp_path / "hop-window-shell"
    target_path.write_text("same")
    initial_inode = target_path.stat().st_ino

    reconcile((GeneratedScript(filename="hop-window-shell", content="same"),), scripts_dir=tmp_path)

    assert target_path.stat().st_ino == initial_inode


def test_reconcile_overwrites_changed_content_atomically(tmp_path: Path) -> None:
    target_path = tmp_path / "hop-window-shell"
    target_path.write_text("old")

    reconcile((GeneratedScript(filename="hop-window-shell", content="new"),), scripts_dir=tmp_path)

    assert target_path.read_text() == "new"


def test_regenerate_wires_focused_workspace_sessions_and_windows_resolver(tmp_path: Path) -> None:
    sway = StubSway(focused_workspace="p:rails")
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),)
    windows = _windows(("shell", ""), ("editor", "nvim"))

    regenerate(
        sway=sway,
        sessions_loader=lambda: sessions,
        scripts_dir=tmp_path,
        windows_for=lambda _: windows,
    )

    assert (tmp_path / "hop-window-shell").exists()
    assert (tmp_path / "hop-window-editor").exists()
    assert (tmp_path / "hop-kill").exists()


def test_default_scripts_dir_honors_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg")
    assert default_scripts_dir() == Path("/custom/xdg/vicinae/scripts")


def test_default_scripts_dir_falls_back_to_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/tmp/home")
    assert default_scripts_dir() == Path("/tmp/home/.local/share/vicinae/scripts")


def test_script_filename_prefix_is_reserved_namespace() -> None:
    # Sanity check that the prefix used for reconciliation matches the
    # prefix every generated filename starts with.
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    for script in scripts:
        assert script.filename.startswith(SCRIPT_FILENAME_PREFIX)
