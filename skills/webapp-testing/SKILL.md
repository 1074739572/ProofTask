---
name: webapp-testing
description: Verify a local web application's rendered UI and interaction behavior with a browser or Playwright. Use for frontend Goal Tasks only when browser tooling is available and permitted.
---

# Web Application Testing

This is a compact adaptation of `anthropics/skills`' `webapp-testing` skill
(Apache-2.0; see `LICENSE.txt`). It supplies a verification method, not a
browser, server, package, or permission.

1. Confirm the local app can be started within the Task's allowed commands.
2. Navigate to the rendered page and wait until the UI has settled before
   inspecting DOM state or acting on it.
3. Discover selectors from the rendered UI. Prefer semantic role, label, ID,
   or visible text selectors over fragile layout selectors.
4. Exercise the acceptance case, then assert on visible result and relevant
   console/runtime errors.
5. Capture a screenshot or a deterministic assertion when visual state is the
   proof. Close the browser and stop the server after verification.

## Boundaries

- Do not claim UI verification if browser tooling, dependencies, or permission
  are unavailable; report that as a blocker.
- Do not launch a dev server or browser unless the active Goal agent has a
  permitted tool and the Task needs it.
- The runner's bound test command remains the completion gate. Browser checks
  complement it and never replace it.
