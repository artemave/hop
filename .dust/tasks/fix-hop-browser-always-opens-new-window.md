# Fix: hop browser should always open a new window for the session

`hop browser` opens a tab in an existing browser window instead of creating a new browser window in the session workspace when no session browser exists.

## Root Cause

`_infer_new_window_flag` returns `None` for any browser not in `CHROMIUM_BROWSER_IDENTIFIERS` or `FIREFOX_BROWSER_IDENTIFIERS`. When `new_window_flag` is `None`, `_build_browser_command` omits `--new-window` from the launch command even when `new_window=True`. The browser then opens a tab in the existing window instead of a new window, and `_wait_for_new_browser_window` times out with `BrowserCommandError`.

## Fix

Remove the browser-family lookup. The `--new-window` flag is the universal convention for both Chromium-family and Firefox-family browsers (and their forks). Always set `new_window_flag="--new-window"` in `_resolve_default_browser_spec`. Remove `CHROMIUM_BROWSER_IDENTIFIERS`, `FIREFOX_BROWSER_IDENTIFIERS`, and `_infer_new_window_flag`.

## Spec Alignment

- "launch the system default browser in a new window when the session browser is missing"
- "opening URLs should reuse or create a browser window within the session workspace"
