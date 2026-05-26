from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

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


class OpenTargetNeovimAdapter(Protocol):
    def open_target(self, session: ProjectSession, *, target: str) -> None: ...


class OpenNeovimAdapter(OpenTargetNeovimAdapter, Protocol):
    """Wider protocol for the CLI: no-arg ``hop open`` focuses the editor.

    The kitten never calls ``focus`` (it always has a parsed target), so it
    binds against the narrower :class:`OpenTargetNeovimAdapter`.
    """

    def focus(self, session: ProjectSession) -> None: ...


class OpenBrowserAdapter(Protocol):
    def ensure_browser(self, session: ProjectSession, *, url: str | None) -> None: ...


def dispatch_resolved_target(
    resolved: ResolvedTarget,
    *,
    session: ProjectSession,
    backend: SessionBackend,
    neovim: OpenTargetNeovimAdapter,
    browser: OpenBrowserAdapter,
) -> ResolvedTarget:
    """Hand a parsed target to the right adapter.

    Returns the dispatched target — for URLs that's the post-backend-translated
    one, so callers logging the dispatch see the URL the browser actually got.
    """

    if isinstance(resolved, ResolvedUrlTarget):
        translated_url = backend.translate_localhost_url(session, resolved.url)
        browser.ensure_browser(session, url=translated_url)
        return ResolvedUrlTarget(url=translated_url)
    neovim.open_target(session, target=resolved.editor_target)
    return resolved


def open_target_in_session(
    cwd: Path | str,
    *,
    target: str | None,
    neovim: OpenNeovimAdapter,
    browser: OpenBrowserAdapter,
    session_backend_for: Callable[[ProjectSession], SessionBackend] = lambda _session: _BUILTIN_HOST_BACKEND,
) -> ProjectSession:
    """Resolve and dispatch a single target for the cwd-derived session.

    No-arg form focuses the session editor. Otherwise the parser splits
    URL vs Rails-ref vs file:line (``parse_visible_output_target``), the
    backend turns it into a concrete dispatchable target (``resolve_target``
    — Rails refs read the controller file to find the ``def <action>``
    line; plain file paths are passed through as typed so the editor in
    the backend resolves them against its own cwd), then it's handed to
    the right adapter. Plain file paths skip any existence check so opening
    a not-yet-created file lands in ``:enew``; Rails refs DO get verified
    because the line number lookup needs the action to exist anyway.
    """

    session = resolve_project_session(cwd)

    if target is None:
        neovim.focus(session)
        return session

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
    )
    return session
