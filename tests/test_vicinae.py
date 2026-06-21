import os
import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest

from hop.commands.session import SessionListing
from hop.layouts import WindowSpec
from hop.vicinae import (
    SCRIPT_FILENAME_PREFIX,
    GeneratedScript,
    WindowsResolver,
    compute_target_scripts,
    default_scripts_dir,
    reconcile,
    regenerate,
)

# Absolute paths the generated scripts must invoke instead of bare `hop` /
# `hopd` — vicinae runs the scripts under Sway's inherited PATH, which need
# not contain hop's install dir. Picked with no shell-special characters so
# they render verbatim (no `shlex.quote` wrapping) in content assertions.
HOP_BIN = "/opt/hop/bin/hop"
HOPD_BIN = "/opt/hop/bin/hopd"


class StubSway:
    def __init__(self, focused_workspace: str = "") -> None:
        self.focused_workspace = focused_workspace

    def get_focused_workspace(self) -> str:
        return self.focused_workspace


def _windows(*roles_and_commands: tuple[str, str]) -> tuple[WindowSpec, ...]:
    return tuple(WindowSpec(role=role, command=command, active=False) for role, command in roles_and_commands)


def _builtin_windows() -> tuple[WindowSpec, ...]:
    return _windows(("shell", ""), ("editor", "nvim"), ("browser", ""))


def _targets(
    focused_workspace: str,
    sessions: Sequence[SessionListing],
    *,
    windows_for: WindowsResolver,
) -> tuple[GeneratedScript, ...]:
    """`compute_target_scripts` pinned to `HOP_BIN`.

    Most tests don't care which hop binary the scripts call, only that the
    set, filenames, and headers are right — so they go through here and let
    the absolute-path behavior be asserted once, explicitly, below.
    """
    return compute_target_scripts(focused_workspace, sessions, windows_for=windows_for, hop_bin=HOP_BIN)


def test_focused_session_emits_window_kill_and_other_session_switch_scripts() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),
    )

    scripts = _targets(
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
        "hop-create",
        "hop-move",
    ]


def test_custom_layout_roles_get_their_own_window_scripts() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = _targets(
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

    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("editor", "nvim"), ("browser", ""), ("console", "bin/rails c")),
    )

    by_filename = {script.filename: script.content for script in scripts}
    # `setsid -f` detaches hop from vicinae's SIGTERM-on-UI-close, so a
    # slow first-time prepare can't be killed mid-bootstrap. The hop binary
    # is the absolute `HOP_BIN`, never bare `hop` (vicinae's PATH need not
    # contain hop's install dir).
    assert f"exec setsid -f {HOP_BIN} term --role editor\n" in by_filename["hop-window-editor"]
    assert f"exec setsid -f {HOP_BIN} browser\n" in by_filename["hop-window-browser"]
    assert f"exec setsid -f {HOP_BIN} term --role console\n" in by_filename["hop-window-console"]


