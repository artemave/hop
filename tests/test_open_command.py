from pathlib import Path
from typing import Sequence

import pytest

from hop.commands.open import open_target_in_session
from hop.errors import HopError
from hop.session import ProjectSession


class StubNeovimAdapter:
    def __init__(self) -> None:
        self.opened_targets: list[tuple[str, str]] = []

    def open_target(self, session: ProjectSession, *, target: str) -> None:
        self.opened_targets.append((session.session_name, target))


class StubBrowserAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None:
        self.calls.append((session.session_name, url))


class StubBackend:
    def __init__(
        self,
        *,
        url_translation: dict[str, str] | None = None,
        binary_names: frozenset[str] = frozenset(),
    ) -> None:
        self._url_translation = url_translation or {}
        self._binary_names = binary_names
        self.translate_calls: list[str] = []

    def translate_localhost_url(self, _session: ProjectSession, url: str) -> str:
        self.translate_calls.append(url)
        return self._url_translation.get(url, url)

    def paths_exist(self, _session: ProjectSession, paths: Sequence[Path]) -> set[Path]:
        # CLI path doesn't call this — included so the stub fits the SessionBackend Protocol.
        return set()

    def is_binary_file(self, _session: ProjectSession, path: Path) -> bool:
        return path.name in self._binary_names

    def materialize_on_host(self, _session: ProjectSession, path: Path) -> Path:
        # The local-host stub leaves the path where it is — no copy needed.
        return path


