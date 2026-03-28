from pathlib import Path

from hop.commands.open_selection import open_selection_in_window
from hop.kitty import KittyWindowContext
from hop.session import derive_session_name
from hop.targets import ResolvedFileTarget, resolve_visible_output_target


class StubSwayAdapter:
    def __init__(self) -> None:
        self.switched_workspaces: list[str] = []

    def switch_to_workspace(self, workspace_name: str) -> None:
        self.switched_workspaces.append(workspace_name)


class StubKittyAdapter:
    def __init__(self, context: KittyWindowContext | None) -> None:
        self.context = context

    def inspect_window(self, window_id: int) -> KittyWindowContext | None:
        return self.context


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


def test_open_selection_ignores_missing_source_window() -> None:
    sway = StubSwayAdapter()

    assert (
        open_selection_in_window(
            "README.md",
            source_window_id=17,
            sway=sway,
            kitty=StubKittyAdapter(None),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )
        is None
    )
    assert sway.switched_workspaces == []


def test_open_selection_ignores_windows_without_project_root_or_cwd() -> None:
    sway = StubSwayAdapter()
    context = KittyWindowContext(
        id=17,
        session_name="demo",
        role="shell",
        project_root=None,
        cwd=None,
    )

    assert (
        open_selection_in_window(
            "README.md",
            source_window_id=17,
            sway=sway,
            kitty=StubKittyAdapter(context),
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )
        is None
    )
    assert sway.switched_workspaces == []


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