def test_off_session_workspace_emits_only_session_switch_scripts() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),
        SessionListing(name="third", workspace="p:third", project_root=Path("/projects/third")),
    )

    scripts = _targets(
        "scratch",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    filenames = [script.filename for script in scripts]
    assert filenames == ["hop-switch-rails", "hop-switch-other", "hop-switch-third", "hop-create", "hop-move"]


def test_no_sessions_and_no_session_focus_still_emits_create_and_move_scripts() -> None:
    scripts = _targets("scratch", (), windows_for=lambda _: _builtin_windows())
    assert [s.filename for s in scripts] == ["hop-create", "hop-move"]


def test_move_script_dispatches_to_vicinae_dmenu_over_hop_list() -> None:
    scripts = _targets("scratch", (), windows_for=lambda _: ())
    move = next(s for s in scripts if s.filename == "hop-move")

    assert "# @vicinae.title Hop move window to session\n" in move.content
    assert "# @vicinae.mode silent\n" in move.content
    # Candidates come from `hop list` (one session name per line); the user
    # picks the destination in a `vicinae dmenu`. `hop` is the absolute path.
    assert f"candidates=$({HOP_BIN} list)\n" in move.content
    assert 'vicinae dmenu --placeholder "Move window to session"' in move.content
    # `setsid -f` survives vicinae's SIGTERM-on-UI-close, matching the rest
    # of the hop-* script set.
    assert f'exec setsid -f {HOP_BIN} move "$chosen"\n' in move.content


def test_create_script_dispatches_to_vicinae_dmenu_over_home_directories() -> None:
    scripts = _targets("scratch", (), windows_for=lambda _: ())
    create = next(s for s in scripts if s.filename == "hop-create")

    # Directive header — title fuzzy-matches "hop cr".
    assert "# @vicinae.title Hop create session\n" in create.content
    assert "# @vicinae.mode silent\n" in create.content
    # Falls through to a second vicinae dmenu — that's the whole point;
    # static root enumeration over $HOME isn't workable.
    assert 'find "$HOME"' in create.content
    assert "vicinae dmenu" in create.content
    # Emits relative paths so vicinae doesn't auto-collapse same-basename
    # nested directories into a single visible entry.
    assert "-printf '%P\\n'" in create.content
    # Project roots (`.git` / `.jj`) prune so subdirectories don't outrank
    # their parent in vicinae's fuzzy ranking.
    assert "-exec test -e {}/.git \\;" in create.content
    assert "-exec test -e {}/.jj \\;" in create.content
    # `hop` from the picked directory creates the session if missing or
    # attaches if it already exists — same dispatch hop's CLI uses.
    # `setsid -f` survives vicinae's SIGTERM-on-UI-close, so a slow
    # first-time prepare can't be killed mid-bootstrap.
    assert f'cd "$HOME/$chosen"\nexec setsid -f {HOP_BIN}\n' in create.content


def test_focused_workspace_with_unregistered_session_falls_back_to_off_session_set() -> None:
    sessions = (SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),)

    scripts = _targets(
        "p:not-a-real-session",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    assert [s.filename for s in scripts] == ["hop-switch-other", "hop-create", "hop-move"]


def test_session_without_project_root_does_not_emit_window_scripts() -> None:
    sessions = (
        SessionListing(name="lost", workspace="p:lost", project_root=None),
        SessionListing(name="other", workspace="p:other", project_root=Path("/projects/other")),
    )

    scripts = _targets(
        "p:lost",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    assert [s.filename for s in scripts] == ["hop-switch-other", "hop-create", "hop-move"]


def test_role_filename_sanitization_replaces_disallowed_characters() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("test:integration", "bin/test")),
    )

    by_filename = {script.filename: script.content for script in scripts}
    assert "hop-window-test_integration" in by_filename
    assert "Hop test:integration" in by_filename["hop-window-test_integration"]


def test_filename_collisions_are_resolved_with_numeric_suffixes() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/projects/rails")),)

    scripts = _targets(
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
    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("test:int", ""), ("test/int", ""), ("test\\int", "")),
    )

    filenames = [s.filename for s in scripts if s.filename.startswith("hop-window-")]
    assert filenames == ["hop-window-test_int", "hop-window-test_int-2", "hop-window-test_int-3"]


def test_every_generated_script_advertises_the_hop_icon_with_a_resolvable_path() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/tmp/other")),
    )

    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    # window-* / kill / switch-* / create all share one icon — vicinae
    # surfaces them side-by-side, so a coherent visual identity matters.
    for script in scripts:
        icon_line = next(line for line in script.content.splitlines() if line.startswith("# @vicinae.icon "))
        icon_path = Path(icon_line.removeprefix("# @vicinae.icon ").strip())
        assert icon_path.is_absolute(), f"{script.filename} icon path is not absolute: {icon_path}"
        assert icon_path.is_file(), f"{script.filename} icon path does not resolve: {icon_path}"


def test_generated_script_has_directive_header_and_atomic_chmod_markers() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),)

    scripts = _targets(
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

    scripts = _targets(
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

    scripts = _targets("p:rails", sessions, windows_for=lambda _: ())
    switch = next(s for s in scripts if s.filename == "hop-switch-other")

    # The session name is already in the title ("Hop switch to other"), so
    # an empty packageName hides vicinae's fallback ("scripts") and avoids
    # redundancy.
    assert "# @vicinae.packageName \n" in switch.content


def test_kill_script_uses_setsid_detach_and_vicinae_close_guard() -> None:
    sessions = (SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),)

    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: (),
    )
    kill = next(s for s in scripts if s.filename == "hop-kill")

    assert "setsid -f bash -c" in kill.content
    assert "vicinae close || true" in kill.content
    assert f"exec {HOP_BIN} kill" in kill.content