def test_file_target_dispatches_to_shared_editor(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()

    session = open_target_in_session(
        session_root,
        target="app/models/user.rb",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    # CLI passes the path through as typed; nvim resolves it against its own
    # cwd in the session's backend (which the host can't address).
    assert session.session_name == "demo"
    assert neovim.opened_targets == [("demo", "app/models/user.rb")]


def test_file_with_line_target_keeps_line_suffix(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()

    open_target_in_session(
        session_root,
        target="app/models/user.rb:42",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    assert neovim.opened_targets == [("demo", "app/models/user.rb:42")]


def test_rails_controller_action_target_translates_to_path_with_def_line(tmp_path: Path) -> None:
    """``hop open UsersController#index`` derives the controller path AND
    looks up the line where ``def index`` is defined via the session
    backend's ``read_file``, so the editor jumps straight to the action."""
    session_root = tmp_path / "demo"
    (session_root / "app/controllers").mkdir(parents=True)
    (session_root / "app/controllers/users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def index\n  end\nend\n"
    )

    neovim = StubNeovimAdapter()

    open_target_in_session(
        session_root,
        target="UsersController#index",
        neovim=neovim,
        browser=StubBrowserAdapter(),
    )

    # def index is on line 2 of the controller file. The editor target stays
    # relative so the editor (running in the session backend) resolves it
    # against its own cwd, matching how plain file paths flow through.
    assert neovim.opened_targets == [("demo", "app/controllers/users_controller.rb:2")]


def test_rails_controller_action_target_raises_when_def_not_in_file(tmp_path: Path) -> None:
    """If the action isn't defined in the controller, the CLI surfaces a
    clear ``HopError`` rather than silently opening the file at line 1
    (or some unrelated location)."""
    session_root = tmp_path / "demo"
    (session_root / "app/controllers").mkdir(parents=True)
    (session_root / "app/controllers/users_controller.rb").write_text(
        "class UsersController < ApplicationController\n  def show\n  end\nend\n"
    )

    with pytest.raises(HopError, match="could not resolve target"):
        open_target_in_session(
            session_root,
            target="UsersController#index",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_rails_controller_action_target_raises_when_controller_file_missing(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    with pytest.raises(HopError, match="could not resolve target"):
        open_target_in_session(
            session_root,
            target="UsersController#index",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_url_target_dispatches_to_session_browser(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    browser = StubBrowserAdapter()

    open_target_in_session(
        session_root,
        target="https://example.com/path",
        neovim=StubNeovimAdapter(),
        browser=browser,
    )

    assert browser.calls == [("demo", "https://example.com/path")]


def test_url_target_is_translated_through_backend(tmp_path: Path) -> None:
    """For container/ssh backends, a localhost URL needs `host_translate` /
    `port_translate` rewriting before it reaches the host browser. The CLI
    routes URLs through the same `backend.translate_localhost_url` the kitten
    uses, so `hop open http://localhost:3000` opens the translated URL."""
    session_root = tmp_path / "demo"
    session_root.mkdir()

    browser = StubBrowserAdapter()
    backend = StubBackend(url_translation={"http://localhost:3000/": "http://localhost:35231/"})

    open_target_in_session(
        session_root,
        target="http://localhost:3000/",
        neovim=StubNeovimAdapter(),
        browser=browser,
        session_backend_for=lambda _session: backend,  # type: ignore[arg-type]
    )

    assert backend.translate_calls == ["http://localhost:3000/"]
    assert browser.calls == [("demo", "http://localhost:35231/")]


def test_unparseable_target_raises_hop_error(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    session_root.mkdir()

    with pytest.raises(HopError, match="could not parse"):
        open_target_in_session(
            session_root,
            target="   ",
            neovim=StubNeovimAdapter(),
            browser=StubBrowserAdapter(),
        )


def test_nested_directories_are_distinct_sessions(tmp_path: Path) -> None:
    session_root = tmp_path / "demo"
    nested_directory = session_root / "src"
    nested_directory.mkdir(parents=True)

    neovim = StubNeovimAdapter()

    open_target_in_session(session_root, target="lib/a.rb", neovim=neovim, browser=StubBrowserAdapter())
    open_target_in_session(nested_directory, target="lib/b.rb", neovim=neovim, browser=StubBrowserAdapter())

    assert neovim.opened_targets == [("demo", "lib/a.rb"), ("src", "lib/b.rb")]


# ─── binary files open on the host; text files go to the editor ──────────────


class RecordingOpener:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def open(self, path: Path) -> None:
        self.paths.append(path)


@pytest.mark.parametrize(
    "filename",
    [
        "config.json",
        "docker-compose.yaml",
        "Cargo.toml",
        "app/models/user.rb",
        "Makefile",
        "Dockerfile",
        "notes.md",
        "README",
        "icon.svg",
        "src/main.rs",
        "weird.unknownextension",
    ],
)
def test_text_and_source_files_dispatch_to_nvim(tmp_path: Path, filename: str) -> None:
    """Anything the backend classifies as text — JSON, YAML, TOML, Markdown,
    SVG, source code, files without an extension — falls through to the editor.
    This is the guarantee users rely on for normal editing flow. (Real
    ``file``-based classification is covered in ``test_backends``; here the
    backend reports no binaries so we exercise the dispatch wiring.)"""
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()
    opener = RecordingOpener()

    open_target_in_session(
        session_root,
        target=filename,
        neovim=neovim,
        browser=StubBrowserAdapter(),
        session_backend_for=lambda _session: StubBackend(),  # type: ignore[arg-type]
        opener=opener,
    )

    assert neovim.opened_targets == [("demo", filename)]
    assert opener.paths == []


def test_binary_file_opens_on_host(tmp_path: Path) -> None:
    """A file the backend classifies as binary skips the editor and is handed
    to the host opener — the path the opener sees comes from
    ``materialize_on_host`` (here a no-op for the local stub)."""
    session_root = tmp_path / "demo"
    session_root.mkdir()

    neovim = StubNeovimAdapter()
    opener = RecordingOpener()
    backend = StubBackend(binary_names=frozenset({"email-logo.png"}))

    open_target_in_session(
        session_root,
        target="public/email-logo.png",
        neovim=neovim,
        browser=StubBrowserAdapter(),
        session_backend_for=lambda _session: backend,  # type: ignore[arg-type]
        opener=opener,
    )

    assert neovim.opened_targets == []
    assert opener.paths == [Path("public/email-logo.png")]


def test_real_host_backend_classifies_png_as_binary_and_opens(tmp_path: Path) -> None:
    """End to end through the default host backend: a real PNG on disk is
    classified binary by ``file`` and routed to the host opener with its
    actual (host-visible) path — no copy, since it already lives on the host."""
    session_root = tmp_path / "demo"
    (session_root / "public").mkdir(parents=True)
    png = session_root / "public" / "logo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-pixels")

    neovim = StubNeovimAdapter()
    opener = RecordingOpener()

    open_target_in_session(
        session_root,
        target="public/logo.png",
        neovim=neovim,
        browser=StubBrowserAdapter(),
        opener=opener,
    )

    assert neovim.opened_targets == []
    # The host backend resolves the relative path against the session root; the
    # opener receives it unchanged (no materialize copy for a local file).
    assert opener.paths == [Path("public/logo.png")]


def test_real_host_backend_classifies_source_as_text(tmp_path: Path) -> None:
    """The mirror of the PNG case: a real source file is classified text by
    ``file`` and dispatched to the editor, not the host opener."""
    session_root = tmp_path / "demo"
    session_root.mkdir()
    (session_root / "main.rs").write_text("fn main() {}\n")

    neovim = StubNeovimAdapter()
    opener = RecordingOpener()

    open_target_in_session(
        session_root,
        target="main.rs",
        neovim=neovim,
        browser=StubBrowserAdapter(),
        opener=opener,
    )

    assert neovim.opened_targets == [("demo", "main.rs")]
    assert opener.paths == []


def test_subprocess_host_opener_invokes_xdg_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default opener execs the host's ``xdg-open`` with the file path.
    A fake ``xdg-open`` on PATH records the argument, exercising the real
    fire-and-forget Popen path without launching a GUI viewer."""
    import os
    import time

    from hop.commands.open import SubprocessHostOpener

    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "opened"
    fake = bindir / "xdg-open"
    fake.write_text(f'#!/bin/sh\nprintf %s "$1" > {marker}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    target = tmp_path / "weird name.png"
    SubprocessHostOpener().open(target)

    # Popen is fire-and-forget; give the child a moment to actually write.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.read_text() == str(target)


# ─── a binary on a remote session: copy to host, open the local copy ─────────


def test_remote_binary_downloads_to_host_and_opens_local_copy(tmp_path: Path) -> None:
    """On a remote session a GUI viewer runs on the host, which can't see the
    remote path. The dispatch must pull the file off the remote into a host
    temp file and hand the opener *that* local path, not the remote one."""
    import base64
    import subprocess
    from typing import Sequence

    from hop.backends import CommandBackend
    from hop.commands.open import dispatch_resolved_target
    from hop.targets import ResolvedFileTarget

    payload = b"\x89PNG\r\n\x1a\nfake-pixels"
    captured_stdin: list[str] = []

    def fake_runner(
        args: Sequence[str],
        cwd: Path,
        *,
        stdin: str | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        del args, cwd
        # Two backend calls ride stdin: first the `file` classify probe, then
        # the `base64 <path>` fetch. Answer each by what its script contains,
        # recording both so we can prove the *remote* path was the one read.
        captured_stdin.append(stdin or "")
        stdout = "binary" if "file -b --mime-encoding" in (stdin or "") else base64.b64encode(payload).decode("ascii")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    backend = CommandBackend(
        name="ssh",
        interactive_prefix="",
        noninteractive_prefix="",
        runner=fake_runner,
        host="devbox",
    )
    session = ProjectSession(
        session_root=Path("/remote/proj"),
        session_name="proj",
        workspace_name="p:proj",
        host="devbox",
    )
    opener = RecordingOpener()

    dispatch_resolved_target(
        ResolvedFileTarget(path=Path("public/logo.png")),
        session=session,
        backend=backend,
        neovim=StubNeovimAdapter(),
        browser=StubBrowserAdapter(),
        opener=opener,
    )

    # The remote path was the one classified and fetched.
    assert any("public/logo.png" in script for script in captured_stdin)

    # Exactly one opener launch, against a *local* temp copy, not the remote path.
    assert len(opener.paths) == 1
    local = opener.paths[0]
    assert local != Path("public/logo.png")
    assert local.name == "logo.png"
    assert local.read_bytes() == payload
