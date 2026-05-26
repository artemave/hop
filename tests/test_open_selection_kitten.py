from pathlib import Path
from typing import Iterable

import pytest

from hop.kitten.hints import main


@pytest.fixture(autouse=True)
def _stub_focused_paths_exist(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Replace ``focused_paths_exist`` with a deterministic local-filesystem
    check against the test's cwd. The real implementation queries sway +
    kitty IPC to find the focused hop session; in a dev environment where
    sway is running, it would consult whichever session the user is currently
    in, ignoring the test's tmp_path."""

    def fake_paths_exist(candidates: Iterable[str]) -> set[str]:
        # Replays the production flow against the local filesystem: plain
        # files survive on Path.exists; Rails refs require the controller
        # file to exist AND the action to be defined in it.
        import re

        from hop.targets import (
            SyntacticFileTarget,
            SyntacticRailsRefTarget,
            parse_visible_output_target,
            resolve_file_candidate,
        )

        base = Path.cwd()
        result: set[str] = set()
        for candidate in candidates:
            syntactic = parse_visible_output_target(candidate)
            if isinstance(syntactic, SyntacticFileTarget):
                if resolve_file_candidate(syntactic.path_text, terminal_cwd=base).exists():
                    result.add(candidate)
            elif isinstance(syntactic, SyntacticRailsRefTarget):
                controller_path_text = "app/controllers/" + _snake(syntactic.controller) + ".rb"
                path = resolve_file_candidate(controller_path_text, terminal_cwd=base)
                if not path.exists():
                    continue
                pattern = re.compile(rf"^\s*def\s+{syntactic.action}\b")
                if any(pattern.match(line) for line in path.read_text().splitlines()):
                    result.add(candidate)
        return result

    def _snake(controller: str) -> str:
        import re as _re

        parts = controller.split("::")
        snake_parts: list[str] = []
        for part in parts:
            part = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", part)
            part = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", part)
            snake_parts.append(part.lower())
        return "/".join(snake_parts)

    monkeypatch.setattr(main, "focused_paths_exist", fake_paths_exist)


def test_mark_finds_supported_visible_output_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "user.rb").write_text("")
    (tmp_path / "app" / "controllers").mkdir(parents=True)
    (tmp_path / "app" / "controllers" / "users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def index\n  end\nend\n"
    )
    monkeypatch.chdir(tmp_path)

    text = "See app/models/user.rb:12 and https://example.com and Processing UsersController#index"
    matches = list(main.mark(text, None, _mark_factory, [], None))

    assert [match["text"] for match in matches] == [
        "app/models/user.rb:12",
        "https://example.com",
        "Processing UsersController#index",
    ]


def test_mark_skips_file_shaped_tokens_that_do_not_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bun.lock").write_text("")
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.chdir(tmp_path)

    text = "via w1.3.13 v1.3.13 bun.lock package.json https://example.com"
    matches = list(main.mark(text, None, _mark_factory, [], None))

    assert [match["text"] for match in matches] == [
        "bun.lock",
        "package.json",
        "https://example.com",
    ]
    assert [match["index"] for match in matches] == [0, 1, 2]


def test_mark_finds_bare_directories_and_extensionless_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "Gemfile").write_text("")
    (tmp_path / ".gitignore").write_text("")
    monkeypatch.chdir(tmp_path)

    # Mimic an `ls` output line.
    text = "app Gemfile .gitignore Rakefile-missing"
    matches = list(main.mark(text, None, _mark_factory, [], None))

    assert [match["text"] for match in matches] == ["app", "Gemfile", ".gitignore"]


def test_mark_unwraps_function_call_around_file_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "foo.js").write_text("")
    monkeypatch.chdir(tmp_path)

    text = "require(foo.js)"
    matches = list(main.mark(text, None, _mark_factory, [], None))

    assert len(matches) == 1
    assert matches[0]["text"] == "foo.js"
    assert text[matches[0]["start"] : matches[0]["end"]] == "foo.js"


class StubWindow:
    def __init__(self, cwd_of_child: str | None) -> None:
        self.cwd_of_child = cwd_of_child


class StubBoss:
    def __init__(self, *, listening_on: str, windows: dict[int, StubWindow]) -> None:
        self.listening_on = listening_on
        self.window_id_map = windows


def test_handle_result_dispatches_each_match_with_cwd_and_listen_on_from_boss() -> None:
    dispatched: list[tuple[str, str | None, str | None]] = []

    def stub_dispatch(selection: str, *, source_cwd: str | None, listen_on: str | None, boss: object) -> None:
        dispatched.append((selection, source_cwd, listen_on))

    boss = StubBoss(
        listening_on="unix:@hop-demo",
        windows={41: StubWindow(cwd_of_child="/work/demo/src")},
    )

    original_dispatch = main.dispatch_selected_match
    main.dispatch_selected_match = stub_dispatch
    try:
        main.handle_result(
            None,
            {"match": ["app/models/user.rb:12", "", "https://example.com"]},
            41,
            boss,
            [],
            None,
        )
    finally:
        main.dispatch_selected_match = original_dispatch

    assert dispatched == [
        ("app/models/user.rb:12", "/work/demo/src", "unix:@hop-demo"),
        ("https://example.com", "/work/demo/src", "unix:@hop-demo"),
    ]


def test_handle_result_passes_none_cwd_when_target_window_unknown() -> None:
    dispatched: list[tuple[str, str | None, str | None]] = []

    def stub_dispatch(selection: str, *, source_cwd: str | None, listen_on: str | None, boss: object) -> None:
        dispatched.append((selection, source_cwd, listen_on))

    boss = StubBoss(listening_on="unix:@hop-demo", windows={})

    original_dispatch = main.dispatch_selected_match
    main.dispatch_selected_match = stub_dispatch
    try:
        main.handle_result(
            None,
            {"match": ["app/models/user.rb:12"]},
            41,
            boss,
            [],
            None,
        )
    finally:
        main.dispatch_selected_match = original_dispatch

    assert dispatched == [("app/models/user.rb:12", None, "unix:@hop-demo")]


def test_handle_result_passes_none_listen_on_when_boss_lacks_socket() -> None:
    dispatched: list[tuple[str, str | None, str | None]] = []

    def stub_dispatch(selection: str, *, source_cwd: str | None, listen_on: str | None, boss: object) -> None:
        dispatched.append((selection, source_cwd, listen_on))

    boss = StubBoss(listening_on="", windows={41: StubWindow(cwd_of_child="/work")})

    original_dispatch = main.dispatch_selected_match
    main.dispatch_selected_match = stub_dispatch
    try:
        main.handle_result(
            None,
            {"match": ["app/models/user.rb:12"]},
            41,
            boss,
            [],
            None,
        )
    finally:
        main.dispatch_selected_match = original_dispatch

    assert dispatched == [("app/models/user.rb:12", "/work", None)]


def _mark_factory(index: int, start: int, end: int, text: str, groupdict: dict[str, object]) -> dict[str, object]:
    return {
        "index": index,
        "start": start,
        "end": end,
        "text": text,
        "groupdict": groupdict,
    }