def test_remote_session_scripts_use_hop_remote_env_not_cd() -> None:
    # A remote session's project root only exists on the remote, so the vicinae
    # window / kill scripts must pass identity via HOP_REMOTE_* env rather than
    # `cd`-ing into a path that doesn't exist on the laptop.
    sessions = (
        SessionListing(
            name="thonon-les-pains",
            workspace="p:thonon-les-pains",
            project_root=Path("/home/admin/projects/thonon-les-pains"),
            host="devbox.local",
        ),
    )

    scripts = _targets("p:thonon-les-pains", sessions, windows_for=lambda _: _windows(("editor", "nvim")))
    by_filename = {s.filename: s.content for s in scripts}
    env_line = "export HOP_REMOTE_HOST=devbox.local HOP_REMOTE_CWD=/home/admin/projects/thonon-les-pains\n"

    window = by_filename["hop-window-editor"]
    assert "cd " not in window
    assert env_line in window

    kill = by_filename["hop-kill"]
    assert env_line in kill
    assert f"exec {HOP_BIN} kill" in kill


def test_switch_script_dispatches_hop_switch_with_quoted_session_name() -> None:
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),
        SessionListing(name="weird name", workspace="p:weird name", project_root=Path("/tmp/weird")),
    )

    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: (),
    )
    weird = next(s for s in scripts if "weird" in s.filename)

    assert f"exec setsid -f {HOP_BIN} switch 'weird name'\n" in weird.content


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
        hop_bin=HOP_BIN,
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

    scripts = _targets(
        "p:rails",
        sessions,
        windows_for=lambda _: _builtin_windows(),
    )

    for script in scripts:
        assert script.filename.startswith(SCRIPT_FILENAME_PREFIX)


def test_every_script_invokes_hop_by_absolute_shell_quoted_path() -> None:
    """Vicinae runs these scripts under whatever PATH it inherited from Sway,
    which need not contain hop's install dir. So every dispatch must name the
    hop binary by absolute path — and a path with shell-special characters
    (here, a space) must be quoted, never left to word-split."""
    sessions = (
        SessionListing(name="rails", workspace="p:rails", project_root=Path("/tmp/rails")),
        SessionListing(name="other", workspace="p:other", project_root=Path("/tmp/other")),
    )
    hop_bin = "/opt/my hop/bin/hop"
    quoted = shlex.quote(hop_bin)

    scripts = compute_target_scripts(
        "p:rails",
        sessions,
        windows_for=lambda _: _windows(("editor", "nvim"), ("browser", "")),
        hop_bin=hop_bin,
    )
    by_filename = {s.filename: s.content for s in scripts}

    assert f"exec setsid -f {quoted} term --role editor\n" in by_filename["hop-window-editor"]
    assert f"exec setsid -f {quoted} browser\n" in by_filename["hop-window-browser"]
    assert f"exec {quoted} kill" in by_filename["hop-kill"]
    assert f"exec setsid -f {quoted} switch other\n" in by_filename["hop-switch-other"]
    assert f"exec setsid -f {quoted}\n" in by_filename["hop-create"]
    assert f"candidates=$({quoted} list)\n" in by_filename["hop-move"]
    assert f'exec setsid -f {quoted} move "$chosen"\n' in by_filename["hop-move"]

    # Nothing falls back to a bare `hop` command that the inherited PATH
    # would fail to resolve.
    for content in by_filename.values():
        assert "setsid -f hop " not in content
        assert "exec hop " not in content
        assert "$(hop " not in content


# --- write_daemon_down_script ---------------------------------------------


def test_write_daemon_down_script_writes_single_restart_entry(tmp_path: Path) -> None:
    from hop.vicinae import DAEMON_DOWN_FILENAME, write_daemon_down_script

    write_daemon_down_script(tmp_path, hopd_bin=HOPD_BIN, error=RuntimeError("the daemon died"))

    entries = [p.name for p in tmp_path.iterdir()]
    assert entries == [DAEMON_DOWN_FILENAME]

    content = (tmp_path / DAEMON_DOWN_FILENAME).read_text()
    assert "# @vicinae.title Hop daemon stopped — restart" in content
    assert "RuntimeError: the daemon died" in content
    # The action detaches a fresh hopd so vicinae closing its UI doesn't
    # take the new daemon down with it — by absolute path, since vicinae's
    # PATH need not contain hop's install dir.
    assert f"setsid -f {HOPD_BIN}" in content


def test_write_daemon_down_script_shell_quotes_the_hopd_path(tmp_path: Path) -> None:
    """A hopd install path with shell-special characters (here, a space)
    must be quoted in the restart line, not left to word-split."""
    from hop.vicinae import DAEMON_DOWN_FILENAME, write_daemon_down_script

    hopd_bin = "/opt/my hop/bin/hopd"
    write_daemon_down_script(tmp_path, hopd_bin=hopd_bin, error=RuntimeError("x"))

    content = (tmp_path / DAEMON_DOWN_FILENAME).read_text()
    assert f"exec setsid -f {shlex.quote(hopd_bin)} </dev/null" in content
    assert "setsid -f hopd " not in content


