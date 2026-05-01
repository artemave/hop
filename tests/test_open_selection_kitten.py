from pathlib import Path

import pytest

from kittens.open_selection import main


def test_mark_finds_supported_visible_output_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "user.rb").write_text("")
    (tmp_path / "app" / "controllers").mkdir(parents=True)
    (tmp_path / "app" / "controllers" / "users_controller.rb").write_text("")
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
