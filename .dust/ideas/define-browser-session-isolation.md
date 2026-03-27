# Define browser session isolation

Decide how `hop browser` keeps browser windows scoped to a project workspace without hijacking unrelated browsing sessions.

## Open Questions

### Should hop reuse the user's default browser?

#### Yes, reuse the default browser windowing model

Open URLs through the default browser and rely on Sway workspace moves or app IDs to keep the window near the session.

#### No, launch a dedicated browser profile or app mode

Start a session-owned browser or profile so `hop` can reliably rediscover and focus it later, with more setup complexity.

### What should happen when the browser is moved away from the session workspace?

#### Follow the window wherever it is

Treat the browser as session-owned even if the user moves it manually.

#### Recreate or reattach in the session workspace

Prefer a window that lives in the expected workspace and replace drifted windows if necessary.
