from hop.backends import SessionBackendError, UnknownBackendError
from hop.errors import HopError, ProjectConfigUntrusted


def test_hoperror_defaults_surfaced_by_popup_to_false() -> None:
    error = HopError("boom")

    assert str(error) == "boom"
    assert error.surfaced_by_popup is False


def test_hoperror_carries_surfaced_by_popup_flag() -> None:
    error = HopError("boom", surfaced_by_popup=True)

    assert error.surfaced_by_popup is True


def test_session_backend_error_inherits_kwarg() -> None:
    error = SessionBackendError("prepare failed", surfaced_by_popup=True)

    assert isinstance(error, HopError)
    assert error.surfaced_by_popup is True
    assert str(error) == "prepare failed"


def test_unknown_backend_error_inherits_kwarg() -> None:
    error = UnknownBackendError("unknown backend 'foo'")

    assert isinstance(error, HopError)
    assert error.surfaced_by_popup is False


def test_project_config_untrusted_carries_path_and_content() -> None:
    error = ProjectConfigUntrusted("/proj/.hop.toml", "activate = true")

    assert isinstance(error, HopError)
    assert error.config_path == "/proj/.hop.toml"
    assert error.content == "activate = true"
    assert str(error) == "/proj/.hop.toml is not trusted"
