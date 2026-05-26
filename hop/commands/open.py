from __future__ import annotations

import fnmatch
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Protocol, Sequence

from hop.backends import CommandBackend, SessionBackend
from hop.errors import HopError
from hop.session import ProjectSession, resolve_project_session
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


class OpenHandlerRunner(Protocol):
    def run(self, session: ProjectSession, backend: SessionBackend, *, command: str) -> None: ...


class SubprocessOpenHandlerRunner:
    """Default runner: wrap the command in the session backend's interactive
    prefix and fire it via ``subprocess.Popen`` with ``start_new_session=True``
    so the GUI viewer survives hop's exit. Stdout/stderr go to ``/dev/null`` —
    these are fire-and-forget launches; the viewer process is not awaited."""

    def run(self, session: ProjectSession, backend: SessionBackend, *, command: str) -> None:
        wrapped = backend.inline(command, session)
        subprocess.Popen(  # noqa: S603
            ["sh", "-c", wrapped],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def match_handler(path: Path, handlers: Sequence[tuple[str, str]]) -> str | None:
    """Pick a handler template for ``path``.

    Iterates patterns in declaration order; first match wins. An empty
    template means "no handler — fall through to nvim", letting users opt
    out of a built-in default via ``"*.png" = ""``. Matching uses
    ``fnmatch`` against the file *name*, not the full path or the typed
    token, so Rails-resolved ``app/controllers/...rb`` paths and
    ``path:42`` line suffixes don't interact with extension patterns.
    """

    name = path.name
    for pattern, template in handlers:
        if fnmatch.fnmatch(name, pattern):
            return template or None
    return None


def dispatch_resolved_target(
    resolved: ResolvedTarget,
    *,
    session: ProjectSession,
    backend: SessionBackend,
    neovim: OpenNeovimAdapter,
    browser: OpenBrowserAdapter,
    handlers: Sequence[tuple[str, str]] = (),
    handler_runner: OpenHandlerRunner | None = None,
) -> ResolvedTarget:
    """Hand a parsed target to the right adapter.

    Returns the dispatched target — for URLs that's the post-backend-translated
    one, so callers logging the dispatch see the URL the browser actually got.

    Dispatch order for non-URL targets:
    1. If the resolved file name matches an ``open_handlers`` pattern with a
       non-empty template, run that command through the session backend.
    2. Otherwise hand the target to the shared Neovim.
    """

    if isinstance(resolved, ResolvedUrlTarget):
        translated_url = backend.translate_localhost_url(session, resolved.url)
        browser.ensure_browser(session, url=translated_url)
        return ResolvedUrlTarget(url=translated_url)
    template = match_handler(resolved.path, handlers)
    if template is not None:
        runner = handler_runner if handler_runner is not None else SubprocessOpenHandlerRunner()
        command = template.format(path=shlex.quote(str(resolved.path)))
        runner.run(session, backend, command=command)
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
    handlers_for_session: Callable[[ProjectSession], Sequence[tuple[str, str]]] = lambda _session: (),
    handler_runner: OpenHandlerRunner | None = None,
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

    session = resolve_project_session(cwd)

    syntactic = parse_visible_output_target(target)
    if syntactic is None:
        msg = f"could not parse target {target!r}"
        raise HopError(msg)

    backend = session_backend_for(session)
    # terminal_cwd=None preserves plain-file paths as typed; the editor (nvim
    # in the session's backend) resolves them against its own cwd. Absolutizing
    # against the host's project_root would hand the editor a host path it
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
        handlers=handlers_for_session(session),
        handler_runner=handler_runner,
    )
    return session
