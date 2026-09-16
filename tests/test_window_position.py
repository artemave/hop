from hop.window_position import compute_insert_index


def test_first_window_with_no_positions_inserts_at_end() -> None:
    assert compute_insert_index([], {}, None) == 0


def test_unpositioned_window_appends_after_existing_unpositioned_ones() -> None:
    position_by_role = {"shell": None, "test": None}
    assert compute_insert_index(["shell", "test"], position_by_role, None) == 2


def test_positive_position_sorts_before_unpositioned_and_negative() -> None:
    position_by_role = {"shell": None, "server": -1}
    # A window pinned to position 1 (leftmost) goes before everything else.
    assert compute_insert_index(["shell", "server"], position_by_role, 1) == 0


def test_negative_position_sorts_after_unpositioned_and_positive() -> None:
    position_by_role = {"editor": 1, "shell": None}
    # -1 (rightmost) goes after everything else.
    assert compute_insert_index(["editor", "shell"], position_by_role, -1) == 2


def test_positive_positions_order_ascending_among_themselves() -> None:
    position_by_role = {"a": 1, "c": 3}
    assert compute_insert_index(["a", "c"], position_by_role, 2) == 1


def test_negative_positions_order_ascending_among_themselves() -> None:
    # -3 sorts before -2 sorts before -1 (more negative == further left of
    # the sticky-right block).
    position_by_role = {"a": -3, "c": -1}
    assert compute_insert_index(["a", "c"], position_by_role, -2) == 1


def test_new_window_fills_middle_between_positive_and_negative_anchors() -> None:
    position_by_role = {"left": 1, "right": -1}
    assert compute_insert_index(["left", "right"], position_by_role, None) == 1


def test_tie_on_same_positive_position_inserts_after_existing() -> None:
    position_by_role = {"a": 1}
    assert compute_insert_index(["a"], position_by_role, 1) == 1


def test_unknown_existing_role_treated_as_unpositioned() -> None:
    assert compute_insert_index(["mystery"], {}, None) == 1
