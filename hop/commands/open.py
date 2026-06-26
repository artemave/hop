from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Protocol

from hop.backends import CommandBackend, SessionBackend
from hop.errors import HopError
from hop.session import ProjectSession, remote_session_from_env, resolve_project_session
from hop.targets import (
    ResolvedTarget,
    ResolvedUrlTarget,
    parse_visible_output_target,
    resolve_target,
)

_BUILTIN_HOST_BACKEND = CommandBackend(name="host", interactive_prefix="", noninteractive_prefix="")


class OpenNeovimAdapter(Protocol):
    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class OpenBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


class HostOpener(Protocol):
    def open(self, path: Path) -> None: ...


class SubprocessHostOpener:
    """Default opener: hand the host's ``xdg-open`` a host-visible path and
    fire it via ``subprocess.Popen`` with ``start_new_session=True`` so the GUI
    viewer survives hop's exit. Stdout/stderr go to ``/dev/null`` — these are
    fire-and-forget launches; the viewer process is not awaited. The viewer is
    always the user's own host tool, already configured to their preference."""

    def open(self, path: Path) -> None:
        subprocess.Popen(  # noqa: S603
            ["xdg-open", str(path)],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def dispatch_resolved_target(
    resolved: ResolvedTarget,
    *,
    session: ProjectSession,
    backend: SessionBackend,
    neovim: OpenNeovimAdapter,
    browser: OpenBrowserAdapter,
    opener: HostOpener | None = None,
) -> ResolvedTarget:
    """Hand a parsed target to the right adapter.

    Returns the dispatched target — for URLs that's the post-backend-translated
    one, so callers logging the dispatch see the URL the browser actually got.

    Dispatch order for non-URL targets:
    1. If the backend classifies the file as binary, materialize it on the host
       and open it with the host's ``xdg-open``. A file living in a container or
       on a remote is copied across first, so the viewer always runs on the host
       against a path it can see.
    2. Otherwise hand the target to the shared Neovim.
    """

    if isinstance(resolved, ResolvedUrlTarget):
        translated_url = backend.translate_localhost_url(session, resolved.url)
        browser.ensure_browser(session, url=translated_url)
        return ResolvedUrlTarget(url=translated_url)
    if backend.is_binary_file(session, resolved.path):
        host_path = backend.materialize_on_host(session, resolved.path)
        host_opener = opener if opener is not None else SubprocessHostOpener()
        host_opener.open(host_path)
        return resolved
    neovim.open_target(session, target=resolved.editor_target)
    return resolved


def open_target_in_session(
    cwd: Path | str,
    *,
    target: str,
    neovim: OpenNeovimAdapter,
    browser: OpenBrowserAdapter,
    session_backend_for: Callable[[ProjectSession], SessionBackend] = lambda _session: _BUILTIN_HOST_BACKEND,
    opener: HostOpener | None = None,
) -> ProjectSession:
    """Resolve and dispatch a single target for the cwd-derived session.

    The parser splits URL vs Rails-ref vs file:line
    (``parse_visible_output_target``), the backend turns it into a concrete
    dispatchable target (``resolve_target`` — Rails refs read the controller
    file to find the ``def <action>`` line; plain file paths are passed
    through as typed so the editor in the backend resolves them against its
    own cwd), then it's handed to the right adapter. Plain file paths skip
    any existence check so opening a not-yet-created file lands in ``:enew``;
    Rails refs DO get verified because the line number lookup needs the
    action to exist anyway.
    """

    session = remote_session_from_env() or resolve_project_session(cwd)

    syntactic = parse_visible_output_target(target)
    if syntactic is None:
        msg = f"could not parse target {target!r}"
        raise HopError(msg)

    backend = session_backend_for(session)
    # terminal_cwd=None preserves plain-file paths as typed; the editor (nvim
    # in the session's backend) resolves them against its own cwd. Absolutizing
    # against the host's session_root would hand the editor a host path it
    # can't see when the backend is a devcontainer / ssh host.
    resolved = resolve_target(syntactic, session=session, backend=backend, terminal_cwd=None)
    if resolved is None:
        msg = f"could not resolve target {target!r}"
        raise HopError(msg)

    dispatch_resolved_target(
        resolved,
        session=session,
        backend=backend,
        neovim=neovim,
        browser=browser,
        opener=opener,
    )
    return session
