from pathlib import Path

from hop.commands.open_selection import open_selection_in_window
from hop.session import derive_session_name
from hop.state import SessionState
from hop.targets import ResolvedFileTarget, resolve_visible_output_target


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.targets: list[str] = []

    def open_target(self, session: object, *, target: str) -> None:
        self.targets.append(target)


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.urls: list[str | None] = []

    def ensure_browser(self, session: object, *, url: str | None) -> None:
        self.urls.append(url)


def test_open_selection_ignores_missing_source_cwd() -> None:
    assert (
        open_selection_in_window(
            "README.md",
            source_cwd=None,
            listen_on="unix:@hop-demo",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=Path("/tmp")),
            },
        )
        is None
    )


def test_open_selection_ignores_invocation_outside_a_hop_session_kitty(tmp_path: Path) -> None:
    assert (
        open_selection_in_window(
            "README.md",
            source_cwd=tmp_path,
            listen_on="",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {},
        )
        is None
    )


def test_open_selection_ignores_session_without_recorded_state(tmp_path: Path) -> None:
    from hop.kitty import session_socket_path

    listen_on = f"unix:{session_socket_path('demo')}"
    assert (
        open_selection_in_window(
            "README.md",
            source_cwd=tmp_path,
            listen_on=listen_on,
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {},
        )
        is None
    )


def test_open_selection_returns_none_when_target_does_not_resolve(tmp_path: Path) -> None:
    assert (
        open_selection_in_window(
            "   ",
            source_cwd=tmp_path,
            listen_on="unix:@hop-demo",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
            sessions_loader=lambda: {
                "demo": SessionState(name="demo", project_root=tmp_path),
            },
        )
        is None
    )


def test_derive_session_name_rejects_root_path() -> None:
    try:
        derive_session_name(Path("/"))
    except ValueError as error:
        assert "Cannot derive a session name" in str(error)
    else:
        raise AssertionError("Expected root path to be rejected")


def test_resolve_visible_output_target_handles_empty_invalid_url_and_absolute_paths(tmp_path: Path) -> None:
    absolute_file = tmp_path / "README.md"
    absolute_file.write_text("ok\n")

    assert resolve_visible_output_target("   ", terminal_cwd=tmp_path, project_root=tmp_path) is None
    assert resolve_visible_output_target("https://", terminal_cwd=tmp_path, project_root=tmp_path) is None
    assert (
        resolve_visible_output_target(
            "Processing MissingController#index", terminal_cwd=tmp_path, project_root=tmp_path
        )
        is None
    )
    assert resolve_visible_output_target(
        str(absolute_file), terminal_cwd=tmp_path, project_root=tmp_path
    ) == ResolvedFileTarget(path=absolute_file.resolve())


def test_resolved_file_target_editor_target_omits_line_number_when_absent() -> None:
    resolved_path = Path("/tmp/demo").resolve()

    assert ResolvedFileTarget(path=resolved_path).editor_target == str(resolved_path)
