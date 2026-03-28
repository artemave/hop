from kittens.open_selection import main


def test_mark_finds_supported_visible_output_targets() -> None:
    text = "See app/models/user.rb:12 and https://example.com and Processing UsersController#index"
    matches = list(main.mark(text, None, _mark_factory, [], None))

    assert [match["text"] for match in matches] == [
        "app/models/user.rb:12",
        "https://example.com",
        "Processing UsersController#index",
    ]


def test_handle_result_dispatches_each_selected_match() -> None:
    dispatched: list[tuple[str, int]] = []

    def stub_dispatch(selection: str, *, source_window_id: int) -> None:
        dispatched.append((selection, source_window_id))

    original_dispatch = main.dispatch_selected_match
    main.dispatch_selected_match = stub_dispatch
    try:
        main.handle_result(
            None,
            {"match": ["app/models/user.rb:12", "", "https://example.com"]},
            41,
            None,
            [],
            None,
        )
    finally:
        main.dispatch_selected_match = original_dispatch

    assert dispatched == [
        ("app/models/user.rb:12", 41),
        ("https://example.com", 41),
    ]


def _mark_factory(index: int, start: int, end: int, text: str, groupdict: dict[str, object]) -> dict[str, object]:
    return {
        "index": index,
        "start": start,
        "end": end,
        "text": text,
        "groupdict": groupdict,
    }