def test_write_daemon_down_script_advertises_the_hop_icon(tmp_path: Path) -> None:
    """The daemon-down entry is the only hop-* card the user sees when the
    daemon is dead — it still needs the hop logo so it's identifiable in
    the vicinae result list rather than reading as some unrelated tool."""
    from hop.vicinae import DAEMON_DOWN_FILENAME, write_daemon_down_script

    write_daemon_down_script(tmp_path, hopd_bin=HOPD_BIN, error=RuntimeError("x"))

    content = (tmp_path / DAEMON_DOWN_FILENAME).read_text()
    icon_line = next(line for line in content.splitlines() if line.startswith("# @vicinae.icon "))
    icon_path = Path(icon_line.removeprefix("# @vicinae.icon ").strip())
    assert icon_path.is_file()


def test_write_daemon_down_script_clears_existing_hop_scripts(tmp_path: Path) -> None:
    """Pre-existing hop-* entries are deleted so the user sees only the
    "daemon stopped" entry — no stale hop-switch-* / hop-kill / hop-window-*
    misleading them into thinking the daemon is alive."""
    from hop.vicinae import write_daemon_down_script

    (tmp_path / "hop-kill").write_text("stale")
    (tmp_path / "hop-switch-rails").write_text("stale")
    (tmp_path / "hop-window-shell").write_text("stale")

    write_daemon_down_script(tmp_path, hopd_bin=HOPD_BIN, error=RuntimeError("boom"))

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["hop-_daemon-down"]


def test_write_daemon_down_script_preserves_non_hop_files(tmp_path: Path) -> None:
    """Unrelated files in the scripts dir (other vicinae scripts the user
    or other tools installed) must be left untouched."""
    from hop.vicinae import write_daemon_down_script

    (tmp_path / "unrelated-script").write_text("not hop")
    (tmp_path / "hop-switch-foo").write_text("hop")

    write_daemon_down_script(tmp_path, hopd_bin=HOPD_BIN, error=RuntimeError("boom"))

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["hop-_daemon-down", "unrelated-script"]
    assert (tmp_path / "unrelated-script").read_text() == "not hop"


def test_write_daemon_down_script_creates_scripts_dir(tmp_path: Path) -> None:
    """If the scripts dir doesn't exist yet (fresh installs, never-launched
    vicinae), the entry write still succeeds."""
    from hop.vicinae import write_daemon_down_script

    target = tmp_path / "vicinae" / "scripts"
    assert not target.exists()

    write_daemon_down_script(target, hopd_bin=HOPD_BIN, error=RuntimeError("boom"))

    assert target.is_dir()
    assert (target / "hop-_daemon-down").exists()


def test_write_daemon_down_script_collapses_multiline_errors(tmp_path: Path) -> None:
    """Vicinae's description header is line-oriented; newlines in the error
    message would break parsing or split the description. Collapse to a
    single line."""
    from hop.vicinae import write_daemon_down_script

    write_daemon_down_script(tmp_path, hopd_bin=HOPD_BIN, error=RuntimeError("first line\nsecond line\nthird"))

    content = (tmp_path / "hop-_daemon-down").read_text()
    # Every @vicinae.* header sits on its own line; description must not
    # introduce extra ones.
    description_line = next(line for line in content.splitlines() if line.startswith("# @vicinae.description"))
    assert "first line second line third" in description_line


def test_write_daemon_down_script_truncates_long_descriptions(tmp_path: Path) -> None:
    """Vicinae renders descriptions in a fixed-width column. A 5KB
    traceback-style error would be unreadable — truncate at the budget."""
    from hop.vicinae import write_daemon_down_script

    long_message = "x" * 5000
    write_daemon_down_script(tmp_path, hopd_bin=HOPD_BIN, error=RuntimeError(long_message))

    description_line = next(
        line
        for line in (tmp_path / "hop-_daemon-down").read_text().splitlines()
        if line.startswith("# @vicinae.description")
    )
    # Should end with `...` after truncation.
    assert description_line.endswith("...")
    # The header overhead (`# @vicinae.description ` — 23 chars including
    # the trailing space) plus the 200-char budget for the value itself.
    header_prefix = "# @vicinae.description "
    assert len(description_line) <= len(header_prefix) + 200
