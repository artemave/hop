from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from hop.backends import CommandBackend, SessionBackend
from hop.errors import HopError
from hop.session import ProjectSession, resolve_project_session
from hop.targets import (
    ResolvedTarget,
    ResolvedUrlTarget,
    resolve_visible_output_target,
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

    No-arg form focuses the session editor. Otherwise the parser decides
    URL vs Rails-ref vs file:line via ``resolve_visible_output_target``, and
    dispatch flows through ``dispatch_resolved_target``. No existence check
    — the kitten owns that filter because it needs it for highlighting; the
    CLI hands the path straight to nvim so opening a not-yet-created file
    works.
    """

    session = resolve_project_session(cwd)

    if target is None:
        neovim.focus(session)
        return session

    # terminal_cwd=None preserves the path as typed; the editor (nvim in
    # the session's backend) resolves it against its own cwd. Absolutizing
    # against the host's project_root would hand the editor a host path it
    # can't see when the backend is a devcontainer / ssh host.
    resolved = resolve_visible_output_target(target, terminal_cwd=None)
    if resolved is None:
        msg = f"could not parse target {target!r}"
        raise HopError(msg)

    backend = session_backend_for(session)
    dispatch_resolved_target(
        resolved,
        session=session,
        backend=backend,
        neovim=neovim,
        browser=browser,
    )
    return session
